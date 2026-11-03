# 部分实现（mouse_move 边界钳制、save_anchor 记录当前鼠标位置、落点吸附的
# objectFromPoint 思路）衍生自 NVDA 插件 Golden Cursor 26.1
# （<https://github.com/nvda-es/goldenCursor>）：
# Copyright (C) 2015-2022 Salah Atair, Wafeeq Taher, Joseph Lee,
# Abdel (Abdelkrim Bensaid) 及 NVDA 西语社区等贡献者。
# Golden Cursor 以 GNU GPL v2 授权；衍生部分随本项目同样以
# GNU GPL v2 发布（见项目根 LICENSE / THIRD-PARTY-NOTICES.md）。
from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any, Callable

import win32api
import win32clipboard
import win32con

from . import anchors, hittest
from .decision import AgentAction


NORMALIZED_SCALE = 1000


class ActionExecutionError(Exception):
    """动作无法执行时抛出（坐标缺失、键名不认识等）。"""


@dataclass(frozen=True)
class Geometry:
    """整块虚拟屏的几何。

    一定要用**截图当时**的那份几何去换算坐标：抓图和点击之间如果出现分辨率 /
    显示器布局变化（插拔外接屏、改缩放），现算的 ``screen_bounds()`` 会和模型看到的
    那张图对不上，点击就会整体偏移。所以由观察层把截图自带的 left/top/w/h 传下来。
    """
    left: int = 0
    top: int = 0
    width: int = 0
    height: int = 0

    @property
    def valid(self) -> bool:
        return self.width > 0 and self.height > 0


# --------------------------------------------------------------------------- #
# 屏幕坐标
# --------------------------------------------------------------------------- #
def screen_bounds() -> tuple[int, int, int, int]:
    """整块虚拟屏的范围，返回 (left, top, w, h)。

    真正的实现在 ``hittest.virtual_screen()``——落点校验也要用同一份边界，
    两处各算一遍迟早会对不上，所以这里只保留名字兼容。
    """
    return hittest.virtual_screen()


def current_geometry() -> Geometry:
    left, top, width, height = screen_bounds()
    return Geometry(left=left, top=top, width=width, height=height)


def norm_to_screen(x: int, y: int,
                   geometry: "Geometry | None" = None) -> tuple[int, int]:
    """归一化 0-1000 → 整屏像素坐标。

    ``geometry`` 传了就用它（推荐：来自本步截图），否则现查虚拟屏。
    """
    x = min(max(int(x), 0), NORMALIZED_SCALE)
    y = min(max(int(y), 0), NORMALIZED_SCALE)
    geo = geometry or current_geometry()
    w, h = geo.width, geo.height
    if w <= 0 or h <= 0:
        return geo.left + x, geo.top + y
    sx = geo.left + min(int(round(x * w / NORMALIZED_SCALE)), max(w - 1, 0))
    sy = geo.top + min(int(round(y * h / NORMALIZED_SCALE)), max(h - 1, 0))
    return sx, sy


def screen_to_norm(sx: int, sy: int) -> tuple[int, int]:
    """整屏像素坐标 → 归一化 0-1000（观察层用）。"""
    left, top, w, h = screen_bounds()
    nx = int(round((sx - left) * NORMALIZED_SCALE / w)) if w else 0
    ny = int(round((sy - top) * NORMALIZED_SCALE / h)) if h else 0
    return min(max(nx, 0), NORMALIZED_SCALE), min(max(ny, 0), NORMALIZED_SCALE)


CheckCancelled = Callable[[], None]


# --------------------------------------------------------------------------- #
# 基础原子操作
# --------------------------------------------------------------------------- #
def _sleep(seconds: float, check: CheckCancelled) -> None:
    end = time.time() + max(0.0, seconds)
    while True:
        remaining = end - time.time()
        if remaining <= 0:
            return
        check()
        time.sleep(min(0.05, remaining))


