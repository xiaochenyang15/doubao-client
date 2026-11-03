"""错误分类与重试回归测试：分类判定 / 退避 / 单会话执行 / 截图上传缓存。

跑法：``python test_failures.py``（用装好 requirements.txt 的解释器；
随包的 ``bin\\python`` 是给用户代码执行用的精简环境，没有 wxPython /
websocket-client，跑不了本测试）。
"""
from __future__ import annotations

import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from doubao_client.services import api as api_mod  # noqa: E402
from doubao_client.services.api import (  # noqa: E402
    AuthError, BridgeError, as_bridge_error, classify_failure)
from doubao_client.agent import session as sess  # noqa: E402
from doubao_client.services import cookies as cookie_bridge  # noqa: E402

PASS, FAIL = 0, 0


def check(name: str, cond: bool, detail: str = "") -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ok   {name}")
    else:
        FAIL += 1
        print(f"  FAIL {name}  {detail}")


# ---------------------------------------------------------------- classify
print("\n[1] classify_failure —— 该不该重试的判定")
check("会话过期 → auth", classify_failure(0, "会话过期，请重新登录") == "auth")
check("登录态无效 → auth", classify_failure(0, "登录态无效：xx") == "auth")
check("图片不存在 → fatal", classify_failure(0, "图片不存在：C:/x.png") == "fatal")
check("超时 → transient", classify_failure(0, "回复超时") == "transient")
check("连接中断 → transient", classify_failure(0, "连接中断：xxx") == "transient")
check("认不出的 → transient（短退避试一下）",
      classify_failure(0, "未知错误") == "transient")
check("耗时 1429ms 是普通瞬时错误",
      classify_failure(0, "请求失败，耗时 1429ms") == "transient")
check("服务端通用错误文案归瞬时",
      classify_failure(0, "系统错误") == "transient")
check("710022004 + 「系统错误」→ 限流",
      classify_failure(710022004, "系统错误") == "rate_limit",
      classify_failure(710022004, "系统错误"))
check("rate limited 文案 → 限流",
      classify_failure(0, "rate limited") == "rate_limit")
check("710022002「访问频繁」→ 限流",
      classify_failure(710022002, "当前服务访问频繁，请稍后重试") == "rate_limit")
check("限流错误码构造出的 BridgeError 也是限流",
      BridgeError("系统错误", 710022004).kind == "rate_limit")
check("「频繁」类文案归瞬时",
      classify_failure(0, "操作过于频繁") == "transient")

print("\n[1b] 登录判定不能误伤（回归）")
# 13 是**账号接口**的 error_code，不能拿来判聊天接口的错误——
# 误判成 auth 会让 Agent 直接说「登录态已失效」收工。
check("code=13 但不是账号接口 → 不该判 auth",
      classify_failure(13, "") == "transient", classify_failure(13, ""))
check("code=13 + 无关文案 → 不该判 auth",
      classify_failure(13, "生成失败") == "transient")
# 「重新登录 / 未登录 / 请登录」大量出现在界面提示里，不能当 auth
check("TTS 提示语含「登录态已过期」→ auth 是对的",
      classify_failure(0, "豆包登录态已过期，按 Ctrl+L 重新登录后再朗读")
      == "auth", classify_failure(0, "豆包登录态已过期，按 Ctrl+L 重新登录后再朗读"))
check("纯操作提示不该判 auth",
      classify_failure(0, "请重新登录后再跑") == "transient",
      classify_failure(0, "请重新登录后再跑"))
check("未登录的界面文案不该判 auth",
      classify_failure(0, "未登录。登录后这里显示昵称") == "transient",
      classify_failure(0, "未登录。登录后这里显示昵称"))
check("错误码不是数字也不该炸",
      classify_failure("abc", "x") == "transient")
check("错误码是嵌套结构也不该炸",
      classify_failure({"code": 13}, "x") == "transient")


# ---------------------------------------------------------------- BridgeError
print("\n[2] BridgeError —— code/kind 必须一路带到底")
e = BridgeError("系统错误", 0)
check("构造时自动分类（瞬时）", e.kind == "transient", f"kind={e.kind}")
e2 = BridgeError("随便", 700000000)
check("非限流错误码不影响分类", e2.kind == "transient", f"kind={e2.kind}")
a = AuthError("登录态无效：xx")
check("AuthError 恒为 auth", a.kind == "auth", f"kind={a.kind}")
check("AuthError 是 BridgeError", isinstance(a, BridgeError))
check("str() 仍是原文案", str(e) == "系统错误", repr(str(e)))

