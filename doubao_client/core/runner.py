"""本机 Python 代码执行引擎。

职责：
    1. 解释器定位——随包分发的便携 Python 优先，其次本机安装；
    2. 脚本执行——写入临时脚本、在指定工作目录运行、合并输出流；
    3. 依赖自愈——从报错中提取缺失模块名，自动 pip 安装后重试；
    4. 环境画像——向模型描述可用解释器与已安装的第三方包；
    5. 代码块提取——从模型回复的 Markdown 中解析可执行 Python 片段。

上层调用方（主窗口、AI Agent、包管理对话框）统一通过模块级默认实例
``interpreter`` / ``code_runner`` 访问，也可以为测试注入独立实例。
"""
from __future__ import annotations

import os
import queue
import re
import shutil
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

from . import md_render
from .config import APP_DIR, FROZEN, load_config

# 仅识别明确的 Python 语言标记。未标记语言（```）或标记 bash/cmd 的代码块
# 属于命令行示例或输出样例，不纳入执行范围。
PY_LANGS = {"python", "py", "python3", "py3", "pythonw"}

DEFAULT_TIMEOUT = 300          # 单次脚本执行上限（秒）
MAX_OUTPUT = 30000             # 输出超过该字符数时仅保留首尾
MAX_INSTALL_ROUNDS = 6         # 缺包安装并重试的最大轮次
LOG_KEEP = 4000                # 超过该字符数的完整输出另存为日志文件

# 默认 PyPI 在部分网络环境下可能无法访问，因此默认使用国内镜像；
# 可通过「设置」菜单中的「pip 镜像源」全局修改（存储于 config.json 的
# pip_mirror 字段），此处仅为初始默认值。
MIRROR = "https://pypi.tuna.tsinghua.edu.cn/simple"

# 单次安装尝试的时限下限。慢速网络下仅依赖解析就可能需要数分钟，
# 较短的时限会将正常下载误判为失败。
INSTALL_PER_TRY = 600


def pip_mirror() -> str:
    """全局镜像源；空串表示使用官方 PyPI。每次安装前实时读取，修改后立即生效。"""
    try:
        return (load_config().get("pip_mirror") or "").strip()
    except Exception:  # noqa: BLE001
        return MIRROR


# import 名与 pip 包名不一致的常见映射关系
_PKG_ALIAS = {
    "pil": "pillow",
    "cv2": "opencv-python",
    "yaml": "pyyaml",
    "dateutil": "python-dateutil",
    "bs4": "beautifulsoup4",
    "docx": "python-docx",
    "pptx": "python-pptx",
    "fitz": "pymupdf",
    "crypto": "pycryptodome",
    "win32api": "pywin32",
    "win32con": "pywin32",
    "win32gui": "pywin32",
    "win32com": "pywin32",
    "pythoncom": "pywin32",
    "serial": "pyserial",
    "usb": "pyusb",
    "sklearn": "scikit-learn",
    "skimage": "scikit-image",
    "attr": "attrs",
    "wx": "wxpython",
    "websocket": "websocket-client",
    "psutil": "psutil",
    "pyautogui": "pyautogui",
    "pywinauto": "pywinauto",
    "openpyxl": "openpyxl",
    "pandas": "pandas",
}


@dataclass
class Snippet:
    """回复中的一段可执行代码。"""

    code: str
    lang: str
    index: int                  # 从 1 开始，供界面下拉框显示

    @property
    def first_line(self) -> str:
        for line in self.code.splitlines():
            if line.strip():
                return line.strip()[:60]
        return "（空）"


