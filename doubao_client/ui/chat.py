"""消息区视图：消息列表、单条消息通读窗口与相关交互。"""
from __future__ import annotations

import wx

from ..core import md_render, runner

ROLE_LABEL = {"user": "你", "assistant": "豆包", "hint": "提示"}

# 默认配色与 theme.py 一致；高对比度时由 apply_theme 换成系统色
DEF_FG = wx.Colour(0x1F, 0x23, 0x28)
DEF_BG = wx.Colour(0xFF, 0xFF, 0xFF)
DEF_MUTED = wx.Colour(0x5A, 0x62, 0x70)


def _flatten(text: str) -> str:
    """列表一行放不下换行：把换行压成空格，正文本身不改动。

    Markdown 正文在列表里显示摘要时先摘掉 ``**`` ``#`` ``` 这类标记符号，
    一行看得清；完整原文（含符号）仍然留在 ``_msgs`` 里，详情和复制不受影响。
    """
    shown = md_render.summary(text)
    return " ".join(shown.replace("\r\n", "\n").replace("\r", "\n").split("\n"))


class MessageDialog(wx.Dialog):
    """单条消息的通读窗口：标准只读多行编辑框。

    为什么用 ``wx.TextCtrl`` 而不是富文本控件：只有标准编辑框的 ↑↓ 才真是
    一行一行走、视图跟着滚、读屏跟着念。wx.richtext 的 RichTextCtrl 在只读
    状态下根本不处理键盘（按 5 次 ↓ 插入点仍是 0），去掉只读样式后又会在空行
    上出现阻塞——本机 wxPython 4.3.2 / wxWidgets 3.3.4 实测多种方案均不理想，
    因此改用标准编辑框。

    代价是不再排 Markdown：正文按原文逐字铺开，一个字都不改，复制也复制原文。
    """

    TITLE = {"user": "你的消息", "assistant": "豆包的消息", "hint": "提示"}

    def __init__(self, parent, role, text, fg=DEF_FG, bg=DEF_BG, font=None,
                 on_run=None):
        super().__init__(parent, title=self.TITLE.get(role, "消息"),
                         style=wx.DEFAULT_DIALOG_STYLE | wx.RESIZE_BORDER)
        self._text = text
        self._on_run = on_run
        self._run_asked = False

        # 标准多行只读编辑框：滚动、光标移动、拖选、Ctrl+A / Ctrl+C 全是原生行为
        self.body = wx.TextCtrl(
            self, value=text,
            style=wx.TE_MULTILINE | wx.TE_READONLY | wx.BORDER_SIMPLE)
        self.body.SetFont(font or self.GetFont())
        self.body.SetForegroundColour(fg)
        self.body.SetBackgroundColour(bg)

        sz = wx.BoxSizer(wx.VERTICAL)
        sz.Add(self.body, 1, wx.EXPAND | wx.ALL, self.FromDIP(12))

        btns = wx.BoxSizer(wx.HORIZONTAL)
        self.btn_copy = wx.Button(self, label="复制(&C)")
        self.btn_copy.SetToolTip("把这条消息复制到剪贴板")
        self.btn_close = wx.Button(self, wx.ID_CLOSE, "关闭")
        btns.AddStretchSpacer()
        # 仅当回复中确实包含 Python 代码块时出现，避免点击后无响应
        self.btn_run = None
        if on_run is not None and runner.snippets(text):
            self.btn_run = wx.Button(self, label="运行代码(&R)")
            self.btn_run.SetToolTip(
                "把这条回复里的 Python 代码放到本机执行；点开后会先显示完整"
                "代码和工作目录，确认后才运行")
            btns.Add(self.btn_run, 0, wx.RIGHT, self.FromDIP(8))
        btns.Add(self.btn_copy, 0, wx.RIGHT, self.FromDIP(8))
        btns.Add(self.btn_close, 0)
        sz.Add(btns, 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, self.FromDIP(12))
        self.SetSizer(sz)

        self.btn_copy.Bind(wx.EVT_BUTTON, self._on_copy)
        self.btn_close.Bind(wx.EVT_BUTTON, lambda e: self.EndModal(wx.ID_CLOSE))
        if self.btn_run is not None:
            self.btn_run.Bind(wx.EVT_BUTTON, self._on_run_click)
        # 不要用 SetEscapeId(wx.ID_CLOSE)：它和上面同 id 的「关闭」按钮冲突，
        # 会让销毁路径与按钮路径互相干扰，窗口关不掉。这里显式处理 X 与 Esc。
        self.Bind(wx.EVT_CLOSE, lambda e: self.EndModal(wx.ID_CLOSE))
        self.Bind(wx.EVT_CHAR_HOOK, self._on_char_hook)

        self.SetSize(self.FromDIP(wx.Size(640, 460)))
        self.body.SetInsertionPoint(0)      # 从开头读，别滚到末尾
        self.CentreOnParent()

    def _on_run_click(self, evt):
        # 记个标记就退出：等这个窗口销毁完，再由 show() 去开确认框，
        # 免得新对话框挂在一个正在销毁的父窗口上
        self._run_asked = True
        self.EndModal(wx.ID_OK)

    def _on_copy(self, evt):
        if not wx.TheClipboard.Open():
            return
        wx.TheClipboard.SetData(wx.TextDataObject(self._text))
        wx.TheClipboard.Close()

    def _on_char_hook(self, evt):
        if evt.GetKeyCode() == wx.WXK_ESCAPE:
            self.EndModal(wx.ID_CLOSE)
            return
        evt.Skip()

    def show(self):
        try:
            self.ShowModal()
        finally:
            self.Destroy()
        if self._run_asked and self._on_run is not None:
            self._on_run(self._text)