w = as_bridge_error(e, "图片上传失败：")
check("as_bridge_error 保留 kind", w.kind == "transient", f"kind={w.kind}")
check("as_bridge_error 保留 code", w.code == 0)
check("as_bridge_error 加前缀", str(w).startswith("图片上传失败："), repr(str(w)))
w2 = as_bridge_error(e, "")
check("无前缀时原样返回同一对象", w2 is e)
w3 = as_bridge_error(ValueError("会话过期"), "发送失败：")
check("普通异常也能分类", w3.kind == "auth", f"kind={w3.kind}")


# ---------------------------------------------------------------- 限流
print("\n[3] 本地不得再有退避/冷却（请求节奏交给服务端）")
check("session 已无 _backoff 等待", not hasattr(sess, "_backoff"))
check("session 已无 BACKOFF_SHORT/BACKOFF_MAX",
      not hasattr(sess, "BACKOFF_SHORT") and not hasattr(sess, "BACKOFF_MAX"))
check("api 已无 RECONNECT_COOLDOWN 冷却",
      not hasattr(api_mod, "RECONNECT_COOLDOWN"))
check("_failure_kind 取 BridgeError.kind",
      sess._failure_kind(AuthError("x")) == "auth")
check("_failure_kind 对裸异常退回 transient",
      sess._failure_kind(RuntimeError("boom")) == "transient")
check("限流等待序列存在且递增",
      list(sess.RATE_LIMIT_WAITS) == sorted(sess.RATE_LIMIT_WAITS)
      and len(sess.RATE_LIMIT_WAITS) >= 3, str(sess.RATE_LIMIT_WAITS))
check("_failure_kind 认得 rate_limit",
      sess._failure_kind(BridgeError("系统错误", 710022004)) == "rate_limit")


# ---------------------------------------------------------------- 单会话执行
print("\n[4] 单会话执行：任意步数都不轮换，任务结束才删除会话")


class FakeAPI:
    """每次调用都按脚本返回：先失败若干次，再成功。"""

    def __init__(self, script):
        self.script = list(script)
        self.upload_calls = 0
        self.chat_calls = 0
        self.deleted = []

    def upload_image(self, path):
        self.upload_calls += 1
        return {"uri": f"uri{self.upload_calls}", "width": 1, "height": 1, "md5": "m"}

    def chat_with_image(self, text, uri, cid):
        self.chat_calls += 1
        yield from self._reply()

    def stream_chat(self, text, cid):
        self.chat_calls += 1
        yield from self._reply()

    def _reply(self):
        if self.script:
            item = self.script.pop(0)
            if isinstance(item, BaseException):
                yield "error", item
                return
        yield "conv", "cid-1"
        yield "delta", "ok"
        yield "done", None

    def delete_conversation(self, cid):
        self.deleted.append(cid)


from doubao_client.ui.agent_dialog import AgentDecider  # noqa: E402

# 4.1 连续失败 3 次：不得因失败而切换会话
api = FakeAPI([BridgeError("网络抖动", 0)] * 3)
dec = AgentDecider(api)
for _ in range(3):
    try:
        dec.decide("hi", None)
        check("应抛异常", False)
    except BridgeError:
        pass
check("3 次失败后未删除会话", api.deleted == [], str(api.deleted))

# 4.2 失败后成功：会话 id 被采用并复用
api = FakeAPI([BridgeError("网络抖动", 0), "ok"])
dec = AgentDecider(api)
try:
    dec.decide("hi", None)
except BridgeError:
    pass
dec.decide("hi", None)
check("成功后记录会话 id", dec._cid == "cid-1", dec._cid)

# 4.3 单会话执行：任意步数都不轮换会话
api = FakeAPI(["ok"] * 20)
dec = AgentDecider(api)
for _ in range(20):
    dec.decide("hi", None)
check("20 步后仍未删除会话（全程单会话）", api.deleted == [], str(api.deleted))
check("会话 id 保持为首条消息分配的值", dec._cid == "cid-1", dec._cid)

# 4.4 任务结束才删除临时会话
dec.close()
check("close() 删除本次任务的会话", api.deleted == ["cid-1"], str(api.deleted))
check("close() 后本地会话 id 清空", dec._cid == "", dec._cid)

# 4.5 不可重试错误原样抛出：不再做换会话 id 的二次自愈（那是无效代码）
api = FakeAPI([BridgeError("图片不存在：x", 0, kind="fatal"), "ok"])
dec = AgentDecider(api)
dec._cid = "cid-1"
try:
    dec.decide("hi", None)
    check("应抛出异常", False)
