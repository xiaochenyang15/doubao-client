# 衍生自 NVDA 插件 Golden Cursor 26.1（<https://github.com/nvda-es/goldenCursor>）：
# Copyright (C) 2015-2022 Salah Atair, Wafeeq Taher, Joseph Lee,
# Abdel (Abdelkrim Bensaid) 及 NVDA 西语社区等贡献者。
# Golden Cursor 以 GNU GPL v2 授权；本文件作为其衍生作品同样以
# GNU GPL v2 发布（见项目根 LICENSE / THIRD-PARTY-NOTICES.md）。
"""落点命中测试：不截图也能知道鼠标底下是什么。

对应 goldenCursor 的 ``api.getDesktopObject().objectFromPoint(x, y)``。屏幕阅读器
用户靠它确认「这个坐标上是哪个控件」；Agent 靠它做两件事：

1. **点击前**把落点吸到真实控件的正中——这是当下直接问系统，不像上一步的观察
   缓存那样可能已经过期；
2. **点击后**把「命中了什么」写进历史，模型才知道自己到底点到了哪儿，而不是
   只能等下一张截图猜。

优先用 UI Automation（能拿到控件名、类型、是否可用）；UIA 不可用时退回纯 Win32
（只拿得到顶层窗口和 exe），再不行返回 ``None``，调用方按原坐标点，行为不变。
"""
from __future__ import annotations

import ctypes
import logging
import os
import threading
from dataclasses import dataclass

import win32api
import win32con
import win32gui
import win32process

logger = logging.getLogger("agent")

GA_ROOT = 2

# UIA 里算「可交互」的控件类型；与 Win32 的窗口类名表分开维护，
# 但两边都只在这里定义一份，observe 直接引用，避免各写一套。
INTERACTIVE_TYPES = frozenset({
    "Button", "Edit", "ComboBox", "ListItem", "MenuItem", "CheckBox",
    "RadioButton", "Link", "TabItem", "Slider", "TreeItem", "SplitButton",
    "Hyperlink", "Spinner", "Document",
})

INTERACTIVE_CLASSES = frozenset({
    "Button", "Edit", "ComboBox", "ComboBoxEx32", "ListBox",
    "SysListView32", "SysTreeView32", "SysTabControl32", "Link",
    "RichEdit20W", "RICHEDIT50W", "msctls_trackbar32", "msctls_progress32",
})


def normalize_kind(kind: str) -> str:
    """把 UIA 的控件类型名统一成不带后缀的形式。

    ``uiautomation`` 的 ``ControlTypeName`` 实际返回的是 ``ButtonControl`` /
    ``TextControl`` / ``PaneControl`` 这种带 ``Control`` 后缀的写法，而 Win32 那边
    是 ``Button`` / ``Edit``。不统一的话 ``INTERACTIVE_TYPES`` 一个都对不上——
    原先 observe.py 里就是这么比的，等于「可交互」永远判 False，落点吸附也因此
    一直没用上这个优先级。顺手也让提示词里的 ``[ButtonControl]`` 变短好读。
    """
    k = (kind or "").strip()
    if len(k) > 7 and k.endswith("Control"):
        return k[:-7]
    return k


# --------------------------------------------------------------------------- #
# 屏幕范围
# --------------------------------------------------------------------------- #
def virtual_screen() -> tuple[int, int, int, int]:
    """整块虚拟屏的范围，返回 (left, top, w, h)。

    全项目只在这里算一次——actions.screen_bounds() 也转调它。
    """
    user = ctypes.windll.user32
    left = user.GetSystemMetrics(76)    # SM_XVIRTUALSCREEN
    top = user.GetSystemMetrics(77)     # SM_YVIRTUALSCREEN
    width = user.GetSystemMetrics(78)   # SM_CXVIRTUALSCREEN
    height = user.GetSystemMetrics(79)  # SM_CYVIRTUALSCREEN
    if width <= 0 or height <= 0:
        left, top = 0, 0
        width = user.GetSystemMetrics(0)   # SM_CXSCREEN
        height = user.GetSystemMetrics(1)  # SM_CYSCREEN
    return left, top, width, height


def inside(x: int, y: int) -> bool:
    left, top, w, h = virtual_screen()
    return left <= x < left + w and top <= y < top + h


