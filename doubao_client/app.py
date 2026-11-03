"""应用程序入口：DPI 感知、单实例互斥锁与主循环。

启动顺序有严格约束：

1. DPI 感知必须在**任何窗口创建之前**开启——进程若是 DPI 不感知的，
   Windows 会先把桌面缩小再交给 BitBlt，AI Agent 的截图就是糊的，
   坐标也会落在缩小后的网格上；
2. 单实例互斥锁在进入 wx 主循环之前获取，第二个实例只弹提示后退出；
3. 互斥锁的释出交给 ``atexit``，主循环无论以何种方式结束都会解锁。
"""
from __future__ import annotations

import os

import wx

from .core.singleinstance import SingleInstance
from .ui.main_window import MainFrame


def enable_dpi_awareness() -> None:
    """让本进程按物理像素工作（Per-Monitor V2）。

    只有一个进程一次机会：已经被 wx 或清单设过就会失败，失败则跳过，
    不影响运行。
    """
    if os.name != "nt":
        return
    import ctypes

    try:      # Win10 1703+：PER_MONITOR_AWARE_V2
        if ctypes.windll.user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4)):
            return
    except Exception:  # noqa: BLE001
        pass
    try:      # Win8.1+：PROCESS_PER_MONITOR_DPI_AWARE
        if ctypes.windll.shcore.SetProcessDpiAwareness(2) == 0:
            return
    except Exception:  # noqa: BLE001
        pass
    try:      # Vista+：系统级
        ctypes.windll.user32.SetProcessDPIAware()
    except Exception:  # noqa: BLE001
        pass


class DoubaoApp(wx.App):
    """wx 应用对象：只负责创建主窗口，业务一律在 MainFrame 及各服务内。"""

    def OnInit(self):
        self.SetAppName("豆包客户端")
        frame = MainFrame()
        frame.Show()
        return True


def main() -> int:
    """程序入口：DPI → 互斥锁 → 主循环。返回进程退出码。"""
    enable_dpi_awareness()          # 必须在任何窗口创建之前

    guard = SingleInstance()
    if not guard.acquire():
        wx.MessageBox(
            "豆包客户端已经在运行。\n\n请使用已打开的窗口；如需重新启动，"
            "请先退出当前实例。", "豆包客户端", wx.OK | wx.ICON_INFORMATION)
        return 0

    app = DoubaoApp(False)
    app.MainLoop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