def mouse_move(x: int, y: int) -> None:
    """把指针挪到 (x, y)。

    先钳进屏幕再动（goldenCursor 的 ``moveMouse`` 也是先判 ``0 <= x < w``），
    坐标算错时至少不会把指针丢到不存在的区域上。

    这里**故意不补发 MOUSEEVENTF_MOVE**：实测 ``SetCursorPos`` 本身就会产生一条
    WM_MOUSEMOVE，而 dx=dy=0 的 ``mouse_event`` 一条都不多产生；用绝对坐标的
    ``mouse_event`` 反而会因为 0-65535 取整偏 1 像素。goldenCursor 里那句
    ``executeMouseMoveEvent`` 是让 NVDA 自己播报新坐标用的，对外部程序没有额外作用。
    """
    x, y = hittest.clamp(x, y)
    win32api.SetCursorPos((x, y))
    time.sleep(0.04)


@dataclass
class ActionReport:
    """一次动作的结果说明。

    写进历史后模型才知道自己「实际做到了什么」，而不是只能靠下一张截图猜。
    """
    note: str = ""
    hit: "hittest.PointHit | None" = None


def _prepare_point(x: int, y: int,
                   app_name: str = "") -> tuple[int, int, "hittest.PointHit | None", str]:
    """点击前的落点校验：吸到真实控件正中，并把「命中了什么」记下来。

    对应 goldenCursor 的 ``objectFromPoint``。命中具体控件就挪到控件中心——这是
    当下直接问系统，比上一步观察缓存里的元素列表更准。落点属于**别的程序**时不
    拦截（任务本来就可能要跨程序，键盘切窗口也一样），但会明说，模型能立刻发现
    「我点歪到别的窗口上了」。
    """
    x, y = hittest.clamp(x, y)
    hit = hittest.element_at(x, y)
    if hit is None or not hit.valid:
        return x, y, None, ""
    if hit.specific:
        x, y = hit.center
    note = f"落点命中：{hit.describe()}"
    if app_name and hit.exe and hit.exe.lower() != app_name.lower():
        note = f"⚠ 落点不在目标程序上：{hit.describe()}（目标是 {app_name}）"
    return x, y, hit, note


def _remember_hit(app_name: str, hit, x: int, y: int) -> None:
    """点中带名字的可交互控件，就把「名字 → 位置」记进锚点库。

    锚点库能自己长大靠的就是这里：第一次靠看图找按钮，之后同名按钮可以直接
    ``click_anchor``，不用再走一次视觉决策。只记可交互、够具体（不是整块面板
    那种）、名字长度合理的，免得把正文或空容器当成锚点存进去。
    """
    if hit is None or not hit.interactive or not hit.specific or not app_name:
        return
    label = (hit.name or "").strip()
    if not (1 <= len(label) <= anchors.MAX_NAME):
        return
    try:
        nx, ny = screen_to_norm(x, y)
        anchors.remember(app_name, label, nx, ny, note=hit.kind, auto=True)
    except Exception:  # noqa: BLE001  记不住不影响点击本身
        pass


def _click_spec(name: str) -> tuple[str, int]:
    """动作名 → (哪个键, 点几下)。"""
    button = "left"
    if name.startswith("right_"):
        button = "right"
    elif name.startswith("middle_"):
        button = "middle"
    if name.startswith("double_"):
        return button, 2
    if name.startswith("triple_"):
        return button, 3
    return button, 1


def _anchor_missing(app: str, name: str) -> str:
    """锚点找不到时的报错：把「现在有什么」摊给模型，它才好换一个名字。"""
    msg = f"没有叫「{name}」的锚点"
    if app:
        msg += f"（当前程序 {app}）"
    msg += "。"
    known = anchors.describe(app, 6)
    if known:
        msg += "这个程序现在有的：" + "；".join(known) + "。"
    else:
        msg += "这个程序还没记过锚点，可以先用 save_anchor 记一个，或者直接 click_at。"
    other = anchors.find_any(name)
    if other is not None:
        msg += f"（{other[0]} 下倒是有个「{other[1]}」，但锚点不跨程序用。）"
    return msg


