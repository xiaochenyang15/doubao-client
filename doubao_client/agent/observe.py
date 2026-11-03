from __future__ import annotations

import logging
import math
import subprocess
from dataclasses import dataclass, field

import win32clipboard
import win32con
import win32gui

from . import hittest
from .actions import screen_to_norm
from .hittest import (INTERACTIVE_CLASSES, INTERACTIVE_TYPES, normalize_kind)
from .hittest import process_name as _process_name
from .screen import Screenshot, ScreenCaptureError, capture as capture_screen

logger = logging.getLogger("agent")


@dataclass
class Element:
    """一个可定位的界面元素（控件 / 带文字的区域），坐标是**整屏像素**。

    留着精确矩形是为了「点击吸附」：模型看图只能给个大概位置，误差几十像素很常见；
    落到控件矩形里就把它挪到控件正中，一下就准了。
    """
    left: int
    top: int
    width: int
    height: int
    label: str = ""
    kind: str = ""
    interactive: bool = False

    @property
    def right(self) -> int:
        return self.left + self.width

    @property
    def bottom(self) -> int:
        return self.top + self.height

    @property
    def area(self) -> int:
        return max(0, self.width) * max(0, self.height)

    @property
    def center(self) -> tuple[int, int]:
        return (self.left + self.width // 2, self.top + self.height // 2)

    def contains(self, x: int, y: int) -> bool:
        return self.left <= x < self.right and self.top <= y < self.bottom

    def distance_to(self, x: int, y: int) -> float:
        """点到矩形边缘的距离（点在矩形内为 0）。"""
        dx = max(self.left - x, 0, x - self.right)
        dy = max(self.top - y, 0, y - self.bottom)
        return math.hypot(dx, dy)


@dataclass
class Observation:
    window_title: str = ""
    app_name: str = ""
    window_rect: tuple[int, int, int, int] = (0, 0, 0, 0)   # left, top, w, h
    focus_text: str = ""
    focus_class: str = ""
    controls: list[str] = field(default_factory=list)        # 已格式化的控件行
    elements: list[Element] = field(default_factory=list)    # 控件矩形（点击吸附用）
    clipboard: str = ""
    screenshot: Screenshot | None = None                      # 本步附带的整屏截图
    shot_error: str = ""                                      # 截图失败原因
    signature: str = ""                                       # 用于「无变化」判定的指纹

    def summary(self) -> str:
        lines = []
        if self.window_title or self.app_name:
            lines.append(f"前台窗口：{self.window_title or '（无标题）'}（程序：{self.app_name or '未知'}）")
        l, t, w, h = self.window_rect
        if w and h:
            nx0, ny0 = screen_to_norm(l, t)
            nx1, ny1 = screen_to_norm(l + w, t + h)
            lines.append(f"窗口像素范围：left={l} top={t} w={w} h={h}（归一化 x={nx0}-{nx1}, y={ny0}-{ny1}）")
        if self.screenshot is not None:
            lines.append("画面：" + self.screenshot.context())
        elif self.shot_error:
            lines.append(f"画面：这次没能抓到截图（{self.shot_error}），请只用文字信息判断。")
        if self.focus_text or self.focus_class:
            lines.append(f"焦点控件：{self.focus_class or '?'} 文本={self.focus_text!r}")
        if self.clipboard:
            clip = self.clipboard[:120].replace("\n", " ")
            lines.append(f"剪贴板预览：{clip!r}")
        if self.controls:
            lines.append("可见控件（x/y 是归一化 0-1000 的整屏坐标，可直接当点击坐标用）：")
            lines.extend("  " + c for c in self.controls)
        if not lines:
            lines.append("（没有可读到任何屏幕状态）")
        return "\n".join(lines)


# --------------------------------------------------------------------------- #
# UI Automation（可选）
# --------------------------------------------------------------------------- #
_UIA_TRIED = False


def ensure_uia() -> bool:
    """尽量能用 UIA：有就直接用，没有就静默装一次，返回是否可用。

    真正的「拿模块 + 给当前线程初始化 COM」在 ``hittest.uia()`` 里——读控件和
    落点命中必须共用同一个入口，否则某个线程遗漏初始化会导致阻塞。
    """
    global _UIA_TRIED
    if hittest.uia() is not None:
        return True
    if _UIA_TRIED:
        return False
    _UIA_TRIED = True
    # 按需静默安装（与 runner 的「缺包自动装」思路一致）
    try:
        subprocess.run([_python(), "-m", "pip", "install", "--disable-pip-version-check",
                        "--no-input", "uiautomation"],
                       capture_output=True, text=True, timeout=120,
                       creationflags=_no_window())
    except Exception as exc:  # noqa: BLE001
        logger.warning("uiautomation 不可用，退化为仅前台窗口观察：%s", exc)
        return False
    if hittest.uia() is None:
        logger.warning("uiautomation 装完仍不可用，退化为仅前台窗口观察")
        return False
    return True


def _python() -> str:
    import sys
    return sys.executable


def _no_window() -> int:
    return getattr(subprocess, "CREATE_NO_WINDOW", 0)


def _uia_controls(hwnd: int) -> list[Element]:
    """用 uiautomation 读前台窗口控件树，转成带整屏矩形的元素列表。"""
    if not ensure_uia() or not hwnd:
        return []
    auto = hittest.uia()
    if auto is None:
        return []
    try:
        root = auto.ControlFromHandle(hwnd)
        if root is None:
            return []
    except Exception:
        return []

    out: list[Element] = []
    seen: set[tuple] = set()
    MAX = 80

    def walk(ctrl, depth: int):
        if len(out) >= MAX:
            return
        try:
            cr = ctrl.BoundingRectangle  # (left, top, right, bottom)
            name = (ctrl.Name or "").strip()
            ctype = normalize_kind(ctrl.ControlTypeName or "")
        except Exception:
            return
        w = int(cr.right - cr.left)
        h = int(cr.bottom - cr.top)
        if w <= 2 or h <= 2:
            return
        # 只保留有名字、或本身是输入框/按钮这类可交互控件
        interactive = ctype in INTERACTIVE_TYPES
        if name or interactive:
            key = (ctype, name, int(cr.left), int(cr.top))
            if key in seen:
                return
            seen.add(key)
            out.append(Element(left=int(cr.left), top=int(cr.top), width=w, height=h,
                               label=name, kind=ctype, interactive=interactive))
        if depth > 6:
            return
        try:
            for child in ctrl.GetChildren():
                if len(out) >= MAX:
                    break
                walk(child, depth + 1)
        except Exception:
            pass

    try:
        walk(root, 0)
    except Exception:
        pass
    return out


# --------------------------------------------------------------------------- #
# 纯 Win32 兜底（零依赖，永远可用，带坐标）
# --------------------------------------------------------------------------- #
# UIA 没装 / 没读到控件时，用 Windows 自带的 EnumChildWindows 枚举子窗口并带坐标。
# 这样即使没有任何第三方包，Agent 也能拿到「点哪儿」的坐标，满足「不依赖单独一台
# 电脑」的要求。可交互类名表与命中测试共用 hittest 里那一份，别各写一套。
def _win32_controls(hwnd: int) -> list[Element]:
    """纯 Win32 枚举子窗口，给出带整屏矩形的元素列表（无需任何第三方包）。"""
    if not hwnd:
        return []
    out: list[Element] = []
    seen: set[tuple] = set()

    def enum(child, _lparam):
        if len(out) >= 80:
            return
        try:
            if not win32gui.IsWindowVisible(child):
                return
            left, top, right, bottom = win32gui.GetWindowRect(child)
            if right - left <= 2 or bottom - top <= 2:
                return
            cls = win32gui.GetClassName(child) or ""
            text = win32gui.GetWindowText(child) or ""
        except Exception:
            return
        # 只保留：交互类控件，或带文字的标签（帮模型理解界面）
        interactive = cls in INTERACTIVE_CLASSES
        if not interactive and not text:
            return
        key = (cls, text, left, top)
        if key in seen:
            return
        seen.add(key)
        out.append(Element(left=left, top=top, width=right - left, height=bottom - top,
                           label=text, kind=cls, interactive=interactive))

    try:
        win32gui.EnumChildWindows(hwnd, enum, None)
    except Exception as exc:  # noqa: BLE001
        logger.debug("Win32 控件枚举失败：%s", exc)
    return out


def format_elements(elements: list[Element], limit: int = 60) -> list[str]:
    """把元素列表格式化成提示词里那几行（归一化坐标，和模型输出的尺子一致）。"""
    lines: list[str] = []
    for el in elements[:limit]:
        nx0, ny0 = screen_to_norm(el.left, el.top)
        nx1, ny1 = screen_to_norm(el.right, el.bottom)
        kind = f"[{el.kind}] " if el.kind else ""
        name = f"{el.label!r} " if el.label else ""
        lines.append(f"{kind}{name}@ (x={nx0}-{nx1}, y={ny0}-{ny1})")
    if len(elements) > limit:
        lines.append(f"…（共 {len(elements)} 个，已省略）")
    return lines


def snap_point(x: int, y: int, elements: list[Element],
               screen_w: int, screen_h: int,
               tolerance: int = 20) -> tuple[int, int, str] | None:
    """把整屏像素点 (x, y) 吸附到附近的控件中心。

    这是「点击不准确」的核心修复：视觉模型给的坐标通常偏几十像素，但它多半仍在
    目标控件附近。把落点归到命中的**最小可交互控件**的正中，就能把「点偏了」
    变成「点中了」。返回 (x, y, 说明) 或 None（附近没有可用元素，就按原点打）。
    """
    if not elements or screen_w <= 0 or screen_h <= 0:
        return None
    screen_area = float(screen_w * screen_h)
    usable = [el for el in elements
              if el.width >= 4 and el.height >= 4 and el.area <= screen_area * 0.45]

    hits = [el for el in usable if el.contains(x, y)]
    if hits:
        # 命中：优先可交互的、面积最小的（= 最具体的那个控件）
        hits.sort(key=lambda e: (0 if e.interactive else 1, e.area))
        best: Element | None = hits[0]
    else:
        near = [el for el in usable
                if el.interactive and el.distance_to(x, y) <= tolerance]
        near.sort(key=lambda e: (e.distance_to(x, y), e.area))
        best = near[0] if near else None

    if best is None:
        return None
    cx, cy = best.center
    if (cx, cy) == (x, y):
        return None
    label = f"{best.kind} {best.label}".strip() or "控件"
    return cx, cy, label


# --------------------------------------------------------------------------- #
# 基础窗口信息
# --------------------------------------------------------------------------- #
def _window_title(hwnd: int) -> str:
    try:
        return win32gui.GetWindowText(hwnd)
    except Exception:
        return ""


def _app_name(hwnd: int) -> str:
    """窗口所属程序（exe 名，小写）。

    实现在 hittest 里，命中测试要用同一份判断（落点「是不是目标程序」全靠它），
    两处各算一遍迟早不一致。
    """
    return _process_name(hwnd)


def _focus_control() -> tuple[str, str]:
    try:
        gui = win32gui.GetGUIThreadInfo(0)
        focus = gui.hwndFocus
    except Exception:
        return "", ""
    if not focus:
        return "", ""
    try:
        cls = win32gui.GetClassName(focus)
    except Exception:
        cls = ""
    try:
        text = win32gui.GetWindowText(focus)
    except Exception:
        text = ""
    return cls, text


def _clipboard_preview() -> str:
    try:
        win32clipboard.OpenClipboard(0)
        try:
            if win32clipboard.IsClipboardFormatAvailable(win32con.CF_UNICODETEXT):
                return win32clipboard.GetClipboardData(win32con.CF_UNICODETEXT) or ""
        finally:
            win32clipboard.CloseClipboard()
    except Exception:
        return ""
    return ""


def capture_observation(use_uia: bool = True,
                        with_screenshot: bool = False) -> Observation:
    """采集当前屏幕的可读状态。

    ``with_screenshot=True`` 时额外抓一张整屏截图（给模型"看"），这需要多花
    ~200ms；抓图失败不会中断流程，只在观察里标注原因，Agent 仍可用文字推进。
    """
    hwnd = 0
    try:
        hwnd = win32gui.GetForegroundWindow()
    except Exception:
        hwnd = 0
    obs = Observation()
    try:
        obs.window_title = _window_title(hwnd)
        obs.app_name = _app_name(hwnd)
    except Exception:
        pass
    if hwnd:
        try:
            left, top, right, bottom = win32gui.GetWindowRect(hwnd)
            obs.window_rect = (left, top, right - left, bottom - top)
        except Exception:
            pass
        elements: list[Element] = []
        if use_uia:
            try:
                elements = _uia_controls(hwnd)
            except Exception as exc:
                logger.debug("UIA 读取失败：%s", exc)
        if not elements:
            # UIA 不可用或没读到控件 → 纯 Win32 兜底（零依赖，永远带坐标）
            try:
                elements = _win32_controls(hwnd)
            except Exception as exc:
                logger.debug("Win32 控件枚举失败：%s", exc)
        obs.elements = elements
        obs.controls = format_elements(elements)
    obs.focus_class, obs.focus_text = _focus_control()
    try:
        obs.clipboard = _clipboard_preview()
    except Exception:
        pass

    if with_screenshot:
        try:
            obs.screenshot = capture_screen(window_title=obs.window_title)
        except ScreenCaptureError as exc:
            obs.shot_error = str(exc)
            logger.warning("截图失败：%s", exc)
        except Exception as exc:  # noqa: BLE001
            obs.shot_error = str(exc)
            logger.warning("截图异常：%s", exc)

    sig_parts = [
        obs.window_title, obs.app_name, obs.focus_text, obs.focus_class,
        str(len(obs.controls)),
    ]
    if obs.screenshot is not None:
        sig_parts.append(obs.screenshot.signature)
    obs.signature = "\n".join(sig_parts)
    return obs
