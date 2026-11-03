from __future__ import annotations

import json
import os
import socket
import subprocess
import threading
import time
import urllib.request
import uuid

import websocket

from ..core.config import APP_DIR
from .imagex import ImageXUploader

PORTABLE_CHROME = os.path.join(APP_DIR, "bin", "chrome-win64", "chrome.exe")
CHROME_CANDIDATES = [
    PORTABLE_CHROME,  # 随程序附带的便携版，优先使用
    os.path.expandvars(r"%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe"),
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    os.path.expandvars(r"%LOCALAPPDATA%\Microsoft\Edge\Application\msedge.exe"),
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
]
PROFILE_DIR = os.path.join(APP_DIR, "_chrome_profile")
PORT = 9222


def find_chrome() -> str | None:
    for p in CHROME_CANDIDATES:
        if os.path.exists(p):
            return p
    return None

# 豆包 Web 端固定查询参数（device_id / web_id 由首次登录时生成，保存在本地）
BASE_QUERY = (
    "version_code=20800&language=zh&device_platform=web&doubao_device_platform=web"
    "&aid=497858&real_aid=497858&pkg_type=release_version"
    "&device_id={device_id}&pc_version=3.43.5&doubao_pc_version=3.43.5"
    "&web_id={web_id}&tea_uuid={web_id}&region=CN&sys_region=CN"
    "&samantha_web=1&web_platform=browser&use-olympus-account=1"
)


# 13 是**账号接口**（/passport/account/info/v2/）的 error_code，只在
# ``AuthError`` 中有意义；聊天接口的 code 语义不同，不能套用该值。
# 误判为 auth 会导致 Agent 直接提示「登录态已失效」并结束任务。

# 聊天接口的风控/限流错误码。响应体形如
# {"code":710022004,"message":"rate limited","error_detail":{"ext":{"decision":
#  "{\"type\":\"verify\",\"verify_scene\":\"doubao_message_web\"}"}}}，
# 外层展示文案是「系统错误」。它表示「请求太快，稍后再来」，不是故障。
# 710022002 是同一类限制的另一条文案：「当前服务访问频繁，请稍后重试」。
RATE_LIMIT_CODES = (710022004, 710022002)


def _as_int(code) -> int:
    """错误码可能是字符串/嵌套结构，别让 int() 在构造异常时炸掉。"""
    try:
        return int(code or 0)
    except (TypeError, ValueError):
        return 0


def classify_failure(code: int = 0, msg: str = "") -> str:
    """把一次失败归类，供上层决定「要不要重试」。

    返回值就是 ``BridgeError.kind``：

    * ``rate_limit`` —— 服务端风控限流（710022004 / rate limited），只能等它放开；
    * ``transient``  —— 网络抖动、超时、页面被回收等临时故障，短退避重试即可；
    * ``auth``       —— 登录态失效，重试一万次也一样，必须重新登录；
    * ``fatal``      —— 永久失败（图片不存在之类），别重试。

    只看服务端原话，不在文案里猜不存在的类别。
    """
    code = _as_int(code)
    msg = msg or ""
    # 710022004 / 710022002 = 服务端风控限流（响应体里分别是 "rate limited" 和
    # 「当前服务访问频繁」，外层文案常被包成「系统错误」）。这不是故障，
    # 是服务端要求降速：立刻重发只会继续被拒，必须等它自己放开。
    if (code in RATE_LIMIT_CODES or "rate limited" in msg.lower()
            or "访问频繁" in msg):
        return "rate_limit"
    if "图片不存在" in msg:
        return "fatal"
    # auth 仅依据服务端原始错误信息。不应使用「未登录/请登录/重新登录」这类宽泛措辞：它们大量
    # 出现在界面文案和操作提示里（例如「按 Ctrl+L 重新登录后再朗读」），
    # 一旦误判为 auth，Agent 会将登录态判定为失效并结束任务。真正的登录判定在 account()，
    # 它不认就抛 AuthError，那个才叫准。
    if any(h in msg for h in ("会话过期", "登录已过期", "登录态已过期",
                              "登录态无效", "登录态已失效")):
        return "auth"
    if any(h in msg for h in ("超时", "timeout", "连接中断", "发送失败",
                              "页面被刷新", "标签被浏览器回收", "图片上传")):
        return "transient"
    # 认不出来的按瞬时处理：短退避试一下，总比直接放弃强
    return "transient"


class BridgeError(RuntimeError):
    """桥接/服务端错误。

    ``code`` 是服务端错误码（0 = 未知）；``kind`` 由 :func:`classify_failure`
    给出，上层据此决定要不要重试。

    ``generation`` 标记「服务端已经收到请求、但这一轮生成失败」（对应流式
    协议里 event_type=2005 的错误事件）。这类失败与网络无关，重发同一个
    会话往往还是失败，正确做法是换一个新会话重发。
    """

    def __init__(self, msg, code: int = 0, kind: str | None = None,
                 generation: bool = False):
        super().__init__(msg)
        self.code = _as_int(code)
        self.kind = kind or classify_failure(self.code, str(msg))
        self.generation = generation


class AuthError(BridgeError):
    """登录态缺失或不被服务端认可——这种错误唯一正确的处理是重新登录。"""

    def __init__(self, msg, code: int = 0):
        super().__init__(msg, code, kind="auth")


def as_bridge_error(exc_or_msg, prefix: str = "") -> BridgeError:
    """把任意异常/字符串统一成带 ``kind`` 的 :class:`BridgeError`。

    已经是 BridgeError 时保留它的 code/kind，只补充前缀，不重新推断分类信息。
    """
    if isinstance(exc_or_msg, BridgeError):
        if not prefix:
            return exc_or_msg
        return BridgeError(f"{prefix}{exc_or_msg}",
                           exc_or_msg.code, exc_or_msg.kind,
                           generation=getattr(exc_or_msg, "generation", False))
    if isinstance(exc_or_msg, BaseException):
        code = getattr(exc_or_msg, "code", 0) or 0
        return BridgeError(f"{prefix}{exc_or_msg}", code)
    return BridgeError(f"{prefix}{exc_or_msg}")


class _LinkDown(RuntimeError):
    """内部信号：CDP 连接已经断了（不是业务失败，重连就能恢复）。

    单独一种异常类型的作用是把自己和「接口返回错误」分开：前者要重连，
    后者绝不能重连（重连等于把浏览器的登录态/页面状态全重置一遍）。
    """


class _ChannelBroken(RuntimeError):
    """内部信号：页面级通道坏了（刷新页面即可恢复）。

    与 :class:`_LinkDown` 的区别：CDP 连接还在、浏览器还活着，只是页面里的
    请求/上传通道失效（页面被刷新、脚本上下文丢失、拦截钩子被重置）。
    ``sent`` 表示请求是否可能已到达服务端——True 时不得自动重发，避免重复消息。
    """

    def __init__(self, msg: str, sent: bool = True):
        super().__init__(msg)
        self.sent = sent


# --------------------------------------------------------------------------- #
# CDP 桥
# --------------------------------------------------------------------------- #
# 长跑相关参数。客户端可能连续开几天不关，Chrome 那边会因为内存压力丢标签、
# 渲染进程崩溃、系统休眠把 TCP 连接变成半开——这些都不是业务错误，必须能自愈。
KEEPALIVE_IDLE = 40.0        # 空闲多少秒发一次保活探针
KEEPER_TICK = 2.0            # 守护线程巡检间隔
CDP_TIMEOUT = 45.0           # 单次 CDP 往返上限（fetch 自身 30s，留出余量）
LOG_MAX_BYTES = 256 * 1024
# 说明：此处不设置重连冷却或重试退避等待。请求节奏与服务端保持一致：
# 出错后立即重建通道并重发，不额外延迟。

# 这些命令是只读或幂等的：断线重连后可以安全地重发一次。
# Runtime.evaluate 故意不在里面——它可能正在发消息/上传图片，
# 盲目重发会造成重复发送。
_REPLAY_SAFE = {
    "Network.enable", "Page.enable", "Runtime.enable", "DOM.enable",
    "Network.getCookies", "Network.setCookies",
    "Browser.getVersion", "Browser.getWindowForTarget", "Browser.setWindowBounds",
    "Page.bringToFront", "Page.navigate", "Page.reload",
    "DOM.getDocument", "DOM.querySelector",
    "Page.addScriptToEvaluateOnNewDocument",
}


def _log(msg: str) -> None:
    """把桥接层的异常写进 bridge.log（带大小上限）。

    界面上的报错一闪就过去了，而「用几个小时之后自己断」这种问题必须留下
    现场才有得查——这里只记连接层事件，不记任何正文/登录态。
    """
    try:
        path = os.path.join(APP_DIR, "bridge.log")
        try:
            if os.path.getsize(path) > LOG_MAX_BYTES:
                os.replace(path, path + ".1")
        except OSError:
            pass
        stamp = time.strftime("%m-%d %H:%M:%S")
        with open(path, "a", encoding="utf-8") as f:
            f.write(f"[{stamp}] {msg}\n")
    except Exception:  # noqa: BLE001
        pass


class _Call:
    """一次未完成的 CDP 请求：等响应 / 等错误 / 等超时。"""
    __slots__ = ("event", "result", "error", "method")

    def __init__(self, method: str):
        self.event = threading.Event()
        self.result = None
        self.error = None
        self.method = method