except BridgeError as exc:
    check("fatal 原样抛出（不降级）", exc.kind == "fatal", exc.kind)
check("本地会话 id 保持不变", dec._cid == "cid-1", dec._cid)


# ---------------------------------------------------------------- 生成失败换会话
print("\n[4c] 服务端生成失败（2005）—— 换新会话重发一次")


class RotFakeAPI(FakeAPI):
    """每次成功都用新的会话 id，便于观察是否真的换了会话。"""

    def _reply(self):
        if self.script:
            item = self.script.pop(0)
            if isinstance(item, BaseException):
                yield "error", item
                return
        yield "conv", f"cid-{self.chat_calls}"
        yield "delta", "ok"
        yield "done", None


# 生成失败 → 删旧会话、换新会话、重发一次并成功
api = RotFakeAPI([BridgeError("系统错误", 0, generation=True)])
dec = AgentDecider(api)
dec._cid = "cid-1"
check("生成失败后自动重发成功", dec.decide("hi", None) == "ok")
check("旧会话被删除", api.deleted == ["cid-1"], str(api.deleted))
check("改用新会话", dec._cid == "cid-2", dec._cid)

# 新会话仍然生成失败：不再无限重发，异常交给会话层计数
api = RotFakeAPI([BridgeError("系统错误", 0, generation=True)] * 2)
dec = AgentDecider(api)
dec._cid = "cid-1"
try:
    dec.decide("hi", None)
    check("应抛出异常", False)
except BridgeError as exc:
    check("换会话后仍失败则抛出", exc.generation is True, str(exc))
check("只换一次会话", api.deleted == ["cid-1"], str(api.deleted))
check("请求恰好 2 次", api.chat_calls == 2, str(api.chat_calls))

# 非生成类错误不换会话（沿用 4.1 的判定，这里确认 generation 标记的作用）
api = RotFakeAPI([BridgeError("连接中断了", 0)])
dec = AgentDecider(api)
dec._cid = "cid-1"
try:
    dec.decide("hi", None)
except BridgeError:
    pass
check("通道类错误不换会话", api.deleted == [], str(api.deleted))

# 限流错误不换会话：换会话也照样被拒，只能等
api = RotFakeAPI([BridgeError("系统错误", 710022004)])
dec = AgentDecider(api)
dec._cid = "cid-1"
try:
    dec.decide("hi", None)
    check("应抛出异常", False)
except BridgeError as exc:
    check("限流错误原样抛出", exc.kind == "rate_limit", exc.kind)
check("限流不删会话", api.deleted == [], str(api.deleted))
check("限流只发 1 次请求（不立刻重撞）", api.chat_calls == 1, str(api.chat_calls))

# 2005 事件必须带上 generation 标记
raw = ('data: {"event_type":2005,"event_data":{"error_detail":'
       '{"code":710022004,"message":"系统错误"}}}')
parsed = api_mod.DoubaoAPI()._parse_sse_line(raw)
check("2005 解析出 error", parsed and parsed[0][0] == "error", str(parsed))
gen_err = parsed[0][1] if parsed else None
check("2005 标记为 generation", getattr(gen_err, "generation", False) is True)
check("2005 保留错误码", getattr(gen_err, "code", 0) == 710022004,
      str(getattr(gen_err, "code", None)))
check("2005 保留原文案", str(gen_err) == "系统错误", str(gen_err))
check("generation 标记能穿过 as_bridge_error",
      as_bridge_error(gen_err, "发送失败：").generation is True)


# ---------------------------------------------------------------- 通道自愈
print("\n[4b] 会话通道自愈 —— 与上传共用 _recover_page")
from doubao_client.services.api import DoubaoAPI  # noqa: E402


class FakeBridge:
    """start_stream 第一次失败（请求没发出去），第二次成功。"""

    def __init__(self):
        self.start_calls = 0
        self.pulls = 0

    def start_stream(self, path, body, sid):
        self.start_calls += 1
        if self.start_calls == 1:
            raise BridgeError("页面脚本上下文已丢失")

    def pull_stream(self, sid):
        self.pulls += 1
        return ("", True, None)

    def abort_stream(self):
        pass


dapi = DoubaoAPI.__new__(DoubaoAPI)
dapi.bridge = FakeBridge()
recovered = []
dapi._recover_page = lambda: recovered.append(1)
items = list(dapi._stream("hi", "cid-1", []))
check("通道恢复被调用", recovered == [1], str(recovered))
check("未发出的请求重建后重发", dapi.bridge.start_calls == 2,
      str(dapi.bridge.start_calls))