@dataclass
class RunResult:
    """一次执行的完整结果。"""

    script: str = ""
    workdir: str = ""
    python: str = ""
    exit_code: int | None = None
    output: str = ""
    log_path: str = ""          # 输出过长时，完整内容保存到该文件
    duration: float = 0.0
    timed_out: bool = False
    cancelled: bool = False
    installed: list[str] = field(default_factory=list)
    error: str = ""             # 启动失败（如找不到解释器），与脚本报错区分

    @property
    def ok(self) -> bool:
        return not self.error and not self.timed_out and self.exit_code == 0

    def summary(self) -> str:
        if self.error:
            return f"未能启动执行：{self.error}"
        if self.cancelled:
            return f"已取消（运行 {self.duration:.0f} 秒）"
        if self.timed_out:
            return f"执行超时（运行 {self.duration:.0f} 秒）"
        if self.exit_code == 0:
            return f"执行成功，用时 {self.duration:.1f} 秒"
        return f"脚本报错，退出码 {self.exit_code}，用时 {self.duration:.1f} 秒"

    def report(self) -> str:
        """拼接可读文本：结果框显示、复制、回传模型均使用该格式。"""
        lines = [self.summary()]
        if self.installed:
            lines.append("自动安装的包：" + "、".join(self.installed))
        if self.workdir:
            lines.append(f"工作目录：{self.workdir}")
        if self.script:
            lines.append(f"脚本：{self.script}")
        if self.log_path:
            lines.append(f"完整输出（超过显示上限的部分）已保存至：{self.log_path}"
                         "（后续可通过 run_code 读取该文件）")
        lines.append("")
        lines.append("—— 输出 ——")
        lines.append(self.output.rstrip() or "（无输出）")
        return "\n".join(lines)


def _no_window() -> int:
    return getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0


def _truncate(text: str) -> str:
    if len(text) <= MAX_OUTPUT:
        return text
    head = MAX_OUTPUT * 2 // 3
    tail = MAX_OUTPUT - head
    return (text[:head]
            + f"\n\n……中间省略 {len(text) - MAX_OUTPUT} 个字符……\n\n"
            + text[-tail:])


def _kill(proc: subprocess.Popen) -> None:
    """终止子进程及其全部后代，避免脚本启动的浏览器等进程残留。"""
    if proc.poll() is not None:
        return
    if os.name == "nt":
        try:
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                           capture_output=True, timeout=20,
                           creationflags=_no_window())
        except (OSError, subprocess.SubprocessError):
            pass
    try:
        proc.kill()
    except OSError:
        pass


def _pump(proc: subprocess.Popen, timeout: float,
          on_output=None, cancel=None) -> tuple[str, bool, bool]:
    """边执行边逐行交出输出（进度窗实时显示），返回 (输出, 是否超时, 是否被取消)。

    子进程输出通过管道读取；Windows 上无法使用 select 等待，因此由独立
    读线程写入队列，主循环按超时与取消标志轮询。``on_output`` 在本线程
    回调，调用方自行切换回 UI 线程。
    """
    q: queue.Queue = queue.Queue()

    def reader():
        try:
            for line in iter(proc.stdout.readline, ""):
                q.put(line)
        except Exception:  # noqa: BLE001
            pass
        finally:
            q.put(None)

    threading.Thread(target=reader, daemon=True).start()
    deadline = time.time() + timeout
    buf: list[str] = []
    timed_out = cancelled = False
    while True:
        if cancel is not None and cancel.is_set():
            cancelled = True
            _kill(proc)
            break
        left = deadline - time.time()
        if left <= 0:
            timed_out = True
            _kill(proc)
            break
        try:
            line = q.get(timeout=min(0.25, left))
        except queue.Empty:
            continue
        if line is None:
            break
        buf.append(line)
        if on_output:
            try:
                on_output(line.rstrip("\r\n"))
            except Exception:  # noqa: BLE001
                pass
    try:
        proc.wait(timeout=15)
    except (OSError, subprocess.SubprocessError):
        pass
    while True:          # 读取管道中剩余的最后几行
        try:
            line = q.get_nowait()
        except queue.Empty:
            break
        if line is None:
            break
        buf.append(line)
        if on_output:
            try:
                on_output(line.rstrip("\r\n"))
            except Exception:  # noqa: BLE001
                pass
    return "".join(buf), timed_out, cancelled


def _missing_modules(output: str) -> list[str]:
    """从报错文本中提取缺失的顶层模块名。"""
    names = []
    for m in re.finditer(r"No module named '([A-Za-z0-9_.]+)'", output):
        top = m.group(1).split(".")[0].lower()
        if top and top not in names:
            names.append(top)
    return names


