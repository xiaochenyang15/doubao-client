"""界面主题：配色常量、字体与高对比度检测。

颜色对比度已按 WCAG 1.4.3 校验（正文 4.5:1 以上）；高对比度模式下
所有配色交还给系统，详见各窗口的 ``_apply_theme``。
"""
from __future__ import annotations

import wx

# 配色
C_BG = wx.Colour(0xFA, 0xFA, 0xFB)
C_SIDE = wx.Colour(0xF3, 0xF4, 0xF6)
C_BORDER = wx.Colour(0xE5, 0xE7, 0xEB)
C_TEXT = wx.Colour(0x1F, 0x23, 0x28)
# 次要文字：#6B7280 在侧栏 #F3F4F6 上对比度仅 4.39:1，不满足 WCAG 1.4.3
# 的 4.5:1 要求；#5A6270 实测 5.59:1（白底 6.15:1）。
C_MUTED = wx.Colour(0x5A, 0x62, 0x70)
C_ERR = wx.Colour(0xB9, 0x1C, 0x1C)
C_WHITE = wx.Colour(0xFF, 0xFF, 0xFF)

# 账号信息框未登录时的固定文案
INFO_UNLOGGED = "未登录。\n登录后此处显示昵称、UID 和绑定手机。"

FONT_NAME = "Microsoft YaHei UI"


def mono_font(base: wx.Font, size: int = 10) -> wx.Font:
    """等宽字体：代码需要按列对齐。"""
    for face in ("Consolas", "Cascadia Mono", "Courier New"):
        f = wx.Font(wx.FontInfo(size).FaceName(face))
        if f.IsOk():
            return f
    return base


def _system_base_pt() -> int:
    """系统默认字号（会反映「使文本更大」设置）。"""
    try:
        return wx.SystemSettings.GetFont(wx.SYS_DEFAULT_GUI_FONT).GetPointSize() or 9
    except Exception:  # noqa: BLE001
        return 9


def ui_font(size=10, bold=False):
    """按系统基准字号等比放大，保证文本缩放后不丢失可读性。"""
    base = _system_base_pt()
    pt = max(8, int(round(size * base / 9.0)))
    f = wx.Font(wx.FontInfo(pt).FaceName(FONT_NAME))
    if bold:
        f = f.Bold()
    return f


def is_high_contrast() -> bool:
    """检测 Windows 高对比度模式。"""
    import ctypes
    from ctypes import wintypes

    class HIGHCONTRAST(ctypes.Structure):
        _fields_ = [("cbSize", wintypes.UINT),
                    ("dwFlags", wintypes.DWORD),
                    ("lpszDefaultScheme", wintypes.LPWSTR)]

    hc = HIGHCONTRAST()
    hc.cbSize = ctypes.sizeof(HIGHCONTRAST)
    SPI_GETHIGHCONTRAST = 0x0042
    HCF_HIGHCONTRASTON = 0x0001
    try:
        ok = ctypes.windll.user32.SystemParametersInfoW(
            SPI_GETHIGHCONTRAST, hc.cbSize, ctypes.byref(hc), 0)
        return bool(ok) and bool(hc.dwFlags & HCF_HIGHCONTRASTON)
    except Exception:  # noqa: BLE001
        return False