def _mouse_click(button: str = "left", count: int = 1,
                 release_delay: float = 0.04, seq_delay: float = 0.12) -> None:
    down = win32con.MOUSEEVENTF_LEFTDOWN if button == "left" else (
        win32con.MOUSEEVENTF_RIGHTDOWN if button == "right" else win32con.MOUSEEVENTF_MIDDLEDOWN)
    up = win32con.MOUSEEVENTF_LEFTUP if button == "left" else (
        win32con.MOUSEEVENTF_RIGHTUP if button == "right" else win32con.MOUSEEVENTF_MIDDLEUP)
    for _ in range(max(1, count)):
        win32api.mouse_event(down, 0, 0, 0, 0)
        time.sleep(release_delay)
        win32api.mouse_event(up, 0, 0, 0, 0)
        time.sleep(seq_delay)


def _tap_vk(vk: int) -> None:
    win32api.keybd_event(vk, 0, 0, 0)
    time.sleep(0.02)
    win32api.keybd_event(vk, 0, win32con.KEYEVENTF_KEYUP, 0)
    time.sleep(0.02)


def _select_all() -> None:
    """Ctrl+A：按下 -> 点 A -> 抬 A -> 抬 Ctrl（顺序不能乱，否则可能卡住 Ctrl）。"""
    win32api.keybd_event(win32con.VK_CONTROL, 0, 0, 0)
    time.sleep(0.02)
    win32api.keybd_event(ord("A"), 0, 0, 0)
    time.sleep(0.02)
    win32api.keybd_event(ord("A"), 0, win32con.KEYEVENTF_KEYUP, 0)
    time.sleep(0.02)
    win32api.keybd_event(win32con.VK_CONTROL, 0, win32con.KEYEVENTF_KEYUP, 0)
    time.sleep(0.03)


# 友好键名 → 虚拟键码 / 字符
_VK_NAMES = {
    "enter": win32con.VK_RETURN, "return": win32con.VK_RETURN,
    "esc": win32con.VK_ESCAPE, "escape": win32con.VK_ESCAPE,
    "tab": win32con.VK_TAB,
    "backspace": win32con.VK_BACK, "back": win32con.VK_BACK,
    "delete": win32con.VK_DELETE, "del": win32con.VK_DELETE,
    "space": win32con.VK_SPACE, "spacebar": win32con.VK_SPACE,
    "shift": win32con.VK_SHIFT, "ctrl": win32con.VK_CONTROL,
    "control": win32con.VK_CONTROL, "alt": win32con.VK_MENU,
    "menu": win32con.VK_MENU, "win": win32con.VK_LWIN, "windows": win32con.VK_LWIN,
    "capslock": win32con.VK_CAPITAL,
    "home": win32con.VK_HOME, "end": win32con.VK_END,
    "pgup": win32con.VK_PRIOR, "pageup": win32con.VK_PRIOR,
    "pgdn": win32con.VK_NEXT, "pagedown": win32con.VK_NEXT,
    "left": win32con.VK_LEFT, "leftarrow": win32con.VK_LEFT,
    "right": win32con.VK_RIGHT, "rightarrow": win32con.VK_RIGHT,
    "up": win32con.VK_UP, "uparrow": win32con.VK_UP,
    "down": win32con.VK_DOWN, "downarrow": win32con.VK_DOWN,
    "f1": win32con.VK_F1, "f2": win32con.VK_F2, "f3": win32con.VK_F3,
    "f4": win32con.VK_F4, "f5": win32con.VK_F5, "f6": win32con.VK_F6,
    "f7": win32con.VK_F7, "f8": win32con.VK_F8, "f9": win32con.VK_F9,
    "f10": win32con.VK_F10, "f11": win32con.VK_F11, "f12": win32con.VK_F12,
}


