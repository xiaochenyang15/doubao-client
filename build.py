"""一键打包：PyInstaller 出 exe，再把便携 Python 和 Chrome 组装进 dist。

产物目录（可直接压包分发）::

    dist/
      doubao_client.exe          主程序（含 sounds/）
      bin/python/                便携 Python 3.13 —— Agent 的 run_code 用它
      bin/chrome-win64/          便携 Chrome —— CDP 桥用它
      config.json                设备标识（首次运行会自动生成）

刻意**不**进包的东西：`cookies.dat`（登录凭据）、`_chrome_profile/`（浏览器
配置）、`_work/`（跑过的脚本）、`*.log`。这些是跑起来之后才产生的私有数据，
打进去等于把上一个用户的登录态一起发出去。

用法::

    python build.py            # 打包
    python build.py --zip      # 打包并压成 zip
    python build.py --no-exe   # 只重新组装 dist（跳过 PyInstaller）
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
import zipfile
from pathlib import Path

from doubao_client.core.config import DEFAULT_CONFIG

ROOT = Path(__file__).resolve().parent
DIST = ROOT / "dist"
BUILD = ROOT / "build"
BIN = ROOT / "bin"
EXE = DIST / "doubao_client.exe"
SPEC = ROOT / "doubao_client.spec"

# 便携 Python 里用不上的标准库（删了不影响 import，pip 照常工作）
PY_JUNK_DIRS = ("Lib/idlelib", "Lib/turtledemo", "Lib/test", "Lib/lib2to3")
# Chrome 里与 CDP 自动化无关的可执行文件
CHROME_JUNK_FILES = ("setup.exe", "chrome_proxy.exe", "chrome_pwa_launcher.exe",
                     "notification_helper.exe", "elevation_service.exe")
# 界面语言包只留中英，其余是几十兆的纯文本
CHROME_KEEP_LOCALES = {"zh-CN.pak", "zh-TW.pak", "en-US.pak"}
# 运行期私有数据，一律不进分发包
PRIVATE = ("cookies.dat", "cookies.json", "_chrome_profile", "_work",
           "bridge.log", "bridge.log.1", "LocalState.json",
           # Ctrl+V 粘贴待发送的图片：属于使用痕迹，不进包
           "_paste",
           # Agent 学到的命名锚点：属于这台机器的使用痕迹，不该跟着安装包发出去
           "agent_anchors.json", "agent_anchors.json.tmp")

# 预装进随包解释器的常用库：Agent 第一段 run_code 就能直接 import，
# 不必先等一次 pip；没网也能干活。其余包仍按需自动安装。
EXTRAS = ("requests", "beautifulsoup4", "openpyxl", "python-docx", "pillow")
MIRROR = "https://pypi.tuna.tsinghua.edu.cn/simple"


def human(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f}{unit}" if unit == "B" else f"{n:.1f}{unit}"
        n /= 1024.0
    return f"{n:.1f}GB"


def size_of(path: Path) -> int:
    if path.is_file():
        return path.stat().st_size
    total = 0
    for base, _dirs, files in os.walk(path):
        for name in files:
            try:
                total += os.path.getsize(os.path.join(base, name))
            except OSError:
                pass
    return total


def say(msg: str) -> None:
    print(f"[build] {msg}", flush=True)


def copy_tree(src: Path, dst: Path, skip_dirs: tuple[str, ...] = (),
              skip_files: tuple[str, ...] = ()) -> None:
    """整目录复制。Windows 上用 robocopy，几十万个文件也只要几秒。

    `skip_dirs` / `skip_files` 直接交给 robocopy 排除，而不是「先全复制、
    再把没用的删掉」：少删几十万个文件，构建快一截，也不会在某些安全策略眼里
    变成一次批量删除。带 `*` 的名字按通配符原样传给 robocopy（如 `*.pak`）。
    """
    if not src.is_dir():
        raise SystemExit(f"缺少目录：{src}")
    dst.mkdir(parents=True, exist_ok=True)
    if os.name == "nt" and shutil.which("robocopy"):
        def _arg(name: str) -> str:
            # 带路径的条目补成绝对路径（精确排除）；裸名字和通配符原样传，
            # robocopy 会按名字匹配任意层级（`__pycache__`、`*.pak` 都是这种）。
            if "*" in name or ("/" not in name and "\\" not in name):
                return name
            return str(src / name)

        args = ["robocopy", str(src), str(dst), "/E", "/NFL", "/NDL", "/NJH",
                "/NJS", "/NC", "/NS", "/NP", "/MT:8"]
        if skip_dirs:
            args += ["/XD", *[_arg(d) for d in skip_dirs]]
        if skip_files:
            args += ["/XF", *[_arg(f) for f in skip_files]]
        r = subprocess.run(args, capture_output=True, text=True,
                           encoding="utf-8", errors="replace")
        if r.returncode >= 8:            # 0-7 都是成功（含「有文件被复制」）
            raise SystemExit(f"复制 {src} 失败：\n{(r.stdout or '')[-1200:]}")
    else:
        def _ignore(directory, names):
            base = os.path.relpath(directory, src).replace("\\", "/")
            out = set()
            for name in names:
                rel = name if base == "." else f"{base}/{name}"
                if rel in skip_dirs or name in skip_dirs:
                    out.add(name)
                elif rel in skip_files or name in skip_files:
                    out.add(name)
            return out

        shutil.copytree(src, dst, dirs_exist_ok=True, ignore=_ignore)


def prune_python(py: Path) -> None:
    for rel in PY_JUNK_DIRS:
        shutil.rmtree(py / rel, ignore_errors=True)
    for cache in list(py.rglob("__pycache__")):
        shutil.rmtree(cache, ignore_errors=True)


def restore_locales(src: Path, dst: Path) -> None:
    """上面把 *.pak 全排除了，再把要留的三门语言包单独拷回来。"""
    out = dst / "locales"
    for name in CHROME_KEEP_LOCALES:
        f = src / "locales" / name
        if f.is_file():
            out.mkdir(parents=True, exist_ok=True)
            shutil.copy2(f, out / name)


def prune_chrome(ch: Path) -> None:
    for name in CHROME_JUNK_FILES:
        try:
            (ch / name).unlink()
        except OSError:
            pass
    locales = ch / "locales"
    if locales.is_dir():
        for f in locales.glob("*.pak"):
            if f.name not in CHROME_KEEP_LOCALES:
                try:
                    f.unlink()
                except OSError:
                    pass


def install_extras(py: Path) -> bool:
    """往随包解释器里预装常用库（装在包里，不是装在本机系统 Python）。"""
    cmd = [str(py), "-m", "pip", "install", "-q", "--disable-pip-version-check",
           "--no-input", "--no-warn-script-location", *EXTRAS]
    for index in (MIRROR, ""):
        run = cmd + (["-i", index] if index else [])
        say(f"预装常用库：{'、'.join(EXTRAS)}" + ("（国内镜像）" if index else ""))
        try:
            r = subprocess.run(run, capture_output=True, text=True,
                               encoding="utf-8", errors="replace", timeout=600)
        except (OSError, subprocess.SubprocessError) as exc:
            say(f"预装失败：{exc}")
            continue
        if r.returncode == 0:
            return True
        say("预装没成功，换下一个源")
    say("预装跳过：常用库没装上，Agent 第一次用到时会自己装")
    return False


def sanitize() -> None:
    """把私有数据从 dist 里清掉，并补一份 config.json。"""
    for name in PRIVATE:
        p = DIST / name
        if p.is_dir():
            shutil.rmtree(p, ignore_errors=True)
        else:
            try:
                p.unlink()
            except OSError:
                pass
    for log in DIST.glob("*.log"):
        try:
            log.unlink()
        except OSError:
            pass
    cfg = DIST / "config.json"
    if not cfg.exists():
        cfg.write_text(json.dumps(DEFAULT_CONFIG, indent=2), encoding="utf-8")


def clean_outputs() -> None:
    for p in (BUILD, DIST):
        if p.is_dir():
            say(f"清空旧产物 {p.name}/")
            shutil.rmtree(p, ignore_errors=True)
    for cache in list(ROOT.rglob("__pycache__")):
        if DIST in cache.parents or BUILD in cache.parents or BIN in cache.parents:
            continue
        shutil.rmtree(cache, ignore_errors=True)


def pyinstaller() -> None:
    say("PyInstaller 打包中（约 1-3 分钟）…")
    started = time.time()
    r = subprocess.run([sys.executable, "-m", "PyInstaller", "--noconfirm",
                        "--clean", str(SPEC)], cwd=str(ROOT))
    if r.returncode != 0 or not EXE.is_file():
        raise SystemExit("PyInstaller 失败，看上面的输出")
    say(f"exe 生成完成，用时 {time.time() - started:.0f}s，"
        f"{human(EXE.stat().st_size)}")


def assemble(with_pkgs: bool = True) -> None:
    copy_tree(BIN / "python", DIST / "bin" / "python",
              skip_dirs=PY_JUNK_DIRS + ("__pycache__",))
    prune_python(DIST / "bin" / "python")
    say("已组装便携 Python")

    copy_tree(BIN / "chrome-win64", DIST / "bin" / "chrome-win64",
              skip_files=CHROME_JUNK_FILES + ("*.pak",))
    restore_locales(BIN / "chrome-win64", DIST / "bin" / "chrome-win64")
    prune_chrome(DIST / "bin" / "chrome-win64")
    say("已组装便携 Chrome")

    copy_tree(BIN / "bass", DIST / "bin" / "bass")
    say("已组装 BASS（朗读放音）")

    if with_pkgs:
        install_extras(DIST / "bin" / "python" / "python.exe")

    sanitize()


def verify() -> bool:
    ok = True
    py = DIST / "bin" / "python" / "python.exe"
    if not py.is_file():
        say("× 便携 Python 没进包")
        return False
    r = subprocess.run([str(py), "-c",
                        "import sys,pip;print(sys.version.split()[0])"],
                       capture_output=True, text=True, encoding="utf-8",
                       errors="replace")
    if r.returncode == 0:
        say(f"√ 便携 Python 可用：{r.stdout.strip()}")
    else:
        say("× 便携 Python 跑不起来：" + (r.stderr or "")[-300:])
        ok = False

    probe = ("import requests, bs4, openpyxl, docx, PIL;"
             "print('常用库就绪')")
    r = subprocess.run([str(py), "-X", "utf8", "-c", probe],
                       capture_output=True, text=True, encoding="utf-8",
                       errors="replace")
    if r.returncode == 0:
        say("√ " + r.stdout.strip())
    else:
        say("· 常用库未预装（Agent 用到时会自动装）")
    chrome = DIST / "bin" / "chrome-win64" / "chrome.exe"
    if chrome.is_file():
        say("√ 便携 Chrome 就位")
    else:
        say("× 缺 bin/chrome-win64/chrome.exe")
        ok = False
    bass = DIST / "bin" / "bass" / "x64" / "bass.dll"
    if bass.is_file() and (bass.parent / "bass_aac.dll").is_file():
        say("√ BASS 放音组件就位")
    else:
        say("× 缺 bin/bass/x64/bass.dll 或 bass_aac.dll（朗读放不出声）")
        ok = False
    if EXE.is_file():
        say(f"√ 主程序 {human(EXE.stat().st_size)}")
    else:
        ok = False
    return ok


def report() -> None:
    say("产物清单：")
    total = 0
    for entry in sorted(DIST.iterdir(), key=lambda p: -size_of(p)):
        s = size_of(entry)
        total += s
        print(f"        {human(s):>9}  {entry.name}")
    print(f"        {human(total):>9}  合计")


def make_zip() -> Path:
    name = f"doubao_client_{time.strftime('%Y%m%d')}.zip"
    out = ROOT / name
    say(f"压缩中 → {name}（大文件较多，耐心等）")
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as z:
        for base, _dirs, files in os.walk(DIST):
            for f in files:
                full = Path(base) / f
                z.write(full, Path("doubao_client") / full.relative_to(DIST))
    say(f"zip 完成：{out}（{human(out.stat().st_size)}）")
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description="打包豆包桌面客户端")
    ap.add_argument("--zip", action="store_true", help="顺便压成 zip")
    ap.add_argument("--no-exe", action="store_true", help="跳过 PyInstaller，只重组 dist")
    ap.add_argument("--no-pkgs", action="store_true", help="不预装常用库")
    args = ap.parse_args()

    if not (BIN / "python" / "python.exe").is_file():
        raise SystemExit("缺 bin/python：先跑 python setup_python.py")
    if not (BIN / "chrome-win64" / "chrome.exe").is_file():
        raise SystemExit("缺 bin/chrome-win64：先跑 python setup_chrome.py")
    if not (BIN / "bass" / "x64" / "bass.dll").is_file():
        raise SystemExit("缺 bin/bass/x64/bass.dll：朗读要靠它放音")

    clean_outputs()
    if not args.no_exe:
        pyinstaller()
    elif not EXE.is_file():
        raise SystemExit("dist 里没有 exe，去掉 --no-exe 重新打包")

    assemble(with_pkgs=not args.no_pkgs)
    ok = verify()
    report()
    if args.zip:
        make_zip()
    if not ok:
        say("有检查项没通过，别急着分发")
        return 1
    say("完成：dist/ 可以直接拷走运行")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
