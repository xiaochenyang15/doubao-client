from __future__ import annotations

import ctypes
from ctypes import wintypes

import wx

# winuser.h
EVENT_SYSTEM_ALERT = 0x0002
EVENT_OBJECT_NAMECHANGE = 0x800C
OBJID_CLIENT = -4
CHILDID_SELF = 0

# 警示色：运行代码那种「可能改你文件」的提示用它。白底 6.7:1，过了 WCAG AA
C_WARN = wx.Colour(0xA3, 0x3B, 0x1E)


def _hwnd(win) -> int:
    if isinstance(win, int):
        return win
    try:
        return int(win.GetHandle())
    except Exception:  # noqa: BLE001
        return 0


def notify(win, alert: bool = False) -> None:
    """告诉辅助技术「这个对象的名称变了」。

    alert=True 时额外发送系统警报事件，等价于 ARIA 的 alert，
    NVDA / 讲述人会立刻朗读该控件文本，而不需要用户把焦点移过去。
    """
    hwnd = _hwnd(win)
    if not hwnd:
        return
    try:
        user32 = ctypes.windll.user32
        user32.NotifyWinEvent(EVENT_OBJECT_NAMECHANGE, wintypes.HWND(hwnd),
                              OBJID_CLIENT, CHILDID_SELF)
        if alert:
            user32.NotifyWinEvent(EVENT_SYSTEM_ALERT, wintypes.HWND(hwnd),
                                  OBJID_CLIENT, CHILDID_SELF)
    except Exception:  # noqa: BLE001
        pass


def tell(label, text: str, alert: bool = False) -> bool:
    """更新静态文本并播报；文本没变化时不重复打扰。"""
    if label.GetLabel() == text:
        return False
    label.SetLabel(text)
    notify(label, alert)
    return True


def contrast_ratio(fg, bg) -> float:
    """WCAG 2.x 相对亮度对比度（1.0 ~ 21.0）。"""

    def channel(c: int) -> float:
        s = c / 255.0
        return s / 12.92 if s <= 0.04045 else ((s + 0.055) / 1.055) ** 2.4

    def luminance(colour) -> float:
        r, g, b = (channel(v) for v in (colour.Red(), colour.Green(), colour.Blue()))
        return 0.2126 * r + 0.7152 * g + 0.0722 * b

    a, b = sorted((luminance(fg), luminance(bg)), reverse=True)
    return (a + 0.05) / (b + 0.05)


if __name__ == "__main__":
    # 配色自检：正文 4.5:1，大字号 3:1（WCAG 1.4.3）
    pairs = [
        ("C_TEXT 正文 / C_BG", wx.Colour(0x1F, 0x23, 0x28), wx.Colour(0xFA, 0xFA, 0xFB)),
        ("C_MUTED 次要文字 / C_SIDE", wx.Colour(0x5A, 0x62, 0x70), wx.Colour(0xF3, 0xF4, 0xF6)),
        ("C_MUTED 次要文字 / 白底", wx.Colour(0x5A, 0x62, 0x70), wx.Colour(0xFF, 0xFF, 0xFF)),
        ("C_ERR 错误 / 白底", wx.Colour(0xB9, 0x1C, 0x1C), wx.Colour(0xFF, 0xFF, 0xFF)),
        ("C_WARN 执行风险提示 / 白底", C_WARN, wx.Colour(0xFF, 0xFF, 0xFF)),
        ("旧 C_MUTED(#6B7280) / C_SIDE", wx.Colour(0x6B, 0x72, 0x80), wx.Colour(0xF3, 0xF4, 0xF6)),
    ]
    for name, fg, bg in pairs:
        ratio = contrast_ratio(fg, bg)
        print(f"{name:32s} {ratio:5.2f}:1  {'PASS' if ratio >= 4.5 else 'FAIL'}")
