"""代码执行相关对话框：运行确认、结果展示、执行进度与镜像源设置。"""
from __future__ import annotations

import threading
import time
import urllib.request

import wx

from ..services import a11y
from ..core.runner import Snippet
from .theme import mono_font


class RunConfirmDialog(wx.Dialog):
    """运行前的确认框。返回选中的代码，而不是让调用方自己再解析一遍。"""

    def __init__(self, parent, items: list[Snippet], workdir: str,
                 python: str, fg=None, bg=None, font=None):
        super().__init__(parent, title="在本机运行代码",
                         style=wx.DEFAULT_DIALOG_STYLE | wx.RESIZE_BORDER)
        self._items = items
        self._choice_index = 0

        body_font = font or self.GetFont()

        # 说明：把这事的风险讲在明面上，别让用户以为只是「预览一下」
        warn = wx.StaticText(
            self,
            label="这段代码会在你的电脑上真实执行：可以新建、修改、删除文件，"
                  "可以安装第三方包，也可以操作窗口和键鼠。\n"
                  "下面显示的就是将要运行的完整代码，确认无误再点「运行」。")
        warn.SetForegroundColour(a11y.C_WARN if fg is None else fg)
        warn.Wrap(self.FromDIP(660))

        sz = wx.BoxSizer(wx.VERTICAL)
        sz.Add(warn, 0, wx.EXPAND | wx.ALL, self.FromDIP(12))

        # 多段代码时才出现的选择器；只有一段就不给用户多余的选择
        self.pick = None
        if len(items) > 1:
            row = wx.BoxSizer(wx.HORIZONTAL)
            lbl = wx.StaticText(self, label="执行哪一段(&P)：")
            self.pick = wx.Choice(
                self, choices=[f"第 {s.index} 段（{len(s.code.splitlines())} 行）"
                               f"　{s.first_line}" for s in items])
            self.pick.SetSelection(0)
            row.Add(lbl, 0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, self.FromDIP(8))
            row.Add(self.pick, 1, wx.ALIGN_CENTER_VERTICAL)
            sz.Add(row, 0, wx.EXPAND | wx.LEFT | wx.RIGHT, self.FromDIP(12))

        code_lbl = wx.StaticText(self, label="代码(&D)：")
        sz.Add(code_lbl, 0, wx.LEFT | wx.RIGHT | wx.TOP, self.FromDIP(12))
        self.code = wx.TextCtrl(
            self, value=items[0].code,
            style=wx.TE_MULTILINE | wx.TE_READONLY | wx.BORDER_SIMPLE
            | wx.HSCROLL)
        self.code.SetFont(mono_font(body_font))
        if fg is not None:
            self.code.SetForegroundColour(fg)
        if bg is not None:
            self.code.SetBackgroundColour(bg)
        sz.Add(self.code, 1, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.TOP,
               self.FromDIP(12))

        dir_row = wx.BoxSizer(wx.HORIZONTAL)
        dir_lbl = wx.StaticText(self, label="工作目录(&W)：")
        self.dir = wx.DirPickerCtrl(
            self, path=workdir,
            message="选择脚本运行时所在的目录（相对路径、生成的文件都在这里）",
            style=wx.DIRP_USE_TEXTCTRL)
        dir_row.Add(dir_lbl, 0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, self.FromDIP(8))
        dir_row.Add(self.dir, 1, wx.ALIGN_CENTER_VERTICAL)
        sz.Add(dir_row, 0, wx.EXPAND | wx.ALL, self.FromDIP(12))

        self.py_lbl = wx.StaticText(
            self, label=f"用这个解释器：{python or '没找到，运行时会报错'}")
        self.py_lbl.SetForegroundColour(wx.Colour(0x5A, 0x62, 0x70))
        sz.Add(self.py_lbl, 0, wx.LEFT | wx.RIGHT | wx.BOTTOM, self.FromDIP(12))

        btns = wx.BoxSizer(wx.HORIZONTAL)
        self.btn_run = wx.Button(self, wx.ID_OK, "运行(&R)")
        self.btn_cancel = wx.Button(self, wx.ID_CANCEL, "取消")
        btns.AddStretchSpacer()
        btns.Add(self.btn_run, 0, wx.RIGHT, self.FromDIP(8))
        btns.Add(self.btn_cancel, 0)
        sz.Add(btns, 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, self.FromDIP(12))

        self.SetSizer(sz)
        self.SetSize(self.FromDIP(wx.Size(760, 620)))
        self.SetMinSize(self.FromDIP(wx.Size(560, 420)))

        self.btn_run.Bind(wx.EVT_BUTTON, self._on_run)
        if self.pick is not None:
            self.pick.Bind(wx.EVT_CHOICE, self._on_pick)
        self.SetAffirmativeId(wx.ID_OK)
        # 不要 SetEscapeId(wx.ID_CANCEL)：它和上面同 id 的「取消」按钮冲突，
        # 会让窗口关不掉。模态对话框的 Esc→取消 / Enter→运行 是内建行为，
        # 不需要 SetEscapeId。
        self.btn_cancel.SetDefault()      # 默认落在取消上，回车不会误触运行
        self.btn_cancel.SetFocus()
        self.CentreOnParent()

    # ------------------------------------------------------------ 事件
    def _on_pick(self, evt):
        self._choice_index = max(self.pick.GetSelection(), 0)
        self.code.SetValue(self._items[self._choice_index].code)
        self.code.SetInsertionPoint(0)

    def _on_run(self, evt):
        # 二次确认：这一段能动手改本机文件，值得再问一次
        dlg = wx.MessageDialog(
            self,
            "确定要在这台电脑上运行这段代码吗？\n"
            "它可以修改或删除文件，也可能自行安装第三方包。",
            "再确认一次", wx.YES_NO | wx.NO_DEFAULT | wx.ICON_WARNING)
        try:
            if dlg.ShowModal() != wx.ID_YES:
                return
        finally:
            dlg.Destroy()
        self.EndModal(wx.ID_OK)

    # ------------------------------------------------------------ 结果
    def selected(self) -> Snippet:
        return self._items[min(self._choice_index, len(self._items) - 1)]

    def workdir(self) -> str:
        return self.dir.GetPath()