class ChatView(wx.ListCtrl):
    """竖向消息列表：一条消息一行。"""

    def __init__(self, parent):
        super().__init__(parent,
                         style=wx.LC_REPORT | wx.LC_SINGLE_SEL
                         | wx.LC_HRULES | wx.BORDER_SIMPLE)

        self._fg = DEF_FG
        self._bg = DEF_BG
        self._muted = DEF_MUTED
        self._mdc = None

        # (role, text)，与列表行一一对应；对外只读，一律走 append / append_delta
        self._msgs: list[tuple[str, str]] = []
        # 每行的服务端 message_id：历史消息有，本地流式刚生成的那条可能还没有。
        # 只有「赞 / 踩」需要它，没有 id 的行就不给反馈菜单。
        self._ids: list[str] = []
        # 由 main 挂上来：(message_id, feedback_type) -> None
        self.on_feedback = None
        # 由 main 挂上来：收到「运行代码」请求时回调 (text) -> None
        self.on_run = None
        # 由 main 挂上来：朗读一条消息 / 停止朗读 / 开关变了要存盘
        self.on_speak = None
        self.on_speak_stop = None
        self.on_speak_switch_changed = None
        # 自动朗读有两个开关：「我的消息」和「豆包的回复」各一个，状态分开判断，
        # 但判定和执行只有下面 auto_speak 一份代码。勾上就一直开着，之后每来一条
        # 同角色的新消息都会自动念，不会念完就自动关。
        self._speak_switch = {"user": False, "assistant": False}
        self._last_spoken_role = ""

        self.InsertColumn(0, "发送者")
        self.InsertColumn(1, "消息")
        self.SetBackgroundColour(self._bg)
        self.SetTextColour(self._fg)
        self.SetToolTip("对话内容列表：一条消息一行；单击一行（或回车）"
                        "可在只读窗口里通读全文，Ctrl+C 复制")
        self._fit_columns()

        self.Bind(wx.EVT_SIZE, self._on_size)
        self.Bind(wx.EVT_LEFT_UP, self._on_click)
        self.Bind(wx.EVT_LIST_ITEM_ACTIVATED, self._on_activate)
        self.Bind(wx.EVT_CONTEXT_MENU, self._on_context)
        self.Bind(wx.EVT_KEY_DOWN, self._on_key)

    # ------------------------------------------------------------ 度量 / 列宽
    def _dc(self):
        if self._mdc is None:
            self._mdc = wx.ClientDC(self)
            self._mdc.SetFont(self.GetFont())
        return self._mdc

    def _fit_columns(self):
        """「发送者」按文字宽度自适应，「消息」占满剩下的宽度。"""
        width = self.GetClientSize().width
        if width <= 1:
            return
        dc = self._dc()
        dc.SetFont(self.GetFont())
        c0 = dc.GetTextExtent("发送者")[0] + self.FromDIP(28)
        self.SetColumnWidth(0, c0)
        self.SetColumnWidth(1, max(width - c0 - self.FromDIP(6), self.FromDIP(120)))

    def _on_size(self, evt):
        evt.Skip()
        self._fit_columns()

    def SetFont(self, font):
        ok = super().SetFont(font)
        if getattr(self, "_mdc", None) is not None:
            self._mdc.SetFont(font)
        if getattr(self, "_msgs", None):
            self._fit_columns()
        return ok

    # ------------------------------------------------------------ 行
    def _paint_row(self, n):
        role = self._msgs[n][0]
        self.SetItemTextColour(n, self._muted if role == "hint" else self._fg)

    def _add_row(self, role: str, text: str) -> int:
        n = self.GetItemCount()
        self.InsertItem(n, ROLE_LABEL.get(role, "消息"))
        self.SetItem(n, 1, _flatten(text))
        self._paint_row(n)
        return n

    # ------------------------------------------------------------ 交互
    def _row_at(self, point) -> int:
        idx, _flags = self.HitTest(point)
        return idx if idx != wx.NOT_FOUND and 0 <= idx < len(self._msgs) else -1

    def _on_click(self, evt):
        n = self._row_at(evt.GetPosition())
        evt.Skip()
        if n >= 0:
            self.Select(n)
            self.Focus(n)
            # 事件走完再弹窗：模态循环夹在鼠标事件中途容易留下按下的键态
            wx.CallAfter(self.open_detail, n)

    def _on_activate(self, evt):
        n = evt.GetIndex()
        if 0 <= n < len(self._msgs):
            self.open_detail(n)

    def open_detail(self, n):
        """打开只读多行编辑框，通读第 n 条消息。"""
        if not (0 <= n < len(self._msgs)):
            return
        role, text = self._msgs[n]
        MessageDialog(self, role, text, self._fg, self._bg,
                      self.GetFont(), on_run=self.on_run).show()

    def _on_key(self, evt):
        # 只补 Ctrl+C：ListCtrl 不管剪贴板。方向键 / 翻页 / Home / End
        # 一概不碰，那些是控件自己的事。
        if evt.GetKeyCode() == ord("C") and evt.ControlDown():
            self.copy_selected()
            return
        evt.Skip()

    def _on_context(self, evt):
        n = self._row_at(self.ScreenToClient(evt.GetPosition()))
        if n >= 0:
            self.Select(n)
            self.Focus(n)
        menu = wx.Menu()
        mi_one = menu.Append(wx.ID_ANY, "查看这条消息")
        mi_copy = menu.Append(wx.ID_ANY, "复制这条消息")
        mi_all = menu.Append(wx.ID_ANY, "复制全部对话")
        menu.Bind(wx.EVT_MENU, lambda e: self.open_detail(self._current_row()), mi_one)
        menu.Bind(wx.EVT_MENU, lambda e: self.copy_selected(), mi_copy)
        menu.Bind(wx.EVT_MENU, lambda e: self.copy_all(), mi_all)
        row0 = self._current_row()
        role0 = self._msgs[row0][0] if 0 <= row0 < len(self._msgs) else ""
        # 朗读只占一个位置，但「我的消息」和「豆包回复」是两个独立开关：
        # 选中哪一类就显示哪一类的开关，勾上＝开（这条立刻念，之后同类新消息也念），
        # 再点一下取消勾选＝关。状态存在 chat 里，不会念完自己弹回去。
        label = "朗读我的消息" if role0 == "user" else "朗读豆包的回复"
        mi_speak = menu.Append(wx.ID_ANY, label, kind=wx.ITEM_CHECK)
        mi_speak.Check(bool(self._speak_switch.get(role0)))
        if role0 not in self._speak_switch:
            mi_speak.Enable(False)      # 提示行之类不是真消息，没什么可念的
        menu.Bind(wx.EVT_MENU,
                  lambda e: self._toggle_speak(row0, e.IsChecked()), mi_speak)
        # 只有「豆包的回复 + 有服务端 id」才能反馈，其他行插这两项只会添乱
        row = self._current_row()
        # 这一行里真带 Python 代码块才给「运行」，否则点了只会得到一句「没代码」
        if row >= 0 and self.on_run is not None and runner.snippets(self._msgs[row][1]):
            mi_run = menu.Append(wx.ID_ANY, "运行这条回复里的代码…")
            menu.Bind(wx.EVT_MENU,
                      lambda e: wx.CallAfter(self._run_row, row), mi_run)
            menu.AppendSeparator()
        if row >= 0 and self._msgs[row][0] == "assistant" and self._ids[row]:
            menu.AppendSeparator()
            mi_like = menu.Append(wx.ID_ANY, "点赞这条回复")
            mi_dislike = menu.Append(wx.ID_ANY, "点踩这条回复")
            mi_undo = menu.Append(wx.ID_ANY, "取消反馈")
            menu.Bind(wx.EVT_MENU, lambda e: self._feedback(row, 1), mi_like)
            menu.Bind(wx.EVT_MENU, lambda e: self._feedback(row, 2), mi_dislike)
            menu.Bind(wx.EVT_MENU, lambda e: self._feedback(row, 0), mi_undo)
        try:
            self.PopupMenu(menu)
        finally:
            menu.Destroy()

    def _speak_row(self, row: int):
        if 0 <= row < len(self._msgs) and self.on_speak is not None:
            self._last_spoken_role = self._msgs[row][0]
            self.on_speak(self._msgs[row][1])

    def auto_speak(self, role: str, text: str) -> bool:
        """消息到了要不要念：只看这个角色自己的开关。

        用户消息和豆包回复各自判断，但判定与播放只有这一份代码——两边都走这里。
        """
        if not text or self.on_speak is None:
            return False
        if not self._speak_switch.get(role):
            return False
        self._last_spoken_role = role
        self.on_speak(text)
        return True

    def speak_switches(self) -> dict:
        return dict(self._speak_switch)

    def set_speak_switches(self, values):
        """启动时把上次存的开关读回来。"""
        for role in self._speak_switch:
            if isinstance(values, dict) and role in values:
                self._speak_switch[role] = bool(values[role])

    def _toggle_speak(self, row: int, on: bool):
        """勾选/取消：两个角色共用这一套，按行的角色分派到对应开关。"""
        role = self._msgs[row][0] if 0 <= row < len(self._msgs) else ""
        if role not in self._speak_switch:
            return
        self._speak_switch[role] = bool(on)
        if on:
            self._speak_row(row)          # 勾上先把当前这条念了
        elif self._last_spoken_role == role and self.on_speak_stop is not None:
            # 只停「正在念的这一类」：关掉我的消息，不该把豆包的回复也掐了
            self.on_speak_stop()
        if self.on_speak_switch_changed is not None:
            self.on_speak_switch_changed(self.speak_switches())

    def _feedback(self, row, feedback_type: int):
        if not (0 <= row < len(self._ids)) or not self._ids[row]:
            return
        if self.on_feedback:
            self.on_feedback(self._ids[row], feedback_type)

    def _run_row(self, row: int):
        """把某一行的原文交给外面的执行流程（右键菜单里的「运行」）。"""
        if 0 <= row < len(self._msgs) and self.on_run is not None:
            self.on_run(self._msgs[row][1])

    def _current_row(self) -> int:
        n = self.GetFirstSelected()
        return n if 0 <= n < len(self._msgs) else -1

    # ------------------------------------------------------------ 滚动
    def _at_bottom(self) -> bool:
        n = self.GetItemCount()
        if not n:
            return True
        return self.GetTopItem() + max(self.GetCountPerPage(), 1) >= n

    def scroll_end(self):
        """滚到最后一行（最新消息）。"""
        n = self.GetItemCount()
        if n:
            self.EnsureVisible(n - 1)

    # ------------------------------------------------------------ 数据
    def clear(self):
        self._msgs.clear()
        self._ids.clear()
        self.DeleteAllItems()
        self._fit_columns()

    def hint(self, text: str):
        self.append("hint", text)

    def append(self, role: str, text: str, mid: str = ""):
        follow = self._at_bottom()
        self._msgs.append((role, text))
        self._ids.append(str(mid or ""))
        self._add_row(role, text)
        if follow:
            self.scroll_end()

    def message_id(self, n: int) -> str:
        return self._ids[n] if 0 <= n < len(self._ids) else ""

    def append_delta(self, text: str):
        """流式增量：改最后一行，不新增行。"""
        if not text:
            return
        if not self._msgs or self._msgs[-1][0] != "assistant":
            self.append("assistant", text)
            return
        follow = self._at_bottom()
        role, old = self._msgs[-1]
        self._msgs[-1] = (role, old + text)
        n = len(self._msgs) - 1
        self.SetItem(n, 1, _flatten(self._msgs[-1][1]))
        if follow:
            self.scroll_end()

    def is_empty(self) -> bool:
        return not any(m[0] != "hint" for m in self._msgs)

    def has_message(self) -> bool:
        return not self.is_empty()

    def last_text(self) -> str:
        for role, text in reversed(self._msgs):
            if role != "hint":
                return text
        return ""

    def last_assistant(self) -> str:
        """最后一条豆包的回复。菜单里的「运行回复里的代码」以它为准。"""
        for role, text in reversed(self._msgs):
            if role == "assistant":
                return text
        return ""

    def get_text(self) -> str:
        """整块纯文本，给「复制全部」和外面做断言用。"""
        return "\n\n".join(
            f"{ROLE_LABEL.get(r, '消息')}：{t}" for r, t in self._msgs)

    # ------------------------------------------------------------ 样式
    def apply_theme(self, fg=None, bg=None, muted=None):
        self._fg = fg or DEF_FG
        self._bg = bg or DEF_BG
        self._muted = muted or DEF_MUTED
        self.SetBackgroundColour(self._bg)
        self.SetTextColour(self._fg)
        for i in range(self.GetItemCount()):
            self._paint_row(i)
        self.Refresh()

    # ------------------------------------------------------------ 复制
    def copy_selected(self) -> bool:
        """复制选中的那一行；没有选中就退回最后一条回复。"""
        n = self._current_row()
        if n >= 0:
            return self._copy(self._msgs[n][1])
        return self.copy_last()

    def copy_last(self) -> bool:
        return self._copy(self.last_text())

    def copy_all(self) -> bool:
        return self._copy(self.get_text())

    @staticmethod
    def _copy(text: str) -> bool:
        if not text:
            return False
        if not wx.TheClipboard.Open():
            return False
        wx.TheClipboard.SetData(wx.TextDataObject(text))
        wx.TheClipboard.Close()
        return True
