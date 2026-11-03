from __future__ import annotations

import base64
import glob
import hashlib
import json
import os
import shutil
import sqlite3
import tempfile
import time

from Crypto.Cipher import AES
import win32crypt

from ..core.config import APP_DIR

BROWSER_ROOTS = [
    os.path.expandvars(r"%LOCALAPPDATA%\Google\Chrome\User Data"),
    os.path.expandvars(r"%LOCALAPPDATA%\Microsoft\Edge\User Data"),
]

LOCAL_PROFILE = os.path.join(APP_DIR, "_chrome_profile")
# 登录态的长期存储：与 exe 同目录，重启/换会话都还在。
# 内容用 DPAPI 加密，只有当前 Windows 用户能解开，不是明文 cookie。
STORE_PATH = os.path.join(APP_DIR, "cookies.dat")
STORE_VERSION = 1


def _roots() -> list[str]:
    roots = []
    # 便携包自带的浏览器配置优先：保证登录态来源稳定、可控
    if os.path.isdir(LOCAL_PROFILE):
        roots.append(LOCAL_PROFILE)
    # 其次才是本机日常使用的浏览器（用户可能已经登录过，省掉重复登录）
    roots += [r for r in BROWSER_ROOTS if os.path.isdir(r)]
    return roots

# 判定"已登录"的关键 cookie
LOGIN_COOKIES = ("sessionid", "sessionid_ss", "sid_guard", "sid_tt")


class CookieError(RuntimeError):
    pass


def _master_key(user_data: str) -> bytes:
    state_path = os.path.join(user_data, "Local State")
    if not os.path.exists(state_path):
        raise CookieError("缺少 Local State，无法取得解密密钥")
    with open(state_path, "r", encoding="utf-8") as f:
        state = json.load(f)
    enc = base64.b64decode(state.get("os_crypt", {}).get("encrypted_key", ""))
    if enc[:5] != b"DPAPI":
        raise CookieError("密钥格式异常")
    return win32crypt.CryptUnprotectData(enc[5:], None, None, None, 0)[1]


def _decrypt(blob: bytes, key: bytes, host: str) -> str:
    if not blob:
        return ""
    if blob[:3] in (b"v10", b"v20"):
        nonce, payload = blob[3:15], blob[15:]
        try:
            raw = AES.new(key, AES.MODE_GCM, nonce=nonce).decrypt_and_verify(
                payload[:-16], payload[-16:])
        except ValueError:
            raw = win32crypt.CryptUnprotectData(blob, None, None, None, 0)[1]
    else:
        raw = win32crypt.CryptUnprotectData(blob, None, None, None, 0)[1]
    prefix = hashlib.sha256(host.encode()).digest()
    if raw.startswith(prefix):
        raw = raw[32:]
    return raw.decode("utf-8", "ignore")


def _profiles(user_data: str) -> list[str]:
    dirs = [os.path.join(user_data, "Default")]
    dirs += sorted(glob.glob(os.path.join(user_data, "Profile *")))
    return [d for d in dirs if os.path.isdir(d)]


def _cookie_db(profile: str) -> str | None:
    for rel in (("Network", "Cookies"), ("Cookies",)):
        p = os.path.join(profile, *rel)
        if os.path.exists(p):
            return p
    return None


def extract_from_profile(profile: str, keyword: str = "doubao") -> dict:
    db = _cookie_db(profile)
    if not db:
        return {}
    user_data = os.path.dirname(profile)
    key = _master_key(user_data)

    tmpdir = tempfile.mkdtemp(prefix="dbcookie_")
    tmp_db = os.path.join(tmpdir, "Cookies")
    try:
        shutil.copy2(db, tmp_db)
        # WAL 文件也一起带上，避免读到旧快照
        for suffix in ("-wal", "-shm"):
            if os.path.exists(db + suffix):
                shutil.copy2(db + suffix, tmp_db + suffix)
        con = sqlite3.connect(f"file:{tmp_db}?mode=ro", uri=True)
        rows = con.execute(
            "select host_key,name,encrypted_value,path from cookies where host_key like ?",
            (f"%{keyword}%",)).fetchall()
        con.close()
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)

    out = {}
    for host, name, blob, path in rows:
        try:
            value = _decrypt(blob, key, host)
        except Exception:  # noqa: BLE001
            continue
        if value:
            out[name] = {"value": value, "domain": host, "path": path or "/"}
    return out