def _resolve_vk(token: str) -> int:
    """把单个键名解析成虚拟键码。"""
    t = token.strip().lower()
    if t in _VK_NAMES:
        return _VK_NAMES[t]
    # 单个字符：用 VkKeyScan 解析（兼容大小写、标点符号）
    if len(token.strip()) == 1:
        code = win32api.VkKeyScan(token.strip()) & 0xFF
        if code:
            return code
    raise ActionExecutionError(f"不认识的键名：{token!r}")


def key_combination(keys: str) -> None:
    """按下一组组合键，如 'control+a'、'alt+f4'、'control+shift+a'、'enter'。"""
    parts = [p.strip() for p in keys.replace(" ", "").split("+") if p.strip()]
    if not parts:
        raise ActionExecutionError("组合键为空")
    vks = [_resolve_vk(p) for p in parts]
    for vk in vks:
        win32api.keybd_event(vk, 0, 0, 0)
        time.sleep(0.03)
    time.sleep(0.04)
    for vk in reversed(vks):
        win32api.keybd_event(vk, 0, win32con.KEYEVENTF_KEYUP, 0)
        time.sleep(0.03)


# --------------------------------------------------------------------------- #
# 剪贴板（打字用）
# --------------------------------------------------------------------------- #
def _get_clipboard_text() -> str | None:
    try:
        win32clipboard.OpenClipboard(0)
        try:
            if win32clipboard.IsClipboardFormatAvailable(win32con.CF_UNICODETEXT):
                return win32clipboard.GetClipboardData(win32con.CF_UNICODETEXT)
        finally:
            win32clipboard.CloseClipboard()
    except Exception:
        return None
    return None


def _set_clipboard_text(text: str) -> None:
    for _ in range(3):
        try:
            win32clipboard.OpenClipboard(0)
            try:
                win32clipboard.EmptyClipboard()
                win32clipboard.SetClipboardData(win32con.CF_UNICODETEXT, text)
            finally:
                win32clipboard.CloseClipboard()
            return
        except Exception:
            time.sleep(0.05)


def type_text(text: str, press_enter: bool = False,
              clear_before: bool = True, check: CheckCancelled = lambda: None) -> None:
    """把文字粘贴进当前焦点控件。执行前后尽量还原剪贴板。"""
    old = _get_clipboard_text()
    try:
        if clear_before:
            # 先 Ctrl+A 全选再删：比「发 30 个退格」快得多，也不会在长文本里
            # 因为退格没删干净而把新旧内容粘在一起（那是经常「打错」的真实原因）
            for _ in range(2):
                check()
                _select_all()
                _tap_vk(win32con.VK_DELETE)
                time.sleep(0.05)
            _tap_vk(win32con.VK_END)
            time.sleep(0.03)
        if text:
            _set_clipboard_text(text)
            time.sleep(0.05)
            check()
            win32api.keybd_event(win32con.VK_CONTROL, 0, 0, 0)
            time.sleep(0.05)
            win32api.keybd_event(ord("V"), 0, 0, 0)
            win32api.keybd_event(ord("V"), 0, win32con.KEYEVENTF_KEYUP, 0)
            time.sleep(0.05)
            win32api.keybd_event(win32con.VK_CONTROL, 0, win32con.KEYEVENTF_KEYUP, 0)
            time.sleep(0.05)
        if press_enter:
            time.sleep(0.1)
            _tap_vk(win32con.VK_RETURN)
    finally:
        if old is not None:
            try:
                _set_clipboard_text(old)
            except Exception:
                pass


# --------------------------------------------------------------------------- #
# 分发
# --------------------------------------------------------------------------- #
PointResolver = Callable[[int, int], tuple[int, int]]


def _point(args: dict[str, Any], geometry: "Geometry | None",
           resolve: PointResolver | None) -> tuple[int, int]:
    """把动作里的归一化坐标换算成整屏像素，优先走外部传入的吸附器。"""
    nx, ny = _need(args, "x"), _need(args, "y")
    if resolve is not None:
        try:
            px, py = resolve(nx, ny)
            return int(px), int(py)
        except Exception:  # noqa: BLE001  吸附失败就当没给
            pass
    return norm_to_screen(nx, ny, geometry)