class Interpreter:
    """解释器定位与环境画像。

    集中管理解释器查找顺序、版本探测与已装包列表，探测结果在实例内
    缓存；安装新包后调用 :meth:`refresh` 重新探测。
    """

    def __init__(self):
        self._py_cache: str | None = None
        self._info_cache: dict | None = None
        self._env_cache: str | None = None

    # ------------------------------------------------------------ 路径解析
    def app_dir(self) -> Path:
        """应用所在目录：打包后为 exe 旁，开发时为仓库根目录。"""
        return Path(APP_DIR)

    def bundled_python(self) -> Path:
        """随客户端分发的便携解释器（由 setup_python.py 下载组装）。

        查找顺序最高：保证任意 Windows 主机使用同一份 Python，行为一致，
        且不受目标机器安装版本差异影响。
        """
        name = "python.exe" if os.name == "nt" else "python3"
        return self.app_dir() / "bin" / "python" / name

    def _probe(self, path: str) -> bool:
        """确认指定路径为可用的 Python 3 解释器。"""
        if not path:
            return False
        try:
            r = subprocess.run([path, "-c", "import sys;print(sys.version_info[0])"],
                               capture_output=True, text=True, timeout=15,
                               creationflags=_no_window())
        except (OSError, subprocess.SubprocessError):
            return False
        return r.returncode == 0 and r.stdout.strip().startswith("3")

    def find(self) -> str:
        """定位可用的解释器路径；未找到时返回空串，由调用方给出明确提示。

        顺序：随包 bin/python → 环境变量 DOUBAO_PYTHON → 当前解释器
        （仅开发态）→ PATH 中的 python / python3 / py 启动器 → 常见安装目录。
        """
        if self._py_cache is not None:
            return self._py_cache

        cands: list[str] = []
        bundled = self.bundled_python()
        if bundled.is_file():
            cands.append(str(bundled))

        env = os.environ.get("DOUBAO_PYTHON", "").strip()
        if env:
            cands.append(env)

        if not FROZEN:
            cands.append(sys.executable)

        for name in ("python", "python3"):
            p = shutil.which(name)
            if p:
                cands.append(p)

        if os.name == "nt" and shutil.which("py"):
            try:
                r = subprocess.run(["py", "-3", "-c", "import sys;print(sys.executable)"],
                                   capture_output=True, text=True, timeout=15,
                                   creationflags=_no_window())
                if r.returncode == 0 and r.stdout.strip():
                    cands.append(r.stdout.strip())
            except (OSError, subprocess.SubprocessError):
                pass

        home = Path.home()
        for base in (home / "AppData" / "Local" / "Programs" / "Python",
                     Path("C:/Program Files"), Path("C:/Program Files (x86)"),
                     Path("C:/"), home / "AppData" / "Local" / "Programs"):
            try:
                if not base.is_dir():
                    continue
                for sub in base.glob("Python3*"):
                    exe = sub / "python.exe"
                    if exe.is_file():
                        cands.append(str(exe))
            except OSError:
                pass

        seen: set[str] = set()
        for path in cands:
            key = os.path.normcase(os.path.abspath(path)) if path else ""
            if not key or key in seen:
                continue
            seen.add(key)
            if self._probe(path):
                self._py_cache = path
                return path

        self._py_cache = ""
        return ""

    # ------------------------------------------------------------ 探测信息
    _ENV_PROBE = """\
import sys
seen = {}
try:
    import importlib.metadata as md
    for d in md.distributions():
        try:
            n = d.metadata["Name"]
        except Exception:
            continue
        if n:
            seen[n] = d.version
except Exception:
    pass
print("VER " + sys.version.split()[0])
print("PKGS " + "|".join(sorted(seen)))
"""

    def info(self) -> dict:
        """解释器画像：路径、版本、是否随包分发、site-packages 是否可写。

        界面据此显示「Agent 使用哪个 Python」，提示词据此描述环境，
        安装逻辑根据 ``writable`` 决定是否追加 ``--user`` 参数。
        """
        if self._info_cache is not None:
            return self._info_cache

        python = self.find()
        result = {"path": python, "version": "", "bundled": False, "writable": False}
        if not python:
            self._info_cache = result
            return result

        try:
            result["bundled"] = (os.path.normcase(os.path.abspath(python))
                                == os.path.normcase(str(self.bundled_python().resolve())))
        except (OSError, RuntimeError):
            pass

        try:
            r = subprocess.run(
                [python, "-X", "utf8", "-c",
                 "import sys;print(sys.version.split()[0])"],
                capture_output=True, text=True, encoding="utf-8", errors="replace",
                timeout=30, creationflags=_no_window())
            if r.returncode == 0:
                result["version"] = (r.stdout or "").strip()
        except (OSError, subprocess.SubprocessError):
            pass

        # site-packages 可写时直接安装；只读目录（Program Files 等）改用 --user
        site = os.path.join(os.path.dirname(python), "Lib", "site-packages")
        result["writable"] = (os.access(site, os.W_OK) if os.path.isdir(site)
                             else os.access(os.path.dirname(python), os.W_OK))

        self._info_cache = result
        return result

    def environment_report(self, max_pkgs: int = 28) -> str:
        """生成环境画像：解释器版本、路径与已安装的第三方包列表。

        该画像随每一步提示词发送给模型，使其明确哪些包可以直接 import、
        哪些需要安装，减少「执行-报错-安装-重试」的往返次数。结果缓存于
        实例内，仅探测一次。
        """
        if self._env_cache is not None:
            return self._env_cache
        python = self.find()
        if not python:
            self._env_cache = ("本机未找到可用的 Python 解释器，run_code 不可用；"
                              "请确认 bin\\python 目录随程序一起分发。")
            return self._env_cache
        ver, pkgs = "", []
        try:
            r = subprocess.run([python, "-X", "utf8", "-c", self._ENV_PROBE],
                               capture_output=True, text=True, encoding="utf-8",
                               errors="replace", timeout=30,
                               creationflags=_no_window())
            for line in (r.stdout or "").splitlines():
                if line.startswith("VER "):
                    ver = line[4:].strip()
                elif line.startswith("PKGS "):
                    pkgs = [p for p in line[5:].split("|") if p]
        except (OSError, subprocess.SubprocessError):
            pass

        info = self.info()
        tag = "随包便携版" if info.get("bundled") else "本机已安装"
        stdlib_only = not pkgs or all(
            p.lower().startswith(("pip", "setuptools", "wheel")) for p in pkgs)
        bits = [f"可用的 Python 为 {ver or info.get('version') or '3.x'}"
                f"（{tag}，{python}）"]
        if info.get("bundled") and not info.get("writable", True):
            bits.append("程序目录只读，pip 安装的包会写入当前用户的 "
                       "site-packages，同样可以 import")
        if stdlib_only:
            bits.append("当前未安装第三方包，可由代码自动 pip 安装")
        else:
            shown = "、".join(pkgs[:max_pkgs])
            more = (f" 等 {len(pkgs)} 个" if len(pkgs) > max_pkgs
                    else f"（共 {len(pkgs)} 个）")
            bits.append(f"已安装第三方包：{shown}{more}")
        self._env_cache = "；".join(bits) + "。"
        return self._env_cache

    def refresh(self) -> None:
        """清除缓存（安装包之后调用，下次查询将重新探测）。"""
        self._py_cache = None
        self._info_cache = None
        self._env_cache = None

    # ------------------------------------------------------------ pip 安装
    def pip_install(self, pkgs: list[str], timeout: int,
                    on_output=None, cancel=None) -> tuple[bool, str]:
        """将包安装至当前解释器。缺多个包时合入一条命令批量安装。

        镜像源顺序：全局配置优先，官方 PyPI 兜底——官方源在部分网络下
        可能长时间无响应，优先使用镜像可显著提高成功率。目录只读时追加
        ``--user``。必须安装到当前解释器自身的 site-packages，后续代码
        才能直接 import，无需重复安装。
        """
        if isinstance(pkgs, str):
            pkgs = [pkgs]
        per_try = max(INSTALL_PER_TRY, int(timeout or 0))
        mirror = pip_mirror()
        indexes = [mirror, ""] if mirror else [""]
        writable = self.info().get("writable", True)
        extras = ([], ["--user"]) if writable else (["--user"],)
        attempts = [(idx, ex) for ex in extras for idx in indexes]
        last = ""
        for index, extra in attempts:
            cmd = [self.find(), "-m", "pip", "install",
                   "--disable-pip-version-check",
                   "--no-input", "--no-warn-script-location"]
            if index:
                cmd += ["-i", index]
            cmd += extra + list(pkgs)
            try:
                proc = subprocess.Popen(
                    cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                    text=True, encoding="utf-8", errors="replace",
                    creationflags=_no_window())
            except OSError as exc:
                last = str(exc)
                continue
            out, _timed_out, cancelled = _pump(proc, per_try, on_output, cancel)
            if cancelled:
                return False, "已取消"
            last = out
            if proc.returncode == 0:
                return True, ""
            # 版本冲突提示或重复安装（已满足依赖）视为成功
            if "Requirement already satisfied" in last and "ERROR" not in last:
                return True, ""
        return False, last

    def pip_uninstall(self, pkgs: list[str], timeout: int = 300,
                      on_output=None, cancel=None) -> tuple[bool, str]:
        """从当前解释器卸载指定包（自动确认，不交互）。"""
        if isinstance(pkgs, str):
            pkgs = [pkgs]
        python = self.find()
        if not python:
            return False, "未找到 Python 解释器"
        cmd = [python, "-m", "pip", "uninstall", "-y",
               "--disable-pip-version-check"] + list(pkgs)
        try:
            proc = subprocess.Popen(
                cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, encoding="utf-8", errors="replace",
                creationflags=_no_window())
        except OSError as exc:
            return False, str(exc)
        out, _timed_out, cancelled = _pump(proc, timeout, on_output, cancel)
        if cancelled:
            return False, "已取消"
        return proc.returncode == 0, out

    def pip_list(self, timeout: int = 120) -> list[dict]:
        """以 ``pip list --format=json`` 返回已安装包列表。

        返回 ``[{"name": ..., "version": ...}, ...]``；解析失败时返回
        空列表，由调用方决定是否提示。
        """
        python = self.find()
        if not python:
            return []
        import json as _json
        try:
            r = subprocess.run(
                [python, "-m", "pip", "list", "--format=json",
                 "--disable-pip-version-check"],
                capture_output=True, text=True, encoding="utf-8",
                errors="replace", timeout=timeout, creationflags=_no_window())
        except (OSError, subprocess.SubprocessError):
            return []
        if r.returncode != 0:
            return []
        try:
            data = _json.loads(r.stdout or "[]")
        except ValueError:
            return []
        if not isinstance(data, list):
            return []
        out = []
        for item in data:
            if isinstance(item, dict) and item.get("name"):
                out.append({"name": str(item["name"]),
                            "version": str(item.get("version") or "")})
        return out


