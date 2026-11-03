"""主窗口：会话列表、消息区、输入区与菜单。

无障碍约定（全文生效）：
    1. 每个需要名称的控件前放一个紧邻的 wx.StaticText，且两者在创建顺序上
       相邻——这是原生控件能被读屏读出名称的唯一可靠方式；
    2. 状态栏承担实时播报职责，文字变化时主动发送 MSAA 事件；
    3. 菜单项的可用状态跟随实际状态，读屏才会读出「不可用」。

键盘约定与 theme/dialogs 一致；全局快捷键在 EVT_CHAR_HOOK 中兜底处理。
"""
from __future__ import annotations

import os
import threading
import time

import wx

from ..core import runner
from ..core.config import APP_DIR, load_config, update_config
from ..services import a11y, tts
from ..services import cookies as cookie_bridge
from ..services.api import AuthError, DoubaoAPI
from .theme import C_BG, C_SIDE, C_TEXT, C_MUTED, C_ERR, C_WHITE, INFO_UNLOGGED, ui_font, is_high_contrast
from .chat import ChatView
from .dialogs import AccountDialog, LoginDialog, grab_clipboard_image
from .runner_dialogs import MirrorDialog, RunConfirmDialog, RunProgressDialog, RunResultDialog
from .agent_dialog import AgentDialog
from .packages_dialog import PackageManagerDialog


