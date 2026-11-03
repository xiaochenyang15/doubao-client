"""账号相关对话框与剪贴板图片工具。

键盘约定：Enter 触发默认按钮，Esc 取消/关闭，初始焦点在主操作上。
不使用 ``SetEscapeId``：与同 ID 的「关闭/退出」按钮冲突时，销毁路径
与按钮路径会相互干扰导致窗口无法关闭，因此统一在 EVT_CHAR_HOOK 中
显式处理 Esc。
"""
from __future__ import annotations

import os
import threading
import time

import wx

from ..services import a11y
from ..core.config import APP_DIR
from .theme import C_ERR, ui_font, is_high_contrast


def grab_clipboard_image() -> str | None:
    """剪贴板里有图片就存成 PNG 返回路径，没有就返回 None。

    Windows 截图工具放的是 PNG 格式，另一些程序只给位图（CF_BITMAP），两种都认。
    落到 ``<APP_DIR>/_paste/``（属运行期私有数据，不进分发包）。
    """
    out_dir = os.path.join(APP_DIR, "_paste")
    try:
        os.makedirs(out_dir, exist_ok=True)
    except OSError:
        return None
    path = os.path.join(out_dir, f"paste_{time.strftime('%Y%m%d_%H%M%S')}.png")
    if not wx.TheClipboard.Open():
        return None
    try:
        fmt = wx.DataFormat("PNG")
        if wx.TheClipboard.IsSupported(fmt):
            do = wx.CustomDataObject(fmt)
            if wx.TheClipboard.GetData(do):
                raw = b""
                try:
                    raw = bytes(do.GetData())
                except Exception:  # noqa: BLE001
                    size = do.GetDataSize()
                    buf = bytearray(size)
                    if do.GetDataHere(buf):
                        raw = bytes(buf)
                if raw.startswith(b"\x89PNG\r\n\x1a\n"):
                    with open(path, "wb") as f:
                        f.write(raw)
                    return path
        bd = wx.BitmapDataObject()
        if wx.TheClipboard.GetData(bd):
            bmp = bd.GetBitmap()
            if bmp.IsOk() and bmp.GetWidth() > 0 and bmp.GetHeight() > 0:
                if bmp.SaveFile(path, wx.BITMAP_TYPE_PNG):
                    return path
    except Exception:  # noqa: BLE001
        return None
    finally:
        wx.TheClipboard.Close()
    return None


