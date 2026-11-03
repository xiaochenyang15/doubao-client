from __future__ import annotations

import json
import os
import subprocess
import sys
import tarfile
import urllib.request

from doubao_client.core.config import APP_DIR

BIN_DIR = os.path.join(APP_DIR, "bin")
PY_DIR = os.path.join(BIN_DIR, "python")
PY_EXE = os.path.join(PY_DIR, "python.exe")

# 与 NVDA 屏幕阅读器使用同一版本的解释器，便于 comtypes / pywin32 这类无障碍
# 相关包的行为与轮子保持一致。
#
# 版本依据（NVDA 仓库的 .python-versions）：
#   release-2026.3（当前稳定版）→ cpython-3.13.15-windows-x86_64-none
#   release-2026.1            → cpython-3.13.12-windows-x86_64-none
#   master                    → cpython-3.13.17-windows-x86_64-none
# 本程序对齐稳定版 2026.3，即 3.13.15。
WANT = "3.13"
PATCH = "3.13.15"        # 找不到这一版就退到同系列最新的 3.13.x
MIRROR = "https://registry.npmmirror.com/-/binary/python-build-standalone"
GITHUB = "https://github.com/astral-sh/python-build-standalone/releases/download"
RELEASE_API = ("https://api.github.com/repos/astral-sh/"
               "python-build-standalone/releases/latest")


def installed_version() -> str:
    if not os.path.exists(PY_EXE):
        return ""
    try:
        r = subprocess.run(
            [PY_EXE, "-c", "import platform;print(platform.python_version())"],
            capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.SubprocessError):
        return ""
    return r.stdout.strip() if r.returncode == 0 else ""


def python_ready() -> bool:
    """不仅要求文件在，还要求大版本对得上——不然换版本时会被旧目录挡住。"""
    return installed_version().startswith(WANT + ".")


def retire_old():
    """版本不符时把旧目录挪开。

    直接删要递归删上万个文件，既慢又容易被安全策略拦；改名是瞬间完成的，
    用户确认没问题后自己删掉即可。
    """
    if not os.path.isdir(PY_DIR):
        return
    old = installed_version() or "unknown"
    dst = os.path.join(BIN_DIR, f"python_{old}_backup")
    n = 1
    while os.path.exists(dst):
        dst = os.path.join(BIN_DIR, f"python_{old}_backup{n}")
        n += 1
    os.replace(PY_DIR, dst)
    print(f"旧版本 {old} 已挪到 {os.path.basename(dst)}，确认新版本可用后可以删除")


def _pick(files: list[str], exact: bool = True) -> str:
    """在文件清单里挑：win x64 + install_only_stripped（体积最小）。

    ``exact=True`` 只认 ``PATCH`` 那一版；给 False 就退到同系列最新的 3.13.x。
    """
    head = f"cpython-{PATCH}+" if exact else f"cpython-{WANT}."
    for f in files:
        if (f.startswith(head) and "x86_64-pc-windows-msvc" in f
                and "-freethreaded-" not in f
                and f.endswith("-install_only_stripped.tar.gz")):
            return f
    return ""


def resolve_mirror() -> tuple[str, str]:
    """镜像目录按日期命名。先找钉死的那一版，再退到最新的 3.13.x。

    精确版可能在几个月前的 tag 里（补丁版一发布就不再重出），所以按时间倒序
    扫最近的若干个目录，最多 24 个（约半年），扫不到就用最新 3.13.x。
    """
    with urllib.request.urlopen(MIRROR + "/", timeout=45) as r:
        tags = [d["name"].strip("/") for d in json.loads(r.read().decode())
                if d.get("type") == "dir"]
    tags = sorted(tags, reverse=True)[:24]

    fallback: tuple[str, str] = ("", "")
    for tag in tags:
        try:
            with urllib.request.urlopen(f"{MIRROR}/{tag}/", timeout=45) as r:
                files = [d["name"] for d in json.loads(r.read().decode())]
        except Exception:  # noqa: BLE001
            continue
        name = _pick(files, exact=True)
        if name:
            return f"{MIRROR}/{tag}/{name}", name
        if not fallback[0]:
            loose = _pick(files, exact=False)
            if loose:
                fallback = (f"{MIRROR}/{tag}/{loose}", loose)
    if fallback[0]:
        print(f"镜像里没有 {PATCH}，退到最近的 3.13.x 构建")
        return fallback
    raise RuntimeError(f"镜像里没找到 {WANT} 的 Windows 构建")


