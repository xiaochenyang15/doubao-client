from __future__ import annotations

import json
import os
import sys
import urllib.request
import zipfile

from doubao_client.core.config import APP_DIR

BIN_DIR = os.path.join(APP_DIR, "bin")
TARGET_ZIP = os.path.join(BIN_DIR, "chrome-win64.zip")
CHROME_EXE = os.path.join(BIN_DIR, "chrome-win64", "chrome.exe")
VERSION_API = ("https://googlechromelabs.github.io/chrome-for-testing/"
               "last-known-good-versions-with-downloads.json")
# 国内镜像，下载同一份官方构建
MIRROR = "https://registry.npmmirror.com/-/binary/chrome-for-testing"


def chrome_ready() -> bool:
    return os.path.exists(CHROME_EXE)


def resolve_url() -> tuple[str, str]:
    with urllib.request.urlopen(VERSION_API, timeout=30) as r:
        data = json.loads(r.read().decode())
    stable = data["channels"]["Stable"]
    for item in stable["downloads"]["chrome"]:
        if item["platform"] == "win64":
            return stable["version"], item["url"]
    raise RuntimeError("没有找到 win64 版本")


def download(url: str, dest: str):
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    tmp = dest + ".part"
    with urllib.request.urlopen(url, timeout=120) as r:
        total = int(r.headers.get("Content-Length") or 0)
        done = 0
        with open(tmp, "wb") as f:
            while True:
                chunk = r.read(256 * 1024)
                if not chunk:
                    break
                f.write(chunk)
                done += len(chunk)
                if total:
                    pct = done * 100 // total
                    sys.stdout.write(f"\r下载中 {pct}%  ({done // 1024 // 1024}MB/"
                                     f"{total // 1024 // 1024}MB)")
                    sys.stdout.flush()
    print()
    os.replace(tmp, dest)


def extract(zip_path: str):
    print("解压中…")
    with zipfile.ZipFile(zip_path) as z:
        z.extractall(BIN_DIR)
    os.remove(zip_path)


def main():
    if chrome_ready():
        print(f"已存在: {CHROME_EXE}")
        return
    version, url = resolve_url()
    print(f"准备下载 Chrome {version}")
    candidates = [f"{MIRROR}/{version}/win64/chrome-win64.zip", url]
    last_error = None
    for i, candidate in enumerate(candidates, 1):
        print(f"[{i}/{len(candidates)}] {candidate}")
        try:
            download(candidate, TARGET_ZIP)
            break
        except Exception as exc:  # noqa: BLE001
            last_error = exc
            print(f"\n下载失败({exc})，尝试下一个源…")
    else:
        raise SystemExit(f"所有下载源均失败: {last_error}")

    extract(TARGET_ZIP)
    if chrome_ready():
        print(f"完成: {CHROME_EXE}")
    else:
        raise SystemExit("解压后未找到 chrome.exe")


if __name__ == "__main__":
    main()