class RunResultDialog(wx.Dialog):
    """执行结果：退出码、耗时、装了什么包、以及原始输出。"""

    def __init__(self, parent, result, fg=None, bg=None, font=None,
                 on_send=None):
        ok = result.ok
        title = "执行完成" if ok else "执行结束（有错误）"
        super().__init__(parent, title=title,
                         style=wx.DEFAULT_DIALOG_STYLE | wx.RESIZE_BORDER)
        self._result = result
        self._on_send = on_send

        head = wx.StaticText(self, label=result.summary())
        bold = wx.Font(font or self.GetFont())
        bold.SetWeight(wx.FONTWEIGHT_BOLD)
        head.SetFont(bold)

        sz = wx.BoxSizer(wx.VERTICAL)
        sz.Add(head, 0, wx.ALL, self.FromDIP(12))

        lbl = wx.StaticText(self, label="输出(&O)：")
        sz.Add(lbl, 0, wx.LEFT | wx.RIGHT, self.FromDIP(12))
        self.body = wx.TextCtrl(
            self, value=result.report(),
            style=wx.TE_MULTILINE | wx.TE_READONLY | wx.BORDER_SIMPLE
            | wx.HSCROLL)
        self.body.SetFont(mono_font(font or self.GetFont()))
        if fg is not None:
            self.body.SetForegroundColour(fg)
        if bg is not None:
            self.body.SetBackgroundColour(bg)
        sz.Add(self.body, 1, wx.EXPAND | wx.ALL, self.FromDIP(12))

        btns = wx.BoxSizer(wx.HORIZONTAL)
        self.btn_copy = wx.Button(self, label="复制结果(&C)")
        self.btn_send = wx.Button(self, label="发给豆包(&S)")
        self.btn_send.SetToolTip("把这段结果放进输入框，你可以补充一句再发送")
        self.btn_close = wx.Button(self, wx.ID_CLOSE, "关闭")
        btns.AddStretchSpacer()
        btns.Add(self.btn_copy, 0, wx.RIGHT, self.FromDIP(8))
        btns.Add(self.btn_send, 0, wx.RIGHT, self.FromDIP(8))
        btns.Add(self.btn_close, 0)
        sz.Add(btns, 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, self.FromDIP(12))

        self.SetSizer(sz)
        self.SetSize(self.FromDIP(wx.Size(720, 520)))
        self.SetMinSize(self.FromDIP(wx.Size(520, 340)))

        self.btn_copy.Bind(wx.EVT_BUTTON, self._on_copy)
        self.btn_send.Bind(wx.EVT_BUTTON, self._on_send_click)
        self.btn_close.Bind(wx.EVT_BUTTON, lambda e: self.EndModal(wx.ID_CLOSE))
        # 不要用 SetEscapeId(wx.ID_CLOSE)：它和上面同 id 的「关闭」按钮冲突，
        # 会让窗口关不掉。这里显式处理 X 与 Esc。
        self.Bind(wx.EVT_CLOSE, lambda e: self.EndModal(wx.ID_CLOSE))
        self.Bind(wx.EVT_CHAR_HOOK, self._on_char_hook)
        self.btn_close.SetFocus()
        self.CentreOnParent()

    def _on_copy(self, evt):
        if wx.TheClipboard.Open():
            wx.TheClipboard.SetData(wx.TextDataObject(self._result.report()))
            wx.TheClipboard.Close()

    def _on_char_hook(self, evt):
        if evt.GetKeyCode() == wx.WXK_ESCAPE:
            self.EndModal(wx.ID_CLOSE)
            return
        evt.Skip()

    def _on_send_click(self, evt):
        if self._on_send:
            self._on_send(self._result.report())
        self.EndModal(wx.ID_CLOSE)

    def show(self):
        try:
            return self.ShowModal()
        finally:
            self.Destroy()