def resolve_github() -> tuple[str, str]:
    req = urllib.request.Request(RELEASE_API,
                                 headers={"User-Agent": "doubao-client"})
    with urllib.request.urlopen(req, timeout=45) as r:
        data = json.loads(r.read().decode())
    tag = data["tag_name"]
    files = [a["name"] for a in data.get("assets", [])]
    name = _pick(files, exact=True) or _pick(files, exact=False)
    if not name:
        raise RuntimeError(f"release {tag} 里没有 {WANT} 的 Windows 构建")
    return f"{GITHUB}/{tag}/{name}", name


def download(url: str, dest: str):
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    tmp = dest + ".part"
    with urllib.request.urlopen(url, timeout=180) as r:
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
                    sys.stdout.write(f"\r下载中 {pct}%  ({done // 1048576}MB/"
                                     f"{total // 1048576}MB)")
                    sys.stdout.flush()
    print()
    os.replace(tmp, dest)


def extract(tar_path: str):
    print("解压中…")
    with tarfile.open(tar_path, "r:gz") as t:
        # filter="data"：拒绝绝对路径 / 越界链接，别让一个压缩包写到 bin 外面去
        t.extractall(BIN_DIR, filter="data")
    os.remove(tar_path)


def check() -> tuple[bool, str]:
    """确认这份解释器真能跑、pip 也在。"""
    try:
        r = subprocess.run([PY_EXE, "-c", "import sys;print(sys.version)"],
                           capture_output=True, text=True, timeout=60)
        if r.returncode != 0:
            return False, r.stdout + r.stderr
        version = r.stdout.strip().split()[0]
        p = subprocess.run([PY_EXE, "-m", "pip", "--version"],
                           capture_output=True, text=True, timeout=60)
        has_pip = p.returncode == 0
        if not has_pip:
            subprocess.run([PY_EXE, "-m", "ensurepip", "--default-pip"],
                           capture_output=True, text=True, timeout=180)
            p = subprocess.run([PY_EXE, "-m", "pip", "--version"],
                               capture_output=True, text=True, timeout=60)
            has_pip = p.returncode == 0
        return True, f"{version}，pip {'可用' if has_pip else '缺失（装包会失败）'}"
    except (OSError, subprocess.SubprocessError) as exc:
        return False, str(exc)


def main():
    if python_ready():
        ok, info = check()
        print(f"已存在: {PY_EXE}（{info}）")
        return
    os.makedirs(BIN_DIR, exist_ok=True)
    retire_old()
    print(f"准备下载 Python {PATCH} 便携版（与 NVDA 2026.3 一致）")
    tar_path = os.path.join(BIN_DIR, f"python-{PATCH}.tar.gz")
    last = None
    for label, resolver in (("国内镜像", resolve_mirror), ("GitHub", resolve_github)):
        try:
            url, name = resolver()
        except Exception as exc:  # noqa: BLE001
            last = exc
            print(f"{label} 拿不到地址：{exc}")
            continue
        print(f"[{label}] {name}")
        try:
            download(url, tar_path)
            break
        except Exception as exc:  # noqa: BLE001
            last = exc
            print(f"下载失败({exc})，换下一个源…")
    else:
        raise SystemExit(f"所有下载源均失败: {last}")

    extract(tar_path)
    if not python_ready():
        raise SystemExit(f"解压后没找到 python.exe，检查 {PY_DIR}")
    ok, info = check()
    print(("完成: " if ok else "有问题: ") + f"{PY_EXE}（{info}）")


if __name__ == "__main__":
    main()
