"""朗读：用豆包自己的声音念，BASS 放音；豆包不可用时退回系统语音。

两种后端
--------
1. **豆包语音（默认）**：网页端「朗读」走的那条 WebSocket
   （`wss://ws-samantha.doubao.com/samantha/audio/tts`），带登录态连上去，
   文本进去、ADTS AAC 音频流出来，逐句交给 `bassplayer` 播放。
   声音就是豆包自己的音色，不需要在本机装任何语音包。
2. **系统语音（兜底）**：SAPI（`SAPI.SpVoice`）。没有登录态、bass.dll 缺失、
   或者接口报错且一个字节都没放出来时，退回它——朗读功能不至于直接失效。

为什么要逐句
------------
一次把整段文本送过去，要等全部合成完才开始出声；长回复就是十几秒的沉默。
这里按句切分：第一句合成完立刻开播，后面几句在播放的同时继续合成，边放边等。
所以「按句切分」既是延迟优化，也是播放队列的单位。

线程
----
* `_synth_loop`：走网络，把音频块写入播放队列；
* `_play_loop`：只碰 BASS，放音、等放完；
* `_sapi_loop`：兜底后端独占一个线程（COM 对象绑定初始化线程）。

每次 `speak()` / `stop()` 都会让「代次」（`_gen`）加一，队列里旧代次的音频
被直接丢掉——这样「正在念第三条，又让念第五条」不会两份声音叠在一起。
"""

from __future__ import annotations

import json
import logging
import os
import queue
import re
import threading
import time
import uuid

from . import bassplayer
from . import cookies as cookie_bridge
from ..core.config import APP_DIR

logger = logging.getLogger("tts")

WS_URL = "wss://ws-samantha.doubao.com/samantha/audio/tts"
ORIGIN = "https://www.doubao.com"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")

# 豆包音色（网页端「音色」列表里的那批，简称写在这里方便配置）
SPEAKERS = {
    "taozi": "zh_female_taozi_conversation_v4_wvae_bigtts",        # 桃子 · 女声
    "shuangkuai": "zh_female_shuangkuai_emo_v3_wvae_bigtts",       # 爽快 · 女声
    "tianmei": "zh_female_tianmei_conversation_v4_wvae_bigtts",    # 甜美 · 女声
    "qingche": "zh_female_qingche_moon_bigtts",                    # 清澈 · 女声
    "yangguang": "zh_male_yangguang_conversation_v4_wvae_bigtts",   # 阳光 · 男声
    "chenwen": "zh_male_chenwen_moon_bigtts",                      # 沉稳 · 男声
    "rap": "zh_male_rap_mars_bigtts",                              # 说唱 · 男声
    "en_female": "en_female_sarah_conversation_bigtts",
    "en_male": "en_male_adam_conversation_bigtts",
}
DEFAULT_SPEAKER = SPEAKERS["taozi"]

SEGMENT_CHARS = 120       # 一次送多少字：太长的话没有句号的段落要等太久
WS_TIMEOUT = 20.0         # 单次收包超时：这么久没消息就当本段结束


class _TtsError(RuntimeError):
    pass


# ------------------------------------------------------------------ 状态
_player = bassplayer.Player()
_api = None                # 由 main 注入 DoubaoAPI，用来取登录态
_speaker = DEFAULT_SPEAKER
_volume = 1.0
_error = ""
_speaking = False
_synthing = False
_sapi_ready = False       # SAPI 引擎起没起来（兜底后端）

_lock = threading.RLock()
_gen = 0                   # 朗读代次：变了就说明这一轮已经作废
_jobs: "queue.Queue[tuple]" = queue.Queue()
_audio: "queue.Queue[tuple]" = queue.Queue()
_sapi_jobs: "queue.Queue[tuple]" = queue.Queue()
_workers: dict[str, threading.Thread] = {}
_current_ws = None
_ck_cache = {"header": "", "at": 0.0}

_ZH_HINTS = ("chinese", "中文", "huihui", "yaoyao", "kangkang", "xiaoxiao",
             "yunxi", "xiaoyi", "hanhan", "lili")


