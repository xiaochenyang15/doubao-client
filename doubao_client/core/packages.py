"""Python 包管理服务：列出、安装与卸载随包解释器中的第三方包。

设计参考 polyglot 插件的 modelManager 分层：数据目录（catalog）、安装器
（installer）、服务层（service）与界面层分离；本模块对应其中的服务层，
负责与 pip 子进程交互并向上层提供线程友好的阻塞式 API（界面层负责把
耗时操作放入后台线程并转发进度）。

所有操作均作用于 ``Interpreter`` 定位到的解释器——即 AI Agent 的
run_code 所使用的同一环境，因此在这里安装的包可以直接被 Agent 代码
import，无需重复安装。
"""
from __future__ import annotations

import threading
from dataclasses import dataclass

from .runner import Interpreter


@dataclass
class PackageInfo:
    """一个已安装包的元数据。"""

    name: str
    version: str = ""


class PackageError(Exception):
    """包操作失败（解释器缺失、pip 返回非零等）。"""


class PackageManager:
    """pip 包管理服务。

    用法（在后台线程中调用，进度经 ``on_output`` 回调转回 UI 线程）::

        mgr = PackageManager()
        packages = mgr.list_installed()
        ok, message = mgr.install(["requests"], on_output=print)
    """

    def __init__(self, interpreter: Interpreter | None = None):
        self.interp = interpreter or Interpreter()
        self._list_lock = threading.Lock()   # 列表操作互斥，防止并发 pip 交叉

    # ------------------------------------------------------------ 列表
    def list_installed(self, timeout: int = 120) -> list[PackageInfo]:
        """返回已安装包列表，按名称排序；解释器缺失时抛出 PackageError。"""
        with self._list_lock:
            if not self.interp.find():
                raise PackageError(
                    "未找到 Python 解释器，无法读取包列表。"
                    "请确认 bin\\python 目录随程序一起分发。")
            rows = self.interp.pip_list(timeout=timeout)
        return [PackageInfo(r["name"], r.get("version", ""))
                for r in sorted(rows, key=lambda r: r["name"].lower())]

    # ------------------------------------------------------------ 安装
    def install(self, names: list[str], timeout: int = 600,
                on_output=None, cancel: threading.Event | None = None
                ) -> tuple[bool, str]:
        """安装一个或多个包。成功后刷新环境画像，供下一次提示词使用。"""
        cleaned = self._normalize(names)
        if not cleaned:
            return False, "请填写至少一个包名"
        if not self.interp.find():
            raise PackageError("未找到 Python 解释器，无法安装包。")
        ok, detail = self.interp.pip_install(cleaned, timeout,
                                             on_output=on_output, cancel=cancel)
        if ok:
            self.interp.refresh()
        return ok, detail

    # ------------------------------------------------------------ 卸载
    def uninstall(self, names: list[str], timeout: int = 300,
                  on_output=None, cancel: threading.Event | None = None
                  ) -> tuple[bool, str]:
        """卸载一个或多个包。成功后刷新环境画像。"""
        cleaned = self._normalize(names)
        if not cleaned:
            return False, "请选择至少一个包"
        if not self.interp.find():
            raise PackageError("未找到 Python 解释器，无法卸载包。")
        ok, detail = self.interp.pip_uninstall(cleaned, timeout,
                                                on_output=on_output,
                                                cancel=cancel)
        if ok:
            self.interp.refresh()
        return ok, detail

    # ------------------------------------------------------------ 升级
    def upgrade(self, names: list[str], timeout: int = 600,
                on_output=None, cancel: threading.Event | None = None
                ) -> tuple[bool, str]:
        """升级一个或多个包（``pip install --upgrade``）。"""
        cleaned = self._normalize(names)
        if not cleaned:
            return False, "请选择至少一个包"
        python = self.interp.find()
        if not python:
            raise PackageError("未找到 Python 解释器，无法升级包。")
        from .runner import _pump, _no_window, pip_mirror  # noqa: PLC2701
        import subprocess
        mirror = pip_mirror()
        attempts = ([mirror, ""] if mirror else [""])
        last = ""
        for index in attempts:
            cmd = [python, "-m", "pip", "install", "--upgrade",
                   "--disable-pip-version-check", "--no-input",
                   "--no-warn-script-location"]
            if index:
                cmd += ["-i", index]
            cmd += cleaned
            try:
                proc = subprocess.Popen(
                    cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                    text=True, encoding="utf-8", errors="replace",
                    creationflags=_no_window())
            except OSError as exc:
                last = str(exc)
                continue
            out, _t, cancelled = _pump(proc, timeout, on_output, cancel)
            if cancelled:
                return False, "已取消"
            last = out
            if proc.returncode == 0:
                self.interp.refresh()
                return True, ""
        return False, last

    # ------------------------------------------------------------ 内部
    @staticmethod
    def _normalize(names) -> list[str]:
        """去除空白与重复，保持顺序；支持用空格分隔的复合输入。"""
        out: list[str] = []
        for raw in names or []:
            for part in str(raw).replace(",", " ").split():
                part = part.strip()
                if part and part.lower() not in (p.lower() for p in out):
                    out.append(part)
        return out