class LoginDialog(wx.Dialog):
    """没有可用登录态时的提示框：引导到浏览器登录，登录成功后自动返回。

    reason 区分两种进入方式：
      "missing" —— 本机根本没有 cookie；
      "expired" —— 有 cookie，但服务端已经不认（过期 / 被踢下线）；
      "switch"  —— 主动切换账号，必须等到「换成了另一个账号」才算成功。
    """

    def __init__(self, parent, reason="missing", api=None, current_uid=""):
        super().__init__(parent, title="需要登录豆包")
        expired = reason == "expired"
        switching = reason == "switch"
        self.api = api
        self.current_uid = str(current_uid or "")
        self.switching = switching
        self.cookies: dict = {}
        self.profile: dict = {}
        self.source = ""
        self._polling = False
        self._browser_open = False
        self._last_msg = ""
        # 对话框整体跟随系统配色：白底硬编码会盖掉高对比度设置
        bg = wx.SystemSettings.GetColour(wx.SYS_COLOUR_WINDOW)
        fg = wx.SystemSettings.GetColour(wx.SYS_COLOUR_WINDOWTEXT)
        muted = wx.SystemSettings.GetColour(wx.SYS_COLOUR_GRAYTEXT)
        self.SetBackgroundColour(bg)

        sz = wx.BoxSizer(wx.VERTICAL)
        if switching:
            head = "登录 / 切换账号"
            first = "登录成功后会自动替换当前保存的账号。"
            extra = ("检测到仍是原来那个账号时不会退出本窗口："
                     "请先在登录窗口里点头像 → 退出登录，再登录新账号。")
        elif expired:
            head = "登录态已失效，请重新登录"
            first = "保存的登录态已不被服务器认可。"
            extra = ""
        else:
            head = "未检测到豆包登录态"
            first = "本机暂时没有可用的豆包登录态。"
            extra = ""
        title = wx.StaticText(self, label=head)
        title.SetFont(ui_font(13, True))
        title.SetForegroundColour(fg)
        sz.Add(title, 0, wx.LEFT | wx.RIGHT | wx.TOP, 22)

        tip = wx.StaticText(
            self,
            label=first + "\n"
                  "点「去登录」会打开登录窗口，登录成功后自动返回；\n"
                  "登录态会加密保存在程序目录，下次启动直接复用，无需重复登录。"
                  + (f"\n{extra}" if extra else ""))
        tip.SetFont(ui_font(10))
        tip.SetForegroundColour(muted)
        sz.Add(tip, 0, wx.ALL, 22)

        self.lbl_msg = wx.StaticText(self, label="")
        self.lbl_msg.SetFont(ui_font(9))
        self.lbl_msg.SetForegroundColour(C_ERR)
        sz.Add(self.lbl_msg, 0, wx.LEFT | wx.RIGHT | wx.BOTTOM, 22)

        row = wx.BoxSizer(wx.HORIZONTAL)
        # 不写死像素尺寸：字号放大到 200% 时按钮文字不会被裁掉（WCAG 1.4.4）
        self.btn_open = wx.Button(self, label="去登录(&L)")
        self.btn_open.Bind(wx.EVT_BUTTON, self._on_open)
        row.Add(self.btn_open, 0, wx.RIGHT, 10)

        self.btn_retry = wx.Button(self, label="重新检测(&R)")
        self.btn_retry.Bind(wx.EVT_BUTTON, self._on_retry)
        row.Add(self.btn_retry, 0, wx.RIGHT, 10)

        btn_cancel = wx.Button(self, wx.ID_CANCEL, "退出")
        btn_cancel.Bind(wx.EVT_BUTTON, lambda e: self.EndModal(wx.ID_CANCEL))
        row.Add(btn_cancel, 0)
        sz.Add(row, 0, wx.LEFT | wx.RIGHT | wx.BOTTOM, 22)

        self.SetSizerAndFit(sz)
        self.CentreOnParent()

    def _on_char_hook(self, evt):
        if evt.GetKeyCode() == wx.WXK_ESCAPE:
            self.EndModal(wx.ID_CANCEL)
            return
        evt.Skip()

        # 键盘约定：Enter 触发默认按钮、Esc 取消、初始焦点在主操作上
        self.btn_open.SetDefault()
        # 不要用 SetEscapeId(wx.ID_CANCEL)：它和上面同 id 的「退出」按钮冲突，
        # 会让销毁路径与按钮路径互相干扰，窗口关不掉。这里显式处理 X 与 Esc。
        self.Bind(wx.EVT_CLOSE, lambda e: self.EndModal(wx.ID_CANCEL))
        self.Bind(wx.EVT_CHAR_HOOK, self._on_char_hook)
        self.btn_open.SetFocus()

        self.timer = wx.Timer(self)
        self.Bind(wx.EVT_TIMER, self._poll, self.timer)

    def _show_msg(self, text, error=False):
        """提示区文本 + 播报。错误同时用文字前缀表达，不单靠颜色（WCAG 1.4.1）。

        轮询每 2 秒写一次同样的文字，重复播报会把读屏刷屏，所以只在
        文字真的变化时才发事件。
        """
        hc = is_high_contrast()
        if error:
            self.lbl_msg.SetForegroundColour(
                wx.SystemSettings.GetColour(wx.SYS_COLOUR_WINDOWTEXT) if hc else C_ERR)
            text = f"错误：{text}"
        else:
            self.lbl_msg.SetForegroundColour(
                wx.SystemSettings.GetColour(wx.SYS_COLOUR_WINDOWTEXT) if hc
                else wx.SystemSettings.GetColour(wx.SYS_COLOUR_GRAYTEXT))
        changed = text != self._last_msg
        self._last_msg = text
        if self.lbl_msg.GetLabel() != text:
            self.lbl_msg.SetLabel(text)
            self.Layout()
        if changed:
            a11y.tell(self.lbl_msg, text, alert=error)

    def _accept(self, profile, cookies) -> bool:
        """登录框认不认这次结果：切换账号时必须是「另一个账号」。"""
        if not (profile and cookies):
            return False
        if self.switching and self.current_uid:
            uid = str(profile.get("uid") or "")
            if not uid or uid == self.current_uid:
                name = profile.get("name") or "原账号"
                self._show_msg(f"当前仍是原账号「{name}」，"
                               "请在登录窗口里退出登录后再登录新账号。")
                return False
        return True

    # 打开登录窗口后自动轮询，登录成功即返回
    def _on_open(self, _evt):
        if self.api is None:
            self._show_msg("无法打开登录窗口：浏览器连接未就绪。", error=True)
            return
        self.btn_open.Enable(False)
        self._show_msg("正在打开登录窗口…")

        def work():
            try:
                self.api.open_login_window()
            except Exception as exc:  # noqa: BLE001
                wx.CallAfter(self._on_open_failed, str(exc))
                return
            wx.CallAfter(self._on_open_ok)

        threading.Thread(target=work, daemon=True).start()

    def _on_open_ok(self):
        self._browser_open = True
        self._show_msg("登录窗口已打开，请在窗口里完成豆包登录，成功后会自动返回。")
        self.timer.Start(2000)

    def _on_open_failed(self, msg):
        self.btn_open.Enable(True)
        self._show_msg(f"打开登录窗口失败：{msg}", error=True)

    def _poll(self, _evt):
        if self._polling or self.api is None:
            return
        self._polling = True

        def work():
            try:
                # 直接问服务端：页面已经登录就算成功，同时顺手取回 cookie
                profile, cookies = self.api.probe_login()
            except Exception as exc:  # noqa: BLE001
                wx.CallAfter(self._on_poll_result, None, None, False, str(exc))
                return
            wx.CallAfter(self._on_poll_result, profile, cookies, True, "")

        threading.Thread(target=work, daemon=True).start()

    def _on_poll_result(self, profile, cookies, ok, error):
        self._polling = False
        if ok and self._accept(profile, cookies):
            self.timer.Stop()
            self.profile = profile or {}
            self.cookies = cookies
            self.source = "浏览器登录"
            self.EndModal(wx.ID_OK)
            return
        # 轮询期间页面跳转/加载中都会失败，属于正常抖动，不打断等待
        if not ok:
            self._show_msg("等待登录中… 请在登录窗口里完成豆包登录。")

    def _on_retry(self, _evt):
        """重新检测：没开窗口就先开，开了就立刻问一次服务端。"""
        if not self._browser_open:
            self._on_open(None)
            return
        self.btn_retry.Enable(False)
        self._show_msg("正在检测登录状态…")

        def work():
            try:
                profile, cookies = self.api.probe_login()
            except Exception as exc:  # noqa: BLE001
                wx.CallAfter(self._on_result, None, None, False, str(exc))
                return
            wx.CallAfter(self._on_result, profile, cookies, True, "")

        threading.Thread(target=work, daemon=True).start()

    def _on_result(self, profile, cookies, ok, error):
        self.btn_retry.Enable(True)
        if ok and self._accept(profile, cookies):
            self.profile = profile or {}
            self.cookies = cookies
            self.source = "浏览器登录"
            self.EndModal(wx.ID_OK)
            return
        if not ok:
            self._show_msg(error or "仍未检测到登录态，请确认已在登录窗口里登录豆包。",
                           error=True)

    def Destroy(self):
        # 计时器挂在对话框上，销毁前必须停掉，否则会在已析构的窗口上回调
        try:
            self.timer.Stop()
        except Exception:  # noqa: BLE001
            pass
        return super().Destroy()


