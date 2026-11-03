"""AI Agent 控制台：任务目标输入、执行日志展示与会话生命周期管理。

会话模型（单会话执行）：
    一次任务从开始到结束**始终使用同一个服务端会话**。首条消息由服务端
    分配会话 id，此后每一步复用该 id，模型因此能够引用本次任务的完整
    上下文；任务结束（完成、停止或失败）时删除该临时会话，不占用用户
    的会话列表。

    早期实现按固定步数轮换会话，导致模型在轮换边界丢失上下文；本实现只在
    服务端这一轮生成失败（流式协议 2005，「系统错误」之类）时更换会话，
    其余情况始终复用同一会话。提示词每次都带目标、动作历史与当前屏幕状态，
    因此必要时换会话不会丢失任务上下文。截图上传结果按（路径, 修改时间,
    大小）缓存——重试使用同一张截图时不再重复上传，减少请求量。

线程模型：
    所有 UI 更新均由工作线程经 ``wx.CallAfter`` 派发至主线程；需要用户
    确认的环节（代码执行确认、回答问题）通过 ``threading.Event`` 将主
    线程的对话框结果传回工作线程阻塞等待。截图前将本窗口暂时隐藏，
    避免面板遮挡待操作的界面。
"""
from __future__ import annotations

import threading
import time

import wx

from ..core import runner
from ..services import a11y
from ..services.api import BridgeError
from .. import agent
from .runner_dialogs import RunConfirmDialog
from .theme import C_BG, C_ERR, C_MUTED, C_TEXT, ui_font, is_high_contrast


class AgentDecider:
    """Agent 决策请求的传输适配层：连接决策循环与豆包视觉接口。

    每一步携带一张当前屏幕截图发起请求；会话 id 由服务端在首条消息时
    分配，此后整个任务期间复用（见模块 docstring 的会话模型）。

    页面通道级故障（页面被刷新、上传/请求通道坏）由 api 层自愈重连
    （见 ``api._recover_page``）。服务端「这一轮生成失败」属于会话级
    故障，由 :meth:`decide` 换新会话重发一次；其余错误原样抛给会话层
    按分类决定是否重试。
    """

    IMG_CACHE_MAX = 4           # 仅保留最近几张截图的上传结果（重试均为同一张）

    def __init__(self, api, on_upload=None):
        self.api = api
        self._on_upload = on_upload
        self._cid = ""
        self._img_cache: dict[str, tuple[tuple[float, int], dict]] = {}

    def _drop(self) -> None:
        """删除当前临时会话（任务结束时调用），失败时静默忽略。"""
        cid, self._cid = self._cid, ""
        if cid and cid != "0":
            try:
                self.api.delete_conversation(cid)
            except Exception:  # noqa: BLE001
                pass

    def close(self) -> None:
        """任务结束：删除临时会话。"""
        self._drop()

    def _image_info(self, path: str) -> dict:
        """上传截图并缓存返回的 uri。

        重试使用的是同一张截图，缓存命中时不再重复上传——重复上传
        只会白白增加请求量。
        """
        import os
        key = os.path.abspath(path)
        try:
            st = os.stat(key)
            stamp = (st.st_mtime, st.st_size)
        except OSError:
            stamp = (0.0, 0)
        hit = self._img_cache.get(key)
        if hit is not None and hit[0] == stamp and (hit[1] or {}).get("uri"):
            return hit[1]
        info = self.api.upload_image(path)
        self._img_cache[key] = (stamp, info)
        while len(self._img_cache) > self.IMG_CACHE_MAX:
            self._img_cache.pop(next(iter(self._img_cache)), None)
        return info

    def decide(self, message: str, image_path: str | None = None) -> str:
        """发送决策请求，返回模型回复全文。

        会话复用规则：``self._cid`` 为空时使用 "0"（服务端将创建新会话），
        否则复用本次任务已建立的会话 id。

        服务端返回「已收到请求但这一轮生成失败」（流式协议 2005，
        常见文案「系统错误」）时换一个新会话立刻重发一次：这类失败与网络
        无关，同一个会话重发仍然会失败，只有新会话才能继续——提示词本身
        已经带上了目标、动作历史和当前屏幕状态，换会话不丢上下文。

        限流（710022004 / rate limited）除外：换会话一样被拒，只能按服务端
        要求的节奏等待，因此原样抛给会话层处理。
        """
        try:
            return self._decide_once(message, image_path)
        except BridgeError as exc:
            if not getattr(exc, "generation", False):
                raise
            if getattr(exc, "kind", "") == "rate_limit":
                raise
        self._drop()
        return self._decide_once(message, image_path)

    def _decide_once(self, message: str, image_path: str | None = None) -> str:
        """在单个会话里发一次决策请求（见 :meth:`decide` 的会话复用规则）。"""
        cid = self._cid or "0"
        if image_path:
            info = self._image_info(image_path)
            if self._on_upload:
                try:
                    self._on_upload(info)
                except Exception:  # noqa: BLE001
                    pass
            source = self.api.chat_with_image(message, info["uri"], cid)
        else:
            source = self.api.stream_chat(message, cid)
        parts: list[str] = []
        error = ""
        new_cid = ""
        for kind, payload in source:
            if kind == "delta":
                parts.append(payload)
            elif kind == "conv":
                new_cid = payload
            elif kind == "image":
                if self._on_upload:
                    try:
                        self._on_upload(payload)
                    except Exception:  # noqa: BLE001
                        pass
            elif kind == "error":
                error = payload
                break
        if new_cid and new_cid != "0":
            self._cid = new_cid
        if error and not parts:
            # BridgeError 原样抛出：code/kind 是会话层重试策略的依据。
            # 页面通道级的故障（页面被刷新、上传通道坏）已在 api 层自愈
            # （见 api._recover_page），这里不再做换会话之类的二次自愈。
            if isinstance(error, BaseException):
                raise error
            raise BridgeError(error)
        return "".join(parts)


