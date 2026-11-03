"""豆包图片直传（ImageX 四步上传）。

背景：网页上传通道每上传一张图片，会在输入区保留一张附件卡片；该卡片的
删除按钮无法从页面外部触发，附件数量累积到 10 张后，网页会静默忽略新的
文件选择，导致连续带图对话在第 11 轮上传失败，且只能通过刷新页面恢复。

本模块直接调用网页自身使用的上传接口，流程与 doubao2api、Doubao-AI 两个
开源实现一致：

    1. POST /alice/resource/prepare_upload             获取 STS 临时凭证（约 1 小时）
    2. GET  /top/v1?Action=ApplyImageUpload            获取 StoreUri / Auth / SessionKey
    3. POST https://{UploadHost}/upload/v1/{StoreUri}  上传二进制内容（Auth + CRC32）
    4. POST /top/v1?Action=CommitImageUpload           获取 ImageUri

第 2、4 步使用 AWS Signature V4 签名（region=cn-north-1，service=imagex）；
第 3 步使用第 2 步返回的 ``Auth`` 字符串鉴权，不属于 SigV4。

整个过程只发送 HTTP 请求，不操作页面 DOM，因此不产生附件卡片，也就不受
输入区附件数量上限的限制。
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import random
import string
import time
import zlib
from datetime import datetime, timezone
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, quote, urlparse
from urllib.request import Request, urlopen

BASE = "https://www.doubao.com"
REGION = "cn-north-1"
SERVICE = "imagex"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/138.0.0.0 Safari/537.36")
STS_TTL = 58 * 60          # 服务端凭证有效期 1 小时，预留 2 分钟余量


class ImageXError(RuntimeError):
    """直传失败。调用方据此决定是否回退到网页上传通道。"""


def _cookie_value(value) -> str:
    """统一 cookie 取值：CDP 返回字符串，浏览器库返回 {"value": ...}。"""
    if isinstance(value, dict):
        value = value.get("value")
    return "" if value is None else str(value)


def _crc32_hex(data: bytes) -> str:
    """TOS 要求 8 位小写十六进制 CRC32。"""
    return format(zlib.crc32(data) & 0xFFFFFFFF, "08x")


def _sign_v4(method: str, url: str, body: bytes, access_key: str,
             secret_key: str, session_token: str) -> dict:
    """按 AWS Signature V4 生成 Authorization、X-Amz-Date 与安全令牌请求头。"""
    parsed = urlparse(url)
    host = parsed.hostname or ""
    path = parsed.path or "/"
    now = datetime.now(timezone.utc)
    amz_date = now.strftime("%Y%m%dT%H%M%SZ")
    date_stamp = now.strftime("%Y%m%d")

    # 规范查询串：参数按名称排序后逐项进行 URL 编码
    query = parse_qs(parsed.query, keep_blank_values=True)
    pairs = sorted((k, v[0] if v else "") for k, v in query.items())
    canonical_qs = "&".join(f"{quote(k, safe='~')}={quote(v, safe='~')}"
                            for k, v in pairs)

    to_sign = {"host": host, "x-amz-date": amz_date}
    if session_token:
        to_sign["x-amz-security-token"] = session_token
    signed_headers = ";".join(sorted(to_sign))
    canonical_headers = "".join(f"{k}:{to_sign[k]}\n" for k in sorted(to_sign))
    payload_hash = hashlib.sha256(body or b"").hexdigest()

    canonical_request = (f"{method}\n{path}\n{canonical_qs}\n"
                         f"{canonical_headers}\n{signed_headers}\n{payload_hash}")
    scope = f"{date_stamp}/{REGION}/{SERVICE}/aws4_request"
    string_to_sign = (
        f"AWS4-HMAC-SHA256\n{amz_date}\n{scope}\n"
        f"{hashlib.sha256(canonical_request.encode()).hexdigest()}")

    def _hmac(key: bytes, msg: str) -> bytes:
        return hmac.new(key, msg.encode(), hashlib.sha256).digest()

    k_date = _hmac(f"AWS4{secret_key}".encode(), date_stamp)
    k_region = _hmac(k_date, REGION)
    k_service = _hmac(k_region, SERVICE)
    k_signing = _hmac(k_service, "aws4_request")
    signature = hmac.new(k_signing, string_to_sign.encode(),
                         hashlib.sha256).hexdigest()

    headers = {
        "Authorization": (f"AWS4-HMAC-SHA256 Credential={access_key}/{scope}, "
                          f"SignedHeaders={signed_headers}, "
                          f"Signature={signature}"),
        "X-Amz-Date": amz_date,
    }
    if session_token:
        headers["x-amz-security-token"] = session_token
    return headers


class ImageXUploader:
    """图片直传器。实例内复用 STS 凭证，连续上传仅增加申请与提交两次往返。"""

    def __init__(self, cookies: dict, timeout: float = 30.0):
        self.cookie_header = "; ".join(
            f"{k}={_cookie_value(v)}" for k, v in (cookies or {}).items()
            if _cookie_value(v))
        self.timeout = timeout
        self._sts: dict | None = None
        self._sts_at = 0.0

    # ---------- HTTP ----------
    def _call(self, method: str, url: str, body: bytes | None = None,
              headers: dict | None = None, raw: bool = False):
        hdrs = {"User-Agent": UA, "Accept": "*/*", "Origin": BASE,
                "Referer": BASE + "/chat", "Cookie": self.cookie_header}
        hdrs.update(headers or {})
        req = Request(url, data=body, headers=hdrs, method=method)
        try:
            with urlopen(req, timeout=self.timeout) as resp:
                data = resp.read()
                status = resp.status
        except HTTPError as exc:
            detail = exc.read()[:300]
            raise ImageXError(f"HTTP {exc.code} {urlparse(url).path}: "
                              f"{detail.decode('utf-8', 'ignore')}") from exc
        except (URLError, TimeoutError, OSError) as exc:
            raise ImageXError(f"网络错误 {urlparse(url).path}: {exc}") from exc
        if raw:
            return status, data
        try:
            return status, json.loads(data.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ImageXError(
                f"{urlparse(url).path} 返回非 JSON 内容: "
                f"{data[:200].decode('utf-8', 'ignore')}") from exc

    # ---------- 四步上传 ----------
    def _prepare(self) -> dict:
        """获取 STS 临时凭证；有效期内复用。"""
        if self._sts and time.time() - self._sts_at < STS_TTL:
            return self._sts
        query = ("version_code=20800&language=zh&device_platform=web&"
                 "doubao_device_platform=web&aid=497858&real_aid=497858")
        _, data = self._call(
            "POST", f"{BASE}/alice/resource/prepare_upload?{query}",
            body=json.dumps({"tenant_id": "5", "scene_id": "5",
                             "resource_type": 2}).encode(),
            headers={"Content-Type": "application/json"})
        if data.get("code") not in (0, None):
            raise ImageXError(f"prepare_upload 失败：{data.get('msg') or data}")
        payload = data.get("data") or {}
        token = payload.get("upload_auth_token") or {}
        if not (payload.get("service_id") and token.get("access_key")
                and token.get("secret_key")):
            raise ImageXError(f"prepare_upload 响应缺少必要字段：{str(data)[:200]}")
        self._sts = payload
        self._sts_at = time.time()
        return payload

    def _apply(self, sts: dict, size: int, ext: str) -> dict:
        """申请上传地址，取得 StoreUri、Auth、上传主机与 SessionKey。"""
        token = sts["upload_auth_token"]
        rand = "".join(random.choices(string.ascii_lowercase + string.digits,
                                      k=random.randint(8, 12)))
        url = (f"{BASE}/top/v1?Action=ApplyImageUpload&Version=2018-08-01"
               f"&ServiceId={sts['service_id']}&NeedFallback=true"
               f"&FileSize={size}&FileExtension=.{ext}&s={rand}")
        headers = _sign_v4("GET", url, b"", token["access_key"],
                           token["secret_key"], token.get("session_token", ""))
        _, data = self._call("GET", url, headers=headers)
        if data.get("Error"):
            raise ImageXError(
                f"ApplyImageUpload 失败：{data['Error'].get('Message')}")
        address = ((data.get("Result") or {}).get("UploadAddress") or {})
        infos = address.get("StoreInfos") or []
        hosts = address.get("UploadHosts") or []
        if not infos or not hosts:
            raise ImageXError(f"ApplyImageUpload 响应缺少上传地址：{str(data)[:200]}")
        return {"store_uri": infos[0]["StoreUri"], "auth": infos[0]["Auth"],
                "host": hosts[0], "session_key": address.get("SessionKey", "")}

    def _upload_tos(self, slot: dict, blob: bytes) -> None:
        """上传文件二进制内容；成功码为 2000。"""
        url = f"https://{slot['host']}/upload/v1/{slot['store_uri']}"
        _, data = self._call(
            "POST", url, body=blob,
            headers={"Authorization": slot["auth"],
                     "Content-CRC32": _crc32_hex(blob),
                     "Content-Type": "application/octet-stream"})
        if data.get("code") != 2000:
            raise ImageXError(f"TOS 上传失败：{data.get('message') or data}")

    def _commit(self, sts: dict, session_key: str) -> dict:
        """提交上传结果，返回 ImageUri 及图片元信息。"""
        token = sts["upload_auth_token"]
        url = (f"{BASE}/top/v1?Action=CommitImageUpload&Version=2018-08-01"
               f"&ServiceId={sts['service_id']}")
        body = json.dumps({"SessionKey": session_key}).encode()
        headers = _sign_v4("POST", url, body, token["access_key"],
                           token["secret_key"], token.get("session_token", ""))
        headers["Content-Type"] = "application/json"
        _, data = self._call("POST", url, body=body, headers=headers)
        if data.get("Error"):
            raise ImageXError(
                f"CommitImageUpload 失败：{data['Error'].get('Message')}")
        result = data.get("Result") or {}
        rows = result.get("Results") or []
        if not rows:
            raise ImageXError(f"CommitImageUpload 响应缺少结果：{str(data)[:200]}")
        uri = rows[0].get("Uri") or ""
        width = height = 0
        md5 = ""
        plugins = result.get("PluginResult") or []
        if plugins:
            uri = plugins[0].get("ImageUri") or uri
            width = int(plugins[0].get("ImageWidth") or 0)
            height = int(plugins[0].get("ImageHeight") or 0)
            md5 = plugins[0].get("ImageMd5") or ""
        if not uri:
            raise ImageXError("CommitImageUpload 未返回 ImageUri")
        return {"uri": uri, "width": width, "height": height, "md5": md5}

    # ---------- 对外接口 ----------
    def upload(self, path: str) -> dict:
        """上传一张图片，返回 {'uri', 'width', 'height', 'md5'}。"""
        with open(path, "rb") as f:
            blob = f.read()
        if not blob:
            raise ImageXError(f"图片内容为空：{path}")
        ext = (os.path.splitext(path)[1].lstrip(".") or "png").lower()
        if len(ext) > 8:
            ext = "png"
        sts = self._prepare()
        slot = self._apply(sts, len(blob), ext)
        self._upload_tos(slot, blob)
        return self._commit(sts, slot["session_key"])