class _PulseTimer(wx.Timer):
    """进度条动画的节拍器：Pulse 不连续调用进度条就不走。"""

    def __init__(self, owner):
        super().__init__()
        self._owner = owner

    def Notify(self):
        self._owner._pulse()


class RunProgressDialog:
    """跑代码/装包时的进度窗，样式对齐 Windows 的解压对话框：

    标题、滚动的进度条、当前动作、输出末几行、已用时间、取消按钮。
    早先只有状态栏一行字，装包下载几分钟毫无动静，看起来就像「下不动」——
    现在每个阶段和 pip 的每一行输出都能看见，随时可取消。
    """

    def __init__(self, parent, title: str = "正在执行 Python 代码"):
        self.cancel = threading.Event()      # 交给 runner：置位即杀子进程
        self._msg = "准备中…"
        self._tail: list[str] = []
        self._alive = True
        self._dlg = wx.ProgressDialog(
            title, self._compose(), 100, parent,
            wx.PD_APP_MODAL | wx.PD_CAN_ABORT | wx.PD_AUTO_HIDE
            | wx.PD_SMOOTH | wx.PD_ELAPSED_TIME)
        self._timer = _PulseTimer(self)
        self._timer.Start(200)

    # ------------------------------------- 工作线程用（调用方自己 wx.CallAfter）
    def status(self, msg: str):
        self._msg = msg
        self._pulse()

    def line(self, text: str):
        text = (text or "").strip()
        if text:
            self._tail.append(text[-160:])
            del self._tail[:-3]              # 只留最后三行，别把消息区灌满
        self._pulse()

    # ------------------------------------------------------------- 内部
    def _compose(self) -> str:
        if self._tail:
            return self._msg + "\n" + "\n".join(self._tail)
        return self._msg

    def _pulse(self):
        if not self._alive:
            return
        try:
            keep_going, _ = self._dlg.Pulse(self._compose())
        except RuntimeError:                 # 对话框已销毁
            self._alive = False
            return
        if not keep_going:                   # 用户点了「取消」
            self.cancel.set()
            self._msg = "正在取消…"
            self._tail.clear()
            self._timer.Stop()

    def close(self):
        self._alive = False
        self._timer.Stop()
        try:
            self._dlg.Destroy()
        except RuntimeError:
            pass