def clamp(x: int, y: int) -> tuple[int, int]:
    """把坐标钳进屏幕内。

    来自 goldenCursor ``moveMouse`` 的边界判定（``0 <= x < w and 0 <= y < h``）：
    它越界时不移动、只响一声提醒。Agent 这边越界通常意味着坐标算错了，钳住至少
    不会把鼠标丢到不存在的位置上（多屏时那块区域可能是空的）。
    """
    left, top, w, h = virtual_screen()
    if w <= 0 or h <= 0:
        return int(x), int(y)
    cx = min(max(int(x), left), left + w - 1)
    cy = min(max(int(y), top), top + h - 1)
    return cx, cy


# --------------------------------------------------------------------------- #
# 进程名（缓存，避免每次点击都开进程查路径）
# --------------------------------------------------------------------------- #
_exe_cache: dict[int, str] = {}
_cache_lock = threading.RLock()


def exe_for_pid(pid: int) -> str:
    """pid → 可执行文件名（小写），查不到返回空串。"""
    if not pid:
        return ""
    with _cache_lock:
        if pid in _exe_cache:
            return _exe_cache[pid]
    name = ""
    handle = None
    try:
        handle = win32api.OpenProcess(
            win32con.PROCESS_QUERY_INFORMATION | win32con.PROCESS_VM_READ,
            False, pid)
        path = win32process.GetModuleFileNameEx(handle, 0) or ""
        name = os.path.basename(path).lower()
    except Exception:  # noqa: BLE001  权限不够（比如对方是管理员进程）
        name = ""
    finally:
        if handle:
            try:
                win32api.CloseHandle(handle)
            except Exception:  # noqa: BLE001
                pass
    with _cache_lock:
        if len(_exe_cache) > 200:      # pid 会被回收复用，攒太多就整体作废
            _exe_cache.clear()
        _exe_cache[pid] = name
    return name


def process_name(hwnd: int) -> str:
    """窗口句柄 → 所属进程的可执行文件名（小写），失败返回空串。"""
    if not hwnd:
        return ""
    try:
        _, pid = win32process.GetWindowThreadProcessId(hwnd)
    except Exception:  # noqa: BLE001
        return ""
    return exe_for_pid(pid)


def pid_of(hwnd: int) -> int:
    try:
        return int(win32process.GetWindowThreadProcessId(hwnd)[1] or 0)
    except Exception:  # noqa: BLE001
        return 0


def window_at(x: int, y: int) -> int:
    """点上的顶层窗口句柄（0 = 没有）。"""
    try:
        child = win32gui.WindowFromPoint((int(x), int(y)))
    except Exception:  # noqa: BLE001
        return 0
    if not child:
        return 0
    try:
        return win32gui.GetAncestor(child, GA_ROOT) or child
    except Exception:  # noqa: BLE001
        return child