class MainFrame(wx.Frame):
    def __init__(self):
        screen = wx.Display(0).GetClientArea()
        w = min(1180, screen.width - 60)
        h = min(820, screen.height - 80)
        # wx.TAB_TRAVERSAL：没有这个样式，Frame 不会启用 wx 自带的 Tab 遍历，
        # 多行编辑框里的 Tab 就没有任何反应。遍历本身完全交给 wx。
        super().__init__(None, title="豆包客户端", size=(w, h),
                         style=wx.DEFAULT_FRAME_STYLE | wx.TAB_TRAVERSAL)
        # 最小尺寸按 DPI 换算，否则 150% 缩放下会小于内容所需
        self.SetMinSize(wx.Size(min(self.FromDIP(820), screen.width - 40),
                                min(self.FromDIP(560), screen.height - 60)))
        self.SetBackgroundColour(C_BG)

        cfg = load_config()
        self.api = DoubaoAPI(device_id=cfg.get("device_id"), web_id=cfg.get("web_id"))
        # 朗读要靠 api 拿浏览器里当前生效的登录态：豆包语音是「以登录用户的身份」
        # 去合成的，没有它就只能退回系统语音。
        tts.set_api(self.api)
        if cfg.get("tts_speaker"):
            tts.set_speaker(cfg["tts_speaker"])
        self.lock = threading.Lock()
        self.conversations: list[dict] = []
        self.current_conv = "0"
        self.streaming = False
        self._auth_fail_count = 0
        # 会话 id -> 置顶状态；列表接口不返回 pinned，只能按会话单独查，
        # 查到就缓存，避免每次刷新都重新问一遍。
        self._pin_cache: dict[str, bool] = {}
        # 多选列表没有 GetSelection()（会断言），回车/右键/改名要用的「当前行」
        # 只能自己记：光标 + 上一次的选择集合，用差集算出刚选中的是哪一行。
        self._conv_cursor = -1
        self._conv_prev_sel: set[int] = set()

        self._build_menu()
        self._build_ui()
        self.Centre()
        self.Bind(wx.EVT_CLOSE, self._on_close)
        self.Bind(wx.EVT_SYS_COLOUR_CHANGED, self._apply_theme)
        # 快捷键统一在 CHAR_HOOK 处理：原生控件和读屏会先吃掉 KEY_DOWN
        self.Bind(wx.EVT_CHAR_HOOK, self._on_char_hook)

        threading.Thread(target=self._check_login, daemon=True).start()

    # ------------------------------------------------------------------ 菜单
    def _build_menu(self):
        """菜单只保留主界面按钮和右键菜单覆盖不到的功能。

        与主界面重复的功能不进菜单：新建对话（左栏按钮）、停止生成（发送区
        按钮）、复制 / 运行代码（消息区右键）、重命名 / 清除上下文 / 全选 /
        删除（会话列表右键或列表快捷键）。这些功能的快捷键统一由
        ``_on_char_hook`` 处理，键盘可达性与迁移前一致。
        """
        bar = wx.MenuBar()

        account = wx.Menu()
        self.mi_login = account.Append(wx.ID_ANY, "登录 / 切换账号\tCtrl+L",
                                       "从浏览器读取登录态，或打开登录窗口")
        self.mi_refresh = account.Append(wx.ID_ANY, "刷新登录状态\tCtrl+R",
                                         "重新读取登录态并重连")
        self.mi_account_info = account.Append(wx.ID_ANY, "账号信息\tCtrl+I",
                                              "以只读多行框显示当前登录账号")
        account.AppendSeparator()
        # 不再手工声明「\tAlt+F4」：Alt+F4 由系统处理，重复注册反而抢焦点
        account.Append(wx.ID_EXIT, "退出")
        bar.Append(account, "账号(&A)")

        chat = wx.Menu()
        self.mi_agent = chat.Append(wx.ID_ANY, "AI Agent…\tCtrl+Shift+A",
                                    "AI Agent：每步截一张屏幕图交给豆包看，"
                                    "豆包返回动作，客户端真实操控键鼠")
        self.mi_speak = chat.Append(wx.ID_ANY, "朗读最后一条回复\tCtrl+Shift+V",
                                    "用豆包自己的声音念最后一条回复；"
                                    "音频由 bass.dll 播放，逐句出声，长回复边合成边放")
        self.mi_shut = chat.Append(wx.ID_ANY, "停止朗读",
                                   "立刻停声，并丢掉还没念的部分")
        chat.AppendSeparator()
        chat.Append(wx.ID_REFRESH, "刷新会话列表\tF5")
        bar.Append(chat, "对话(&C)")

        settings = wx.Menu()
        self.mi_packages = settings.Append(
            wx.ID_ANY, "包管理(&P)…",
            "查看、安装、升级、卸载随包 Python 环境里的第三方库")
        self.mi_mirror = settings.Append(
            wx.ID_ANY, "pip 镜像源(&M)…",
            "自动安装第三方包的下载地址，全局生效"
            "（会话跑代码、AI Agent 的 run_code 都用它）")
        bar.Append(settings, "设置(&S)")

        helpm = wx.Menu()
        self.mi_keys = helpm.Append(wx.ID_ANY, "键盘快捷键…\tShift+F1",
                                    "列出全部键盘操作，便于不用鼠标完成操作")
        helpm.Append(wx.ID_ABOUT, "关于\tF1")
        bar.Append(helpm, "帮助(&H)")

        self.SetMenuBar(bar)

        self.Bind(wx.EVT_MENU, lambda e: self._on_relogin(None), self.mi_login)
        self.Bind(wx.EVT_MENU, lambda e: self._on_refresh_login(None), self.mi_refresh)
        self.Bind(wx.EVT_MENU, lambda e: self._on_account_info(None), self.mi_account_info)
        self.Bind(wx.EVT_MENU, lambda e: self.Close(), id=wx.ID_EXIT)
        self.Bind(wx.EVT_MENU, lambda e: self._on_refresh_list(None), id=wx.ID_REFRESH)
        self.Bind(wx.EVT_MENU, lambda e: self._on_about(None), id=wx.ID_ABOUT)
        self.Bind(wx.EVT_MENU, self._on_agent, self.mi_agent)
        self.Bind(wx.EVT_MENU, lambda e: self._speak_text(self.chat.last_assistant()),
                  self.mi_speak)
        self.Bind(wx.EVT_MENU, lambda e: tts.stop(), self.mi_shut)
        self.Bind(wx.EVT_MENU, self._on_keys, self.mi_keys)
        self.Bind(wx.EVT_MENU, self._on_packages, self.mi_packages)
        self.Bind(wx.EVT_MENU, self._on_mirror, self.mi_mirror)

    # -------------------------------------------------------------------- UI
    def _build_ui(self):
        """全部使用标准控件；分隔线用 wx.StaticLine。

        控件统一放在一个 wx.Panel 里：wx 的 Tab 遍历（wxControlContainer）
        只挂在 Panel / Dialog 上，直接挂在 Frame 上时不生效——多行编辑框里的
        Tab 就完全没反应。这是 wx 自己的机制，不需要我们处理任何按键。

        每个需要名称的控件前面都放一个可见 wx.StaticText，并保证两者在
        **创建顺序上紧邻**——这是原生控件唯一能被读屏读到的命名方式
        （SetName / SetToolTip 实测无效，见文件头说明）。
        """
        # Panel 自带 wx.TAB_TRAVERSAL，Tab / Shift+Tab 走它自己的遍历顺序
        p = self.panel = wx.Panel(self)
        p.SetBackgroundColour(C_BG)
        outer = wx.BoxSizer(wx.VERTICAL)

        # 顶栏
        tsz = wx.BoxSizer(wx.HORIZONTAL)
        self.lbl_title = wx.StaticText(p, label="豆包")
        self.lbl_title.SetFont(ui_font(13, True))
        self.lbl_title.SetForegroundColour(C_TEXT)
        tsz.Add(self.lbl_title, 0, wx.ALIGN_CENTER_VERTICAL | wx.LEFT | wx.TOP | wx.BOTTOM, 16)

        # 连接状态同时充当「实时播报区」（见 _set_status）
        self.lbl_status = wx.StaticText(p, label="正在连接…")
        self.lbl_status.SetFont(ui_font(9))
        self.lbl_status.SetForegroundColour(C_MUTED)
        tsz.Add(self.lbl_status, 1, wx.ALIGN_CENTER_VERTICAL | wx.LEFT, 14)
        outer.Add(tsz, 0, wx.EXPAND)
        outer.Add(wx.StaticLine(p), 0, wx.EXPAND)

        body = wx.BoxSizer(wx.HORIZONTAL)

        # 左侧：会话栏
        lsz = wx.BoxSizer(wx.VERTICAL)
        self.btn_new = wx.Button(p, label="＋  新对话(&N)")
        self.btn_new.SetFont(ui_font(10))
        self.btn_new.SetMinSize(wx.Size(-1, self.FromDIP(34)))
        self.btn_new.SetToolTip("开始一个新对话（Ctrl+N）")
        self.btn_new.Bind(wx.EVT_BUTTON, self._on_new_chat)
        lsz.Add(self.btn_new, 0, wx.EXPAND | wx.ALL, 12)

        # 会话列表的名称来自这个紧邻的标签
        self.lbl_hist = wx.StaticText(p, label="最近会话")
        self.lbl_hist.SetFont(ui_font(9, True))
        self.lbl_hist.SetForegroundColour(C_MUTED)
        lsz.Add(self.lbl_hist, 0, wx.LEFT | wx.BOTTOM, 14)

        # LB_EXTENDED = 原生多选（Ctrl 点选、Shift 连选、拖动框选、Ctrl+A 走下面的钩子），
        # 这样才能「选中若干会话 → 一起删」。多选列表的 GetSelection() 会触发
        # C++ 断言，读取当前行一律走 _conv_cursor_index()。
        self.list_conv = wx.ListBox(p, style=wx.LB_EXTENDED | wx.BORDER_SIMPLE)
        self.list_conv.SetBackgroundColour(C_SIDE)
        self.list_conv.SetFont(ui_font(10))
        self.list_conv.SetMinSize(wx.Size(self.FromDIP(232), -1))
        # 方向键只改变选择，回车/双击才打开会话：避免浏览列表时上下文被自动换掉
        # （WCAG 3.2.2 On Input）
        self.list_conv.Bind(wx.EVT_LISTBOX, self._on_conv_select)
        self.list_conv.Bind(wx.EVT_LISTBOX_DCLICK, self._on_pick_conv)
        self.list_conv.Bind(wx.EVT_CONTEXT_MENU, self._on_conv_menu)
        lsz.Add(self.list_conv, 1, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, 8)

        self.lbl_account = wx.StaticText(p, label="未登录")
        self.lbl_account.SetFont(ui_font(9))
        self.lbl_account.SetForegroundColour(C_MUTED)
        lsz.Add(self.lbl_account, 0, wx.LEFT | wx.RIGHT | wx.TOP, 12)

        body.Add(lsz, 0, wx.EXPAND)
        body.Add(wx.StaticLine(p, style=wx.LI_VERTICAL), 0, wx.EXPAND)

        # 右侧：消息 + 输入
        rsz = wx.BoxSizer(wx.VERTICAL)

        # 消息列表的名称来自这个紧邻的标签
        self.lbl_chat = wx.StaticText(p, label="对话内容")
        self.lbl_chat.SetFont(ui_font(9, True))
        self.lbl_chat.SetForegroundColour(C_MUTED)
        rsz.Add(self.lbl_chat, 0, wx.LEFT | wx.TOP, 14)

        self.chat = ChatView(p)
        self.chat.SetFont(ui_font(10))
        # 消息区右键「点赞 / 点踩」交给这里发请求
        self.chat.on_feedback = self._send_feedback
        # 详情窗口里的「运行代码」也走同一条确认流程
        self.chat.on_run = self._run_text
        self.chat.on_speak = self._speak_text
        self.chat.on_speak_stop = tts.stop
        # 自动朗读的两个开关存在 config.json 里，下次启动恢复
        self.chat.set_speak_switches(load_config().get("speak_switches"))
        self.chat.on_speak_switch_changed = self._save_speak_switches
        rsz.Add(self.chat, 1, wx.EXPAND)
        rsz.Add(wx.StaticLine(p), 0, wx.EXPAND)

        row = wx.BoxSizer(wx.HORIZONTAL)

        # 输入框的名称来自这个紧邻的标签
        self.lbl_input = wx.StaticText(p, label="消息输入")
        self.lbl_input.SetFont(ui_font(9, True))
        self.lbl_input.SetForegroundColour(C_MUTED)

        self.input = wx.TextCtrl(p, style=wx.TE_MULTILINE | wx.BORDER_SIMPLE)
        self.input.SetFont(ui_font(10))
        self.input.SetBackgroundColour(C_WHITE)
        self.input.SetMinSize(wx.Size(-1, self.FromDIP(66)))
        self.input.SetToolTip("Enter 发送，Shift+Enter 换行，"
                              "Ctrl+V 粘贴图片随消息发送（帮助 → 键盘快捷键）")
        self.input.Bind(wx.EVT_KEY_DOWN, self._on_input_key)
        self.input.Bind(wx.EVT_TEXT, lambda e: self._refresh_buttons())

        # 待发送图片（Ctrl+V 粘贴后在这里亮出来，回车随消息一起发）
        self._paste_image: str | None = None
        self.lbl_attach = wx.StaticText(p, label="")
        self.lbl_attach.SetFont(ui_font(9))
        self.lbl_attach.SetForegroundColour(C_MUTED)

        input_col = wx.BoxSizer(wx.VERTICAL)
        input_col.Add(self.lbl_input, 0, wx.BOTTOM, self.FromDIP(4))
        input_col.Add(self.input, 1, wx.EXPAND)
        input_col.Add(self.lbl_attach, 0, wx.TOP, self.FromDIP(2))
        row.Add(input_col, 1, wx.EXPAND | wx.LEFT | wx.TOP | wx.BOTTOM, 16)

        btns = wx.BoxSizer(wx.VERTICAL)
        btns.AddStretchSpacer()
        self.btn_send = wx.Button(p, label="发送(&S)")
        self.btn_send.SetFont(ui_font(10))
        self.btn_send.SetMinSize(wx.Size(self.FromDIP(96), -1))
        self.btn_send.SetToolTip("发送输入的消息（Enter）")
        self.btn_send.Bind(wx.EVT_BUTTON, self._on_send)
        btns.Add(self.btn_send, 0, wx.BOTTOM, 6)

        self.btn_stop = wx.Button(p, label="停止(&T)")
        self.btn_stop.SetFont(ui_font(10))
        self.btn_stop.SetMinSize(wx.Size(self.FromDIP(96), -1))
        self.btn_stop.SetToolTip("停止当前回复的生成（Esc）")
        self.btn_stop.Enable(False)
        self.btn_stop.Bind(wx.EVT_BUTTON, self._on_stop)
        btns.Add(self.btn_stop, 0, wx.BOTTOM, 14)
        row.Add(btns, 0, wx.LEFT | wx.RIGHT, 12)

        rsz.Add(row, 0, wx.EXPAND)
        body.Add(rsz, 1, wx.EXPAND)

        outer.Add(body, 1, wx.EXPAND)
        self.panel.SetSizer(outer)
        # Tab 顺序完全交给 wx 自己的遍历，控件创建顺序就是
        # 新对话 → 会话列表 → 消息区 → 输入框 → 发送 → 停止。
        self.chat.hint("正在读取登录状态…")
        self._refresh_buttons()
        self._apply_theme()
        self.input.SetFocus()

    # ------------------------------------------------------------- 启动认证
    def _check_login(self):
        """进客户端第一件事：找登录态，并逐个验证是否真的有效。"""
        self._set_status("正在检查登录态…")
        threading.Thread(target=self._auth_chain, daemon=True).start()

    def _auth_chain(self):
        """工作线程：浏览器现有会话 → 本地存储 → 本机浏览器 → 要求重新登录。

        顺序很关键。浏览器里的会话是唯一「可能被服务端轮换过」的活数据，
        先用它；拿本地存储去覆盖它会把本来能用的登录顶掉。
        """
        try:
            self.api.begin()
        except Exception as exc:  # noqa: BLE001
            wx.CallAfter(self._set_status, f"浏览器启动失败：{exc}", True)
            wx.CallAfter(self._prompt_login, "missing")
            return

        # 1) 浏览器里现成的会话
        try:
            profile = self.api.account()
        except Exception:  # noqa: BLE001  含 AuthError 与网络抖动
            profile = None
        if profile:
            self._persist_and_enter(profile, "浏览器会话")
            return

        # 2) 本地长期存储，其次本机浏览器数据库
        candidates: list[tuple[dict, str]] = []
        store = cookie_bridge.load_store()
        if store and cookie_bridge.is_logged_in(store):
            candidates.append((store, "本地存储"))
        try:
            other, source = cookie_bridge.extract_all()
            if other and cookie_bridge.is_logged_in(other):
                candidates.append((other, source))
        except Exception:  # noqa: BLE001
            pass

        for cookies, source in candidates:
            try:
                profile = self.api.use_cookies(cookies)
            except AuthError:
                if source == "本地存储":
                    cookie_bridge.clear_store()  # 明确失效，别下次再撞
                continue
            except Exception:  # noqa: BLE001
                continue
            self._persist_and_enter(profile, source)
            return

        wx.CallAfter(self._prompt_login, "expired" if candidates else "missing")

    def _persist_and_enter(self, profile, source):
        """校验通过：把当前真实生效的 cookie 落盘，再进主界面。"""
        try:
            live = self.api.current_cookies()
        except Exception:  # noqa: BLE001
            live = {}
        # 只存「确实是登录态」的 cookie：之前把未登录的空 cookie 写进来，
        # 反而把本地那份好的顶掉，下次启动就没登录态了。
        if live and cookie_bridge.is_logged_in(live):
            cookie_bridge.save_store(live)
        # 登录态已经拿到手，就不再需要那个给人看的浏览器窗口：
        # 立刻藏起来（在后台线程做，别让 CDP 往返卡住界面）。
        try:
            self.api.hide_browser()
        except Exception:  # noqa: BLE001
            pass
        self._enter_main(profile, source)

    def _enter_main(self, profile, source):
        """认证通过后的统一入口：拉会话列表并进入就绪状态。

        必须在后台线程调用：list_conversations 要等一次浏览器往返，
        放到界面线程会直接把窗口卡住（登录成功后最容易撞见）。

        刚登录成功那一下页面脚本还在跳转，会话接口有概率返回半截内容；
        异常要重试三次，仍然失败也要把原因说到状态栏，用户可以按 F5 再试。

        还有一种情况不会抛异常，但结果明显不全：浏览器冷启动时第一次拉
        会话列表可能只回来一两条（页面里的 IM 连接还没同步完），所以除了
        异常重试，结果少于两条也要再等一会儿重问，并保留条数最多的那次。
        """
        convs: list[dict] = []
        err = ""
        wx.CallAfter(self._set_status, "正在读取会话列表…")
        for attempt in range(3):
            delay = 1.5 * (attempt + 1)
            try:
                got = self.api.list_conversations()
                err = ""
                if len(got) > len(convs):
                    convs = got
                if len(convs) >= 2:
                    break
            except Exception as exc:  # noqa: BLE001
                err = str(exc)
            if attempt < 2:
                time.sleep(delay)
        wx.CallAfter(self._on_ready, profile, convs, source, err)

    def _prompt_login(self, reason="missing", current_uid=""):
        """没有可用登录态时唯一的出口：弹对话框，登录成功再验一次。"""
        self._set_status("未登录，需要重新登录", error=True)
        dlg = LoginDialog(self, reason, api=self.api, current_uid=current_uid)
        ok = dlg.ShowModal()
        profile, cookies, source = dlg.profile, dlg.cookies, dlg.source
        dlg.Destroy()
        if ok != wx.ID_OK:
            self.Close()
            return
        # 收起登录窗口放在 _persist_and_enter 里做：那里在后台线程，不会卡界面
        if profile:
            # 落盘 + 拉会话列表都要走浏览器，不能压在界面线程上
            threading.Thread(target=self._persist_and_enter,
                             args=(profile, source), daemon=True).start()
        else:
            self._verify_login(cookies, source)

    def _verify_login(self, cookies, source):
        """兜底：登录框没能带回账号信息时，再让服务端认一次。"""
        self._set_status("正在验证登录态…")

        def work():
            try:
                profile = self.api.use_cookies(cookies)
            except Exception as exc:  # noqa: BLE001
                wx.CallAfter(self._on_verify_failed, str(exc))
                return
            self._persist_and_enter(profile, source)

        threading.Thread(target=work, daemon=True).start()

    def _on_verify_failed(self, msg):
        """验证失败：默认回到登录框；连续失败多次则停止自动重试，避免死循环。"""
        self._auth_fail_count += 1
        self._set_status(f"登录态验证未通过：{msg}", True)
        if self._auth_fail_count >= 3:
            wx.MessageBox(
                f"登录态验证连续失败：{msg}\n\n"
                "请先确认网络可用、浏览器里已登录豆包，再从「账号 → 登录 / 切换账号」重试。",
                "登录失败", wx.OK | wx.ICON_ERROR, self)
            return
        wx.MessageBox(f"登录态验证未通过：{msg}\n\n请重新登录。", "登录失败",
                      wx.OK | wx.ICON_ERROR, self)
        self._prompt_login("expired")

    def _apply_theme(self, evt=None):
        """高对比度模式下改用系统颜色，避免自定义配色覆盖用户设置。"""
        self.hc = is_high_contrast()
        headings = (self.lbl_title,)
        secondary = (self.lbl_status, self.lbl_hist, self.lbl_chat,
                     self.lbl_input, self.lbl_account)
        if not self.hc:
            self.SetBackgroundColour(C_BG)
            self.panel.SetBackgroundColour(C_BG)
            for w in headings + secondary:
                w.SetBackgroundColour(C_BG)
            self.lbl_title.SetForegroundColour(C_TEXT)
            for w in secondary:
                w.SetForegroundColour(C_MUTED)
            self.list_conv.SetBackgroundColour(C_SIDE)
            self.list_conv.SetForegroundColour(C_TEXT)
            self.chat.apply_theme(C_TEXT, C_WHITE, C_MUTED)
            self.input.SetBackgroundColour(C_WHITE)
            self.input.SetForegroundColour(C_TEXT)
        else:
            bg = wx.SystemSettings.GetColour(wx.SYS_COLOUR_WINDOW)
            fg = wx.SystemSettings.GetColour(wx.SYS_COLOUR_WINDOWTEXT)
            self.SetBackgroundColour(bg)
            self.panel.SetBackgroundColour(bg)
            for w in headings + secondary:
                w.SetBackgroundColour(bg)
                w.SetForegroundColour(fg)
            self.list_conv.SetBackgroundColour(bg)
            self.list_conv.SetForegroundColour(fg)
            self.chat.apply_theme(
                fg, bg, wx.SystemSettings.GetColour(wx.SYS_COLOUR_GRAYTEXT))
            self.input.SetBackgroundColour(bg)
            self.input.SetForegroundColour(fg)
        self.Refresh()
        if evt is not None:
            evt.Skip()

    def _account_info_text(self, profile=None):
        """账号信息的展示文本；只有点「账号 → 账号信息」时才会被拿出来用。"""
        p = (self.api.profile or {}) if profile is None else profile
        if p and p.get("uid"):
            return (f"昵称：{p.get('name') or '—'}\n"
                    f"UID：{p.get('uid') or '—'}\n"
                    f"手机：{p.get('mobile') or '—'}")
        return INFO_UNLOGGED

    def _on_ready(self, profile, convs, source="", err=""):
        name = profile.get("name") or "已登录"
        self._auth_fail_count = 0
        # 界面以这里拿到的 profile 为准，避免「已显示账号、菜单却说未登录」
        self.api.profile = dict(profile or {})
        self.lbl_account.SetLabel(f"{name} · 已连接")
        self.SetTitle(f"豆包客户端 — {name}")
        base = f"已连接 · {name}" + (f" · 登录态来自{source}，已加密保存" if source else "")
        self._set_conversations(convs)
        if err:
            # 列表没读出来必须说清楚：只留一个空列表，用户会以为「没有会话」
            self._set_status(f"{base}；会话列表加载失败：{err}（按 F5 重试）", error=True)
        elif not convs:
            self._set_status(f"{base}；账号下暂时没有会话", alert=True)
        else:
            self._set_status(f"{base} · {len(convs)} 个会话", alert=True)
        if convs and not self.chat.has_message():
            # 默认直接进入最新一轮对话（列表按最近活跃排序，第一条就是最新）
            self._open_conv(0)
        elif self.chat.is_empty():
            self.chat.clear()
            self.chat.hint("从左侧选择一个会话，或点「新对话」开始聊天")
        self.input.SetFocus()

    def _set_status(self, text, error=False, alert=False):
        """状态栏同时承担「实时播报」职责。

        读屏不会自己轮询静态文本的变化，必须主动发 MSAA 事件；error/alert
        时再补一个 EVENT_SYSTEM_ALERT，等价于 ARIA 的 role=alert。
        """
        label = f"错误：{text}" if error else text
        if self.lbl_status.GetLabel() != label:
            self.lbl_status.SetLabel(label)
            self.Layout()
        if self.hc:
            self.lbl_status.SetForegroundColour(
                wx.SystemSettings.GetColour(wx.SYS_COLOUR_WINDOWTEXT))
        else:
            self.lbl_status.SetForegroundColour(C_ERR if error else C_MUTED)
        a11y.notify(self.lbl_status, alert=error or alert)

    # ------------------------------------------------------- 账号菜单动作
    def _reset_backend(self):
        """断开当前浏览器连接与界面状态，准备换一份登录态。

        close() 会等端口真正释放，紧接着的 launch 才不会连回正在退出的旧实例。
        """
        try:
            self.api.close()
        except Exception:  # noqa: BLE001
            pass
        self.api.profile = {}
        self.conversations = []
        self.current_conv = "0"
        self._conv_cursor = -1
        self._conv_prev_sel = set()
        self.list_conv.Clear()
        self.chat.clear()
        self.chat.hint("正在等待登录…")
        self.lbl_account.SetLabel("未登录")
        self.SetTitle("豆包客户端")
        self._refresh_buttons()

    def _on_relogin(self, _evt):
        """主动登录 / 切换账号：清空当前状态后走标准登录 + 校验流程。"""
        if self.streaming:
            wx.MessageBox("正在生成回复，请先停止。", "提示", wx.OK | wx.ICON_INFORMATION, self)
            return
        # 切换账号必须记住旧账号：浏览器里还是它的话不能算登录成功
        old_uid = (self.api.profile or {}).get("uid", "")
        self._auth_fail_count = 0
        self._reset_backend()
        self._prompt_login("switch", old_uid)

    def _on_refresh_login(self, _evt):
        """重新走一遍「本地存储 → 浏览器」的检查流程。"""
        if self.streaming:
            return
        self._auth_fail_count = 0
        self._check_login()

    def _on_account_info(self, _evt):
        """「账号 → 账号信息」：点菜单项才弹，弹出来是一个只读多行编辑框。

        放在菜单里而不是常驻界面上：个人信息是低频查阅内容，常驻会一直占着
        左栏；但也不能是 MessageBox——那种弹窗不能换行、不能选中复制。
        所以这里用一般的对话框，里面放只读多行 wx.TextCtrl。
        """
        logged = bool((self.api.profile or {}).get("uid"))
        dlg = AccountDialog(self, self._account_info_text(), logged)
        try:
            dlg.ShowModal()
        finally:
            dlg.Destroy()
        self._set_status("已关闭账号信息" if logged else
                         "尚未登录：登录后这里显示昵称、UID 和绑定手机（Ctrl+L 登录）",
                         alert=logged)

    def _on_about(self, _evt):
        wx.MessageBox("豆包桌面客户端\n\n基于 wxPython + Chrome DevTools Protocol。\n"
                      "登录态从本机浏览器解密读取，请求经由真实浏览器发出。",
                      "关于", wx.OK | wx.ICON_INFORMATION, self)

    # ------------------------------------------------------- 会话列表
    # ------------------------------------------------------- 列表选择辅助
    # 列表是多选的（LB_EXTENDED），下面几个方法把「单选语义」包回来：
    # 打开会话、改名、置顶都只作用于一行，删除则可以作用于选中的多行。
    def _conv_sel_set(self) -> set:
        return set(self.list_conv.GetSelections())

    def _conv_cursor_index(self) -> int:
        """列表的「当前行」（光标所在行），没有选中时是 -1。"""
        sels = self._conv_sel_set()
        if self._conv_cursor in sels:
            return self._conv_cursor
        return max(sels) if sels else -1

    def _conv_selected_rows(self) -> list:
        """当前选中的行，从下到上排好（删多行时高下标先删才不串位）。"""
        return sorted(self._conv_sel_set(), reverse=True)

    def _conv_select_only(self, idx):
        """只选中第 idx 行（打开/高亮用）；idx 非法时清空选择。"""
        for i in list(self._conv_sel_set()):
            if i != idx:
                self.list_conv.Deselect(i)
        if 0 <= idx < self.list_conv.GetCount():
            self.list_conv.SetSelection(idx)
            self._conv_cursor = idx
            self._conv_prev_sel = {idx}
        else:
            self._conv_cursor = -1
            self._conv_prev_sel = set()

    def _conv_select_all(self):
        """全选：原生多选列表不处理 Ctrl+A，这里补上。"""
        n = self.list_conv.GetCount()
        for i in range(n):
            self.list_conv.SetSelection(i)
        if n:
            if not (0 <= self._conv_cursor < n):
                self._conv_cursor = 0
            self._conv_prev_sel = set(range(n))
            self._set_status(f"已全选 {n} 个会话，按 Delete 或右键可以一起删除",
                             alert=True)
        else:
            self._set_status("列表里还没有会话")

    def _conv_label(self, conv) -> str:
        """列表里那一行的文字：置顶标记 + 名字 + 未读条数。

        没用图标，是因为纯文本读屏会把表情念成「图钉」之类，反而碍事。
        """
        parts = []
        if conv.get("pinned"):
            parts.append("[置顶]")
        parts.append(conv.get("name") or "未命名对话")
        unread = int(conv.get("unread") or 0)
        if unread:
            parts.append(f"（{unread} 条未读）")
        return " ".join(parts)

    def _set_conversations(self, convs):
        """刷新列表时保住当前选中的会话，不要每次都跳回第一行。"""
        keep = self.current_conv
        for c in convs:
            # 用户刚置顶 / 取消置顶过的，以本地结果为准，别被旧缓存盖掉
            if c["id"] in self._pin_cache:
                c["pinned"] = self._pin_cache[c["id"]]
        self.conversations = convs
        # Clear() 会连带清空选择，先把「光标」标记作废，重建后再重新定位
        self._conv_cursor = -1
        self._conv_prev_sel = set()
        self.list_conv.Clear()
        for c in convs:
            self.list_conv.Append(self._conv_label(c))
        idx = next((i for i, c in enumerate(convs) if c["id"] == keep), -1)
        if idx >= 0:
            self._conv_select_only(idx)
        else:
            # 重新 Append 时原生列表会自动把第一行标成选中，这里清掉，
            # 否则「没打开任何会话」和「打开了第一条」看起来一样
            self._conv_select_only(-1)
        self._start_pin_probe(convs)

    def _start_pin_probe(self, convs):
        """后台补齐「哪些会话是置顶的」。

        ``recent_conv`` 不返回 pinned，只能一个个查 ``conversation/info``，
        所以放到后台做，查到一条就刷一条，不卡界面。
        """
        todo = [c for c in convs if c["id"] not in self._pin_cache][:30]
        if not todo:
            return

        def work():
            for c in todo:
                if self.streaming:
                    return
                try:
                    with self.lock:
                        info = self.api.conversation_info(c["id"])
                except Exception:  # noqa: BLE001
                    # 查不到就算了，列表照常用，不因为锦上添花的信息报错
                    self._pin_cache[c["id"]] = False
                    continue
                self._pin_cache[c["id"]] = info["pinned"]
                wx.CallAfter(self._apply_pin, c["id"], info["pinned"])

        threading.Thread(target=work, daemon=True).start()

    def _apply_pin(self, conv_id, pinned):
        """把后台查到的置顶状态刷到对应那一行。"""
        for i, c in enumerate(self.conversations):
            if c["id"] != conv_id:
                continue
            if bool(c.get("pinned")) == bool(pinned):
                return
            c["pinned"] = pinned
            if i < self.list_conv.GetCount():
                self.list_conv.SetString(i, self._conv_label(c))
            return

    def _refresh_conv_labels(self):
        for i, c in enumerate(self.conversations):
            if i < self.list_conv.GetCount():
                self.list_conv.SetString(i, self._conv_label(c))

    def _on_refresh_list(self, _evt):
        if self.streaming:
            return
        self._set_status("正在刷新会话列表…")

        def work():
            try:
                with self.lock:
                    convs = self.api.list_conversations()
                wx.CallAfter(self._set_conversations, convs)
                wx.CallAfter(self._set_status, f"共 {len(convs)} 个会话")
            except Exception as exc:  # noqa: BLE001
                wx.CallAfter(self._set_status, f"刷新失败：{exc}", True)

        threading.Thread(target=work, daemon=True).start()

    def _on_conv_select(self, _evt):
        """选择只播报，不加载。

        方向键浏览列表就切换整个消息区，会让读屏用户「按一下换一个上下文」，
        也不符合 WCAG 3.2.2（改变选择不应自动改变上下文）。
        """
        sels = self._conv_sel_set()
        added = sels - self._conv_prev_sel
        if added:
            self._conv_cursor = max(added)
        elif sels:
            self._conv_cursor = max(sels)
        else:
            self._conv_cursor = -1
        self._conv_prev_sel = sels

        if len(sels) > 1:
            # 多选时读屏需要知道「选了几条」，否则只听到最后一条的名字
            self._set_status(f"已选中 {len(sels)} 个会话，"
                             f"右键或按 Delete 可以一起删除")
            return
        idx = self._conv_cursor_index()
        if 0 <= idx < len(self.conversations):
            name = self.conversations[idx]["name"]
            self._set_status(f"已选择「{name}」，按回车或双击打开")

    def _on_pick_conv(self, _evt):
        idx = self._conv_cursor_index()
        if idx < 0 or idx >= len(self.conversations) or self.streaming:
            return
        self._open_conv(idx)

    def _open_conv(self, idx, auto=False):
        """打开列表里第 idx 个会话：选中它并在后台拉历史消息。

        会话列表按最近活跃排序，所以 idx=0 就是「最新一轮对话」，启动时
        直接走这里，用户不用先点一下才看到内容。
        """
        conv = self.conversations[idx]
        self.current_conv = conv["id"]
        self._conv_select_only(idx)
        self._set_status(f"正在打开：{conv['name']}")
        if self.chat.is_empty():
            # 启动时消息区还停在「正在读取登录状态…」，换成真实进展
            self.chat.clear()
            self.chat.hint(f"正在打开「{conv['name']}」…")
        threading.Thread(target=self._load_history,
                         args=(conv["id"], conv["name"]),
                         daemon=True).start()
        # 打开即已读：和网页端 behavior 一致，别的设备上的红点也会消掉。
        # 单独一个线程，失败不出声——这只是同步状态，不该挡住看消息。
        threading.Thread(target=self._mark_read_worker, args=(conv["id"],),
                         daemon=True).start()

    def _mark_read_worker(self, conv_id):
        try:
            with self.lock:
                self.api.mark_conversation_read(conv_id)
        except Exception:  # noqa: BLE001
            return
        wx.CallAfter(self._clear_unread, conv_id)

    def _clear_unread(self, conv_id):
        for i, c in enumerate(self.conversations):
            if c["id"] == conv_id and c.get("unread"):
                c["unread"] = 0
                if i < self.list_conv.GetCount():
                    self.list_conv.SetString(i, self._conv_label(c))
                return

    def _load_history(self, conv_id, name=""):
        try:
            with self.lock:
                msgs = self.api.load_messages(conv_id)
            wx.CallAfter(self._render_history, msgs, conv_id, name)
        except Exception as exc:  # noqa: BLE001
            wx.CallAfter(self._set_status, f"加载失败：{exc}", True)

    def _render_history(self, msgs, conv_id, name=""):
        if conv_id != self.current_conv:
            return
        self.chat.clear()
        if not msgs:
            self.chat.hint("这个会话还没有消息")
        for m in msgs:
            self.chat.append(m["role"], m["text"], m.get("id", ""))
        # 打开会话就停在最新一轮，用户不用先滚到底
        self.chat.scroll_end()
        head = f"「{name}」" if name else ""
        self._set_status(f"已打开{head}· 共 {len(msgs)} 条消息", alert=True)

    # ---------------------------------------------------- 会话右键菜单
    def _on_conv_menu(self, evt):
        """右键会话列表：打开 / 重命名 / 置顶 / 标记已读 / 复制 / 清上下文 / 删除。

        选中的是一行时按「单个会话」给菜单；选中多行（Ctrl 点选、Shift 连选或
        Ctrl+A 全选）时，只保留能批量做的项——复制标题和删除。
        """
        pos = evt.GetPosition()
        if pos != wx.DefaultPosition:
            hit = self.list_conv.HitTest(self.list_conv.ScreenToClient(pos))
            if isinstance(hit, (tuple, list)):
                hit = hit[0]
            if hit != wx.NOT_FOUND:
                # 右键点在没选中的行上：按平台惯例改成「只选这一行」，
                # 点在已选中的行上则保持多选，这样才有「选中一堆再右键删」的用法
                if hit not in self._conv_sel_set():
                    self._conv_select_only(hit)
                else:
                    self._conv_cursor = hit
        rows = self._conv_selected_rows()
        rows = [i for i in rows if 0 <= i < len(self.conversations)]
        if not rows:
            idx = self._conv_cursor_index()
            if not (0 <= idx < len(self.conversations)):
                return
            rows = [idx]
        multi = len(rows) > 1
        idx = rows[0]
        conv = self.conversations[idx]

        menu = wx.Menu()
        mi_all = menu.Append(wx.ID_ANY, "全选\tCtrl+A", "选中列表里的全部会话")
        menu.AppendSeparator()
        if not multi:
            mi_open = menu.Append(wx.ID_ANY, "打开这个会话")
            mi_rename = menu.Append(wx.ID_ANY, "重命名这个会话…")
            pinned = bool(conv.get("pinned"))
            mi_pin = menu.Append(wx.ID_ANY, "取消置顶" if pinned else "置顶这个会话")
            mi_read = menu.Append(wx.ID_ANY, "标记为已读")
            menu.AppendSeparator()
        mi_copy = menu.Append(wx.ID_ANY,
                              f"复制选中的 {len(rows)} 个标题" if multi else "复制会话标题")
        if not multi:
            mi_clear = menu.Append(wx.ID_ANY, "清除这个会话的上下文…")
            mi_clear.SetHelp("让模型忘掉之前的对话内容，历史消息还在")
        menu.AppendSeparator()
        mi_del = menu.Append(wx.ID_ANY,
                             f"删除选中的 {len(rows)} 个会话…" if multi
                             else "删除这个会话…")
        mi_del.SetHelp("从豆包账号里删除，无法恢复")

        menu.Bind(wx.EVT_MENU, lambda e: self._conv_select_all(), mi_all)
        if not multi:
            menu.Bind(wx.EVT_MENU, lambda e: self._open_conv(idx), mi_open)
            menu.Bind(wx.EVT_MENU, lambda e: self._rename_conv(idx), mi_rename)
            menu.Bind(wx.EVT_MENU, lambda e: self._toggle_pin(idx), mi_pin)
            menu.Bind(wx.EVT_MENU, lambda e: self._mark_read(idx), mi_read)
            menu.Bind(wx.EVT_MENU, lambda e: self._clear_context(idx), mi_clear)
        if multi:
            menu.Bind(wx.EVT_MENU,
                      lambda e: self._copy_titles(
                          [self.conversations[i]["name"] for i in rows]), mi_copy)
        else:
            menu.Bind(wx.EVT_MENU, lambda e: self._copy_title(conv["name"]), mi_copy)
        menu.Bind(wx.EVT_MENU, lambda e: self._delete_conv(rows), mi_del)
        try:
            self.PopupMenu(menu)
        finally:
            menu.Destroy()

    # ------------------------------------------------ 重命名 / 置顶 / 已读
    def _conv_index(self, conv_id):
        return next((i for i, c in enumerate(self.conversations)
                     if c["id"] == conv_id), -1)

    def _rename_current(self):
        idx = self._conv_index(self.current_conv)
        if idx < 0:
            self._set_status("先打开一个会话再改名", True)
            return
        self._rename_conv(idx)

    def _rename_conv(self, idx):
        """改名对话框用原生 TextEntryDialog：读屏能念标签，回车确认、Esc 取消。"""
        if self.streaming:
            self._set_status("正在生成回复，先「停止」再改名", True)
            return
        if not (0 <= idx < len(self.conversations)):
            self._set_status("这个会话已经不在了，按 F5 刷新列表", True)
            return
        conv = self.conversations[idx]
        cid, old = conv["id"], conv["name"]
        dlg = wx.TextEntryDialog(self, "新的会话名称：", "重命名会话", old)
        dlg.SetSize((420, 180))
        try:
            if dlg.ShowModal() != wx.ID_OK:
                self._set_status("已取消改名")
                return
            name = dlg.GetValue().strip()
        finally:
            dlg.Destroy()
        if not name:
            self._set_status("名字不能为空", True)
            return
        if name == old:
            self._set_status("名字没有变化")
            return

        self._set_status(f"正在改名：{name}")
        threading.Thread(target=self._rename_worker, args=(cid, name),
                         daemon=True).start()

    def _rename_worker(self, cid, name):
        try:
            with self.lock:
                self.api.rename_conversation(cid, name)
        except Exception as exc:  # noqa: BLE001
            wx.CallAfter(self._set_status, f"改名失败：{exc}", True)
            return
        wx.CallAfter(self._after_rename, cid, name)

    def _after_rename(self, cid, name):
        idx = self._conv_index(cid)
        if idx < 0:
            self._set_status(f"已改名为「{name}」", alert=True)
            return
        self.conversations[idx]["name"] = name
        self.list_conv.SetString(idx, self._conv_label(self.conversations[idx]))
        self._set_status(f"已改名为「{name}」", alert=True)

    def _toggle_pin(self, idx):
        """置顶 / 取消置顶。置顶是账号级状态，别的设备同步生效。"""
        if not (0 <= idx < len(self.conversations)):
            self._set_status("这个会话已经不在了，按 F5 刷新列表", True)
            return
        conv = self.conversations[idx]
        cid = conv["id"]
        target = not bool(conv.get("pinned"))
        self._set_status(f"正在{'置顶' if target else '取消置顶'}「{conv['name']}」…")
        threading.Thread(target=self._pin_worker, args=(cid, target),
                         daemon=True).start()

    def _pin_worker(self, cid, target):
        try:
            with self.lock:
                self.api.pin_conversation(cid, target)
        except Exception as exc:  # noqa: BLE001
            wx.CallAfter(self._set_status, f"置顶失败：{exc}", True)
            return
        self._pin_cache[cid] = target
        wx.CallAfter(self._after_pin, cid, target)

    def _after_pin(self, cid, target):
        idx = self._conv_index(cid)
        if idx >= 0:
            self.conversations[idx]["pinned"] = target
            self.list_conv.SetString(idx, self._conv_label(self.conversations[idx]))
        self._set_status("已置顶" if target else "已取消置顶", alert=True)

    def _mark_read(self, idx):
        if not (0 <= idx < len(self.conversations)):
            self._set_status("这个会话已经不在了，按 F5 刷新列表", True)
            return
        conv = self.conversations[idx]
        if not conv.get("unread"):
            self._set_status("这个会话本来就没有未读")
            return

        def work():
            try:
                with self.lock:
                    self.api.mark_conversation_read(conv["id"])
            except Exception as exc:  # noqa: BLE001
                wx.CallAfter(self._set_status, f"标记已读失败：{exc}", True)
                return
            wx.CallAfter(self._after_mark_read, conv["id"])

        self._set_status("正在标记已读…")
        threading.Thread(target=work, daemon=True).start()

    def _after_mark_read(self, cid):
        self._clear_unread(cid)
        self._set_status("已标记为已读", alert=True)

    # ------------------------------------------------ 清除上下文
    def _clear_context_current(self):
        idx = self._conv_index(self.current_conv)
        if idx < 0:
            self._set_status("先打开一个会话再清除上下文", True)
            return
        self._clear_context(idx)

    def _clear_context(self, idx):
        """清上下文：只让模型忘掉前文，历史消息不删。

        这是不可撤销的操作（模型真的会忘），所以先确认、默认停在「否」。
        """
        if self.streaming:
            self._set_status("正在生成回复，先「停止」再清除上下文", True)
            return
        if not (0 <= idx < len(self.conversations)):
            self._set_status("这个会话已经不在了，按 F5 刷新列表", True)
            return
        conv = self.conversations[idx]
        answer = wx.MessageBox(
            f"要清除「{conv['name']}」的上下文吗？\n\n"
            "清除后模型不再记得之前的对话内容，历史消息仍然留在列表里。",
            "清除上下文", wx.YES_NO | wx.NO_DEFAULT | wx.ICON_QUESTION, self)
        if answer != wx.YES:
            self._set_status("已取消")
            return
        self._set_status("正在清除上下文…")
        threading.Thread(target=self._clear_context_worker,
                         args=(conv["id"], conv["name"]), daemon=True).start()

    def _clear_context_worker(self, cid, name):
        try:
            with self.lock:
                section = self.api.clear_context(cid)
        except Exception as exc:  # noqa: BLE001
            wx.CallAfter(self._set_status, f"清除上下文失败：{exc}", True)
            return
        wx.CallAfter(self._set_status,
                     f"已清除「{name}」的上下文" +
                     (f"（新分支 {section}）" if section else ""), True)

    # ------------------------------------------------ 消息反馈（赞 / 踩）
    FEEDBACK_TEXT = {0: "已取消反馈", 1: "已点赞", 2: "已点踩"}

    def _send_feedback(self, message_id, feedback_type):
        """消息区右键「赞 / 踩」的回调。

        当前会话是 ``0``（刚新建还没保存到账号）时没有可反馈的目标，
        直接说明原因，不发无意义的请求。
        """
        cid = self.current_conv
        if not cid or cid == "0":
            self._set_status("这条回复还没存进会话，没法反馈", True)
            return
        self._set_status("正在提交反馈…")
        threading.Thread(target=self._feedback_worker,
                         args=(cid, message_id, feedback_type),
                         daemon=True).start()

    def _feedback_worker(self, cid, mid, feedback_type):
        try:
            with self.lock:
                self.api.send_feedback(cid, mid, feedback_type)
        except Exception as exc:  # noqa: BLE001
            wx.CallAfter(self._set_status, f"反馈失败：{exc}", True)
            return
        wx.CallAfter(self._set_status,
                     self.FEEDBACK_TEXT.get(feedback_type, "已反馈"), True)

    def _copy_title(self, name):
        if wx.TheClipboard.Open():
            wx.TheClipboard.SetData(wx.TextDataObject(name))
            wx.TheClipboard.Close()
            self._set_status("会话标题已复制")

    def _copy_titles(self, names):
        if wx.TheClipboard.Open():
            wx.TheClipboard.SetData(wx.TextDataObject("\n".join(names)))
            wx.TheClipboard.Close()
            self._set_status(f"已复制 {len(names)} 个会话标题")

    # ------------------------------------------------------------ 删除会话
    def _delete_conv(self, target):
        """删除会话：``target`` 可以是单个下标，也可以是下标列表。

        多选删除（Ctrl 点选 / Shift 连选 / Ctrl+A 全选）走同一入口，
        一次确认、一次请求，删完再重新拉一遍列表。
        """
        if self.streaming:
            self._set_status("正在生成回复，先「停止」再删除", True)
            return
        rows = [target] if isinstance(target, int) else list(target or [])
        # 列表刚被刷新过时可能拿到过期下标，宁可什么都不做，
        # 也不能让 -1 落到 conversations[-1] 上误删最后一条
        rows = sorted({i for i in rows if isinstance(i, int)
                       and 0 <= i < len(self.conversations)}, reverse=True)
        if not rows:
            self._set_status("这些会话已经不在了，按 F5 刷新列表", True)
            return

        convs = [self.conversations[i] for i in rows]
        ids = [c["id"] for c in convs]
        names = [c["name"] for c in convs]
        if len(ids) == 1:
            text = (f"要删除「{names[0]}」吗？\n\n"
                    "会话会从豆包账号里一起删掉，删了没有撤销。")
        else:
            preview = "、".join(f"「{n}」" for n in names[:3])
            if len(names) > 3:
                preview += f" 等 {len(names)} 个"
            text = (f"要删除选中的 {len(ids)} 个会话吗？\n\n{preview}\n\n"
                    "它们会从豆包账号里一起删掉，删了没有撤销。")
        answer = wx.MessageBox(text, "删除会话",
                               wx.YES_NO | wx.NO_DEFAULT | wx.ICON_WARNING, self)
        if answer != wx.YES:
            self._set_status("已取消删除")
            return

        head = f"「{names[0]}」" if len(ids) == 1 else f"这 {len(ids)} 个会话"
        self._set_status(f"正在删除{head}…")
        threading.Thread(target=self._delete_worker, args=(ids, names),
                         daemon=True).start()

    def _delete_worker(self, ids, names):
        try:
            with self.lock:
                self.api.delete_conversations(ids)
        except Exception as exc:  # noqa: BLE001
            wx.CallAfter(self._set_status, f"删除失败：{exc}", True)
            return

        # 删完必须重新拉一次列表：本地摘行只是乐观更新，服务端的排序、
        # 未读数、剩下的条目都可能变；而且删到只剩几条时，列表要能补上
        # 原本被 limit 截掉的会话。拉不到就退回本地摘行，不让界面卡住。
        convs = None
        err = ""
        pending = set(ids)
        for attempt in range(3):
            try:
                with self.lock:
                    got = self.api.list_conversations()
                convs = got
                left = pending & {c["id"] for c in got}
                if not left:
                    err = ""
                    break
                err = "服务端还没同步"
                time.sleep(1.0)
            except Exception as exc:  # noqa: BLE001
                err = str(exc)
                time.sleep(1.0)
        wx.CallAfter(self._after_delete, ids, names, convs, err)

    def _after_delete(self, ids, names, convs, err=""):
        gone = set(ids)
        was_current = self.current_conv in gone
        if was_current:
            self.current_conv = "0"
        if convs is not None:
            self._set_conversations(convs)
        else:
            # 列表拉不回来：先把删掉的从本地摘掉，用户按 F5 还能再同步
            self.conversations = [c for c in self.conversations
                                  if c["id"] not in gone]
            self._set_conversations(self.conversations)
        if was_current:
            self._conv_select_only(-1)
            self.chat.clear()
            if self.conversations:
                # 删掉的正好是正在看的这条：直接打开最新的那条并把消息拉回来，
                # 别把消息区留成一块空白（和启动时「默认进最新一轮」一致）
                self._open_conv(0)
            else:
                self.chat.hint("这个会话已删除，列表里已经没有会话了")
        head = f"「{names[0]}」" if len(names) == 1 else f"{len(names)} 个会话"
        tail = f"，共 {len(self.conversations)} 个会话"
        if was_current and self.conversations:
            tail += f"，已为你打开「{self.conversations[0]['name']}」"
        if err:
            tail += "（列表可能还没同步，F5 可以再刷）"
        self._set_status(f"已删除{head}{tail}", alert=True)

    def _on_select_all(self, _evt):
        self.list_conv.SetFocus()
        self._conv_select_all()

    def _on_delete_selected(self, _evt):
        rows = self._conv_selected_rows()
        if not rows:
            self._set_status("先在左侧列表里选中要删除的会话", True)
            return
        self._delete_conv(rows)

    def _on_new_chat(self, _evt):
        if self.streaming:
            return
        self.current_conv = "0"
        self._conv_select_only(-1)
        self.chat.clear()
        self.chat.hint("开始一个新对话吧")
        self.input.SetFocus()
        self._set_status("已新建对话")

    # --------------------------------------------------------- 渲染消息
    def _write_hint(self, text):
        self.chat.clear()
        self.chat.hint(text)

    def _append_message(self, role, text, stream=False):
        self.chat.append(role, text)
        # 开关开着就念：自己发的和豆包回复各看各的开关，判定只有 chat 那一份
        self.chat.auto_speak(role, text)

    def _append_delta(self, text):
        self.chat.append_delta(text)

    # ----------------------------------------------------------- 发送
    def _refresh_buttons(self):
        has_text = bool(self.input.GetValue().strip()) or bool(self._paste_image)
        self.btn_send.Enable(has_text and not self.streaming)
        self.btn_stop.Enable(self.streaming)
        self.btn_new.Enable(not self.streaming)

    # --------------------------------------------------- Ctrl+V 粘贴图片
    @staticmethod
    def _is_paste_key(evt) -> bool:
        return (evt.ControlDown() and not evt.ShiftDown() and not evt.AltDown()
                and evt.GetKeyCode() in (ord("V"), ord("v")))

    def _try_paste_image(self) -> bool:
        """剪贴板里是图片就存下来待发送。返回 True 表示接管了这次粘贴。"""
        path = grab_clipboard_image()
        if not path:
            return False
        self._set_paste_image(path)
        self._set_status(f"已粘贴图片 {os.path.basename(path)}，回车随消息发送")
        return True

    def _set_paste_image(self, path: str | None):
        self._paste_image = path
        if path:
            self.lbl_attach.SetLabel(
                f"已附图片：{os.path.basename(path)}（回车发送；Esc 清除）")
        else:
            self.lbl_attach.SetLabel("")
        self._refresh_buttons()

    def _on_input_key(self, evt):
        key = evt.GetKeyCode()
        if key == wx.WXK_RETURN and not evt.ShiftDown():
            self._on_send(None)
            return
        # 剪贴板里是图片：接管 Ctrl+V，别让控件把它当空文本粘贴。
        # 是纯文本就 Skip()，照常走原生粘贴。
        if self._is_paste_key(evt) and self._try_paste_image():
            return
        evt.Skip()

    def _on_char_hook(self, evt):
        """键盘快捷键兜底层。

        原生控件会先在自己的 WndProc 里处理按键，NVDA/JAWS 还有全局键盘钩子，
        所以 EVT_KEY_DOWN 常常收不到；EVT_CHAR_HOOK 在控件处理之前触发。
        """
        key = evt.GetKeyCode()
        focus = wx.Window.FindFocus()

        # Tab / Shift+Tab 不在这里拦：没有 wx.TE_PROCESS_TAB 的控件由 wx 自己
        # 转成焦点遍历，按键照常往下传即可。
        if key == wx.WXK_F6:
            self._cycle_focus(evt.ShiftDown())
            return
        if key == wx.WXK_ESCAPE and self.streaming:
            self._on_stop(None)
            return
        if key == wx.WXK_ESCAPE and self._paste_image and focus is self.input:
            self._set_paste_image(None)
            self._set_status("已清除待发送的图片")
            return
        # Ctrl+V 也放这里兜一层：原生控件常把按键先吃掉（见上），EVT_KEY_DOWN
        # 收不到时这里仍能接管图片粘贴。没图时不拦截，文本粘贴照常。
        if self._is_paste_key(evt) and focus is self.input and self._try_paste_image():
            return
        if key in (wx.WXK_RETURN, wx.WXK_NUMPAD_ENTER):
            # 原生 ListBox 不保证把回车转成语义事件，这里兜一层
            if focus is self.list_conv:
                self._on_pick_conv(None)
                return
        if key == wx.WXK_F2 and focus is self.list_conv:
            # 资源管理器里 F2 就是改名，列表里沿用这个习惯
            idx = self._conv_cursor_index()
            if 0 <= idx < len(self.conversations):
                self._rename_conv(idx)
                return
        if focus is self.list_conv:
            # 这两项原生多选列表不管：Ctrl+A 不响应，Delete 也不删。
            # 其余方向键、Shift/Ctrl 点选、拖动框选都由控件自己处理。
            sels = self._conv_sel_set()
            if evt.ControlDown() and key in (ord("A"), ord("a"), 1):
                self._conv_select_all()
                return
            if key == wx.WXK_DELETE and sels:
                self._delete_conv(self._conv_selected_rows())
                return
        # 以下快捷键原先是菜单加速键；对应菜单项已按「菜单不与主界面重复」
        # 原则移除，改到这里统一处理，键盘可达性保持不变。
        # key == 1/14 是部分输入法下 Ctrl+字母上报的控制字符。
        if evt.ControlDown() and not evt.AltDown():
            if not evt.ShiftDown() and key in (ord("N"), ord("n"), 14):
                self._on_new_chat(None)
                return
            if evt.ShiftDown() and key in (ord("C"), ord("c")):
                self._on_copy_message(None)
                return
            if evt.ShiftDown() and key in (ord("R"), ord("r"), 18):
                self._on_run_code(None)
                return
            if key == wx.WXK_F2:
                self._rename_current()
                return
        evt.Skip()

    def _cycle_focus(self, backward=False):
        """F6 / Shift+F6 在四个主区域之间切换，不依赖鼠标。"""
        order = [self.list_conv, self.chat, self.input, self.btn_new]
        if self.streaming:
            order.append(self.btn_stop)
        order.append(self.btn_send)
        order = [w for w in order if w.IsShown() and w.IsEnabled()]
        if not order:
            return
        focus = wx.Window.FindFocus()
        if focus not in order and focus is not None and focus.GetParent() is self.chat:
            # 消息区的焦点实际落在内部画布上，归一化到消息区本身
            focus = self.chat
        idx = order.index(focus) if focus in order else -1
        step = -1 if backward else 1
        order[(idx + step) % len(order)].SetFocus()

    def _on_send(self, _evt):
        text = self.input.GetValue().strip()
        image = self._paste_image
        if self.streaming:
            return
        if not text and not image:
            # 明确反馈，避免读屏用户按了回车却不知道发生了什么
            self._set_status("消息为空，未发送", alert=True)
            return
        self.input.SetValue("")
        self._set_paste_image(None)
        if self.chat.is_empty():
            self.chat.clear()  # 清掉占位提示，避免和真实消息混在一起
        shown = text or "（只发了一张图片）"
        if image:
            shown += f"\n[图片] {os.path.basename(image)}"
        self._append_message("user", shown)
        self.streaming = True
        self._refresh_buttons()
        self._set_status("正在上传图片…" if image else "正在生成…")
        threading.Thread(target=self._run_stream,
                         args=(text, self.current_conv, image),
                         daemon=True).start()

    def _run_stream(self, text, conv_id, image_path=None):
        """发消息并逐段渲染。

        这里**不再**用 ``self.lock`` 把整个生成过程包起来：一轮回复可能跑几十秒
        到几分钟，包住它等于在生成期间冻结所有其它请求（刷新列表、点开别的会话、
        改名都要排队），而桥接层现在已经能安全并发。``current_conv`` 只是单个
        字符串赋值，本身是原子的，不需要锁。

        带图时先用 api.upload_image 上传拿 uri（和 AI Agent 走的是同一套上传
        代码），再 chat_with_image 提问。
        """
        error = ""
        try:
            if image_path:
                try:
                    info = self.api.upload_image(image_path)
                except Exception as exc:  # noqa: BLE001
                    wx.CallAfter(self._append_delta, f"\n[错误] 图片上传失败：{exc}")
                    error = str(exc)
                    return
                wx.CallAfter(self._set_status, "图片已上传，正在生成…")
                source = self.api.chat_with_image(text, info["uri"], conv_id)
            else:
                source = self.api.stream_chat(text, conv_id)
            for kind, payload in source:
                if kind == "delta":
                    wx.CallAfter(self._append_delta, payload)
                elif kind == "conv":
                    self.current_conv = payload
                elif kind == "error":
                    error = payload
                    wx.CallAfter(self._append_delta, f"\n[错误] {payload}")
                    break
                elif kind == "done":
                    break
        except Exception as exc:  # noqa: BLE001
            error = str(exc)
            wx.CallAfter(self._append_delta, f"\n[异常] {exc}")
        finally:
            wx.CallAfter(self._stream_finished, error)

    def _stream_finished(self, error=""):
        self.streaming = False
        self._refresh_buttons()
        if error:
            self._set_status(f"生成失败：{error}", error=True)
        else:
            # 读屏用户需要知道「生成结束了」，否则会一直等着
            self._set_status("回复完成", alert=True)
            # 流式回复是一点点拼出来的，只有收尾时才是完整一段，这时候念
            self.chat.auto_speak("assistant", self.chat.last_assistant())
        threading.Thread(target=self._refresh_conversations_bg, daemon=True).start()

    def _refresh_conversations_bg(self):
        try:
            with self.lock:
                convs = self.api.list_conversations()
            wx.CallAfter(self._set_conversations, convs)
        except Exception:  # noqa: BLE001
            pass

    def _on_stop(self, _evt):
        if not self.streaming:
            return
        try:
            self.api.stop()
        except Exception:  # noqa: BLE001
            pass
        self._set_status("已停止", alert=True)

    def _on_copy_message(self, _evt):
        if self.chat.copy_last():
            self._set_status("已复制最后一条消息", alert=True)
        else:
            self._set_status("没有可复制的消息", True)

    # -------------------------------------------------------- 本机执行
    def _dialog_colors(self):
        """弹窗跟着主窗口的配色走；高对比度时一律用系统色，不自己配色。"""
        if getattr(self, "hc", False):
            return (wx.SystemSettings.GetColour(wx.SYS_COLOUR_WINDOWTEXT),
                    wx.SystemSettings.GetColour(wx.SYS_COLOUR_WINDOW))
        return C_TEXT, C_WHITE

    def _default_workdir(self) -> str:
        """脚本落地目录：优先程序旁的 _work/run。

        装到 Program Files 时那个目录通常不可写，这时退到用户自己的
        LOCALAPPDATA——换台电脑、换个账户都不会因为权限跑不起来。
        """
        stamp = time.strftime("%Y%m%d_%H%M%S")
        if os.access(APP_DIR, os.W_OK):
            base = os.path.join(APP_DIR, "_work", "run")
        else:
            base = os.path.join(
                os.environ.get("LOCALAPPDATA") or os.path.expanduser("~"),
                "doubao_client", "run")
        return os.path.join(base, f"task_{stamp}")

    def _run_text(self, text):
        """详情窗口点了「运行代码」：拿这条消息的原文走同一套确认流程。"""
        self._confirm_run(text)

    def _speak_text(self, text: str):
        """朗读一段文字（消息右键、对话菜单共用）。"""
        body = (text or "").strip()
        if not body:
            self._set_status("没有可朗读的内容（先打开一个会话）", error=True)
            return
        if not tts.available():
            self._set_status(tts.error() or "这台机器没有可用的语音合成引擎", error=True)
            return
        if tts.speak(body):
            self._set_status(
                f"正在朗读（{tts.backend()}，对话 → 停止朗读 可中断）", alert=True)
        else:
            self._set_status(tts.error() or "朗读启动失败", error=True)

    def _save_speak_switches(self, values: dict):
        """自动朗读开关变了就写进 config.json，下次启动照旧生效。"""
        update_config(speak_switches={"user": bool(values.get("user")),
                                      "assistant": bool(values.get("assistant"))})

    def _on_run_code(self, _evt=None):
        text = self.chat.last_assistant()
        if not text:
            self._set_status("还没有豆包的回复，没有可执行的代码", True)
            return
        if not runner.snippets(text):
            self._set_status("最后一条回复里没有 Python 代码块", True)
            return
        self._confirm_run(text)

    def _confirm_run(self, text):
        items = runner.snippets(text)
        if not items:
            self._set_status("这条消息里没有 Python 代码块", True)
            return
        fg, bg = self._dialog_colors()
        dlg = RunConfirmDialog(
            self, items, self._default_workdir(), runner.find_python(),
            fg, bg, self.GetFont())
        try:
            if dlg.ShowModal() != wx.ID_OK:
                return
            code, workdir = dlg.selected().code, dlg.workdir()
        finally:
            dlg.Destroy()
        self._start_run(code, workdir or self._default_workdir())

    def _start_run(self, code, workdir):
        """执行放后台线程：装包 + 跑脚本可能几分钟，不能卡住界面。

        进度窗是解压对话框样式：当前动作、输出末几行、耗时、可取消。
        早先只有状态栏一行字，下载装包几分钟毫无动静，看起来就像「装不了」。
        """
        self._set_status("正在本机执行…")
        prog = RunProgressDialog(self)

        def work():
            try:
                res = runner.run(
                    code, workdir,
                    on_progress=lambda m: wx.CallAfter(prog.status, m),
                    on_output=lambda s: wx.CallAfter(prog.line, s),
                    cancel=prog.cancel)
            except Exception as exc:  # noqa: BLE001
                res = runner.RunResult(workdir=str(workdir), error=str(exc))
            wx.CallAfter(prog.close)
            wx.CallAfter(self._on_run_done, res)

        threading.Thread(target=work, daemon=True).start()

    def _on_run_done(self, res):
        self._set_status(res.summary(), not res.ok, alert=True)
        if res.cancelled:
            return
        fg, bg = self._dialog_colors()
        RunResultDialog(self, res, fg, bg, self.GetFont(),
                        on_send=self._send_result_to_input).show()

    def _send_result_to_input(self, text):
        """把结果放进输入框而不是直接发：用户往往还要补一句「这是报错，帮我改」。"""
        self.input.SetValue(text)
        self.input.SetFocus()
        self._set_status("结果已放进输入框，回车发送给豆包", alert=True)

    def _on_mirror(self, _evt):
        """设置 pip 镜像源：存进 config.json，全局生效（下次装包立即用新源）。"""
        cur = load_config().get("pip_mirror") or ""
        dlg = MirrorDialog(self, cur, font=self.GetFont())
        try:
            if dlg.ShowModal() != wx.ID_OK:
                return
            url = dlg.url()
        finally:
            dlg.Destroy()
        update_config(pip_mirror=url)
        self._set_status("pip 镜像源已保存：" + (url or "官方 PyPI"), alert=True)

    def _on_agent(self, _evt=None):
        """打开（或聚焦）AI Agent 控制台。非模态：运行时机器人操作的是真实桌面。"""
        if getattr(self, "_agent_dlg", None) and self._agent_dlg.IsShown():
            self._agent_dlg.Raise()
            return
        self._agent_dlg = AgentDialog(self)
        self._agent_dlg.Show()

    def _on_packages(self, _evt=None):
        """打开（或聚焦）包管理窗口。非模态：安装耗时较长，期间可用主界面。"""
        if getattr(self, "_packages_dlg", None) and self._packages_dlg.IsShown():
            self._packages_dlg.Raise()
            return
        self._packages_dlg = PackageManagerDialog(self)
        self._packages_dlg.Show()

    def _on_keys(self, _evt):
        wx.MessageBox(
            "键盘操作\n\n"
            "Tab / Shift+Tab     依次切换：新对话 → 会话列表 → 消息区 → 输入框 → 发送\n"
            "F6 / Shift+F6       在会话列表、消息区、输入框、按钮之间切换\n"
            "Enter               发送消息（输入框内）；在会话列表上打开选中的会话\n"
            "Shift+Enter         换行\n"
            "Ctrl+V              粘贴图片并随下一条消息发送（剪贴板是文本时照常粘贴）\n"
            "Esc                 停止生成\n"
            "Ctrl+N              新建对话\n"
            "Ctrl+L              登录 / 切换账号（账号菜单）\n"
            "Ctrl+R              刷新登录状态（账号菜单）\n"
            "Ctrl+I              账号信息（账号菜单，只读多行框）\n"
            "Ctrl+Shift+C        复制最后一条消息（消息区右键可复制单条或全部）\n"
            "Ctrl+Shift+R        运行最后一条回复里的 Python 代码（会先确认）\n"
            "Ctrl+Shift+A        AI Agent（对话菜单）：每步截图交给豆包看图决策，\n"
            "                    客户端真实操控键鼠\n"
            "Ctrl+Shift+V        朗读最后一条回复（对话菜单，豆包的声音，bass.dll 播放）\n"
            "Ctrl+F2             重命名当前会话\n"
            "F2                  重命名选中的会话（焦点在会话列表时）\n"
            "F5                  刷新会话列表（对话菜单）\n"
            "Ctrl+A              全选会话（焦点在列表时；也可 Ctrl/Shift 点选、拖动框选）\n"
            "Delete              删除选中的会话（焦点在列表时，删前会确认）\n"
            "Shift+F10 / 菜单键   会话列表右键菜单：打开 / 重命名 / 置顶 / 标记已读 /\n"
            "                    复制标题 / 清除上下文 / 删除（多选时只留复制和删除）\n"
            "上下方向键           浏览会话列表；PageUp/PageDown 翻消息",
            "键盘快捷键", wx.OK | wx.ICON_INFORMATION, self)

    # ----------------------------------------------------------- 关闭
    def _on_close(self, evt):
        try:
            self.api.close()
        except Exception:  # noqa: BLE001
            pass
        evt.Skip()