class MirrorDialog(wx.Dialog):
    """设置 pip 镜像源。布局仿 NVDA 的「设置更新镜像」窗：
    网址标签 + 编辑框 + 「测试」按钮 + 确认/取消。全局生效。"""

    def __init__(self, parent, current: str = "", fg=None, bg=None, font=None):
        super().__init__(parent, title="设置 pip 镜像源",
                         style=wx.DEFAULT_DIALOG_STYLE)
        body_font = font or self.GetFont()

        hint = wx.StaticText(
            self,
            label="自动安装第三方包时从这个地址下载，全局生效"
                  "（会话跑代码、AI Agent 都用它）。\n"
                  "留空则使用官方 PyPI；「测试」会请求该地址验证能否连通。")
        hint.Wrap(self.FromDIP(560))

        sz = wx.BoxSizer(wx.VERTICAL)
        sz.Add(hint, 0, wx.EXPAND | wx.ALL, self.FromDIP(12))

        row = wx.BoxSizer(wx.HORIZONTAL)
        lbl = wx.StaticText(self, label="镜像地址(&U)：")
        self.edit = wx.TextCtrl(self, value=current or "")
        self.edit.SetFont(body_font)
        self.btn_test = wx.Button(self, label="测试(&T)…")
        self.btn_test.SetToolTip("请求这个地址，验证能不能连通")
        row.Add(lbl, 0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, self.FromDIP(8))
        row.Add(self.edit, 1, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, self.FromDIP(8))
        row.Add(self.btn_test, 0, wx.ALIGN_CENTER_VERTICAL)
        sz.Add(row, 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, self.FromDIP(12))

        self.lbl_result = wx.StaticText(self, label="")
        sz.Add(self.lbl_result, 0,
               wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, self.FromDIP(12))

        btns = wx.BoxSizer(wx.HORIZONTAL)
        self.btn_ok = wx.Button(self, wx.ID_OK, "确认")
        self.btn_cancel = wx.Button(self, wx.ID_CANCEL, "取消")
        btns.AddStretchSpacer()
        btns.Add(self.btn_ok, 0, wx.RIGHT, self.FromDIP(8))
        btns.Add(self.btn_cancel, 0)
        sz.Add(btns, 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, self.FromDIP(12))

        self.SetSizer(sz)
        self.SetMinSize(self.FromDIP(wx.Size(560, -1)))
        self.Fit()

        self.btn_test.Bind(wx.EVT_BUTTON, self._on_test)
        self.btn_ok.Bind(wx.EVT_BUTTON, self._on_ok)
        self.edit.SetFocus()
        self.edit.SetSelection(-1, -1)
        self.CentreOnParent()

    def url(self) -> str:
        return self.edit.GetValue().strip()

    def _on_ok(self, _evt):
        u = self.url()
        if u and not u.startswith(("http://", "https://")):
            wx.MessageBox("网址需要以 http:// 或 https:// 开头；留空表示用官方 PyPI。",
                          "镜像地址不合法", wx.OK | wx.ICON_WARNING, self)
            self.edit.SetFocus()
            return
        self.EndModal(wx.ID_OK)

    def _on_test(self, _evt):
        url = self.url() or "https://pypi.org/simple"
        self.btn_test.Enable(False)
        self.lbl_result.SetLabel(f"正在测试 {url} …")

        def finish(ok, msg):
            self.btn_test.Enable(True)
            self.lbl_result.SetLabel(msg)
            self.lbl_result.SetForegroundColour(
                wx.Colour(0x1F, 0x7A, 0x33) if ok else wx.Colour(0xB0, 0x2A, 0x2A))

        def work():
            t0 = time.time()
            try:
                req = urllib.request.Request(
                    url, method="GET", headers={"User-Agent": "pip"})
                with urllib.request.urlopen(req, timeout=10) as r:
                    code = int(getattr(r, "status", 200) or 200)
                    r.read(4096)          # 真读一点，避免只连上没数据
                ok = 200 <= code < 400
                msg = (f"连接成功（HTTP {code}，耗时 {time.time() - t0:.1f} 秒）"
                       if ok else f"连接失败：HTTP {code}")
            except Exception as exc:  # noqa: BLE001
                ok, msg = False, f"连接失败：{exc}"
            wx.CallAfter(finish, ok, msg)

        threading.Thread(target=work, daemon=True).start()