# --------------------------------------------------------------------------- #
# 命中结果
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class PointHit:
    """一个落点上命中的东西。矩形是**整屏像素**。"""
    x: int
    y: int
    left: int = 0
    top: int = 0
    width: int = 0
    height: int = 0
    name: str = ""
    kind: str = ""
    exe: str = ""
    pid: int = 0
    enabled: bool = True
    via: str = "uia"

    @property
    def valid(self) -> bool:
        return self.width > 2 and self.height > 2

    @property
    def area(self) -> int:
        return max(0, self.width) * max(0, self.height)

    @property
    def center(self) -> tuple[int, int]:
        return (self.left + self.width // 2, self.top + self.height // 2)

    @property
    def interactive(self) -> bool:
        return self.kind in INTERACTIVE_TYPES

    @property
    def specific(self) -> bool:
        """够小、够具体，值得把落点挪过去。

        整块屏幕那么大的一块（桌面根节点、浏览器文档区）不能当靶心——那会把点击
        挪到屏幕正中，比模型自己给的坐标还糟。
        """
        if not self.valid:
            return False
        _, _, sw, sh = virtual_screen()
        return self.width < sw * 0.9 and self.area <= sw * sh * 0.45

    def describe(self) -> str:
        bits = []
        if self.kind:
            bits.append(self.kind)
        if self.name:
            bits.append(f"「{self.name}」")
        who = self.exe or "未知程序"
        body = " ".join(bits) or "未命名元素"
        extra = "" if self.enabled else "（不可用/置灰）"
        return f"{body}{extra} — {who}"


# --------------------------------------------------------------------------- #
# UIA 命中
# --------------------------------------------------------------------------- #
_uia_mod = None
_tls = threading.local()


def uia():
    """拿到 uiautomation 模块，并**为当前线程**初始化一次 UIA。

    这一步绝不能省：COM 是按线程初始化的。模块在 A 线程导入、却在 B 线程直接调
    ``ControlFromPoint``，调用会**长时间不返回**（实测 25 秒仍阻塞，且不抛异常）。
    AgentSession 就跑在工作线程里，所以每个线程第一次用都得自己初始化一次；
    初始化失败则返回 None，调用方退回纯 Win32 实现；信息量可以减少，但不能阻塞调用方。

    没装 uiautomation 时每次调用都会重试 import——装好之后自然就能用上，
    不需要重启进程（observe 那边会在缺包时静默 pip install）。
    """
    global _uia_mod
    auto = _uia_mod
    if auto is None:
        try:
            import uiautomation as auto_mod
        except Exception:  # noqa: BLE001
            return None
        auto = _uia_mod = auto_mod
    ready = getattr(_tls, "ready", None)
    if ready is None:
        try:
            auto.InitializeUIAutomationInCurrentThread()
            ready = True
        except Exception:  # noqa: BLE001
            logger.debug("UIA 线程初始化失败，本线程退回 Win32", exc_info=True)
            ready = False
        _tls.ready = ready
    return auto if ready else None


def _uia_hit(x: int, y: int) -> PointHit | None:
    auto = uia()
    if auto is None:
        return None
    try:
        ctrl = auto.ControlFromPoint(x, y)
    except Exception:  # noqa: BLE001  UIA 在个别进程上会抛 COM 错误
        return None
    if ctrl is None:
        return None
    try:
        r = ctrl.BoundingRectangle
        left, top, right, bottom = int(r.left), int(r.top), int(r.right), int(r.bottom)
    except Exception:  # noqa: BLE001
        return None
    if right - left <= 2 or bottom - top <= 2:
        return None
    try:
        name = (ctrl.Name or "").strip()
    except Exception:  # noqa: BLE001
        name = ""
    try:
        kind = normalize_kind(ctrl.ControlTypeName or "")
    except Exception:  # noqa: BLE001
        kind = ""
    try:
        pid = int(ctrl.ProcessId or 0)
    except Exception:  # noqa: BLE001
        pid = 0
    try:
        enabled = bool(ctrl.IsEnabled)
    except Exception:  # noqa: BLE001
        enabled = True
    return PointHit(x=x, y=y, left=left, top=top,
                    width=right - left, height=bottom - top,
                    name=name, kind=kind, exe=exe_for_pid(pid), pid=pid,
                    enabled=enabled, via="uia")


def _win32_hit(x: int, y: int) -> PointHit | None:
    hwnd = window_at(x, y)
    if not hwnd:
        return None
    try:
        left, top, right, bottom = win32gui.GetWindowRect(hwnd)
    except Exception:  # noqa: BLE001
        return None
    if right - left <= 2 or bottom - top <= 2:
        return None
    try:
        title = win32gui.GetWindowText(hwnd) or ""
    except Exception:  # noqa: BLE001
        title = ""
    try:
        cls = win32gui.GetClassName(hwnd) or ""
    except Exception:  # noqa: BLE001
        cls = ""
    pid = pid_of(hwnd)
    return PointHit(x=x, y=y, left=left, top=top,
                    width=right - left, height=bottom - top,
                    name=title, kind=cls, exe=exe_for_pid(pid), pid=pid,
                    enabled=True, via="win32")


def element_at(x: int, y: int) -> PointHit | None:
    """坐标上命中的元素；屏幕外或什么都读不到时返回 None。"""
    x, y = int(x), int(y)
    if not inside(x, y):
        return None
    try:
        hit = _uia_hit(x, y)
    except Exception:  # noqa: BLE001
        hit = None
    if hit is not None and hit.valid:
        return hit
    try:
        return _win32_hit(x, y)
    except Exception:  # noqa: BLE001
        return None


def available() -> bool:
    """UIA 是否可用（只影响信息详细程度，不影响能否用）。"""
    return uia() is not None