class AgentDialog(wx.Dialog):
    """AI Agent 控制台。

    非模态对话框：运行期间用户可以查看真实桌面，Agent 在其上执行鼠标
    与键盘操作。对话框仅承担「填写目标 / 查看观察与进度 / 随时停止」
    的职责。
    """

    def __init__(self, parent):
        super().__init__(parent, title="AI Agent",
                         style=wx.DEFAULT_DIALOG_STYLE | wx.RESIZE_BORDER)
        self.main = parent
        self.session = None
        self.decider = None
        self.running = False
        self._log_tail = []
        self._closing = False        # 关闭中：阻止工作线程的后续回调访问已销毁的窗口
        self._modal = None           # 当前由 ask_user/run_code 弹出的模态子对话框
        self._hidden_for_capture = False   # 截图期间窗口是否处于隐藏状态
        self._code_steps = 0         # 本次任务中执行 Python 代码的次数
        self._auto_code = False      # 启动时锁定的「免确认执行代码」选项状态

        hc = is_high_contrast()
        bg = wx.SystemSettings.GetColour(wx.SYS_COLOUR_WINDOW) if hc else C_BG
        fg = wx.SystemSettings.GetColour(wx.SYS_COLOUR_WINDOWTEXT) if hc else C_TEXT
        self.SetBackgroundColour(bg)
        muted = wx.SystemSettings.GetColour(wx.SYS_COLOUR_GRAYTEXT)

        sz = wx.BoxSizer(wx.VERTICAL)

        # 目标输入（标签与控件在创建顺序上紧邻，见文件头无障碍约定）
        lbl_goal = wx.StaticText(self, label="任务目标")
        lbl_goal.SetFont(ui_font(9, True))
        lbl_goal.SetForegroundColour(fg)
        sz.Add(lbl_goal, 0, wx.LEFT | wx.RIGHT | wx.TOP, 14)

        self.goal = wx.TextCtrl(self, value="",
                                style=wx.TE_PROCESS_ENTER | wx.BORDER_SIMPLE)
        self.goal.SetFont(ui_font(10))
        self.goal.SetMinSize(wx.Size(self.FromDIP(460), self.FromDIP(34)))
        self.goal.SetToolTip("描述需要 Agent 完成的任务，例如：将记事本中的文字全选并复制")
        self.goal.Bind(wx.EVT_TEXT_ENTER, lambda e: self._on_start())
        sz.Add(self.goal, 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, 14)

        # 选项：步数由模型自行决定；是否允许直接执行 Python
        opt_row = wx.BoxSizer(wx.HORIZONTAL)
        self.chk_auto_code = wx.CheckBox(
            self, label="允许直接执行 Python 代码（不再逐次确认，仍可随时停止）")
        self.chk_auto_code.SetFont(ui_font(9))
        self.chk_auto_code.SetValue(False)
        self.chk_auto_code.SetToolTip(
            "勾选后，Agent 每次执行 Python 时不再弹出确认框，任务可以连贯完成。\n"
            "代价是失去逐次人工确认：模型生成的代码将以当前用户身份直接执行。")
        opt_row.Add(self.chk_auto_code, 1, wx.ALIGN_CENTER_VERTICAL)
        sz.Add(opt_row, 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, 14)

        # 解释器状态：run_code 直接使用该解释器，事先向用户明确当前使用的解释器
        self.lbl_py = wx.StaticText(self, label=self._python_label())
        self.lbl_py.SetFont(ui_font(8))
        self.lbl_py.SetForegroundColour(muted)
        sz.Add(self.lbl_py, 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, 14)

        # 按钮行
        row = wx.BoxSizer(wx.HORIZONTAL)
        self.btn_start = wx.Button(self, label="开始(&S)")
        self.btn_start.SetFont(ui_font(10))
        self.btn_start.Bind(wx.EVT_BUTTON, lambda e: self._on_start())
        self.btn_stop = wx.Button(self, label="停止(&T)")
        self.btn_stop.SetFont(ui_font(10))
        self.btn_stop.Bind(wx.EVT_BUTTON, lambda e: self._on_stop())
        self.btn_stop.Enable(False)
        btn_close = wx.Button(self, wx.ID_CLOSE, "关闭")
        btn_close.SetFont(ui_font(10))
        btn_close.Bind(wx.EVT_BUTTON, lambda e: self.Close())
        row.Add(self.btn_start, 0, wx.RIGHT, 8)
        row.Add(self.btn_stop, 0, wx.RIGHT, 8)
        row.Add(btn_close, 0)
        sz.Add(row, 0, wx.LEFT | wx.RIGHT | wx.BOTTOM, 14)

        # 状态行
        self.lbl_status = wx.StaticText(self, label="填写目标后点击「开始」")
        self.lbl_status.SetFont(ui_font(9))
        self.lbl_status.SetForegroundColour(muted)
        sz.Add(self.lbl_status, 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, 14)

        # 执行日志（标签与控件在创建顺序上紧邻）
        lbl_log = wx.StaticText(self, label="执行日志")
        lbl_log.SetFont(ui_font(9, True))
        lbl_log.SetForegroundColour(fg)
        sz.Add(lbl_log, 0, wx.LEFT | wx.RIGHT, 14)

        self.log = wx.TextCtrl(self, style=wx.TE_MULTILINE | wx.TE_READONLY
                               | wx.TE_RICH2 | wx.BORDER_SIMPLE)
        self.log.SetFont(ui_font(9))
        self.log.SetMinSize(wx.Size(self.FromDIP(480), self.FromDIP(300)))
        sz.Add(self.log, 1, wx.EXPAND | wx.ALL, 14)

        hint = wx.StaticText(
            self, label="步数不设上限：Agent 自行判断完成时机（安全网为 1000 步）。"
                        "运行期间本窗口自动隐藏，不遮挡待操作的界面。")
        hint.SetFont(ui_font(8))
        hint.SetForegroundColour(muted)
        sz.Add(hint, 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, 14)

        self.SetSizerAndFit(sz)
        self.SetSize(self.FromDIP(wx.Size(940, 720)))
        self.SetMinSize(self.FromDIP(wx.Size(760, 560)))
        # 不能使用 SetEscapeId(wx.ID_CLOSE)：与「关闭」按钮（wx.ID_CLOSE）冲突，
        # 会导致 EVT_CLOSE 的销毁路径与按钮路径相互干扰（Windows 报
        # DestroyWindow 0x578 无效句柄），窗口无法关闭。
        # 改为在 CHAR_HOOK 中显式处理 Esc。
        self.Bind(wx.EVT_CHAR_HOOK, self._on_char_hook)
        self.Bind(wx.EVT_CLOSE, self._on_close)
        self.goal.SetFocus()

    # ---------------- 生命周期 ----------------
    def _on_char_hook(self, evt):
        """Esc 关闭对话框（替代 SetEscapeId 方案）。"""
        if evt.GetKeyCode() == wx.WXK_ESCAPE and not self._closing:
            self.Close()
            return
        evt.Skip()

    def _on_start(self, _evt=None):
        if self.running:
            return
        if self.main.api is None:
            self._set_status("请先登录豆包", error=True)
            return
        goal = self.goal.GetValue().strip()
        if not goal:
            self._set_status("请先填写任务目标", error=True)
            return
        self.decider = AgentDecider(self.main.api)
        self._code_steps = 0
        # 启动时将勾选状态读取到普通成员：工作线程只读该副本，
        # 不直接访问 wx 控件（跨线程读取控件不安全）
        self._auto_code = bool(self.chk_auto_code.GetValue())
        # 环境画像：Python 版本、已安装第三方包与本次任务的工作目录。
        # 随每步提示词发送给模型，模型即可明确可用的包与文件读写位置。
        try:
            workdir = str(self.main._default_workdir())
        except Exception:  # noqa: BLE001
            workdir = ""

        def _env_note() -> str:
            note = runner.environment_report()
            if workdir:
                note += (f" 任务工作目录：{workdir}"
                         "（run_code 在该目录执行，产出文件也存放在该目录）。")
            return note

        self.session = agent.AgentSession(
            goal, self.decider.decide,
            on_status=lambda t: wx.CallAfter(self._set_status, t),
            on_step=lambda s, d, o, m: wx.CallAfter(self._on_step, s, m),
            on_done=lambda m: wx.CallAfter(self._on_done, m),
            ask_user=self._ask_user,
            run_code_cb=self._run_code,
            before_capture=self._hide_for_capture,
            after_capture=self._show_after_capture,
            use_uia=True,
            with_vision=True,
            # 步数由模型自行决定（不传 max_steps，使用默认安全网）
            env_note=_env_note,
            allow_code=True,
        )
        self.running = True
        self._update_buttons()
        self._set_status("AI Agent 已启动")
        self.session.start()

    def _on_stop(self, _evt=None):
        if self.session and self.running:
            self.session.cancel()
            self._set_status("正在停止…")

    def _on_close(self, evt):
        # 防止重入（关闭过程可能被触发多次）
        if self._closing:
            evt.Skip()
            return
        self._closing = True
        # 1. 停止后台会话
        if self.running and self.session:
            self.session.cancel()
        # 2. 工作线程阻塞在 ask_user / run_code 的模态子对话框上时，
        #    先关闭子对话框，避免父窗口销毁时状态错乱导致无法关闭
        if self._modal:
            try:
                self._modal.EndModal(wx.ID_CANCEL)
            except Exception:  # noqa: BLE001
                try:
                    self._modal.Destroy()
                except Exception:  # noqa: BLE001
                    pass
            self._modal = None
        # 3. 清除主窗口中的引用，避免下次打开时访问已销毁的窗口
        if getattr(self.main, "_agent_dlg", None) is self:
            self.main._agent_dlg = None
        evt.Skip()

    def _python_label(self) -> str:
        """向用户展示解释器信息（run_code 使用的解释器）。"""
        try:
            info = runner.python_info()
        except Exception:  # noqa: BLE001
            info = {}
        if not info.get("path"):
            return ("Python：未找到解释器，run_code 不可用"
                    "（应为 bin\\python\\python.exe，缺失时请恢复该目录）")
        tag = "随包便携版" if info.get("bundled") else "本机已安装"
        text = f"Python：{info.get('version') or '3.x'}（{tag}）· {info['path']}"
        if not info.get("writable", True):
            text += " · 目录只读，安装的包将写入用户目录"
        return text

    def _update_buttons(self):
        self.btn_start.Enable(not self.running)
        self.btn_stop.Enable(self.running)
        self.goal.Enable(not self.running)
        self.chk_auto_code.Enable(not self.running)

    # ---------------- 回调（主线程） ----------------
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

    def _on_step(self, step, msg):
        if self._closing:
            return
        line = f"[{step}] {msg}"
        self._log_tail.append(line)
        if len(self._log_tail) > 200:
            self._log_tail = self._log_tail[-200:]
        self.log.SetValue("\n".join(self._log_tail))
        self.log.SetInsertionPointEnd()

    def _on_done(self, msg):
        if self._closing:
            return
        if self._hidden_for_capture:
            self._show_after_capture()
        self.running = False
        self._update_buttons()
        self._on_step("完成", msg)
        self._set_status(msg)
        if self.session:
            self.session = None
        if self.decider:
            # 删除任务期间使用的临时会话，不在用户的会话列表中保留
            try:
                self.decider.close()
            except Exception:  # noqa: BLE001
                pass
            self.decider = None

    # ---------------- 截图协作（工作线程同步调用） ----------------
    def _ui_sync(self, fn, timeout=3.0):
        """在工作线程中同步执行必须在主线程运行的 UI 操作。"""
        done = threading.Event()

        def wrapper():
            try:
                fn()
            except Exception:  # noqa: BLE001
                pass
            finally:
                done.set()

        wx.CallAfter(wrapper)
        done.wait(timeout)

    def _hide_for_capture(self):
        """截图前隐藏本窗口：Agent 面板悬浮时会遮挡待操作的界面。"""
        if self._closing:
            return

        def do():
            if self._closing:
                return
            self._hidden_for_capture = True
            self.Hide()      # 隐藏后，原本被遮挡的窗口将显示出来
            try:
                wx.YieldIfNeeded()
            except Exception:  # noqa: BLE001
                pass

        self._ui_sync(do)
        time.sleep(0.15)     # 等待桌面完成重绘

    def _show_after_capture(self):
        if self._closing or not self._hidden_for_capture:
            return

        def do():
            if self._closing:
                return
            self._hidden_for_capture = False
            self.Show()
            try:
                wx.YieldIfNeeded()
            except Exception:  # noqa: BLE001
                pass

        self._ui_sync(do)

    # ---------------- 需要用户确认（工作线程调用，阻塞等待） ----------------
    def _ask_user(self, question: str) -> str | None:
        box: dict = {}
        done = threading.Event()

        def ui():
            dlg = wx.TextEntryDialog(self, question, "AI Agent 需要确认")
            self._modal = dlg
            try:
                box["ans"] = dlg.GetValue() if dlg.ShowModal() == wx.ID_OK else None
            finally:
                self._modal = None
                dlg.Destroy()
            done.set()

        wx.CallAfter(ui)
        done.wait()
        return box.get("ans")

    def _run_code(self, code: str) -> str:
        """代码执行桥：将模型生成的代码交由本机 Python 执行。

        勾选「允许直接执行 Python 代码」时跳过确认框直接执行（保证任务
        连贯推进）；未勾选时沿用确认流程——先向用户展示代码原文，确认
        后执行。两种方式下，缺失的第三方包均会自动 pip 安装后重试
        （见 ``core.runner.CodeRunner.run``）。
        """
        workdir = self.main._default_workdir()
        self._code_steps += 1
        n = self._code_steps

        if getattr(self, "_auto_code", False):
            self._set_status(f"执行 Python 代码（第 {n} 段），缺少依赖时将自动安装…")
            try:
                res = runner.run(
                    code, workdir,
                    on_progress=lambda m: wx.CallAfter(self._set_status, m))
            except Exception as exc:  # noqa: BLE001
                return f"代码执行失败：{exc}"
            self._set_status(f"第 {n} 段代码{res.summary()}")
            return res.report()

        box: dict = {}
        done = threading.Event()

        def ui():
            fg, bg = self.main._dialog_colors()
            items = [runner.Snippet(code=code, lang="python", index=1)]
            dlg = RunConfirmDialog(
                self, items, workdir,
                runner.find_python(), fg, bg, self.main.GetFont())
            self._modal = dlg
            try:
                if dlg.ShowModal() != wx.ID_OK:
                    box["res"] = "用户取消了代码执行"
                    return
                sel_code, wd = dlg.selected().code, dlg.workdir()
            finally:
                self._modal = None
                dlg.Destroy()

            def work():
                try:
                    res = runner.run(sel_code, wd,
                                    on_progress=lambda m: wx.CallAfter(self.main._set_status, m))
                except Exception as exc:  # noqa: BLE001
                    res = runner.RunResult(error=str(exc))
                wx.CallAfter(lambda: _finish(res))

            threading.Thread(target=work, daemon=True).start()

        def _finish(res):
            box["res"] = res.report()
            done.set()

        wx.CallAfter(ui)
        done.wait()
        return box.get("res", "未执行")