class ChromeBridge:
    def __init__(self, cookies_path: str, device_id: str, web_id: str):
        self.cookies_path = cookies_path
        self.device_id = device_id
        self.web_id = web_id
        self.proc: subprocess.Popen | None = None
        self.ws: websocket.WebSocket | None = None
        self.headless = False       # 当前实例是否为无头（无窗口）
        self._mid = 0

        # 并发模型：**一个读线程 + 请求号配对**，而不是「所有往返共用一把锁」。
        #
        # 早期版本为避免响应串号，采用整个桥接层串行执行命令的方式：界面拉取会话列表时
        # 流式分片必须排队，一次卡住就全线瘫痪（要等满 120 秒），并发起来以后
        # 还会随机读到对方的响应。CDP 本身是带 id 的多路复用协议，正确做法是
        # 写侧加锁、读侧单线程分发——每个请求只等自己那一条，互不阻塞。
        self._pending: dict[int, _Call] = {}
        self._pending_lock = threading.Lock()
        self._wr_lock = threading.Lock()
        self._reconnect_lock = threading.Lock()
        # 调试用：把 CDP 事件（Network.* 之类）攒起来，排查「网页到底发了什么请求」
        self._events: list[dict] = []
        self._events_lock = threading.Lock()
        self._events_on = False
        self._reader: threading.Thread | None = None
        self._gen = 0                # 连接代数：换代即代表旧连接上的一切作废

        self._last_io = 0.0          # 最近一次成功往返的时刻（保活用）
        self._reconnecting = False
        self._closing = False        # 正在关闭：此时绝不能触发「自动重连」
        self._keeper: threading.Thread | None = None
        self._keeper_stop = threading.Event()

    # ---------- 启动 ----------
    def _port_open(self) -> bool:
        with socket.socket() as s:
            s.settimeout(0.6)
            return s.connect_ex(("127.0.0.1", PORT)) == 0

    def _find_chrome(self) -> str:
        path = find_chrome()
        if not path:
            raise BridgeError("没有找到浏览器，请运行 setup_chrome.py 下载便携版 Chrome")
        return path

    def launch(self, headless: bool = False):
        """启动（或复用）受控浏览器。

        默认有头但放在屏幕外，并在连上后立即隐藏——豆包风控会识别无头 UA
        并拒绝服务（实测 headless 发消息必报「访问频繁」，有头正常），
        所以正常使用必须走有头，只是不让用户看见窗口。需要用户手动登录时再
        show_window 挪回可见区。
        """
        self._closing = False
        if self._port_open():
            # 端口开着不等于能用：刚 close 过的旧实例还会占一会儿端口，
            # 这时连上去只会拿到一个正在退出的浏览器。
            try:
                self._targets()
                self.headless = False
            except Exception:  # noqa: BLE001
                self._wait_port_free()
        if not self._port_open():
            self._start_chrome(headless)
            self.headless = headless

        targets = self._targets()
        page = next((t for t in targets if t["type"] == "page"), None)
        if not page:
            with urllib.request.urlopen(f"http://127.0.0.1:{PORT}/json/new?about:blank",
                                        timeout=10) as r:
                page = json.loads(r.read().decode())

        # 先摘掉旧连接再建新的：否则每次 launch 都会漏一个 socket，
        # 旧读线程还会把新连接的响应抢走。
        self._close_ws("重新建立连接")
        ws = websocket.create_connection(
            page["webSocketDebuggerUrl"], timeout=CDP_TIMEOUT,
            max_size=64 * 1024 * 1024, suppress_origin=True)
        # 读线程要能定期醒来判断「这一代连接是否已被替换」，5 秒的读超时
        # 对本地 CDP 来说足够宽松（帧都是毫秒级到达），不会切坏帧。
        ws.settimeout(5.0)
        with self._pending_lock:
            self._mid = 0
            self.ws = ws
            gen = self._gen
        self._start_reader(ws, gen)
        self.send("Network.enable")
        self.send("Page.enable")
        self.send("Runtime.enable")
        self._last_io = time.time()
        self.start_keeper()
        if not headless:
            # 连上后立刻把窗口藏起来（移到屏幕外 + ShowWindow(SW_HIDE)），正常使用
            # 全程看不到浏览器；需要手动登录时 show_window 会把它挪回可见区。
            try:
                self.hide_window()
            except Exception:  # noqa: BLE001
                pass

    def _start_chrome(self, headless: bool):
        """真正拉起 Chrome 进程。

        无头模式（--headless=new）不产生任何窗口；有头模式则把窗口放到屏幕外，
        等需要用户登录时再由 show_window 挪回可见区。无头起不来时自动回退有头，
        保证任何机器上都至少能跑。
        """
        chrome = self._find_chrome()
        args = [
            chrome,
            f"--remote-debugging-port={PORT}",
            f"--user-data-dir={PROFILE_DIR}",
            "--remote-allow-origins=*",
            "--no-first-run",
            "--no-default-browser-check",
            # 上次是被强制结束时 Chrome 会弹「要恢复页面吗？」，
            # 那个气泡会一直挂在屏幕外，关掉它
            "--hide-crash-restore-bubble",
            "--disable-blink-features=AutomationControlled",
            "--window-size=1280,900",
        ]
        if headless:
            # 新无头：无窗口，支持完整网络栈与 CDP；关掉沙箱/GPU 规避部分机器的
            # 权限与渲染报错（本地受控实例，不涉及不可信页面执行）
            args += ["--headless=new", "--no-sandbox", "--disable-gpu"]
        else:
            args += ["--window-position=-32000,-32000"]
        args.append("about:blank")

        self.proc = subprocess.Popen(args, stdout=subprocess.DEVNULL,
                                     stderr=subprocess.DEVNULL)
        for _ in range(40):
            if self._port_open():
                return
            time.sleep(0.25)

        # 起不来：先把残进程收掉，再回退有头重试一次
        proc, self.proc = self.proc, None
        if proc is not None:
            try:
                proc.terminate()
                proc.wait(timeout=5)
            except Exception:  # noqa: BLE001
                try:
                    proc.kill()
                except Exception:  # noqa: BLE001
                    pass
        self._wait_port_free()
        if headless:
            self._start_chrome(False)
        else:
            raise BridgeError("Chrome 调试端口未就绪")

    def _wait_port_free(self, timeout: float = 8.0):
        deadline = time.time() + timeout
        while time.time() < deadline and self._port_open():
            time.sleep(0.2)

    def shutdown(self, timeout: float = 8.0):
        """断开连接并结束受控浏览器，等端口真正释放后再返回。

        先发 CDP 的 Browser.close：这条命令不关心浏览器是谁拉起的，
        只要连接还在就能让整个实例退出——之前只 terminate 自己拉起的
        那个进程，遇到「复用了上一次残留实例」（proc 为空）就完全关不掉，
        浏览器窗口和页面会一直留在那儿。
        """
        self._closing = True
        self.stop_keeper()
        try:
            if self.ws is not None:
                # 关浏览器时绝不能触发自动重连（那会把刚关掉的浏览器又拉起来）
                self.send("Browser.close", _retry=False)
        except Exception:  # noqa: BLE001
            pass
        self._close_ws("正常关闭")
        proc, self.proc = self.proc, None
        if proc is not None and proc.poll() is None:
            try:
                proc.terminate()
                proc.wait(timeout=5)
            except Exception:  # noqa: BLE001
                try:
                    proc.kill()
                except Exception:  # noqa: BLE001
                    pass
        if proc is not None:
            self._wait_port_free(timeout)

    def _targets(self):
        with urllib.request.urlopen(f"http://127.0.0.1:{PORT}/json/list", timeout=10) as r:
            return json.loads(r.read().decode())

    # ---------- 基础调用 ----------
    def send(self, method, _replay: bool | None = None, _retry: bool = True, **params):
        """发一条 CDP 命令并等它的响应。

        与早先版本的区别（这是「用久了桥接就废掉」的根因）：

        * 连接断了会**自动重连**，而不是把坏掉的 socket 留在 ``self.ws`` 里；
          以前一旦 recv 抛异常，``self.ws`` 仍然非 None，之后每条命令都往死连接
          上写，于是从那一刻起整个客户端都废了，只能重启程序。
        * 超时/断开的异常统一转成 ``_LinkDown``，再按命令是否幂等决定要不要重发，
          绝不把 ``WebSocketConnectionClosedException`` 直接抛给界面。
        """
        try:
            return self._send_once(method, params)
        except _LinkDown as exc:
            if not _retry or self._reconnecting or self._closing:
                raise BridgeError(f"浏览器连接已断开：{exc}") from exc
            if _replay is None:
                _replay = method in _REPLAY_SAFE
            self._reconnect(str(exc))
            if not _replay:
                # 非幂等命令（发消息、上传、点按）不能自动重发，否则会重复提交；
                # 但连接已经恢复，让调用方/用户重试一次即可。
                raise BridgeError(
                    f"浏览器连接中断过一次，现已自动恢复，请重试刚才的操作（{exc}）"
                ) from exc
            try:
                return self._send_once(method, params)
            except _LinkDown as exc2:
                raise BridgeError(f"浏览器连接刚恢复又断开：{exc2}") from exc2

    def _send_once(self, method: str, params: dict):
        """发一条命令并等**它自己**的响应；不同请求之间不互相阻塞。"""
        with self._pending_lock:
            ws = self.ws
            if ws is None or not getattr(ws, "connected", False):
                raise _LinkDown("连接不存在")
            self._mid += 1
            mid = self._mid
            call = _Call(method)
            self._pending[mid] = call
            gen = self._gen

        try:
            with self._wr_lock:
                ws.send(json.dumps({"id": mid, "method": method, "params": params}))
        except Exception as exc:  # noqa: BLE001
            with self._pending_lock:
                self._pending.pop(mid, None)
            # 写失败就是连接坏了：交给读线程/上层去重连
            self._close_ws(f"写入失败（{type(exc).__name__}）", expect=ws)
            raise _LinkDown(f"写入失败（{type(exc).__name__}）") from exc

        try:
            if not call.event.wait(CDP_TIMEOUT):
                raise _LinkDown(f"{method} 等待 {int(CDP_TIMEOUT)}s 无响应")
            # 先看读线程给的错误（信息更准），再看代数是否被换过
            if call.error is not None:
                raise call.error
            if gen != self._gen:
                raise _LinkDown("等待响应期间连接已重建")
            self._last_io = time.time()
            return call.result if call.result is not None else {}
        finally:
            with self._pending_lock:
                self._pending.pop(mid, None)

    # ---------- 读线程 ----------
    def _start_reader(self, ws, gen: int) -> None:
        self._reader = threading.Thread(target=self._reader_loop, args=(ws, gen),
                                        daemon=True, name="cdp-reader")
        self._reader.start()

    def capture_events(self, on: bool = True) -> None:
        """开始/停止收集 CDP 事件（只用于排查，正常流程不打开）。"""
        with self._events_lock:
            self._events.clear()
        self._events_on = on

    def take_events(self) -> list[dict]:
        with self._events_lock:
            out, self._events = self._events, []
        return out

    def _reader_loop(self, ws, gen: int) -> None:
        """唯一的读端：把响应按 id 派发给等待中的调用，事件通知直接丢弃。

        之所以必须只有一个线程 recv：WebSocket 是字节流，两个线程同时读会把
        一帧拆成两半（随机丢响应/串号）。一个线程读、N 个线程等，才是并发的
        正确形态。
        """
        reason = ""
        try:
            while True:
                with self._pending_lock:
                    if self._gen != gen:
                        return                    # 这一代连接已被替换，退出
                try:
                    raw = ws.recv()
                except websocket.WebSocketTimeoutException:
                    continue                      # 空闲超时：连接还在，继续读
                except (websocket.WebSocketConnectionClosedException,
                        websocket.WebSocketProtocolException,
                        websocket.WebSocketPayloadException) as exc:
                    reason = f"连接被关闭（{type(exc).__name__}）"
                    return
                except OSError as exc:
                    reason = f"套接字异常（{exc}）"
                    return
                if not raw:
                    reason = "对端关闭了连接"
                    return
                try:
                    msg = json.loads(raw)
                except (TypeError, ValueError):
                    continue

                # 页面崩溃、调试会话被终止：Chrome 只推送事件，不返回响应。
                # 此前这类事件被静默丢弃，等待方会一直阻塞到超时。
                if msg.get("method") in ("Inspector.targetCrashed",
                                        "Inspector.detached"):
                    reason = "页面已崩溃或调试会话被断开"
                    return

                rid = msg.get("id")
                if rid is None:
                    if self._events_on:
                        with self._events_lock:
                            self._events.append(msg)
                            if len(self._events) > 3000:
                                del self._events[:1000]
                    continue                      # 普通事件通知
                with self._pending_lock:
                    call = self._pending.pop(rid, None)
                if call is None:
                    continue                      # 已被超时清理掉的迟到响应
                if "error" in msg:
                    call.error = BridgeError(f"{call.method}: {msg['error']}")
                else:
                    call.result = msg.get("result", {})
                call.event.set()
        finally:
            # 读线程退出 = 这条连接废了：唤醒所有等待者，并摘掉 ws，
            # 让下一次调用立刻走重连，而不是继续往死 socket 上写。
            #
            # 但如果代数已经变了（说明有人先一步关掉并换了新连接），这里必须
            # 立刻收手：pending 里现在装的都是新连接的请求，盲目清空会把
            # 别人刚发出的请求一起杀掉。
            if self._gen == gen:
                reason = reason or "连接已关闭"
                _log(reason)
                self._fail_all(reason)
                self._close_ws(reason, expect=ws)

    def _fail_all(self, reason: str) -> None:
        with self._pending_lock:
            calls = list(self._pending.values())
            self._pending.clear()
        for call in calls:
            call.error = _LinkDown(reason)
            call.event.set()

    # ---------- 连接自愈 ----------
    def alive(self) -> bool:
        ws = self.ws
        return ws is not None and bool(getattr(ws, "connected", False))

    def _close_ws(self, reason: str = "", expect=None):
        """作废当前连接：换代 + 摘 socket + 唤醒所有等待者。

        ``expect`` 用于防止误伤：读线程/写失败的线程只应该关掉**自己那条**
        连接，不能把别的线程刚重连好的新连接一起摘掉。
        """
        with self._pending_lock:
            if expect is not None and self.ws is not expect:
                return
            self._gen += 1
        ws, self.ws = self.ws, None
        if ws is not None:
            try:
                ws.close()
            except Exception:  # noqa: BLE001
                pass
        self._fail_all(reason or "连接已关闭")
        if reason:
            _log(f"断开 CDP 连接：{reason}")

    def _reset_hook_state(self):
        """重连后上传钩子的 id 属于旧 session，必须作废，否则新页面不会再注入。"""
        cls = globals().get("DoubaoAPI")
        if cls is not None:
            cls._UPLOAD_HOOK_ID = None

    def _reconnect(self, reason: str = "") -> None:
        """重建 CDP 连接（必要时重新拉起浏览器）。

        不设置冷却等待：上一次连接失败后立即重试，请求节奏与网页端保持一致。
        """
        if self._closing:
            raise BridgeError(f"程序正在退出，不再重连浏览器（{reason}）")
        with self._reconnect_lock:
            if self._reconnecting:
                raise BridgeError(f"正在重连浏览器：{reason}")
            self._reconnecting = True
            try:
                _log(f"开始自动重连：{reason}")
                self._close_ws(reason)
                self._reset_hook_state()
                self.launch()
                # 重连拿到的可能是一个新开的空白页（原标签被浏览器回收了），
                # 这时所有相对路径的 fetch 都会 404，必须先把页面拉回豆包。
                self._ensure_doubao_page()
                self._last_io = time.time()
                _log("自动重连成功")
            except Exception as exc:  # noqa: BLE001
                _log(f"自动重连失败：{exc}")
                raise BridgeError(f"浏览器连接已断开，自动重连失败：{exc}") from exc
            finally:
                self._reconnecting = False

    def _ensure_doubao_page(self, timeout: float = 25.0) -> bool:
        """确保当前标签页停在豆包页面（重连后可能落到 about:blank）。"""
        href = self.page_href()
        if "doubao.com" in href:
            return True
        try:
            self.send("Page.navigate", url="https://www.doubao.com/chat/")
        except BridgeError as exc:
            _log(f"重连后导航到豆包失败：{exc}")
            return False
        deadline = time.time() + timeout
        while time.time() < deadline:
            href = self.page_href()
            if "doubao.com" in href:
                time.sleep(1.2)          # 让页面脚本有机会跑起来
                return True
            time.sleep(0.5)
        _log("重连后页面仍未回到豆包")
        return False

    def ensure_alive(self) -> bool:
        """外部入口用：连接坏了就地修一次，修不好给出可读的错误。"""
        if self.alive():
            return True
        self._reconnect("调用前检查发现连接已断开")
        return True

    # ---------- 保活守护线程 ----------
    def start_keeper(self) -> None:
        """后台巡检连接：空闲时发探针，断了就地重连。

        长时间挂着不操作时（切走干别的、晚上不关），Chrome 的渲染进程可能被
        系统回收、休眠唤醒会让 socket 变成半开。没有这个线程，用户下一次点
        「发送」才会发现连接早断了——表现就是「用久了桥接自己失败」。
        """
        if self._keeper is not None and self._keeper.is_alive():
            return
        self._keeper_stop.clear()
        self._keeper = threading.Thread(target=self._keeper_loop, daemon=True,
                                        name="cdp-keeper")
        self._keeper.start()

    def stop_keeper(self) -> None:
        self._keeper_stop.set()
        self._keeper = None

    def _keeper_loop(self) -> None:
        while not self._keeper_stop.wait(KEEPER_TICK):
            if self._keeper_stop.is_set():
                return
            if self._reconnecting:
                continue
            if time.time() - self._last_io < KEEPALIVE_IDLE:
                continue                      # 最近有往来，不需要探针
            try:
                # 用 Runtime.evaluate 而不是轻量的 Browser.getVersion：前者能同时
                # 验证「执行上下文还在」——标签被浏览器丢进内存节省模式时，
                # 只有它会失败，正好用来触发重连。
                # 这里直接走 _send_once：探针失败就要重连，不需要 send 的重试语义。
                self._send_once("Runtime.evaluate",
                                {"expression": "1", "returnByValue": True})
                self._last_io = time.time()
            except Exception as exc:  # noqa: BLE001
                _log(f"保活探针失败：{exc}")
                try:
                    self._reconnect("保活探针失败")
                except BridgeError as exc2:
                    _log(f"保活重连未成功：{exc2}")

    def evaluate(self, expression: str, await_promise: bool = False,
                 _replay: bool | None = None):
        """在页面里执行 JS。

        ``_replay`` 只对**确定只读**的表达式传 True：断线重连后可以安全补发。
        默认不补发——表达式里可能正带着一次 POST（发消息、上传），重发会重复提交。
        """
        res = self.send("Runtime.evaluate", _replay=_replay, expression=expression,
                        returnByValue=True, awaitPromise=await_promise)
        if res.get("exceptionDetails"):
            detail = res["exceptionDetails"]
            raise BridgeError(detail.get("exception", {}).get("description") or str(detail))
        return res.get("result", {}).get("value")

    def page_href(self) -> str:
        try:
            return self.evaluate("location.href", _replay=True) or ""
        except BridgeError:
            return ""

    def page_eval(self, expression: str, await_promise: bool = False):
        """在豆包页面中执行脚本；页面被导航到其他地址或崩溃时，先恢复页面再执行一次。

        只在这种情况下重试一次：**页面根本不在豆包域**（标签被浏览器回收成
        about:blank、崩溃页、被误导航到其他地址）。页面在豆包域却执行失败，说明是业务
        或脚本层面的错误，重试没有意义，直接抛给调用方。
        """
        try:
            return self.evaluate(expression, await_promise=await_promise)
        except BridgeError:
            if "doubao.com" in self.page_href():
                raise
            if not self._ensure_doubao_page():
                raise
            return self.evaluate(expression, await_promise=await_promise)

    # ---------- 登录态 ----------
    def inject_cookies(self, cookies: dict | None = None):
        if cookies is None:
            # 未显式传入时优先用长期存储，再退回旧的 cookies.json
            from .cookies import load_store

            cookies = load_store()
            if not cookies and os.path.exists(self.cookies_path):
                with open(self.cookies_path, encoding="utf-8") as f:
                    cookies = json.load(f)
            if not cookies:
                raise BridgeError("没有可用的登录态")
        exp = int(time.time()) + 86400 * 60
        payload = [{"name": n, "value": m["value"], "domain": m["domain"],
                    "path": m.get("path") or "/", "secure": True, "expires": exp}
                   for n, m in cookies.items() if m.get("value")]
        if not payload:
            raise BridgeError("登录态为空")
        self.send("Network.setCookies", cookies=payload)
        return len(payload)

    def open_doubao(self):
        self.send("Page.navigate", url="https://www.doubao.com/chat/")
        deadline = time.time() + 40
        href = ""
        while time.time() < deadline:
            try:
                href = self.evaluate("location.href") or ""
            except BridgeError:
                href = ""
            if "doubao.com" in href:
                break
            time.sleep(0.5)
        # 等页面脚本跑起来
        for _ in range(40):
            try:
                if self.evaluate("document.readyState") == "complete":
                    break
            except BridgeError:
                pass
            time.sleep(0.3)
        time.sleep(1.2)
        return self.evaluate("location.href")

    def query(self) -> str:
        return BASE_QUERY.format(device_id=self.device_id, web_id=self.web_id)

    # ---------- 登录窗口 ----------
    def show_window(self):
        """把受控浏览器挪回屏幕，让用户能手动登录。

        必须复用同一个 Chrome 实例：同一 user-data-dir 再启动一次只会把命令行
        转交给已在运行的进程，而该窗口位于屏幕之外，用户点击后不会看到反应。
        """
        # 上一次登录成功后窗口被 ShowWindow(SW_HIDE) 隐藏，先恢复其可见状态
        self._show_native(True)
        win = self.send("Browser.getWindowForTarget")["windowId"]
        self.send("Browser.setWindowBounds", windowId=win,
                  bounds={"left": 120, "top": 80, "width": 1100, "height": 860,
                          "windowState": "normal"})
        self.send("Page.bringToFront")

    def hide_window(self):
        """登录完成后把浏览器移出屏幕并隐藏，不给用户留一个多余的窗口。

        两条路一起走：CDP 的 setWindowBounds 负责把窗口放到屏幕外，
        再做一次系统级 ShowWindow(SW_HIDE)——部分 Chrome 版本会把越界的
        坐标调整回可见区域，仅使用 CDP 时窗口仍会停留在屏幕可见范围内。
        """
        try:
            win = self.send("Browser.getWindowForTarget")["windowId"]
            self.send("Browser.setWindowBounds", windowId=win,
                      bounds={"left": -32000, "top": -32000, "windowState": "normal"})
        except (BridgeError, KeyError, TypeError):
            pass
        self._show_native(False)

    def _show_native(self, visible: bool) -> int:
        """直接对受控 Chrome 的顶层窗口 ShowWindow，按进程号精确认窗口。

        只在浏览器由本进程拉起时生效：复用用户自己开的浏览器时不能去动它。
        """
        if self.proc is None:
            return 0
        import ctypes
        from ctypes import wintypes

        u = ctypes.windll.user32
        pid = self.proc.pid
        hwnds: list[int] = []

        @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
        def enum(hwnd, _lparam):
            owner = wintypes.DWORD()
            u.GetWindowThreadProcessId(hwnd, ctypes.byref(owner))
            if owner.value == pid and u.GetWindowTextLengthW(hwnd) > 0:
                hwnds.append(hwnd)
            return True

        u.EnumWindows(enum, 0)
        SW_HIDE, SW_SHOWNORMAL = 0, 1
        for hwnd in hwnds:
            u.ShowWindow(hwnd, SW_SHOWNORMAL if visible else SW_HIDE)
        return len(hwnds)

    def read_cookies(self) -> dict:
        """从浏览器当前会话直接取 cookie——比读磁盘数据库可靠（磁盘可能还没落盘）。"""
        res = self.send("Network.getCookies",
                        urls=["https://www.doubao.com/", "https://www.doubao.com/chat/"])
        out = {}
        for c in res.get("cookies", []):
            if not c.get("value"):
                continue
            out[c["name"]] = {"value": c["value"],
                              "domain": c.get("domain") or ".doubao.com",
                              "path": c.get("path") or "/"}
        return out

    # ---------- 在页面里发 POST ----------
    def post_json(self, path: str, body: dict, timeout: str = "30") -> dict:
        js = """
        (async () => {
          const r = await fetch('%s?%s', {
            method: 'POST', credentials: 'include',
            headers: {'content-type': 'application/json; encoding=utf-8', 'agw-js-conv': 'str'},
            body: %s
          });
          return (await r.text());
        })()
        """ % (path, self.query(), json.dumps(json.dumps(body, ensure_ascii=False),
                                             ensure_ascii=False))
        raw = self.page_eval(js, await_promise=True)
        try:
            return json.loads(raw)
        except (TypeError, json.JSONDecodeError) as exc:
            raise BridgeError(f"{path} 返回非 JSON: {str(raw)[:200]}") from exc

    # ---------- 流式对话 ----------
    def start_stream(self, path: str, body: dict, sid: str = ""):
        """在页面里发起一次流式请求，返回流 id。

        ``sid`` 用来认「这条流还是不是本轮那条」。页面被刷新、标签被回收之后，
        ``window.__DB`` 会消失或换成别的流，只靠「有没有 __DB」判断不出来，
        于是老代码在这种情况下会静默当成「已结束」，界面上就是一条空回复。
        """
        sid = sid or uuid.uuid4().hex[:12]
        js = """
        (() => {
          // 上一次的流还没结束时先中止：同一会话并发两条生成请求，服务端会
          // 直接拒绝（事件 2005「系统错误」），此后这个会话怎么重发都失败。
          if (window.__DB && window.__DB.ctrl && !window.__DB.done) {
            try { window.__DB.ctrl.abort(); } catch (e) {}
          }
          const st = {sid:%s, buf:'', read:0, done:false, err:null};
          window.__DB = st;
          const ctrl = new AbortController();
          st.ctrl = ctrl;
          (async () => {
            try {
              const r = await fetch('%s?%s', {
                method:'POST', credentials:'include', signal: ctrl.signal,
                headers:{'content-type':'application/json; encoding=utf-8',
                         'agw-js-conv':'str', 'accept':'text/event-stream'},
                body: %s
              });
              if(!r.ok){ st.err = 'HTTP ' + r.status; st.done = true; return; }
              const reader = r.body.getReader(); const dec = new TextDecoder();
              while(true){
                const {value, done} = await reader.read();
                if(done) break;
                st.buf += dec.decode(value, {stream:true});
              }
              st.done = true;
            } catch(e) { st.err = String(e); st.done = true; }
          })();
          return st.sid;
        })()
        """ % (json.dumps(sid), path, self.query(),
               json.dumps(json.dumps(body, ensure_ascii=False), ensure_ascii=False))
        got = self.evaluate(js)
        return got or sid

    def pull_stream(self, sid: str = "") -> tuple[str, bool, str | None]:
        """返回 (新增文本, 是否结束, 错误)。

        ``err == 'no_stream'`` 表示这条流在页面里已经不存在（页面被刷新/标签被
        浏览器回收/被另一条流顶掉），调用方必须当成「中断」处理，不能当正常结束。
        """
        js = """
        (() => {
          if(!window.__DB || (window.__DB.sid !== %s && %s !== '')) {
            return ['', true, 'no_stream'];
          }
          const s = window.__DB.buf.slice(window.__DB.read);
          window.__DB.read = window.__DB.buf.length;
          return [s, window.__DB.done, window.__DB.err];
        })()
        """ % (json.dumps(sid or ""), json.dumps(sid or ""))
        chunk, done, err = self.evaluate(js) or ["", True, "empty"]
        return chunk, bool(done), err

    def abort_stream(self):
        try:
            self.evaluate("(() => { if(window.__DB) window.__DB.ctrl.abort(); return 1; })()")
        except Exception:  # noqa: BLE001
            pass