# ------------------------------------------------------------------ 文本
def _clean(text: str) -> str:
    """去掉朗读会念成噪音的东西：代码块、链接、Markdown 记号。"""
    text = re.sub(r"```.*?```", "（这里有一段代码，已跳过）", text, flags=re.S)
    text = re.sub(r"`([^`]*)`", r"\1", text)
    text = re.sub(r"!\[[^\]]*\]\([^)]*\)", "", text)
    text = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", text)
    text = re.sub(r"^[ \t]*[-*+][ \t]+", "", text, flags=re.M)
    text = re.sub(r"[*_#>]+", "", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def _segments(text: str, limit: int = SEGMENT_CHARS) -> list[str]:
    """按句末标点切成不超过 limit 字的小段；长句再按逗号退一步切。"""
    parts = re.split(r"(?<=[。！？!?；;\n])", text)
    out: list[str] = []
    buf = ""
    for part in parts:
        if not part:
            continue
        if len(buf) + len(part) <= limit:
            buf += part
            continue
        if buf.strip():
            out.append(buf.strip())
        buf = ""
        while len(part) > limit:
            cut = max(part.rfind("，", 0, limit), part.rfind(",", 0, limit),
                      part.rfind("、", 0, limit))
            cut = cut + 1 if cut > limit // 3 else limit
            out.append(part[:cut].strip())
            part = part[cut:]
        buf = part
    if buf.strip():
        out.append(buf.strip())
    return [p for p in out if p]


# ------------------------------------------------------------------ 登录态
def _cookie_header(refresh: bool = False) -> str:
    """拼 Cookie 头。

    优先问浏览器要「当前真实生效」的 cookie（服务端会轮换，磁盘上的可能已经
    过期）；浏览器不在（比如刚启动）就退回磁盘里存的那份。
    """
    now = time.time()
    if not refresh and _ck_cache["header"] and now - _ck_cache["at"] < 60:
        return _ck_cache["header"]
    cookies: dict = {}
    if _api is not None:
        try:
            cookies = _api.current_cookies() or {}
        except Exception:  # noqa: BLE001
            cookies = {}
    if not cookies:
        try:
            cookies = cookie_bridge.load_store() or {}
        except Exception:  # noqa: BLE001
            cookies = {}
    header = cookie_bridge.to_header(cookies)
    _ck_cache.update(header=header, at=now)
    return header


def _config() -> dict:
    try:
        with open(os.path.join(APP_DIR, "config.json"), encoding="utf-8") as fh:
            return json.load(fh) or {}
    except Exception:  # noqa: BLE001
        return {}


def set_api(api) -> None:
    """注入 DoubaoAPI：朗读要靠它拿浏览器里当前生效的登录态。"""
    global _api
    _api = api
    _ck_cache.update(header="", at=0.0)


def set_speaker(name: str) -> str:
    """选音色：认简称（taozi）也认完整 ID；认不出就保持原样。"""
    global _speaker
    name = (name or "").strip()
    if not name:
        return _speaker
    _speaker = SPEAKERS.get(name.lower(), name)
    return _speaker


def speaker() -> str:
    return _speaker


def speakers() -> dict:
    return dict(SPEAKERS)


# ------------------------------------------------------------------ 代次
def _bump() -> int:
    global _gen
    with _lock:
        _gen += 1
        return _gen


def _gen_now() -> int:
    with _lock:
        return _gen


# ------------------------------------------------------------------ 豆包合成
def _ws_url(speech_rate: int, speaker_id: str) -> str:
    cfg = _config()
    params = {
        "speaker": speaker_id,
        "format": "aac",                  # 只有 aac 这条能通，mp3 会回「系统错误」
        "speech_rate": speech_rate,       # -100 ~ 100，0 为正常语速
        "pitch": 0,
        "version_code": 20800,
        "language": "zh",
        "device_platform": "web",
        "aid": 497858,
        "real_aid": 497858,
        "pkg_type": "release_version",
        "device_id": cfg.get("device_id", ""),
        "pc_version": "3.43.5",
        "web_id": cfg.get("web_id", ""),
        "tea_uuid": cfg.get("web_id", ""),
        "region": "CN",
        "sys_region": "CN",
        "samantha_web": 1,
        "use-olympus-account": 1,
        "web_tab_id": str(uuid.uuid4()),
    }
    query = "&".join(f"{k}={v}" for k, v in params.items())
    return f"{WS_URL}?{query}"


def _handshake_error(exc: Exception) -> str:
    """把握手失败的 HTTP 响应体翻译成人话（服务端会返回 JSON）。"""
    text = str(exc)
    code = ""
    msg = ""
    if '"code"' in text:
        m = re.search(r'\{"code":(\d+).*?"msg":"([^"]*)"', text, re.S)
        if m:
            code, msg = m.group(1), m.group(2)
    if code == "710012001":
        return "豆包登录态已过期，按 Ctrl+L 重新登录后再朗读"
    if code:
        return f"豆包语音接口报错（{code}）：{msg or '未知错误'}"
    return f"连不上豆包语音接口：{text[:120]}"


def _synth_segment(seg: str, speech_rate: int, speaker_id: str, gen: int,
                   emit) -> None:
    """连一次 WebSocket，把一段文本合成成若干音频块，逐块 emit 出去。"""
    global _current_ws
    try:
        import websocket
    except ImportError as exc:  # pragma: no cover
        raise _TtsError(f"缺 websocket-client：{exc}") from exc

    header = [
        "Accept-Language: zh-CN,zh;q=0.9",
        f"Origin: {ORIGIN}",
        f"User-Agent: {UA}",
    ]
    cookie = _cookie_header()
    if not cookie:
        raise _TtsError("还没有登录豆包，没法用豆包的声音朗读")
    header.append("Cookie: " + cookie)

    try:
        ws = websocket.create_connection(
            _ws_url(speech_rate, speaker_id), header=header,
            timeout=WS_TIMEOUT, origin=ORIGIN)
    except Exception as exc:  # noqa: BLE001
        raise _TtsError(_handshake_error(exc)) from exc

    with _lock:
        _current_ws = ws
    buf = bytearray()
    try:
        ws.send(json.dumps({"event": "text", "text": seg}, ensure_ascii=False))
        ws.send(json.dumps({"event": "finish"}))
        deadline = time.time() + 90
        while True:
            if _gen_now() != gen:
                return
            if time.time() > deadline:
                break
            try:
                msg = ws.recv()
            except Exception:  # noqa: BLE001  超时 / 对端关闭都算本段结束
                break
            if isinstance(msg, bytes):
                buf += msg
                continue
            if not msg:
                break
            try:
                data = json.loads(msg)
            except ValueError:
                continue
            event = data.get("event", "")
            if event == "sentence_end":
                if buf:
                    emit(bytes(buf))
                    buf.clear()
            elif event == "finish":
                break
            elif event in ("error", "open_fail"):
                raise _TtsError(data.get("message") or "豆包语音接口返回错误")
            elif data.get("code"):
                raise _TtsError(data.get("message") or f"错误码 {data.get('code')}")
        if buf and _gen_now() == gen:
            emit(bytes(buf))
    finally:
        with _lock:
            if _current_ws is ws:
                _current_ws = None
        try:
            ws.close()
        except Exception:  # noqa: BLE001
            pass


# ------------------------------------------------------------------ 线程
def _start(name: str, target) -> None:
    th = _workers.get(name)
    if th is not None and th.is_alive():
        return
    th = threading.Thread(target=target, daemon=True, name=name)
    _workers[name] = th
    th.start()


def _synth_loop() -> None:
    global _error, _synthing
    while True:
        job = _jobs.get()
        if job[0] != "speak":
            continue
        _, gen, text, rate = job
        if _gen_now() != gen:
            continue
        with _lock:
            _synthing = True
        played = False

        def emit(blob: bytes) -> None:
            nonlocal played
            if _gen_now() != gen:
                return
            played = True
            _audio.put((gen, blob))

        try:
            for seg in _segments(text):
                if _gen_now() != gen:
                    break
                _synth_segment(seg, rate, _speaker, gen, emit)
        except Exception as exc:  # noqa: BLE001
            _error = str(exc)
            logger.warning("豆包语音失败：%s", _error)
            if not played and _gen_now() == gen:
                # 一个字都没放出来，退回系统语音，朗读功能不至于失效
                _start("tts-sapi", _sapi_loop)
                _sapi_jobs.put(("speak", text, rate, ""))
        finally:
            # 只有「还是这一代」才算收尾：中途又按了一次朗读时，新一轮可能已经在
            # 合成了，老任务不能把它的状态一起抹掉。
            if _gen_now() == gen:
                with _lock:
                    _synthing = False
                _finish_if_idle(gen)


def _finish_if_idle(gen: int) -> None:
    """合成停了、队列空了、也没在出声——这一轮才算说完。

    只在播放成功后才判断会漏掉一种情况：音频建流就失败（比如缺 bass.dll），
    播放循环把每块都丢掉，最后一块丢完时合成线程还没退，状态就永远停在
    「正在朗读」。所以合成线程收尾时也要来问一次。
    """
    global _speaking
    with _lock:
        if (_gen_now() == gen and not _synthing and _audio.empty()
                and not _player.is_playing()):
            _speaking = False


def _play_loop() -> None:
    global _error
    while True:
        gen, blob = _audio.get()
        if _gen_now() != gen:
            continue
        if _player.play(blob):
            while _player.is_playing():
                if _gen_now() != gen:
                    _player.stop()
                    break
                time.sleep(0.03)
        else:
            _error = _player.error() or "播放失败"
            logger.warning("播放失败：%s", _error)
        _finish_if_idle(gen)


# ------------------------------------------------------------------ SAPI 兜底
def _pick_voice(engine, want: str) -> None:
    try:
        items = engine.GetVoices()
    except Exception:  # noqa: BLE001
        return
    for i in range(items.Count):
        try:
            v = items.Item(i)
            desc = v.GetDescription() or ""
        except Exception:  # noqa: BLE001
            continue
        if want and want.lower() in desc.lower():
            engine.Voice = v
            return
        if not want and any(h in desc.lower() for h in _ZH_HINTS):
            engine.Voice = v
            return


def _sapi_loop() -> None:
    global _error, _speaking, _sapi_ready
    import pythoncom
    import win32com.client

    pythoncom.CoInitialize()
    try:
        engine = win32com.client.Dispatch("SAPI.SpVoice")
        _sapi_ready = True
    except Exception as exc:  # noqa: BLE001
        _error = f"没有可用的语音引擎：{exc}"
        logger.warning(_error)
        return
    while True:
        job = _sapi_jobs.get()
        try:
            if job[0] == "stop":
                engine.Speak("", 2)          # SVSF_PURGE
                _speaking = False
            elif job[0] == "speak":
                _, text, rate, voice = job
                _pick_voice(engine, voice)
                engine.Rate = max(-10, min(10, int(rate)))
                engine.Speak("", 2)
                engine.Speak(text, 1)        # SVSF_ASYNC
                _speaking = True
        except Exception as exc:  # noqa: BLE001
            _error = f"朗读失败：{exc}"
            logger.warning(_error)
            _speaking = False


# ------------------------------------------------------------------ 对外接口
def _doubao_ready() -> bool:
    return _api is not None and bool(_cookie_header())


def available() -> bool:
    """能不能朗读：豆包登录态在，或者本机有 SAPI 兜底。"""
    if _doubao_ready():
        return True
    if _sapi_ready:
        return True
    _start("tts-sapi", _sapi_loop)
    for _ in range(20):                     # 等 SAPI 起来，最多 1 秒
        if _sapi_ready:
            break
        time.sleep(0.05)
    return _sapi_ready


def backend() -> str:
    """当前实际会用哪套声音，状态栏显示用。"""
    return "豆包语音" if _doubao_ready() else "系统语音"


def error() -> str:
    return _error


def speak(text: str, rate: int = 0, voice: str = "") -> bool:
    """朗读一段文字。rate 沿用旧的 -10 ~ 10，映射成豆包的 -100 ~ 100。"""
    global _error, _speaking
    body = _clean(text)
    if not body:
        return False
    if voice:
        set_speaker(voice)
    if not _doubao_ready():
        # 没有登录态就走 SAPI，至少还能念
        if not available():
            return False
        _sapi_jobs.put(("speak", body, rate, ""))
        with _lock:
            _speaking = True
        return True

    _start("tts-synth", _synth_loop)
    _start("tts-play", _play_loop)
    _error = ""
    gen = _bump()
    _drain_audio()
    _player.stop()
    _speaking = True
    rate = max(-10, min(10, int(rate)))
    _jobs.put(("speak", gen, body, rate * 10))
    return True


def stop() -> None:
    global _speaking
    _bump()
    with _lock:
        ws = _current_ws
    if ws is not None:
        try:
            ws.close()          # 掐断 recv，合成线程立刻退出
        except Exception:  # noqa: BLE001
            pass
    _player.stop()
    _drain_audio()
    _sapi_jobs.put(("stop",))
    with _lock:
        _speaking = False


def _drain_audio() -> None:
    while True:
        try:
            _audio.get_nowait()
        except queue.Empty:
            return


def is_speaking() -> bool:
    if _player.is_playing():
        return True
    with _lock:
        return _speaking