check("重发后正常完成（无 error）",
      all(k != "error" for k, _ in items), str(items))


class FakeBridge2(FakeBridge):
    """请求已发出后拉流中断（start_stream 正常）。"""

    def start_stream(self, path, body, sid):
        self.start_calls += 1        # 始终成功

    def pull_stream(self, sid):
        self.pulls += 1
        if self.pulls == 1:
            raise BridgeError("连接中断了")
        return ("", True, None)


dapi2 = DoubaoAPI.__new__(DoubaoAPI)
dapi2.bridge = FakeBridge2()
recovered2 = []
dapi2._recover_page = lambda: recovered2.append(1)
items2 = list(dapi2._stream("hi", "cid-1", []))
errs = [p for k, p in items2 if k == "error"]
check("中断后重建通道", recovered2 == [1], str(recovered2))
check("已发出的消息不自动重发（避免重复）",
      dapi2.bridge.start_calls == 1, str(dapi2.bridge.start_calls))
check("中断如实上报", bool(errs), str(items2))


# ---------------------------------------------------------------- 图片缓存
print("\n[5] 同一张截图重试时不该重复上传")
shot = os.path.join(tempfile.gettempdir(), "rl_test_shot.jpg")
with open(shot, "wb") as f:
    f.write(b"\xff\xd8fakejpeg")

api = FakeAPI([BridgeError("网络抖动", 0), "ok"])
dec = AgentDecider(api)
try:
    dec.decide("hi", shot)                     # 第一次：上传 + 失败
except BridgeError:
    pass
dec.decide("hi", shot)                         # 重试：应复用 uri
check("重试只上传了 1 次", api.upload_calls == 1,
      f"upload_calls={api.upload_calls}")
check("提问发了 2 次", api.chat_calls == 2, f"chat_calls={api.chat_calls}")

# 换了内容就该重新上传
os.utime(shot, (0, 0))
dec.decide("hi", shot)
check("截图变了就重新上传", api.upload_calls == 2,
      f"upload_calls={api.upload_calls}")
os.remove(shot)


# ---------------------------------------------------------------- 登录判定
print("\n[7] is_logged_in —— 假值不能判成已登录（回归）")
# str(0)=="0" / str(False)=="False" 都是真值：value_of 若直接 str()，
# 0/False 的 cookie 会被判成「已登录」→ _auth_chain 拿假 cookie 去验 →
# AuthError → clear_store() 把真的 cookies.dat 删掉。
check("value_of(None) 为空", cookie_bridge.value_of(None) == "")
check("value_of(\"\") 为空", cookie_bridge.value_of("") == "")
check("value_of(0) 当空（不是 '0'）", cookie_bridge.value_of(0) == "",
      repr(cookie_bridge.value_of(0)))
check("value_of(False) 当空", cookie_bridge.value_of(False) == "",
      repr(cookie_bridge.value_of(False)))
check("value_of({value:''}) 当空", cookie_bridge.value_of({"value": ""}) == "")
check("value_of({value:0}) 当空", cookie_bridge.value_of({"value": 0}) == "",
      repr(cookie_bridge.value_of({"value": 0})))
check("字符串 '0' 是真值，照常保留", cookie_bridge.value_of("0") == "0")
check("正常值照常返回", cookie_bridge.value_of({"value": "abc"}) == "abc")
check("裸字符串形态照常返回", cookie_bridge.value_of("abc") == "abc")

check("sessionid=0 不算登录", not cookie_bridge.is_logged_in({"sessionid": 0}))
check("sessionid=False 不算登录", not cookie_bridge.is_logged_in({"sessionid": False}))
check("sessionid={'value':0} 不算登录",
      not cookie_bridge.is_logged_in({"sessionid": {"value": 0}}))
check("sessionid='' 不算登录", not cookie_bridge.is_logged_in({"sessionid": ""}))
check("全空不算登录", not cookie_bridge.is_logged_in({}))
check("真值算登录(dict)", cookie_bridge.is_logged_in({"sessionid": {"value": "x"}}))
check("真值算登录(裸串)", cookie_bridge.is_logged_in({"sessionid": "x"}))
# to_header 也不该把假值拼进 Cookie 头
check("to_header 不带假值",
      cookie_bridge.to_header({"a": 0, "b": {"value": "x"}}) == "b=x",
      repr(cookie_bridge.to_header({"a": 0, "b": {"value": "x"}})))


# ---------------------------------------------------------------- 汇总
print(f"\n===== 通过 {PASS} 项，失败 {FAIL} 项 =====")
sys.exit(1 if FAIL else 0)
