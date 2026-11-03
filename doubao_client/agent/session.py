from __future__ import annotations

import inspect
import logging
import time
from collections.abc import Callable
from threading import Event, Thread

from . import actions
from . import anchors
from . import sounds
from .decision import AgentDecision, DecisionError, parse_agent_decision
from .observe import Observation, capture_observation, snap_point
from .prompt import build

logger = logging.getLogger("agent")

MIN_STEPS = 10
# 步数**由模型自己决定**：任务做完了它返回 finish 就收工，没做完就继续做。
# 这里的 1000 只是防死循环的安全网（比如模型卡在同一个动作上），不是任务预算。
# 早先写死 20/25 步会在任务做到一半时硬停，那正是「只能执行 20 步」的根因。
HARD_STEP_LIMIT = 1000
MAX_STEPS = HARD_STEP_LIMIT   # 兼容旧名称（旧版用作任务上限，现仅作安全限制）
UNCHANGED_LIMIT = 5           # 连续多少步画面完全无变化时判定为无响应
FAILURE_LIMIT = 3
DECIDER_RETRIES = 2           # 单次决策的最大重试次数（共 3 次请求）
ACTION_DELAY = 1.2            # 动作执行后等待界面重绘（用于截图，不属于请求限流）
SNAP_TOLERANCE = 22           # 落点离控件边缘多少像素以内就吸附过去

# 服务端限流（710022004 / rate limited）后的等待序列，单位秒。
# 这不是本地人为限制请求频率，而是服务端明确要求降速时的响应：限流期间
# 无论发多少请求都会被拒，立刻重发只会让恢复更晚。等待本身可被用户取消。
# 公开资料给出的恢复窗口是 30 分钟到 2 小时（见 README「限流与恢复」），
# 因此序列覆盖约 37 分钟；超时后终止任务并提示恢复方式。
RATE_LIMIT_WAITS = (30.0, 60.0, 120.0, 240.0, 300.0, 600.0, 900.0)

# 失败分类（来自 api.classify_failure）对应的状态栏文案
_KIND_LABEL = {
    "rate_limit": "服务端限流",
    "transient": "临时故障",
    "auth": "登录失效",
    "fatal": "不可重试",
}


def _failure_kind(exc: BaseException) -> str:
    """取失败分类；异常上没带（非 BridgeError）就按瞬时处理。"""
    kind = getattr(exc, "kind", None)
    return kind if kind in _KIND_LABEL else "transient"

# 回调类型
StatusCb = Callable[[str], None]
StepCb = Callable[[int, AgentDecision | None, Observation, str], None]
DoneCb = Callable[[str], None]
AskCb = Callable[[str], str | None]
Decider = Callable[..., str]


def _decider_takes_image(decider: Decider) -> bool:
    """判断 decider 是否接受第二个「图片路径」参数（兼容旧的一参数写法）。"""
    try:
        params = inspect.signature(decider).parameters.values()
    except (TypeError, ValueError):
        return True
    positional = [p for p in params
                  if p.kind in (p.POSITIONAL_ONLY, p.POSITIONAL_OR_KEYWORD)]
    has_varargs = any(p.kind == p.VAR_POSITIONAL for p in params)
    return has_varargs or len(positional) >= 2


