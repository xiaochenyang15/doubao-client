"""包管理对话框：查看、安装、升级与卸载随包解释器中的第三方包。

界面结构参考 polyglot 插件 ModelManager 的分层设计：列表展示已安装项、
操作按钮统一入口、只读日志区实时输出 pip 结果、忙碌期间通过 ``busy``
标志阻止重复提交与窗口关闭；耗时操作全部在后台线程执行，进度经
``wx.CallAfter`` 派发回主线程。

安装的包作用于 AI Agent 的 run_code 所使用的同一解释器，安装后即可
被 Agent 生成的代码直接 import。
"""
from __future__ import annotations

import threading

import wx

from ..core.packages import PackageError, PackageManager
from ..core.runner import python_info, refresh_environment_cache
from ..services import a11y
from .theme import C_ERR, C_MUTED, C_TEXT, ui_font, is_high_contrast


class PackageManagerDialog(wx.Dialog):
    """Python 第三方包管理界面（非模态）。

    一次仅允许一个后台操作（安装 / 升级 / 卸载 / 刷新）运行；操作期间
    按钮全部禁用，关闭请求将被拒绝，防止 pip 子进程与窗口生命周期错位。
    """

    def __init__(self, parent):
        super().__init__(parent, title="包管理",
                         style=wx.DEFAULT_DIALOG_STYLE | wx.RESIZE_BORDER)
        self.main = parent
        self.manager = PackageManager()
        self.busy = False
        self._closing = False
        self._packages: list = []          # 最近一次列表数据
        self._filter = ""

        hc = is_high_contrast()
        muted = wx.SystemSettings.GetColour(wx.SYS_COLOUR_GRAYTEXT)
        fg = wx.SystemSettings.GetColour(wx.SYS_COLOUR_WINDOWTEXT) if hc else C_TEXT
        self.SetBackgroundColour(
            wx.SystemSettings.GetColour(wx.SYS_COLOUR_WINDOW) if hc
            else wx.Colour(0xFA, 0xFA, 0xFB))

        sz = wx.BoxSizer(wx.VERTICAL)

        # 解释器说明：明确操作生效的环境
        info = python_info()
        if info.get("path"):
            tag = "随包便携版" if info.get("bundled") else "本机已安装"
            head = (f"管理随包 Python 环境中的第三方包。当前解释器："
                    f"{info.get('version') or '3.x'}（{tag}）。"
                    "AI Agent 的 run_code 使用同一环境，安装后可直接 import。")
        else:
            head = ("未找到 Python 解释器，包管理不可用"
                    "（应为 bin\\python\\python.exe，缺失时请恢复该目录）。")
        lbl_head = wx.StaticText(self, label=head)
        lbl_head.SetFont(ui_font(9))
        lbl_head.SetForegroundColour(fg)
        lbl_head.Wrap(self.FromDIP(640))
        sz.Add(lbl_head, 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.TOP, 14)

        # 安装行：包名输入 + 安装按钮（支持空格 / 逗号分隔多个包）
        row_install = wx.BoxSizer(wx.HORIZONTAL)
        lbl_install = wx.StaticText(self, label="安装包(&I)：")
        lbl_install.SetFont(ui_font(9))
        self.edit_pkg = wx.TextCtrl(self, style=wx.TE_PROCESS_ENTER | wx.BORDER_SIMPLE)
        self.edit_pkg.SetFont(ui_font(10))
        self.edit_pkg.SetToolTip(
            "输入包名后回车或点击「安装」；可用空格或逗号分隔多个包名，"
            "如：requests beautifulsoup4")
        self.btn_install = wx.Button(self, label="安装(&N)")
        self.btn_install.SetToolTip("从当前镜像源下载并安装到随包解释器")
        row_install.Add(lbl_install, 0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT,
                        self.FromDIP(8))
        row_install.Add(self.edit_pkg, 1, wx.ALIGN_CENTER_VERTICAL)
        row_install.Add(self.btn_install, 0, wx.LEFT | wx.ALIGN_CENTER_VERTICAL,
                        self.FromDIP(8))
        sz.Add(row_install, 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.TOP,
               self.FromDIP(12))

        # 过滤行
        row_filter = wx.BoxSizer(wx.HORIZONTAL)
        lbl_filter = wx.StaticText(self, label="筛选(&F)：")
        lbl_filter.SetFont(ui_font(9))
        self.edit_filter = wx.TextCtrl(self, style=wx.BORDER_SIMPLE)
        self.edit_filter.SetToolTip("输入关键字过滤下方列表")
        self.btn_refresh = wx.Button(self, label="刷新(&R)")
        self.btn_refresh.SetToolTip("重新读取已安装包列表")
        row_filter.Add(lbl_filter, 0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT,
                       self.FromDIP(8))
        row_filter.Add(self.edit_filter, 1, wx.ALIGN_CENTER_VERTICAL)
        row_filter.Add(self.btn_refresh, 0, wx.LEFT | wx.ALIGN_CENTER_VERTICAL,
                       self.FromDIP(8))
        sz.Add(row_filter, 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.TOP,
               self.FromDIP(12))

        # 已安装列表（标签与控件在创建顺序上紧邻）
        lbl_list = wx.StaticText(self, label="已安装的包(&K)")
        lbl_list.SetFont(ui_font(9, True))
        sz.Add(lbl_list, 0, wx.LEFT | wx.RIGHT | wx.TOP, self.FromDIP(12))

        self.list_pkg = wx.ListCtrl(
            self, style=wx.LC_REPORT | wx.LC_SINGLE_SEL | wx.BORDER_SIMPLE)
        self.list_pkg.InsertColumn(0, "包名")
        self.list_pkg.InsertColumn(1, "版本")
        self.list_pkg.SetFont(ui_font(10))
        self.list_pkg.SetMinSize(wx.Size(self.FromDIP(600), self.FromDIP(240)))
        sz.Add(self.list_pkg, 1, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.TOP,
               self.FromDIP(4))

        # 选中包操作行
        row_ops = wx.BoxSizer(wx.HORIZONTAL)
        self.btn_upgrade = wx.Button(self, label="升级选中(&U)")
        self.btn_upgrade.SetToolTip("将选中的包升级到镜像源上的最新版本")
        self.btn_uninstall = wx.Button(self, label="卸载选中(&D)")
        self.btn_uninstall.SetToolTip("从随包解释器中移除选中的包")
        row_ops.Add(self.btn_upgrade, 0, wx.RIGHT | wx.TOP, self.FromDIP(8))
        row_ops.Add(self.btn_uninstall, 0, wx.RIGHT | wx.TOP, self.FromDIP(8))
        sz.Add(row_ops, 0, wx.EXPAND | wx.LEFT | wx.RIGHT, 0)

        # 状态行
        self.lbl_status = wx.StaticText(self, label="正在读取包列表…")
        self.lbl_status.SetFont(ui_font(9))
        self.lbl_status.SetForegroundColour(muted)
        sz.Add(self.lbl_status, 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.TOP,
               self.FromDIP(10))

        # 日志区（pip 输出实时显示）
        lbl_log = wx.StaticText(self, label="操作日志(&L)")
        lbl_log.SetFont(ui_font(9, True))
        sz.Add(lbl_log, 0, wx.LEFT | wx.RIGHT | wx.TOP, self.FromDIP(10))
        self.log = wx.TextCtrl(self, style=wx.TE_MULTILINE | wx.TE_READONLY
                               | wx.TE_RICH2 | wx.BORDER_SIMPLE)
        self.log.SetFont(ui_font(9))
        self.log.SetMinSize(wx.Size(self.FromDIP(600), self.FromDIP(110)))
        sz.Add(self.log, 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.TOP,
               self.FromDIP(4))

        # 进度条：操作期间显示（不确定进度，脉冲模式）
        self.gauge = wx.Gauge(self)
        self.gauge.Pulse()
        self.gauge.Hide()
        sz.Add(self.gauge, 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.TOP,
               self.FromDIP(8))

        # 关闭按钮
        row_close = wx.BoxSizer(wx.HORIZONTAL)
        self.btn_close = wx.Button(self, wx.ID_CLOSE, "关闭")
        row_close.AddStretchSpacer()
        row_close.Add(self.btn_close, 0)
        sz.Add(row_close, 0, wx.EXPAND | wx.ALL, self.FromDIP(12))

        self.SetSizerAndFit(sz)
        self.SetSize(self.FromDIP(wx.Size(720, 640)))
        self.SetMinSize(self.FromDIP(wx.Size(600, 520)))

        self.Bind(wx.EVT_CLOSE, self._on_close)
        self.Bind(wx.EVT_CHAR_HOOK, self._on_char_hook)
        self.btn_install.Bind(wx.EVT_BUTTON, self._on_install)
        self.btn_refresh.Bind(wx.EVT_BUTTON, lambda e: self._reload())
        self.btn_upgrade.Bind(wx.EVT_BUTTON, self._on_upgrade)
        self.btn_uninstall.Bind(wx.EVT_BUTTON, self._on_uninstall)
        self.btn_close.Bind(wx.EVT_BUTTON, lambda e: self.Close())
        self.edit_pkg.Bind(wx.EVT_TEXT_ENTER, lambda e: self._on_install())
        self.edit_filter.Bind(wx.EVT_TEXT, self._on_filter)
        self.list_pkg.Bind(wx.EVT_LIST_ITEM_SELECTED, self._on_item_selected)
        self.list_pkg.Bind(wx.EVT_LIST_ITEM_DESELECTED, self._on_item_selected)

        self._reload()

    # ---------------------------------------------------------------- 关闭
    def _on_char_hook(self, evt):
        if evt.GetKeyCode() == wx.WXK_ESCAPE and not self._closing:
            self.Close()
            return
        evt.Skip()

    def _on_close(self, evt):
        """操作未结束时拒绝关闭，避免后台线程访问已销毁的窗口。"""
        if self.busy:
            wx.MessageBox("包管理操作正在进行，请等待完成后再关闭。",
                           "包管理", wx.OK | wx.ICON_INFORMATION, self)
            evt.Veto()
            return
        self._closing = True
        if getattr(self.main, "_packages_dlg", None) is self:
            self.main._packages_dlg = None
        evt.Skip()

    # ---------------------------------------------------------------- 状态
    def _set_status(self, text, error=False):
        if self._closing:
            return
        hc = is_high_contrast()
        self.lbl_status.SetForegroundColour(
            wx.SystemSettings.GetColour(wx.SYS_COLOUR_WINDOWTEXT) if hc
            else (C_ERR if error else C_MUTED))
        if self.lbl_status.GetLabel() != text:
            self.lbl_status.SetLabel(text)
        a11y.tell(self.lbl_status, text, alert=error)

    def _log_line(self, text):
        if self._closing or not text:
            return
        self.log.AppendText(text.rstrip() + "\n")

    def _set_busy(self, busy: bool, what: str = ""):
        self.busy = busy
        for btn in (self.btn_install, self.btn_refresh,
                    self.btn_upgrade, self.btn_uninstall):
            btn.Enable(not busy)
        self.gauge.Show(busy)
        if busy:
            self.gauge.Pulse()
        self.Layout()
        if what:
            self._set_status(what)

    # ---------------------------------------------------------------- 列表
    def _reload(self):
        """后台重新读取已安装包列表。"""
        if self.busy:
            return
        self._set_busy(True, "正在读取包列表…")

        def work():
            try:
                packages = self.manager.list_installed()
            except PackageError as exc:
                wx.CallAfter(self._on_list_failed, str(exc))
                return
            except Exception as exc:  # noqa: BLE001
                wx.CallAfter(self._on_list_failed, str(exc))
                return
            wx.CallAfter(self._on_list_ready, packages)

        threading.Thread(target=work, daemon=True).start()

    def _on_list_failed(self, msg):
        self._set_busy(False)
        self._set_status(f"读取包列表失败：{msg}", error=True)

    def _on_list_ready(self, packages):
        self._packages = packages
        self._render_list()
        self._set_busy(False)
        self._update_sel_status()

    def _visible(self) -> list:
        """当前筛选条件下可见的包（顺序与列表行号一致）。"""
        kw = self._filter.strip().lower()
        return [p for p in self._packages if not kw or kw in p.name.lower()]

    def _render_list(self):
        """按当前筛选条件渲染列表。"""
        self.list_pkg.DeleteAllItems()
        for pkg in self._visible():
            n = self.list_pkg.GetItemCount()
            self.list_pkg.InsertItem(n, pkg.name)
            self.list_pkg.SetItem(n, 1, pkg.version)
        w = self.list_pkg.GetClientSize().width - self.FromDIP(8)
        if w > self.FromDIP(80):
            self.list_pkg.SetColumnWidth(0, int(w * 0.62))
            self.list_pkg.SetColumnWidth(1, int(w * 0.34))
        self._update_sel_status()

    def _on_filter(self, evt):
        self._filter = self.edit_filter.GetValue()
        self._render_list()

    # ------------------------------------------------------------ 选中状态
    def _on_item_selected(self, evt):
        """列表原生选中 / 取消选中事件：状态栏说明当前选中项。"""
        self._update_sel_status()
        evt.Skip()

    def _update_sel_status(self):
        names = self._selected_names()
        if names:
            pkg = next((p for p in self._packages if p.name == names[0]), None)
            version = f" {pkg.version}" if pkg and pkg.version else ""
            self._set_status(f"已选中：{names[0]}{version}")
        else:
            self._set_status(f"共 {len(self._packages)} 个已安装包")

    def _selected_names(self) -> list[str]:
        """当前选中的包（列表为单选，按筛选后的行号对应回数据）。"""
        n = self.list_pkg.GetFirstSelected()
        visible = self._visible()
        if 0 <= n < len(visible):
            return [visible[n].name]
        return []

    # ---------------------------------------------------------------- 操作
    def _on_install(self, _evt=None):
        names = self.edit_pkg.GetValue().strip()
        if not names:
            self._set_status("请先输入要安装的包名", error=True)
            return
        self._start_op(
            lambda on_output: self.manager.install(
                names.split(), on_output=on_output),
            f"正在安装：{names}")

    def _on_upgrade(self, _evt=None):
        names = self._selected_names()
        if not names:
            self._set_status("请先在列表中选择要升级的包", error=True)
            return
        self._start_op(
            lambda on_output: self.manager.upgrade(
                names, on_output=on_output),
            f"正在升级：{'、'.join(names)}")

    def _on_uninstall(self, _evt=None):
        names = self._selected_names()
        if not names:
            self._set_status("请先在列表中选择要卸载的包", error=True)
            return
        answer = wx.MessageBox(
            f"要卸载 {'、'.join(names)} 吗？\n\n"
            "卸载后，依赖该包的代码将无法运行，需要时可重新安装。",
            "卸载确认", wx.YES_NO | wx.NO_DEFAULT | wx.ICON_WARNING, self)
        if answer != wx.YES:
            return
        self._start_op(
            lambda on_output: self.manager.uninstall(
                names, on_output=on_output),
            f"正在卸载：{'、'.join(names)}")

    def _start_op(self, op, what: str):
        """在后台线程执行一次包操作；日志逐行转发到主线程。

        ``op`` 接收输出回调，返回 (ok, detail)。
        """
        if self.busy:
            return
        self._set_busy(True, what)
        self._log_line(f"—— {what} ——")

        def on_output(text):
            wx.CallAfter(self._log_line, text)

        def work():
            try:
                ok, detail = op(on_output)
            except PackageError as exc:
                wx.CallAfter(self._on_op_done, False, str(exc))
                return
            except Exception as exc:  # noqa: BLE001
                wx.CallAfter(self._on_op_done, False, str(exc))
                return
            wx.CallAfter(self._on_op_done, ok, "" if ok else (detail or ""))

        threading.Thread(target=work, daemon=True).start()

    def _on_op_done(self, ok: bool, detail: str):
        if self._closing:
            return
        if ok:
            refresh_environment_cache()   # 环境已变化，清除画像缓存
            # 先解除忙碌再刷新：_reload 在忙碌状态下会直接返回，否则按钮
            # 会一直处于禁用状态、窗口也无法关闭。
            self._set_busy(False)
            self._set_status("操作成功，正在刷新包列表…")
            self._log_line("操作完成。")
            self._reload()
        else:
            self._set_busy(False)
            self._set_status(f"操作失败：{detail[-400:]}", error=True)
            self._log_line(f"操作失败：{detail[-400:]}")