def execute_action(action: AgentAction, check: CheckCancelled = lambda: None,
                   geometry: "Geometry | None" = None,
                   resolve: PointResolver | None = None,
                   app_name: str = "") -> ActionReport:
    """执行一个动作，返回这次动作的说明（写进历史用）。

    :param geometry: 本步截图对应的整屏几何，坐标为归一化值时用它换算
    :param resolve:  可选落点吸附器 ``(nx, ny) -> (px, py)``；界面层把「命中控件
                     中心」的逻辑接在这里，模型给的大概位置就能落到控件正中
    :param app_name: 目标程序（exe 名）。用来分区锚点，也用来在落点跑到别的程序
                     上时给模型一句明白的提醒
    """
    check()
    name = action.name
    args = action.arguments
    note, hit = "", None

    if name in ("click_at", "click", "double_click_at", "double_click",
                "triple_click_at", "triple_click", "right_click_at", "right_click",
                "middle_click_at", "middle_click"):
        x, y = _point(args, geometry, resolve)
        x, y, hit, note = _prepare_point(x, y, app_name)
        mouse_move(x, y)
        button, count = _click_spec(name)
        _mouse_click(button, count)
        if (button, count) == ("left", 1):
            _remember_hit(app_name, hit, x, y)
    elif name in ("hover_at", "move_to", "move"):
        x, y = _point(args, geometry, resolve)
        x, y, hit, note = _prepare_point(x, y, app_name)
        mouse_move(x, y)
    elif name == "type_text_at":
        if "x" in args and "y" in args:
            x, y = _point(args, geometry, resolve)
            x, y, hit, note = _prepare_point(x, y, app_name)
            mouse_move(x, y)
            _mouse_click("left", 1)
            _remember_hit(app_name, hit, x, y)
            time.sleep(0.35)
        type_text(str(args.get("text") or ""),
                  press_enter=bool(args.get("press_enter", False)),
                  clear_before=bool(args.get("clear_before_typing", True)),
                  check=check)
    elif name == "click_anchor":
        name_arg = str(args.get("anchor") or args.get("name") or "").strip()
        if not name_arg:
            raise ActionExecutionError("click_anchor 缺少 anchor（锚点名字）")
        found = anchors.find(app_name, name_arg)
        if found is None:
            raise ActionExecutionError(_anchor_missing(app_name, name_arg))
        nx, ny, real = found
        x, y = norm_to_screen(nx, ny, geometry or current_geometry())
        x, y, hit, where = _prepare_point(x, y, app_name)
        mouse_move(x, y)
        _mouse_click("left", 1)
        # 只把「用过一次」记上，**不改坐标**：吸附可能吸到了别的东西，拿它覆盖会把
        # 锚点越用越偏；要更新位置就显式 save_anchor。
        anchors.remember(app_name, real, nx, ny, bump=True)
        note = f"点了锚点「{real}」(x={nx}, y={ny})"
        if where:
            note += "；" + where
    elif name == "save_anchor":
        name_arg = str(args.get("anchor") or args.get("name") or "").strip()
        if not name_arg:
            raise ActionExecutionError("save_anchor 缺少 anchor（要给这个位置起个名字）")
        if "x" in args and "y" in args:
            nx, ny = _need(args, "x"), _need(args, "y")
        else:
            # 不给坐标就记「鼠标现在停的地方」——goldenCursor 存当前位置也是这么做的
            x, y = win32api.GetCursorPos()
            nx, ny = screen_to_norm(x, y)
        real = anchors.remember(app_name, name_arg, nx, ny,
                                note=str(args.get("note") or ""))
        if not real:
            raise ActionExecutionError("记不住：拿不到程序名，锚点要按程序分区")
        note = f"已记住锚点「{real}」= (x={nx}, y={ny})，以后可以直接 click_anchor 叫它"
    elif name in ("key_combination", "hotkey", "press_key"):
        key_combination(str(args.get("keys") or args.get("key") or ""))
    elif name in ("scroll_at", "scroll_document", "scroll"):
        _scroll(args, check, geometry, resolve)
    elif name in ("drag_and_drop", "drag_to"):
        sx, sy = _point(args, geometry, resolve)
        dx, dy = norm_to_screen(_need(args, "destination_x"),
                                _need(args, "destination_y"), geometry)
        _drag(sx, sy, dx, dy, check)
    elif name == "drag_by":
        sx, sy = _point(args, geometry, resolve)
        try:
            dx = int(args.get("delta_x") or 0)
            dy = int(args.get("delta_y") or 0)
        except (TypeError, ValueError) as exc:
            raise ActionExecutionError("drag_by 缺少 delta_x/delta_y") from exc
        _drag(sx, sy, sx + dx, sy + dy, check)
    elif name in ("wait", "wait_5_seconds"):
        _sleep(float(args.get("seconds") or 5), check)
    elif name == "wait_for_change":
        # 纯文字 Agent 没有「像素级变化检测」，退化为固定短等，让屏幕状态有机会刷新
        _sleep(float(args.get("seconds") or 3), check)
    elif name == "go_back":
        key_combination("alt+left")
    elif name == "go_forward":
        key_combination("alt+right")
    elif name == "navigate":
        url = str(args.get("url") or args.get("text") or "").strip()
        if not url:
            raise ActionExecutionError("navigate 缺少 url")
        key_combination("control+l")
        time.sleep(0.2)
        type_text(url, press_enter=True, clear_before=True, check=check)
    else:
        raise ActionExecutionError(f"不支持的动作：{name}")
    return ActionReport(note=note, hit=hit)