class CodeRunner:
    """脚本执行器：写入脚本、运行、缺包自动安装重试、输出落盘。"""

    def __init__(self, interpreter: Interpreter | None = None):
        self.interp = interpreter or Interpreter()

    # ------------------------------------------------------------ 代码提取
    @staticmethod
    def snippets(text: str) -> list[Snippet]:
        """按出现顺序提取消息中所有带 Python 语言标记的代码块。"""
        if not text or "```" not in text:
            return []
        out: list[Snippet] = []
        for block in md_render.parse(text):
            if block.kind != "code":
                continue
            lang = (block.lang or "").split()[0].lower() if block.lang.strip() else ""
            if lang not in PY_LANGS:
                continue
            code = "\n".join(block.lines).strip("\n")
            if code.strip():
                out.append(Snippet(code=code, lang=lang, index=len(out) + 1))
        return out

    # ------------------------------------------------------------ 单次执行
    def _exec_once(self, script: Path, workdir: Path,
                   timeout: int, on_output=None,
                   cancel=None) -> tuple[int | None, str, bool, bool]:
        """执行一次脚本，返回 (退出码, 合并输出, 是否超时, 是否被取消)。"""
        python = self.interp.find()
        if not python:
            return None, "", False, False
        env = os.environ.copy()
        env["PYTHONIOENCODING"] = "utf-8"
        env["PYTHONUTF8"] = "1"          # 中文输出不依赖系统代码页
        env.pop("PYTHONPATH", None)      # 不向脚本注入客户端的模块目录
        try:
            proc = subprocess.Popen(
                [python, "-X", "utf8", "-u", str(script)],
                cwd=str(workdir),
                stdin=subprocess.DEVNULL,     # 脚本中的 input() 立即 EOF，不会阻塞
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,     # 合并输出，错误按发生顺序可见
                text=True, encoding="utf-8", errors="replace",
                env=env, creationflags=_no_window(),
            )
        except OSError:
            return None, "", False, False
        out, timed_out, cancelled = _pump(proc, timeout, on_output, cancel)
        return proc.returncode, out, timed_out, cancelled

    # ------------------------------------------------------------ 主入口
    def run(self, code: str, workdir: str | Path,
            timeout: int = DEFAULT_TIMEOUT,
            allow_install: bool = True,
            on_progress=None,
            on_output=None,
            cancel: threading.Event | None = None) -> RunResult:
        """将代码写入脚本文件并在 ``workdir`` 中执行；缺包时自动安装并重试。

        ``on_progress(str)`` 在子线程中被调用（安装、重试阶段）；
        ``on_output(str)`` 逐行交出脚本与 pip 的输出，调用方自行通过
        ``wx.CallAfter`` 切回 UI 线程；``cancel`` 置位后尽快终止当前子进程。
        """
        res = RunResult(workdir=str(workdir))
        python = self.interp.find()
        if not python:
            res.error = (f"未找到 Python 解释器。随包分发的 "
                        f"{self.interp.bundled_python()} 应与程序一起发行；"
                        "请确认 bin\\python 目录存在，或安装 Python 3 并加入 "
                        "PATH，或通过环境变量 DOUBAO_PYTHON 指定 python.exe "
                        "的完整路径。")
            return res
        res.python = python

        wd = Path(workdir)
        try:
            wd.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            res.error = f"无法创建工作目录 {wd}：{exc}"
            return res

        script = wd / f"task_{time.strftime('%Y%m%d_%H%M%S')}.py"
        try:
            script.write_text(code if code.endswith("\n") else code + "\n",
                              encoding="utf-8", newline="\n")
        except OSError as exc:
            res.error = f"无法写入脚本文件 {script}：{exc}"
            return res
        res.script = str(script)

        started = time.perf_counter()
        for round_no in range(MAX_INSTALL_ROUNDS + 1):
            if cancel is not None and cancel.is_set():
                res.cancelled = True
                res.output = "（暂无输出）"
                break
            code_exit, out, timed_out, cancelled = self._exec_once(
                script, wd, timeout, on_output, cancel)
            if cancelled:
                res.exit_code, res.output, res.cancelled = code_exit, out, True
                break
            if timed_out or code_exit == 0:
                res.exit_code, res.output, res.timed_out = code_exit, out, timed_out
                break

            mods = _missing_modules(out) if allow_install else []
            if not mods or round_no >= MAX_INSTALL_ROUNDS:
                res.exit_code, res.output = code_exit, out
                break

            pkgs: list[str] = []
            for m in mods:                      # 缺多个包时合并为一次安装
                pkg = _PKG_ALIAS.get(m, m)
                if pkg not in pkgs:
                    pkgs.append(pkg)
            if on_progress:
                on_progress(f"缺少 {'、'.join(pkgs)}，正在下载安装…"
                            f"（源：{pip_mirror() or '官方 PyPI'}）")
            ok, err = self.interp.pip_install(pkgs, timeout, on_output, cancel)
            if not ok:
                res.exit_code, res.output = code_exit, out
                if err == "已取消":
                    res.cancelled = True
                    break
                res.output += (f"\n\n[自动安装 {'、'.join(pkgs)} 失败]\n{err[-1500:]}"
                               "\n可通过「设置」菜单更换 pip 镜像源后重试")
                break
            res.installed.extend(p for p in pkgs if p not in res.installed)
            if on_progress:
                on_progress(f"{'、'.join(pkgs)} 安装完成，重新执行…")

        res.duration = time.perf_counter() - started
        # 输出过长时先保存完整日志：后续观察仅携带摘要，模型可按需读取，
        # 比直接截断丢失关键信息更可靠。
        if len(res.output) > LOG_KEEP:
            log = script.with_suffix(".log")
            try:
                log.write_text(res.output, encoding="utf-8", newline="\n")
                res.log_path = str(log)
            except OSError:
                pass
        res.output = _truncate(res.output)
        if res.installed:
            self.interp.refresh()      # 已安装新包，环境画像需要重新生成
        return res


# --------------------------------------------------------------- 默认实例
# 模块级共享实例：主窗口、AI Agent、测试脚本通过下方兼容函数访问，
# 与原脚本版本的调用方式保持一致。
interpreter = Interpreter()
code_runner = CodeRunner(interpreter)


def find_python() -> str:
    return interpreter.find()


def python_info() -> dict:
    return interpreter.info()


def environment_report(max_pkgs: int = 28) -> str:
    return interpreter.environment_report(max_pkgs)


def refresh_environment_cache() -> None:
    interpreter.refresh()


def bundled_python() -> Path:
    return interpreter.bundled_python()


def snippets(text: str) -> list[Snippet]:
    return CodeRunner.snippets(text)


def run(code: str, workdir: str | Path, *args, **kwargs) -> RunResult:
    return code_runner.run(code, workdir, *args, **kwargs)