# --------------------------------------------------------------------------- #
# 豆包业务接口
# --------------------------------------------------------------------------- #
class DoubaoAPI:
    def __init__(self, cookies_path: str | None = None,
                 device_id: str = "7687366431038539314",
                 web_id: str = "7702742599338001983"):
        self.cookies_path = cookies_path or os.path.join(APP_DIR, "cookies.json")
        self.bridge = ChromeBridge(self.cookies_path, device_id, web_id)
        self.profile: dict = {}
        self._conv_id = "0"
        self._uploader = None      # ImageX 直传器（懒建，失败即弃用重建）

    # ---------- 生命周期 ----------
    def start(self, cookies: dict | None = None):
        """拉起浏览器并校验登录态；服务端不认这组 cookie 时抛 AuthError。"""
        self.begin()
        if cookies is not None:
            return self.use_cookies(cookies)
        return self.account()

    def begin(self):
        """只负责把浏览器跑起来并打开豆包，不碰登录态。"""
        self.bridge.launch()
        url = self.bridge.open_doubao()
        if "doubao.com" not in (url or ""):
            raise BridgeError(f"打开豆包失败: {url}")
        return url

    def use_cookies(self, cookies: dict):
        """换成指定登录态并让服务端认一次；不认就抛 AuthError。

        只在浏览器自身没有可用会话时才调用：拿旧 cookie 覆盖浏览器里更新的
        会话，会把本来能用的登录态顶掉，这是必须避免的。
        """
        self.bridge.inject_cookies(cookies)
        # 页面是在注入 cookie **之前**加载的，DOM 还停在未登录状态。只注入不刷新
        # 就会出现「账号接口能通、页面却还是登录页」的错位：截图里没有输入区，
        # 图片上传也找不到 file input（表现为「没找到网页的文件上传入口」）。
        # 强制重载一次，让 SPA 带着新 cookie 重新初始化。
        try:
            self.bridge.send("Page.reload", ignoreCache=False)
        except BridgeError:
            pass
        self.wait_chat_ready()
        return self.account()

    def wait_chat_ready(self, timeout: float = 25.0) -> bool:
        """等聊天输入区真正挂载（file input + 输入框都在），上传和发消息才可用。"""
        js = ("(() => {"
              "  const f = document.querySelector('input[type=file]');"
              "  const e = document.querySelectorAll("
              "    'textarea,[contenteditable=true]').length;"
              "  return !!(f && e);"
              "})()")
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                if self.bridge.evaluate(js):
                    return True
            except BridgeError:
                pass
            time.sleep(0.4)
        _log("聊天输入区迟迟没有挂载（上传/发送可能失败）")
        return False

    def current_cookies(self) -> dict:
        """浏览器当前真实生效的 cookie（可能已被服务端轮换过）。"""
        return self.bridge.read_cookies()

    def close(self):
        """退出程序：先把当前生效的 cookie 落盘（长期存储），再关浏览器。

        保存放在后台线程并最多等 2 秒：浏览器若已异常退出，CDP 往返可能长时间
        阻塞，此处不能阻塞退出流程。另有保底机制 _persist_and_enter：每次
        登录成功都会保存一次，本次保存失败不影响下次免登录。
        """
        def _save():
            try:
                live = self.current_cookies()
                from .cookies import save_store, is_logged_in
                # 退出时浏览器若掉登录态，存下来只会把本地那份好的顶掉
                if live and is_logged_in(live):
                    save_store(live)
            except Exception:  # noqa: BLE001
                pass

        try:
            t = threading.Thread(target=_save, daemon=True)
            t.start()
            t.join(2.0)
        except Exception:  # noqa: BLE001
            pass
        self.bridge.shutdown()

    # ---------- 登录窗口 ----------
    def open_login_window(self):
        """弹出登录窗口：需要可见窗口，若当前是无头实例先切成有头。"""
        if not self.bridge.alive():
            self.bridge.launch(headless=False)
        elif getattr(self.bridge, "headless", False):
            # 无头实例没有窗口可显示，只能关掉重开一个有头的
            self.bridge.shutdown()
            self.bridge.launch(headless=False)
        self.bridge.show_window()
        self.bridge.send("Page.navigate", url="https://www.doubao.com/chat/")

    def probe_login(self):
        """查当前浏览器是否已登录。已登录返回 (profile, cookies)，否则抛 AuthError。"""
        profile = self.account()
        return profile, self.bridge.read_cookies()

    def hide_browser(self):
        # 无头实例本来就没有窗口，不用处理
        if not getattr(self.bridge, "headless", False):
            self.bridge.hide_window()

    # ---------- 账号 ----------
    def account(self) -> dict:
        js = """
        (async () => {
          const r = await fetch('/passport/account/info/v2/?aid=497858',
                                {credentials:'include'});
          return await r.text();
        })()
        """
        raw = self.bridge.page_eval(js, await_promise=True)
        try:
            body = json.loads(raw)
        except (TypeError, ValueError) as exc:
            raise AuthError("账号接口返回异常，登录态可能已失效") from exc
        data = body.get("data") or {}
        if not isinstance(data, dict):
            data = {}
        # user_id 未登录时是数字 0，直接 str() 会变成 "0" —— 非空字符串是真值，
        # 下面那个 `if not uid` 就永远拦不住，界面会带着假账号继续跑。
        uid = data.get("user_id")
        if uid in (None, 0, "0", ""):
            uid = ""
        else:
            uid = str(uid)
        # 未登录的真实响应：HTTP 200 + data.error_code=13 + name=account_info_error，
        # 顶层没有 code/error_code，只能从 data 里读。
        err = data.get("error_code") or 0
        name = data.get("name") or data.get("screen_name") or ""
        if not uid or err or name == "account_info_error":
            reason = data.get("description") or "会话过期，请重新登录"
            raise AuthError(f"登录态无效：{reason}"
                            + (f"（错误码 {err}）" if err else ""))
        self.profile = {
            "name": name,
            "avatar": data.get("avatar_url", ""),
            "uid": uid,
            "mobile": data.get("mobile", ""),
        }
        return self.profile

    # ---------- 会话 ----------
    def list_conversations(self, limit: int = 50) -> list[dict]:
        body = {
            "cmd": 3200,
            "uplink_body": {"pull_recent_conv_chain_uplink_body": {
                "limit": limit,
                "filter": {"conversation_type": [], "project_filter": 0, "device_filter": 1},
                "message_count_per_conv": 0, "api_version": 1, "conv_version": 0,
                "direction": 3,
                "option": {"not_need_message": True, "need_complete_conversation": True,
                           "need_coco_bot": True, "need_pc_pin_chain": True,
                           "pc_pin_query_type": 1, "exclude_archive": True,
                           "only_archive": False}}},
            "sequence_id": str(uuid.uuid4()), "channel": 2, "version": "1",
        }
        data = self.bridge.post_json("/im/chain/recent_conv", body)
        cells = (data.get("downlink_body", {})
                     .get("pull_recent_conv_chain_downlink_body", {})
                     .get("cells", []))
        result = []
        for cell in cells:
            conv = cell.get("conversation") or {}
            cid = conv.get("conversation_id") or cell.get("id")
            if not cid:
                continue
            # badge_count 是「未读条数」，read_badge_count 是「已读到第几条」，
            # 网页端就是拿这两个相减算红点数字（见前端 conversation 映射）。
            badge = int(conv.get("badge_count") or 0)
            read = int(conv.get("read_badge_count") or 0)
            result.append({
                "id": str(cid),
                "name": conv.get("name") or "未命名对话",
                "update_time": int(conv.get("update_time") or 0),
                "icon": conv.get("icon_url", ""),
                "unread": max(0, badge - read),
                # 置顶状态这个接口不返回，由 conversation_info() 单独补
                "pinned": bool(conv.get("pinned")),
            })
        result.sort(key=lambda x: x["update_time"], reverse=True)
        return result

    # ---------- 删除会话 ----------
    def delete_conversation(self, conversation_id: str) -> None:
        """删除一个会话（``delete_conversations`` 的单条封装）。"""
        self.delete_conversations([conversation_id])

    def delete_conversations(self, conversation_ids) -> None:
        """批量删除会话。

        网页端用的是批量接口 ``/samantha/im/conversation/batch_delete``
        （前端里叫 ``AGWBatchDeleteConversation``，参数
        ``{conversation_ids, delete_all}``），接口本身就收数组，所以多选删除
        只发一次请求，不用循环。

        注意：不存在的 id 也返回 ``code: 0``，所以不能拿返回码当「删没删掉」
        的证据——真正的确认是重新拉一次会话列表。
        """
        ids = []
        for cid in conversation_ids or []:
            cid = str(cid or "").strip()
            if cid and cid != "0" and cid not in ids:
                ids.append(cid)
        if not ids:
            raise ValueError("会话 id 无效")
        data = self.bridge.post_json(
            "/samantha/im/conversation/batch_delete",
            {"conversation_ids": ids, "delete_all": False})
        code = data.get("code")
        if code not in (None, 0):
            msg = data.get("msg") or data.get("message") or code
            raise BridgeError(f"删除会话失败：{msg}")

    # ---------- IM 通用请求 ----------
    def _im(self, path: str, cmd: int, uplink_key: str, payload: dict) -> dict:
        """IM 系列接口的统一外壳：``{cmd, uplink_body:{key: payload}}``。

        cmd 号来自网页端 ``chat.js`` 里的命令枚举（如 4150 = 改会话名），
        返回体用 ``status_code`` 判成败，和 ``code`` 不是一回事。
        """
        body = {
            "cmd": cmd,
            "uplink_body": {uplink_key: payload},
            "sequence_id": str(uuid.uuid4()), "channel": 2, "version": "1",
        }
        data = self.bridge.post_json(path, body)
        code = data.get("status_code")
        if code not in (None, 0):
            desc = data.get("status_desc") or data.get("message") or code
            raise BridgeError(f"{path} 失败：{desc}")
        return data

    # ---------- 会话详情 / 改名 / 置顶 / 已读 ----------
    def conversation_info(self, conversation_id: str) -> dict:
        """单个会话的详情（cmd 1110）。

        里面 ``pinned_time`` 有没有值就是「置顶了没有」——列表接口
        ``recent_conv`` 不返回这个字段，只能按会话单独查。
        """
        cid = str(conversation_id or "").strip()
        if not cid or cid == "0":
            raise ValueError("会话 id 无效")
        data = self._im("/im/conversation/info", 1110, "get_conv_info_uplink_body",
                        {"conversation_id": cid, "conversation_type": 3})
        info = (data.get("downlink_body", {})
                    .get("get_conv_info_downlink_body", {})
                    .get("conversation_info", {})) or {}
        return {
            "id": str(info.get("conversation_id") or cid),
            "name": info.get("name") or "",
            "pinned": bool(info.get("pinned_time")),
            "pinned_time": int(info.get("pinned_time") or 0),
            "badge_count": int(info.get("badge_count") or 0),
            "read_badge_count": int(info.get("read_badge_count") or 0),
            "update_time": int(info.get("update_time") or 0),
        }

    def rename_conversation(self, conversation_id: str, name: str) -> None:
        """改会话名（cmd 4150）。``conversation_type`` 必须是 3（单聊机器人）。"""
        cid = str(conversation_id or "").strip()
        if not cid or cid == "0":
            raise ValueError("会话 id 无效")
        new_name = (name or "").strip()
        if not new_name:
            raise ValueError("名字不能为空")
        self._im("/im/conversation/update_name", 4150,
                 "update_conversation_name_uplink_body",
                 {"conversation_id": cid, "name": new_name, "conversation_type": 3})

    def pin_conversation(self, conversation_id: str, pinned: bool = True) -> None:
        """置顶 / 取消置顶。

        走 ``/samantha/im/conversation/{pin,unpin}``，返回 ``code: 0`` 即成功；
        真正的状态要用 :meth:`conversation_info` 的 ``pinned_time`` 复核。
        """
        cid = str(conversation_id or "").strip()
        if not cid or cid == "0":
            raise ValueError("会话 id 无效")
        path = "/samantha/im/conversation/pin" if pinned else \
               "/samantha/im/conversation/unpin"
        data = self.bridge.post_json(path, {"conversation_id": cid})
        code = data.get("code")
        if code not in (None, 0):
            msg = data.get("msg") or data.get("message") or code
            raise BridgeError(f"{'置顶' if pinned else '取消置顶'}失败：{msg}")

    def mark_conversation_read(self, conversation_id: str) -> None:
        """把会话标成已读（cmd 2100），网页端对应的红点会一起消掉。"""
        cid = str(conversation_id or "").strip()
        if not cid or cid == "0":
            raise ValueError("会话 id 无效")
        self._im("/im/message/mark_conv_read", 2100, "mark_conv_read_uplink_body",
                 {"conversation_id": cid, "conversation_type": 3})

    def clear_context(self, conversation_id: str) -> str:
        """清除会话上下文（cmd 2101），返回新的 section id。

        这个只清「模型记得的上下文」，历史消息还在，和删除会话不是一回事。
        """
        cid = str(conversation_id or "").strip()
        if not cid or cid == "0":
            raise ValueError("会话 id 无效")
        data = self._im("/im/message/clear_context", 2101,
                        "clear_msg_context_uplink_body",
                        {"conversation_id": cid, "conversation_type": 3,
                         "scene_type": 0, "reason": 0})
        return str((data.get("downlink_body", {})
                        .get("clear_msg_context_downlink_body", {})
                        .get("new_section_id") or ""))

    # ---------- 消息反馈 ----------
    # 前端枚举：0 取消 / 1 赞 / 2 踩 / 3 更好 / 4 差不多 / 5 都不好 / 6 表单
    FEEDBACK_CANCEL = 0
    FEEDBACK_LIKE = 1
    FEEDBACK_UNLIKE = 2

    def send_feedback(self, conversation_id: str, message_id: str,
                      feedback_type: int) -> None:
        """给某条回复点「赞 / 踩」（cmd 2220），传 0 就是取消。"""
        cid = str(conversation_id or "").strip()
        mid = str(message_id or "").strip()
        if not cid or cid == "0":
            raise ValueError("会话 id 无效")
        if not mid:
            raise ValueError("消息 id 无效")
        self._im("/im/message/feedback_msg", 2220, "feedback_msg_uplink_body", {
            "conversation_id": cid, "message_id": mid, "conversation_type": 3,
            "feedback_type": int(feedback_type), "feedback_source": 1,
            "feedback_detail_types": [], "feedback_detail_type": 0,
            "feedback_detail_content": "",
        })

    # ---------- 历史消息 ----------
    def load_messages(self, conversation_id: str, limit: int = 30) -> list[dict]:
        body = {
            "cmd": 3100,
            "uplink_body": {"pull_singe_chain_uplink_body": {
                "conversation_id": str(conversation_id),
                "anchor_index": 9007199254740991,
                "conversation_type": 3, "direction": 1, "limit": limit,
                "ext": {}, "filter": {"index_list": []},
                "evaluate_ab_params": "", "evaluate_common_params": "",
                "option": {"lazy_load_strategy": 0}}},
            "sequence_id": str(uuid.uuid4()), "channel": 2, "version": "1",
        }
        data = self.bridge.post_json("/im/chain/single", body)
        if data.get("code") not in (None, 0):
            raise BridgeError(f"加载消息失败: {data.get('message') or data}")
        msgs = (data.get("downlink_body", {})
                    .get("pull_singe_chain_downlink_body", {})
                    .get("messages", []))
        out = []
        for m in msgs:
            text = self._extract_text(m)
            if not text:
                continue
            out.append({
                "id": str(m.get("message_id", "")),
                "role": "user" if int(m.get("user_type") or 0) == 1 else "assistant",
                "text": text,
                "index": int(m.get("index_in_conv") or 0),
            })
        out.sort(key=lambda x: x["index"])
        return out

    @staticmethod
    def _extract_text(msg: dict) -> str:
        blocks = msg.get("content_block") or []
        parts = []
        for b in blocks:
            tb = (b.get("content") or {}).get("text_block") or {}
            if tb.get("text"):
                parts.append(tb["text"])
        if parts:
            return "".join(parts)
        raw = msg.get("content") or ""
        if not raw:
            return ""
        try:
            obj = json.loads(raw)
            if isinstance(obj, dict):
                return obj.get("text", "")
        except json.JSONDecodeError:
            pass
        return raw

    # ---------- 图片上传（视觉能力的地基） ----------
    # 第一轮上传的快速判定上限：正常上传 1~2 秒即返回，这个等距内收不到结果
    # 即判定本地通道异常，直接刷新页面重建通道，不等待完整超时。
    QUICK_UPLOAD_TIMEOUT = 8.0
    # 豆包的图片上链是「网页自己那套」：prepare_upload → ApplyImageUpload → TOS 直传
    # → CommitImageUpload。纯 Python 重刻这套签名/直传链路既脆弱又容易被风控，
    # 因此复用网页自身的上传路径：把图片注入网页的 file input，由豆包前端完成上传，
    # 客户端只在页面中安装拦截钩子，取回 CommitImageUpload 的结果（ImageUri）。
    # 拿到 ImageUri 后，就能在 chat/completion 的 attachments 里引用它。
    _UPLOAD_HOOK = r"""
    (() => {
      window.__doubaoUp = window.__doubaoUp || [];
      const push = (t) => {
        try {
          window.__doubaoUp.push(t);
          if (window.__doubaoUp.length > 20) window.__doubaoUp.shift();
        } catch (e) {}
      };
      const KEY = 'CommitImageUpload';
      // 每层拦截各自打标记，可重入：页面运行一段时间后可能把 window.fetch
      // 换回原生实现（拦截随之丢失），这时单独补 fetch 那层即可，不会双重拦截。
      if (!(window.fetch && window.fetch.__doubaoPatched)) {
        const origFetch = window.fetch;
        const patchedFetch = async function(input, init) {
          const url = (typeof input === 'string') ? input : ((input && input.url) || '');
          const resp = await origFetch.apply(this, arguments);
          if (String(url).indexOf(KEY) >= 0) {
            try { push(await resp.clone().text()); } catch (e) {}
          }
          return resp;
        };
        patchedFetch.__doubaoPatched = true;
        window.fetch = patchedFetch;
      }
      if (!XMLHttpRequest.prototype.send.__doubaoPatched) {
        const oOpen = XMLHttpRequest.prototype.open;
        const oSend = XMLHttpRequest.prototype.send;
        const patchedSend = function(b) {
          if (String(this.__doubaoUrl || '').indexOf(KEY) >= 0) {
            this.addEventListener('load', () => {
              try { push(String(this.responseText)); } catch (e) {}
            });
          }
          return oSend.apply(this, arguments);
        };
        patchedSend.__doubaoPatched = true;
        XMLHttpRequest.prototype.send = patchedSend;
        XMLHttpRequest.prototype.open = function(m, u) {
          this.__doubaoUrl = String(u || '');
          return oOpen.apply(this, arguments);
        };
      }
      window.__doubaoAgentHook = true;
      return 'installed';
    })()
    """
    _UPLOAD_HOOK_ID = None

    def _ensure_upload_hook(self) -> None:
        """在当前页（以及之后的新文档）注入上传结果收集钩子，幂等且可自愈。

        「幂等」不能只靠类变量 ``_UPLOAD_HOOK_ID``：它属于某一次 CDP session，
        连接重建或页面刷新之后那个 id 就不作数了。所以这里每次都先问页面
        自己有没有钩子，缺了才重新注册。

        另外不能只看 ``__doubaoAgentHook`` 标记：页面运行一段时间后会把
        ``window.fetch`` 换回原生实现（拦截随之丢失，标记却还在），表现为
        连续若干次上传后收不到上传结果、永久超时。所以这里校验 fetch 那层
        拦截是否真实存在，不在就补装。
        """
        installed = False
        try:
            installed = bool(self.bridge.evaluate(
                "!!(window.fetch && window.fetch.__doubaoPatched)"))
        except BridgeError:
            installed = False
        if not installed and DoubaoAPI._UPLOAD_HOOK_ID is None:
            try:
                res = self.bridge.send("Page.addScriptToEvaluateOnNewDocument",
                                       source=self._UPLOAD_HOOK)
                DoubaoAPI._UPLOAD_HOOK_ID = res.get("identifier")
            except BridgeError:
                DoubaoAPI._UPLOAD_HOOK_ID = None
        # 当前文档里拦截缺失就补装（页面刷新、或 fetch 被重置后走这里）
        if not installed:
            self.bridge.evaluate(self._UPLOAD_HOOK)

    # 输入区附件卡片选择器。网页上传每张图片会在输入区生成一张卡片；本客户端
    # 直接调用 chat/completion 发送消息，不触发网页发送流程，因此这些卡片不会
    # 被网页自行清除。卡片数量达到上限（实测 10 张）后，网页会静默忽略新的文件
    # 选择：既不发 prepare_upload，也不返回错误，表现为第 11 张起上传始终无结果，
    # 刷新页面才能恢复。因此每轮网页上传前后都主动清空输入区。
    _ATTACH_CARDS = ("[data-testid=attachment-image-card],"
                     "[data-testid=attachment-file-card],"
                     "[data-testid=attachment-card],"
                     "[data-testid=attachment_area] [class*=card]")
    _ATTACH_DELETE = ("[data-testid=attachment-delete-btn],"
                      "[data-testid=attachment_area] button[aria-label*='删除'],"
                      "[data-testid=attachment_area] button[aria-label*='移除']")

    def attachment_count(self) -> int:
        """返回输入区当前附件卡片数量；读取失败时返回 -1。"""
        try:
            return int(self.bridge.evaluate(
                f"document.querySelectorAll('{self._ATTACH_CARDS}').length") or 0)
        except BridgeError:
            return -1

    def clear_attachments(self, keep: int = 0) -> int:
        """逐张删除输入区附件卡片，返回剩余数量。

        仅删除卡片，不操作发送按钮，也不修改输入框内容；逐张删除直至达到
        ``keep`` 指定的保留数量。
        """
        for _ in range(20):
            left = self.attachment_count()
            if left < 0 or left <= keep:
                return left
            try:
                clicked = int(self.bridge.evaluate(
                    "(() => {"
                    f"  const s = document.querySelector('{self._ATTACH_DELETE}');"
                    "  if (!s) return 0;"
                    # 删除按钮是 <svg>，没有 click() 方法，必须派发鼠标事件；
                    # React 的 onClick 挂在外层，bubbles 一定要给 true。
                    "  s.dispatchEvent(new MouseEvent('click',"
                    "    {bubbles: true, cancelable: true, view: window}));"
                    "  return 1;"
                    "})()") or 0)
            except BridgeError:
                return left
            if not clicked:
                return left
            time.sleep(0.08)
        return self.attachment_count()

    def _file_input_node(self, timeout: float = 20.0) -> int:
        """拿到网页里那个隐藏的 file input 的 nodeId（输入区渲染出来才有）。

        页面偶尔会残留多个 file input（上传组件重建时旧的没删），此时取
        **最后一个**：重建后的有效入口在 DOM 顺序中靠后，第一个可能为
        未接线的残留节点，向其注入文件不会生效。
        """
        self.bridge.send("DOM.enable")
        deadline = time.time() + timeout
        while time.time() < deadline:
            doc = self.bridge.send("DOM.getDocument", depth=1)
            root = doc["root"]["nodeId"]
            try:
                nodes = self.bridge.send("DOM.querySelectorAll", nodeId=root,
                                         selector="input[type=file]").get("nodeIds") or []
            except BridgeError:
                nodes = []
            if nodes:
                return int(nodes[-1])
            time.sleep(0.4)
        raise BridgeError("没找到网页的文件上传入口（输入区可能还没加载完）")

    @staticmethod
    def _parse_upload_result(text: str) -> dict | None:
        """从 CommitImageUpload 的响应里抽出 ImageUri / 尺寸等。"""
        try:
            data = json.loads(text)
        except Exception:
            return None
        result = (data or {}).get("Result") or {}
        plugins = result.get("PluginResult") or []
        uri, width, height, md5 = "", 0, 0, ""
        if plugins:
            p = plugins[0]
            uri = p.get("ImageUri") or p.get("SourceUri") or ""
            width = int(p.get("ImageWidth") or 0)
            height = int(p.get("ImageHeight") or 0)
            md5 = p.get("ImageMd5") or ""
        if not uri:
            stores = result.get("Results") or []
            if stores:
                uri = stores[0].get("Uri") or ""
        if not uri:
            return None
        return {"uri": uri, "width": width, "height": height, "md5": md5}

    def _recover_page(self) -> None:
        """重建页面通道：刷新页面、等待输入区就绪、重新安装上传拦截钩子。

        页面运行一段时间后通道可能失效（``window.fetch`` 拦截被页面重置、
        页面被刷新、脚本上下文丢失），表现为收不到上传结果或流式数据。
        刷新页面即可恢复；该现象与请求频率无关，因此不设置冷却等待。
        上传与会话请求共用此恢复流程。
        """
        try:
            self.bridge.send("Page.reload", ignoreCache=False)
        except BridgeError:
            pass
        self.wait_chat_ready()
        self._ensure_upload_hook()

    def _upload_round(self, path: str, timeout: float) -> dict | None:
        """执行一轮网页上传：注入文件，等待网页返回 CommitImageUpload 结果。

        不等待完整 ``timeout``：正常上传在 1~2 秒内返回结果；若 2.5 秒后拦截
        钩子已不存在（页面将 ``window.fetch`` 恢复为原生实现，结果无法获取），
        立即返回 None 交由上层恢复通道，以缩短恢复时间。
        """
        self._ensure_upload_hook()
        # 上传前清空输入区：输入区附件数量存在上限（实测 10 张），达到上限后
        # 网页不再处理新的文件选择，表现为上传无响应。
        self.clear_attachments()
        node = self._file_input_node(timeout=12.0)
        # 清除上一轮结果，避免将旧 URI 误判为本次结果；同时重置 input 的值，
        # 否则连续上传同一文件时不会触发 change 事件，网页不会执行上传。
        self.bridge.evaluate(
            "window.__doubaoUp = [];"
            "document.querySelectorAll('input[type=file]').forEach(function(i){"
            "  try { i.value = ''; } catch (e) {}"
            "}); 'cleared'")
        self.bridge.send("DOM.setFileInputFiles", files=[path], nodeId=node)
        started = time.time()
        deadline = started + timeout
        hook_checked = False
        while time.time() < deadline:
            raw = self.bridge.evaluate("JSON.stringify(window.__doubaoUp || [])")
            try:
                items = json.loads(raw or "[]")
            except Exception:
                items = []
            for txt in items:
                info = self._parse_upload_result(txt)
                if info:
                    return info
            if not hook_checked and time.time() - started > 2.5:
                hook_checked = True
                try:
                    alive = bool(self.bridge.evaluate(
                        "!!(window.fetch && window.fetch.__doubaoPatched)"))
                except BridgeError:
                    alive = False
                if not alive:
                    return None       # 通道坏：快速失败，上层自愈
            time.sleep(0.3)
        return None

    def _upload_direct(self, path: str) -> dict | None:
        """通过 ImageX 四步接口直传；失败返回 None，由网页通道处理。

        直传不经过网页输入区，不产生附件卡片，因此不受输入区附件数量上限
        限制。若登录态被服务端轮换导致失败，则销毁当前实例，下次重新获取
        凭证，避免重复使用失效 cookie。
        """
        try:
            if self._uploader is None:
                self._uploader = ImageXUploader(self.current_cookies())
            return self._uploader.upload(path)
        except Exception as exc:  # noqa: BLE001
            _log(f"图片直传失败，回退网页通道：{exc}")
            self._uploader = None
            return None

    def upload_image(self, path: str, timeout: float = 45.0) -> dict:
        """上传本地图片，返回 {'uri', 'width', 'height', 'md5'}。

        ``uri`` 即后续 attachments 中使用的 key。

        优先使用 ImageX 直传（见 :mod:`.imagex`）：通过 HTTP 直接调用上传
        接口，不操作网页输入区，因此不会产生附件卡片。网页通道的附件卡片
        无法从页面外部删除，达到 10 张后网页会静默丢弃新的文件选择，导致
        连续带图对话在第 11 轮上传失败。

        直传不可用时回退网页通道：先等待 ``QUICK_UPLOAD_TIMEOUT`` 秒，正常
        上传在 1~2 秒内返回结果；若仍无结果则判定通道异常，刷新页面重建后
        以完整时限重试一次。
        """
        path = os.path.abspath(path)
        if not os.path.exists(path):
            raise BridgeError(f"图片不存在：{path}")
        info = self._upload_direct(path)
        if info:
            return info
        try:
            info = self._upload_round(path, min(timeout, self.QUICK_UPLOAD_TIMEOUT))
        except BridgeError:
            info = None
        if info:
            # 已取得 ImageUri，立即清除输入区卡片，避免占用附件名额。
            self.clear_attachments()
            return info
        _log("上传通道不可用，刷新页面重建通道后重试")
        self._recover_page()
        try:
            info = self._upload_round(path, timeout)
        except BridgeError:
            info = None
        if info:
            self.clear_attachments()
            return info
        raise BridgeError("图片上传超时（网页没返回上传结果，刷新页面重建通道后仍失败）")

    # ---------- 对话 ----------
    def stream_chat(self, text: str, conversation_id: str = "0"):
        """生成器：逐段产出 ('delta', str) / ('conv', id) / ('error', msg) / ('done', None)。"""
        yield from self._stream(text, conversation_id, [])

    def vision_chat(self, text: str, image_path: str, conversation_id: str = "0"):
        """生成器：先上传图片，再带着图片提问。

        额外产出 ('image', {'uri':..., 'width':..., 'height':...})，方便界面显示进度。
        """
        try:
            info = self.upload_image(image_path)
        except Exception as exc:  # noqa: BLE001
            yield "error", as_bridge_error(exc, "图片上传失败：")
            return
        yield "image", info
        yield from self.chat_with_image(text, info["uri"], conversation_id)

    def chat_with_image(self, text: str, image_uri: str,
                        conversation_id: str = "0"):
        """带图提问，但图已经传好了（``image_uri`` 是 ``upload_image`` 返回的 key）。

        同一张截图重试时用它，别再传一次图——重复上传没有意义，徒增请求量。
        """
        yield from self._stream(text, conversation_id,
                                [{"type": "image", "key": image_uri}])

    def vision_ask(self, text: str, image_path: str,
                   conversation_id: str = "0") -> str:
        """带图提问的同步版本：返回完整回复文本。"""
        parts: list[str] = []
        error = ""
        for kind, payload in self.vision_chat(text, image_path, conversation_id):
            if kind == "delta":
                parts.append(payload)
            elif kind == "error":
                error = payload
                break
        if error and not parts:
            # 是 BridgeError 就原样抛：code/kind 是上层决定要不要重试的依据
            if isinstance(error, BaseException):
                raise error
            raise BridgeError(error)
        return "".join(parts)

    def _stream(self, text: str, conversation_id: str, attachments: list):
        """流式对话（带页面通道自愈）；attachments 决定这轮是纯文字还是带图。

        与上传共用 :meth:`_recover_page` 的通道恢复：页面被刷新、脚本上下文
        丢失、拦截钩子被重置时刷新页面重建通道。请求尚未发出的直接重发一轮；
        已经发出去的不自动重发（避免服务端出现重复消息），把中断如实交给上层。
        """
        try:
            yield from self._stream_once(text, conversation_id, attachments)
            return
        except _ChannelBroken as br:
            _log(f"会话通道异常（{br}），刷新页面重建通道")
            self._recover_page()
            if br.sent:
                yield "error", BridgeError(str(br), kind="transient")
                return
        # 请求没发出去，重建通道后重发一轮
        try:
            yield from self._stream_once(text, conversation_id, attachments)
        except _ChannelBroken as br:
            yield "error", BridgeError(str(br), kind="transient")

    def _stream_once(self, text: str, conversation_id: str, attachments: list):
        """单次流式请求；通道坏时抛 :class:`_ChannelBroken` 交给上层自愈。"""
        now = int(time.time() * 1000)
        # 是否要求服务端「新建会话」：只有首条（conversation_id 为空/0）才需要。
        # 只有首条消息才要求服务端新建会话；后续必须复用同一会话，
        # 否则服务端会把每条消息都当成新会话另起一个。
        is_new = str(conversation_id or "0") in ("0", "", "None")
        body = {
            "messages": [{
                "content": json.dumps({"text": text}, ensure_ascii=False),
                "content_type": 2001, "attachments": attachments, "references": [],
            }],
            "completion_option": {
                "is_regen": False, "with_suggest": True, "need_create_conversation": is_new,
                "launch_stage": 1, "is_replace": False, "is_delete": False,
                "message_from": 0, "use_deep_think": False, "use_auto_cot": False,
                "is_audio": False,
            },
            "conversation_id": str(conversation_id or "0"),
            "local_conversation_id": f"local_{now}",
            "local_message_id": str(now),
        }
        sid = uuid.uuid4().hex[:12]
        try:
            self.bridge.start_stream("/samantha/chat/completion", body, sid)
        except BridgeError as exc:
            # 这里失败说明请求根本没发出去：重建通道后可安全重发
            raise _ChannelBroken(f"发送失败：{exc}", sent=False)

        pending = ""
        got_delta = False
        finished = False
        deadline = time.time() + 300
        try:
            while time.time() < deadline:
                try:
                    chunk, done, err = self.bridge.pull_stream(sid)
                except BridgeError as exc:
                    # 拉流途中连接断了：桥接层已经尝试过重连；消息可能已发出，
                    # 通道由上层重建，本轮不重发。
                    raise _ChannelBroken(f"连接中断：{exc}", sent=True)
                if err == "no_stream":
                    # start_stream 明明成功过，现在页面里没有这条流了 = 页面被刷新/
                    # 标签被浏览器回收。老代码把这种情况当成「正常结束」，用户看到的是
                    # 一条空回复，也是最典型的「用久了就不对劲」。
                    raise _ChannelBroken(
                        "页面被刷新或标签被浏览器回收，本轮回复已中断", sent=True)
                if err:
                    yield "error", as_bridge_error(err)
                    return
                if chunk:
                    got_delta = True
                    pending += chunk
                    while "\n" in pending:
                        line, pending = pending.split("\n", 1)
                        for item in self._parse_sse_line(line):
                            yield item
                            if item[0] == "error":
                                return
                if done:
                    for item in self._parse_sse_line(pending):
                        yield item
                    yield "done", None
                    finished = True
                    return
                time.sleep(0.06)
            yield "error", BridgeError(
                "超时" if not got_delta else "回复超时（已收到的内容仍然有效）",
                kind="transient")
        finally:
            # 没有正常收尾（报错、超时，或调用方提前 break）就主动中止页面里的
            # 这条 fetch：留着它在后台继续生成，会让同一会话的下一条请求被
            # 服务端以「已有对话流正在输出」拒掉，表现为连续「系统错误」。
            if not finished:
                self.bridge.abort_stream()

    def _parse_sse_line(self, line: str):
        line = line.strip()
        if not line.startswith("data:"):
            return []
        payload = line[5:].strip()
        if not payload or payload == "[DONE]":
            return []
        try:
            obj = json.loads(payload)
        except json.JSONDecodeError:
            return []
        etype = obj.get("event_type")
        data = obj.get("event_data")
        if isinstance(data, str):
            try:
                data = json.loads(data)
            except json.JSONDecodeError:
                return []

        if etype == 2002 and isinstance(data, dict):
            cid = data.get("conversation_id")
            if cid:
                self._conv_id = str(cid)
                return [("conv", str(cid))]
            return []
        if etype == 2001 and isinstance(data, dict):
            content = (data.get("message") or {}).get("content") or ""
            try:
                inner = json.loads(content)
            except json.JSONDecodeError:
                return []
            text = inner.get("text") if isinstance(inner, dict) else None
            return [("delta", text)] if text else []
        if etype == 2005:
            msg = "生成失败"
            code = 0
            if isinstance(data, dict):
                detail = data.get("error_detail") or {}
                if not isinstance(detail, dict):
                    detail = {}
                msg = detail.get("message") or data.get("message") or msg
                # 错误码必须留住：它是「要不要重试」唯一可靠的依据，
                # 早先只取 message 就把 code 丢了，上层只能去文案里猜。
                code = (detail.get("code") or detail.get("error_code")
                        or data.get("code") or data.get("error_code") or 0)
            # 原始报文写进日志：这类「系统错误」文案很笼统，只有 detail 里的
            # 码和字段才能定位触发原因，出问题时可以直接查 bridge.log。
            _log(f"服务端返回生成失败：code={code} message={msg} "
                 f"detail={json.dumps(data, ensure_ascii=False)[:400]}")
            return [("error", BridgeError(msg, code, generation=True))]
        return []

    def stop(self):
        self.bridge.abort_stream()

    # ---------- 纯文字 Agent 决策 ----------
    def agent_decide(self, message: str, conversation_id: str = "0") -> tuple[str, str]:
        """把 Agent 拼好的提示词发给豆包，返回 (回复全文, 会话 id)。

        复用现有 ``stream_chat`` 的纯文本通道，不碰任何图片上传。会话 id 在
        首次（conversation_id="0"）发消息时由服务端分配，调用方拿去维持同一次
        Agent 任务的上下文即可。
        """
        cid = str(conversation_id or "0")
        parts: list[str] = []
        for kind, payload in self.stream_chat(message, cid):
            if kind == "delta":
                parts.append(payload)
            elif kind == "conv":
                cid = payload
            elif kind == "error":
                # 原样抛：BridgeError 上的 code/kind 是上层重试策略的依据
                if isinstance(payload, BaseException):
                    raise payload
                raise BridgeError(payload)
            elif kind == "done":
                break
        return "".join(parts), cid