def _need(args: dict[str, Any], key: str) -> int:
    if key not in args:
        raise ActionExecutionError(f"缺少坐标参数 {key}")
    try:
        return int(round(float(args[key])))
    except (TypeError, ValueError) as exc:
        raise ActionExecutionError(f"坐标 {key} 不是数字") from exc


def _scroll(args: dict[str, Any], check: CheckCancelled,
            geometry: "Geometry | None" = None,
            resolve: PointResolver | None = None) -> None:
    if "x" in args and "y" in args:
        x, y = _point(args, geometry, resolve)
        mouse_move(x, y)
    else:
        geo = geometry or current_geometry()
        mouse_move(geo.left + geo.width // 2, geo.top + geo.height // 2)
    direction = str(args.get("direction") or "down").lower()
    magnitude = int(args.get("magnitude") or 0)
    ticks = max(1, round(magnitude / 1000 * 3)) if magnitude else 3
    delta = ticks * win32con.WHEEL_DELTA
    if direction in ("down", "right"):
        delta = -delta
    is_vertical = direction not in ("left", "right")
    for _ in range(ticks):
        check()
        if is_vertical:
            win32api.mouse_event(win32con.MOUSEEVENTF_WHEEL, 0, 0, delta, 0)
        else:
            win32api.mouse_event(win32con.MOUSEEVENTF_HWHEEL, 0, 0, delta, 0)
        time.sleep(0.05)


def _drag(sx: int, sy: int, ex: int, ey: int, check: CheckCancelled) -> None:
    dist = max(abs(ex - sx), abs(ey - sy))
    steps = min(40, max(8, dist // 16)) if dist else 1
    mouse_move(sx, sy)
    time.sleep(0.18)
    win32api.mouse_event(win32con.MOUSEEVENTF_LEFTDOWN, 0, 0, 0, 0)
    time.sleep(0.18)
    for i in range(1, steps + 1):
        check()
        px = sx + (ex - sx) * i // steps
        py = sy + (ey - sy) * i // steps
        mouse_move(px, py)
        time.sleep(0.025)
    time.sleep(0.2)
    win32api.mouse_event(win32con.MOUSEEVENTF_LEFTUP, 0, 0, 0, 0)