def extract_portable(keyword: str = "doubao") -> tuple[dict, str]:
    """只读便携包里的浏览器配置，这是客户端默认的登录态来源。"""
    for prof in _profiles(LOCAL_PROFILE):
        try:
            cookies = extract_from_profile(prof, keyword)
        except Exception:  # noqa: BLE001
            continue
        if cookies:
            return cookies, "便携包/" + os.path.basename(prof)
    return {}, ""


def extract_all(keyword: str = "doubao") -> tuple[dict, str]:
    """遍历所有浏览器配置，返回 (cookies, 来源说明)。优先返回已登录的那份。"""
    best: tuple[dict, str] = ({}, "")
    for root in _roots():
        for prof in _profiles(root):
            try:
                cookies = extract_from_profile(prof, keyword)
            except Exception:  # noqa: BLE001
                continue
            if not cookies:
                continue
            # 统一走 value_of：既兼容裸字符串形态，也不会把 0/False 当成「有值」
            logged = any(value_of(cookies.get(k)) for k in LOGIN_COOKIES)
            label = os.path.basename(os.path.dirname(root)) + "/" + os.path.basename(prof)
            if logged:
                return cookies, label
            if not best[0]:
                best = (cookies, label)
    return best


def value_of(value) -> str:
    """cookie 值有两种来源：浏览器数据库给 {"value": "..."}，CDP 回来的是裸字符串。
    全项目都经过这里取值，别再各写一遍 isinstance 判断。

    **假值一律当空**：``str(0) == "0"``、``str(False) == "False"`` 都是真值，
    直接 ``str()`` 会把「没值」变成「有值」——``is_logged_in`` 就会把没登录
    说成已登录，``_auth_chain`` 于是拿这组假 cookie 去验，服务端不认 →
    ``AuthError`` → ``clear_store()`` 把**真的** cookies.dat 删掉，下次启动
    就「未检测到豆包登录态」。``account()`` 早先栽在 ``str(user_id)`` 把 0
    变成 ``"0"`` 上，属于同类问题，应避免重复。
    （注意 ``"0"`` 这个字符串是真值，照常返回——只有 0/False/空这类假值才当空。）
    """
    if isinstance(value, dict):
        value = value.get("value", "")
    if not value:
        return ""
    return str(value)


def to_header(cookies: dict) -> str:
    """拼成能直接放进请求头 / WebSocket 握手的 Cookie 串。"""
    return "; ".join(f"{key}={value_of(val)}"
                     for key, val in (cookies or {}).items() if value_of(val))


def is_logged_in(cookies: dict) -> bool:
    """只是「看起来登录过」的粗判，不能证明登录态有效（有效期只有服务端知道）。"""
    return any(value_of(cookies.get(k)) for k in LOGIN_COOKIES)


# --------------------------------------------------------------------------- #
# 长期存储
# --------------------------------------------------------------------------- #
def save_store(cookies: dict) -> bool:
    """把登录态加密写入程序目录；下次启动直接复用，不必再翻浏览器数据库。"""
    if not cookies:
        return False
    try:
        raw = json.dumps(cookies, ensure_ascii=False).encode("utf-8")
        blob = win32crypt.CryptProtectData(raw, "doubao_client", None, None, None, 0)
        payload = {
            "v": STORE_VERSION,
            "saved_at": int(time.time()),
            "dpapi": base64.b64encode(blob).decode("ascii"),
        }
        tmp = STORE_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(payload, f)
        os.replace(tmp, STORE_PATH)  # 原子替换，断电不会留下半个文件
        return True
    except Exception:  # noqa: BLE001
        return False


def load_store() -> dict:
    """读回长期存储的登录态；解不开（换了用户/文件损坏）就当没有。"""
    if not os.path.exists(STORE_PATH):
        return {}
    try:
        with open(STORE_PATH, encoding="utf-8") as f:
            payload = json.load(f)
        blob = base64.b64decode(payload.get("dpapi") or "")
        raw = win32crypt.CryptUnprotectData(blob, None, None, None, 0)[1]
        data = json.loads(raw.decode("utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:  # noqa: BLE001
        return {}


def clear_store():
    """登录态确认失效时清掉，避免下次启动拿旧数据反复失败。"""
    for path in (STORE_PATH, STORE_PATH + ".tmp"):
        try:
            os.remove(path)
        except OSError:
            pass


def store_saved_at() -> int:
    """存储时间戳，仅用于界面提示。"""
    try:
        with open(STORE_PATH, encoding="utf-8") as f:
            return int(json.load(f).get("saved_at") or 0)
    except Exception:  # noqa: BLE001
        return 0


def save(cookies: dict, path: str):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(cookies, f, ensure_ascii=False, indent=2)