class AgentSession:
    RESULT_INLINE = 4000        # run_code 结果回灌给模型的最大字符数

    def __init__(self, goal: str,
                 decider: Decider,
                 on_status: StatusCb | None = None,
                 on_step: StepCb | None = None,
                 on_done: DoneCb | None = None,
                 ask_user: AskCb | None = None,
                 run_code_cb: Callable[[str], str] | None = None,
                 before_capture: Callable[[], None] | None = None,
                 after_capture: Callable[[], None] | None = None,
                 use_uia: bool = True,
                 with_vision: bool = True,
                 max_steps: int = HARD_STEP_LIMIT,
                 env_note: Callable[[], str] | str | None = None,
                 allow_code: bool = True) -> None:
        self.goal = goal
        self.decider = decider
        self._on_status = on_status or (lambda _t: None)
        self._on_step = on_step or (lambda *_a: None)
        self._on_done = on_done or (lambda _t: None)
        self._ask_user = ask_user
        self._run_code_cb = run_code_cb
        self._before_capture = before_capture
        self._after_capture = after_capture
        self.use_uia = use_uia
        self.with_vision = with_vision
        # 安全网：只在死循环时才有意义，任务预算由模型自己定
        self.max_steps = max(MIN_STEPS, int(max_steps or HARD_STEP_LIMIT))
        self._plan_deadline = 0      # 模型自报预算的到期步数（0 = 没有预算）
        self._plan_budget = 0        # 模型最近一次自报的步数
        self._env_note = env_note
        self._env_text = ""
        self.allow_code = allow_code and run_code_cb is not None
        self._takes_image = _decider_takes_image(decider)
        self._cancel = Event()
        self._thread: Thread | None = None

    def _environment(self) -> str:
        """Python 环境画像（版本 / 已装第三方包），只探一次。"""
        if self._env_text:
            return self._env_text
        note = self._env_note
        if callable(note):
            try:
                self._env_text = str(note() or "")
            except Exception:  # noqa: BLE001
                self._env_text = ""
        else:
            self._env_text = str(note or "")
        return self._env_text

    # ---- 生命周期 ----
    def start(self) -> None:
        self._thread = Thread(target=self._run, daemon=True, name="AgentSession")
        self._thread.start()

    def cancel(self) -> None:
        self._cancel.set()

    @property
    def is_running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def _status(self, text: str) -> None:
        try:
            self._on_status(text)
        except Exception:  # noqa: BLE001
            pass

    def _check(self) -> None:
        if self._cancel.is_set():
            raise _Cancelled()

    # ---- 主循环 ----
    def _run(self) -> None:
        try:
            self._status(f"AI Agent 已启动：{self.goal}")
            sounds.play("start")
            history: list[str] = []
            unchanged = 0
            decide_failures = 0     # 决策请求/解析连续失败次数
            action_failures = 0     # 动作执行连续失败次数（别和上面共用一个计数）
            last_sig = ""
            last_action = ""

            for step in range(1, self.max_steps + 1):
                self._check()
                want_shot = self.with_vision and self._takes_image
                self._status(f"第 {step} 步：观察屏幕"
                             + ("（截图）…" if want_shot else "…"))
                obs = self._observe(want_shot)

                # 无变化判定：只有动作不是「等待」且屏幕指纹没变才算
                if obs.signature == last_sig and last_action not in ("wait", "wait_for_change") and last_action:
                    unchanged += 1
                else:
                    unchanged = 0
                last_sig = obs.signature

                if unchanged >= UNCHANGED_LIMIT:
                    self._status("屏幕连续无变化，已停止。")
                    sounds.play("error")
                    self._on_done(f"连续 {unchanged} 步屏幕没有任何变化，已停止。"
                                  "通常是目标窗口没在前台，你可以切换到目标窗口后再试。")
                    return
                if unchanged >= 2:
                    # 别自己硬猜，把「没点中」这个事实明确告诉模型，让它换位置/换手段
                    history.append(
                        f"（系统提示）连续 {unchanged} 步画面几乎没变，说明上一步 "
                        f"{last_action} 没有生效。请重新读网格定位目标，"
                        "或改用键盘（Tab 聚焦 + 回车）推进。")

                # 模型自报的步数预算到期：只提醒一次，不强制停——步数由它自己定
                if self._plan_deadline and step > self._plan_deadline:
                    history.append(
                        f"（系统提示）你之前预估的 {self._plan_budget} 步预算已用完。"
                        "步数没有硬上限，收工时机由你判断：任务做完就返回 status=finish，"
                        "没做完就继续做，并在 step_budget 里重新报一个剩余步数。")
                    self._plan_deadline = 0

                shot_note = "（含截图）" if obs.screenshot is not None else ""
                self._status(f"第 {step} 步：请豆包看图决策{shot_note}…")
                sounds.play("auto")
                message = build(self.goal, obs, history,
                                env_note=self._environment(),
                                anchors_note=self._anchors_note(obs),
                                allow_code=self.allow_code)
                image_path = obs.screenshot.path if obs.screenshot is not None else None
                # 请求本身允许重试：网络抖动/超时一次不该让整个任务停摆。
                # 重试节奏完全由服务端反馈决定（api.classify_failure）：
                # 普通瞬时错误立刻重发；服务端限流（710022004）按其语义等待；
                # 登录失效/永久失败根本不重试。
                raw, last_error = "", None
                attempt = 0
                rate_waits = 0
                while True:
                    try:
                        if self._takes_image:
                            raw = self.decider(message, image_path)
                        else:
                            raw = self.decider(message)
                        last_error = None
                        break
                    except Exception as exc:  # noqa: BLE001
                        last_error = exc
                        self._check()
                        kind = _failure_kind(exc)
                        if kind in ("auth", "fatal"):
                            break      # 认证或致命错误重试无意义，立即退出
                        if kind == "rate_limit":
                            # 服务端明确限流：它要的是降速，立刻重发只会继续被拒。
                            # 按服务端自己的节奏等一段时间再试，等待可被用户取消。
                            if rate_waits >= len(RATE_LIMIT_WAITS):
                                break
                            wait = RATE_LIMIT_WAITS[rate_waits]
                            rate_waits += 1
                            self._status(
                                f"服务端限流（{exc}），等待 {wait:.0f} 秒后重试"
                                f"（{rate_waits}/{len(RATE_LIMIT_WAITS)}）…")
                            self._sleep(wait)
                            continue
                        if attempt < DECIDER_RETRIES:
                            attempt += 1
                            self._status(
                                f"决策请求失败（{_KIND_LABEL[kind]}：{exc}），马上重试…")
                            continue
                        break

                if last_error is not None:
                    exc = last_error
                    kind = _failure_kind(exc)
                    if kind == "auth":
                        self._status(f"登录态已失效：{exc}")
                        sounds.play("error")
                        self._on_done(f"登录态已失效，任务已停止：{exc}\n"
                                      "请重新登录后再跑。")
                        return
                    if kind == "fatal":
                        self._status(f"不可重试的失败：{exc}")
                        sounds.play("error")
                        self._on_done(f"任务已停止（这个错误重试也不会好）：{exc}")
                        return
                    if kind == "rate_limit":
                        # 限流等待序列耗尽后仍被拒绝：本地无法解决，如实终止，
                        # 不再按「连续失败」重复计数。
                        self._status(f"服务端持续限流：{exc}")
                        sounds.play("error")
                        self._on_done(
                            f"服务端限流，已累计等待 {sum(RATE_LIMIT_WAITS) / 60:.0f} 分钟"
                            f"仍被拒绝：{exc}\n"
                            "该错误为账号在「网页发消息」场景触发的风控，与图片数量、"
                            "对话轮数无关。可按以下顺序处理：\n"
                            "1）退出程序，等待 30 分钟以上再运行（恢复窗口通常为 30 分钟到 2 小时）；\n"
                            "2）更换网络出口：优先使用手机热点，其次切换宽带；\n"
                            "3）从「账号 → 登录 / 切换账号」打开浏览器窗口，"
                            "在豆包页面手动发送一条消息；若出现滑块验证，完成验证后"
                            "风控标记即解除。")
                        return
                    self._status(f"决策请求失败：{exc}")
                    sounds.play("error")
                    decide_failures += 1
                    if decide_failures >= FAILURE_LIMIT:
                        self._on_done(
                            f"连续 {decide_failures} 次决策请求失败，已停止：{exc}")
                        return
                    self._sleep(ACTION_DELAY)
                    continue

                try:
                    decision = parse_agent_decision(raw)
                except DecisionError as exc:
                    # 没解析出 JSON：把错误反馈给模型，让它重试
                    logger.warning("决策解析失败：%s；回复前 200 字=%s",
                                   exc, raw[:200])
                    decide_failures += 1
                    history.append(f"上一步出错：{exc}。请严格只返回 JSON 对象。")
                    if decide_failures >= FAILURE_LIMIT:
                        self._on_done(f"模型连续 {decide_failures} 次没返回可用 JSON，已停止。")
                        return
                    self._status(f"没解析出动作（{exc}），让模型重来…")
                    sounds.play("error")
                    time.sleep(0.5)
                    continue
                decide_failures = 0

                # 模型自己定的节奏（可省略）：只用于提醒，不用于强制停止
                if decision.step_budget:
                    self._plan_budget = decision.step_budget
                    self._plan_deadline = step + decision.step_budget
                    self._status(f"由模型自己定节奏：计划再用 {decision.step_budget} 步完成")

                if decision.status == "finish":
                    self._on_step(step, decision, obs, decision.message)
                    self._status("任务完成。")
                    sounds.play("success")
                    self._on_done(decision.message or "任务已完成。")
                    return
                if decision.status == "ask_user":
                    self._on_step(step, decision, obs, decision.message)
                    if self._ask_user:
                        sounds.play("question")
                        ans = self._ask_user(decision.message or "需要更多信息")
                        if ans:
                            history.append(f"Agent 问：{decision.message}")
                            history.append(f"用户答：{ans}")
                            self._status("已收到你的回复，继续。")
                            continue
                    self._on_done(decision.message or "已停止，等待你补充信息。")
                    return

                # action
                action = decision.action
                assert action is not None
                self._status(f"执行：{action.message or action.name}")
                sounds.play("action")
                self._on_step(step, decision, obs, action.message or action.name)
                if action.name == "run_code":
                    self._handle_run_code(step, action, history)
                    last_action = action.name
                    self._sleep(ACTION_DELAY)
                    continue
                try:
                    report = actions.execute_action(
                        action, check=self._check,
                        geometry=self._geometry(obs),
                        resolve=self._resolver(obs),
                        app_name=obs.app_name)
                    history.append(self._history_line(step, action, obs, report.note))
                    last_action = action.name
                except actions.ActionExecutionError as exc:
                    action_failures += 1
                    history.append(f"动作失败：{action.name} 参数错误 - {exc}")
                    self._status(f"动作执行失败：{exc}")
                    sounds.play("error")
                    if action_failures >= FAILURE_LIMIT:
                        self._on_done(f"连续 {action_failures} 个动作失败，已停止：{exc}")
                        return
                    continue
                action_failures = 0

                if not action.name.startswith("wait"):
                    self._sleep(ACTION_DELAY)
                self._status("观察结果…")
            sounds.play("error")
            self._on_done(f"已达到步数上限 {self.max_steps} 步，已停止。任务可能还没完成；"
                          "可以在控制台把「最大步数」调大后重跑。")
        except _Cancelled:
            self._status("已停止。")
            sounds.play("cancel")
            self._on_done("你已停止 Agent。")
        except Exception as exc:  # noqa: BLE001
            logger.exception("AI Agent 意外错误")
            self._status(f"AI Agent 出错：{exc}")
            sounds.play("error")
            self._on_done(f"AI Agent 意外停止：{exc}")
        finally:
            pass

    # ---- 观察 ----
    def _anchors_note(self, obs: Observation) -> str:
        """这个程序已经记过的命名锚点：下次可以直接点名点，不用重新看图找。"""
        try:
            return "\n".join(anchors.describe(obs.app_name or ""))
        except Exception:  # noqa: BLE001  锚点读不出来不该影响任务
            logger.debug("锚点读取失败", exc_info=True)
            return ""

    def _observe(self, want_shot: bool) -> Observation:
        """采集一次观察；截图前后调用外部钩子（用来临时收起 Agent 面板）。"""
        if want_shot and self._before_capture:
            try:
                self._before_capture()
            except Exception:  # noqa: BLE001
                logger.debug("截图前置回调失败", exc_info=True)
        try:
            obs = capture_observation(use_uia=self.use_uia, with_screenshot=want_shot)
        finally:
            if want_shot and self._after_capture:
                try:
                    self._after_capture()
                except Exception:  # noqa: BLE001
                    logger.debug("截图后置回调失败", exc_info=True)
        return obs

    # ---- 坐标换算 / 落点吸附 ----
    def _geometry(self, obs: Observation) -> actions.Geometry:
        """用本步截图自带的整屏几何换算坐标（没有截图就现查）。"""
        shot = obs.screenshot
        if shot is not None and shot.screen_width > 0 and shot.screen_height > 0:
            return actions.Geometry(left=shot.screen_left, top=shot.screen_top,
                                    width=shot.screen_width,
                                    height=shot.screen_height)
        return actions.current_geometry()

    def _resolver(self, obs: Observation):
        """把「模型给的归一化坐标」变成「控件正中的像素坐标」。

        这是点击准确性的第二道保险：模型只负责指出「大概在这儿」，我们把落点挪到
        命中的最小控件中心；附近没有控件时按原坐标点，行为完全不变。
        """
        if not obs.elements:
            return None
        geo = self._geometry(obs)

        def resolve(nx: int, ny: int) -> tuple[int, int]:
            px, py = actions.norm_to_screen(nx, ny, geo)
            snap = snap_point(px, py, obs.elements, geo.width, geo.height,
                              SNAP_TOLERANCE)
            if snap is None:
                return px, py
            sx, sy, label = snap
            logger.debug("落点吸附：(%d,%d) -> (%d,%d) %s", px, py, sx, sy, label)
            self._status(f"落点吸附到「{label}」")
            return sx, sy

        return resolve

    def _handle_run_code(self, step: int, action, history: list[str]) -> None:
        """协作桥：豆包判断「这步得写代码」，交给内置 Python 执行，结果回灌循环。"""
        code = str(action.arguments.get("code") or "").strip()
        if not code:
            history.append(f"{step}. run_code 但没有代码内容，跳过。")
            self._status("run_code 缺少代码，已跳过。")
            return
        if self._run_code_cb is None:
            history.append(f"{step}. run_code 未配置执行器，跳过。")
            self._status("当前环境没有代码执行器，run_code 被跳过。")
            return
        self._status("请求执行 Python 代码（需你确认）…")
        try:
            result = self._run_code_cb(code)
        except Exception as exc:  # noqa: BLE001
            history.append(f"{step}. run_code 执行异常：{exc}")
            self._status(f"代码执行出错：{exc}")
            return
        # 跑过代码后环境可能变了（自动装了包），下次重新探测
        self._env_text = ""
        # 结果拼接为一段写入历史，作为下一步的「观察」。保留足够长度：脚本输出
        # 通常是下一步唯一的判断依据，截断过多会丢失关键信息。超长内容已由 runner
        # 另存为 .log 并写入 report，模型可按需读取。
        text = (result or "（无输出）").strip()
        if len(text) > self.RESULT_INLINE:
            head = self.RESULT_INLINE * 3 // 4
            tail = self.RESULT_INLINE - head
            text = (text[:head] + "\n…（中间省略，完整内容见上面的日志路径）…\n"
                    + text[-tail:])
        history.append(f"{step}. run_code 执行结果：\n{text}")
        flat = " ".join(text.split())
        self._status(f"代码已执行，结果已回灌：{flat[:100]}")

    def _history_line(self, step: int, action, obs: Observation,
                      note: str = "") -> str:
        a = action.arguments
        base = f"{step}. {action.name}"
        if action.name in ("click_anchor", "save_anchor"):
            base += f" 锚点={a.get('anchor') or a.get('name')}"
        elif action.name in ("click_at", "double_click_at", "right_click_at", "hover_at"):
            base += f" @ ({a.get('x')},{a.get('y')})"
        elif action.name == "type_text_at":
            base += f" 文本长度={len(str(a.get('text') or ''))}"
        elif action.name in ("key_combination", "press_key"):
            base += f" 键={a.get('keys') or a.get('key')}"
        elif action.name in ("drag_and_drop", "drag_by"):
            base += f" 起点=({a.get('x')},{a.get('y')})"
        elif action.name == "scroll_at":
            base += f" 方向={a.get('direction')}"
        elif action.name == "wait":
            base += f" {a.get('seconds', 5)}秒"
        base += f" | 前台={obs.app_name or '?'}/{obs.window_title[:30]!r}"
        if note:
            # 命中测试结果并进同一行：模型下一步就能看到「刚才到底点到了什么」，
            # 不必靠一张新截图去猜自己有没有点对
            base += f" | {note}"
        return base

    def _sleep(self, seconds: float) -> None:
        end = time.time() + max(0.0, seconds)
        while time.time() < end:
            self._check()
            time.sleep(min(0.1, end - time.time()))


class _Cancelled(Exception):
    """会话内取消信号。"""