class AccountDialog(wx.Dialog):
    """账号信息：一个只读多行编辑框 + 复制 / 关闭。

    用普通对话框而不是 wx.MessageBox：MessageBox 里的文本不能换行、不能
    选中复制，读屏也只能整段读。这里放的是只读 wx.TextCtrl，行为跟消息区
    一致——Tab 交给 wx 的遍历，Ctrl+A / Ctrl+C 都是原生行为。
    """

    def __init__(self, parent, text, logged):
        super().__init__(parent, title="账号信息")
        bg = wx.SystemSettings.GetColour(wx.SYS_COLOUR_WINDOW)
        fg = wx.SystemSettings.GetColour(wx.SYS_COLOUR_WINDOWTEXT)
        self.SetBackgroundColour(bg)

        sz = wx.BoxSizer(wx.VERTICAL)
        # 文本框的可访问名称来自紧邻的这个标签（见文件头第 1 条）
        lbl = wx.StaticText(self, label="账号信息" if logged else "账号信息（未登录）")
        lbl.SetFont(ui_font(9, True))
        lbl.SetForegroundColour(fg)
        sz.Add(lbl, 0, wx.LEFT | wx.RIGHT | wx.TOP, 16)

        self.txt = wx.TextCtrl(self, value=text,
                               style=wx.TE_MULTILINE | wx.TE_READONLY
                               | wx.TE_RICH2 | wx.BORDER_SIMPLE)
        self.txt.SetFont(ui_font(10))
        # 高度按三行文字算，再留一行余量：正好等于内容高度时会顶出一条
        # 用不上的竖向滚动条
        line_h = round(self.txt.GetCharHeight() * 1.35)
        self.txt.SetMinSize(wx.Size(self.FromDIP(320), line_h * 4 + self.FromDIP(16)))
        self.txt.SetMargins(wx.Point(self.FromDIP(8), self.FromDIP(6)))
        self.txt.SetToolTip("只读，可选中后用 Ctrl+C 复制")
        sz.Add(self.txt, 1, wx.EXPAND | wx.ALL, 16)

        row = wx.BoxSizer(wx.HORIZONTAL)
        btn_copy = wx.Button(self, label="复制(&C)")
        btn_copy.Bind(wx.EVT_BUTTON, self._on_copy)
        btn_copy.Enable(bool(logged))
        btn_close = wx.Button(self, wx.ID_CLOSE, "关闭")
        btn_close.Bind(wx.EVT_BUTTON, lambda e: self.EndModal(wx.ID_CLOSE))
        row.AddStretchSpacer()
        row.Add(btn_copy, 0, wx.RIGHT, 8)
        row.Add(btn_close, 0)
        sz.Add(row, 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, 16)

        self.SetSizerAndFit(sz)
        # 不要用 SetEscapeId(wx.ID_CLOSE)：它和上面同 id 的「关闭」按钮冲突，
        # 会让销毁路径与按钮路径互相干扰，窗口关不掉。这里显式处理 X 与 Esc。
        self.Bind(wx.EVT_CLOSE, lambda e: self.EndModal(wx.ID_CLOSE))
        self.Bind(wx.EVT_CHAR_HOOK, self._on_char_hook)
        btn_close.SetDefault()
        self.CentreOnParent()
        self.txt.SetFocus()
        self.txt.SetInsertionPoint(0)

    def _on_copy(self, _evt):
        if wx.TheClipboard.Open():
            wx.TheClipboard.SetData(wx.TextDataObject(self.txt.GetValue()))
            wx.TheClipboard.Close()

    def _on_char_hook(self, evt):
        if evt.GetKeyCode() == wx.WXK_ESCAPE:
            self.EndModal(wx.ID_CLOSE)
            return
        evt.Skip()
