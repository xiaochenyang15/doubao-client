"""端到端真机测试：真实 Chrome 桥 + 真实豆包视觉决策 + 真实键鼠 + 真记事本。

跑法：python e2e_agent.py  （在 doubao_client 目录下）
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)) or ".")

import win32clipboard
import win32con
import win32gui
import win32process


import doubao_client.agent as agent
import doubao_client.services.api as api
from doubao_client.services import cookies as cookie_bridge

# 别把测试用的浏览器配置丢进项目目录
api.PROFILE_DIR = os.path.join(tempfile.gettempdir(), "doubao_e2e_profile")

GOAL = ("在当前记事本（Notepad）窗口的文本编辑区里输入这一行文字：Hello 2026。"
        "输入完成后立刻返回 finish，不要做别的事。")


def log(msg: str) -> None:
    print(f"[e2e] {msg}", flush=True)


class Decider:
    """与客户端 _AgentDecider 同一套策略：一个临时会话用到底，结束再删。"""

    def __init__(self, dapi):
        self.api = dapi
        self.n = 0
        self.cid = ""

    def decide(self, message: str, image_path: str | None = None) -> str:
        self.n += 1
        started = time.time()
        parts, new_cid, err = [], "", ""
        src = (self.api.vision_chat(message, image_path, self.cid or "0")
               if image_path else self.api.stream_chat(message, self.cid or "0"))
        for kind, payload in src:
            if kind == "delta":
                parts.append(payload)
            elif kind == "conv":
                new_cid = payload
            elif kind == "error":
                err = payload
                break
        if new_cid and new_cid != "0":
            self.cid = new_cid
        text = "".join(parts)
        log(f"决策#{self.n} {time.time() - started:.1f}s 截图={bool(image_path)} "
            f"→ {text.strip()[:220]}")
        if err and not text:
            # 是 BridgeError 就原样抛：code/kind 是 session 重试策略的依据
            if isinstance(err, BaseException):
                raise err
            raise RuntimeError(err)
        return text

    def close(self) -> None:
        if self.cid and self.cid != "0":
            try:
                self.api.delete_conversation(self.cid)
            except Exception:  # noqa: BLE001
                pass
            self.cid = ""


def focus(hwnd: int) -> bool:
    """把窗口真正拿到前台（SetForegroundWindow 对后台进程会被系统忽略）。"""
    import win32api
    import win32process

    try:
        win32gui.ShowWindow(hwnd, win32con.SW_RESTORE)
        win32gui.ShowWindow(hwnd, win32con.SW_MAXIMIZE)
    except Exception:  # noqa: BLE001
        pass
    for _ in range(5):
        try:
            cur = win32api.GetCurrentThreadId()
            tid, _pid = win32process.GetWindowThreadProcessId(hwnd)
            win32process.AttachThreadInput(cur, tid, True)
            try:
                win32gui.BringWindowToTop(hwnd)
                win32gui.SetForegroundWindow(hwnd)
            finally:
                win32process.AttachThreadInput(cur, tid, False)
        except Exception:  # noqa: BLE001
            pass
        time.sleep(0.3)
        if win32gui.GetForegroundWindow() == hwnd:
            return True

    # 系统前台锁有时候就是不认 SetForegroundWindow：直接点一下它的客户区，
    # 效果等同于用户自己点了一下窗口（顺便把光标放进编辑区）。
    try:
        left, top, right, bottom = win32gui.GetWindowRect(hwnd)
        x, y = (left + right) // 2, (top + bottom) // 2
        win32api.SetCursorPos((x, y))
        time.sleep(0.2)
        win32api.mouse_event(win32con.MOUSEEVENTF_LEFTDOWN, 0, 0, 0, 0)
        win32api.mouse_event(win32con.MOUSEEVENTF_LEFTUP, 0, 0, 0, 0)
        time.sleep(0.5)
    except Exception:  # noqa: BLE001
        pass
    return win32gui.GetForegroundWindow() == hwnd


def open_notepad() -> tuple[int, subprocess.Popen]:
    # 先收掉遗留的记事本，避免 FindWindow 抓到上一轮那个（它可能已经不响应前台请求）
    subprocess.run(["taskkill", "/F", "/IM", "notepad.exe"],
                   capture_output=True, creationflags=0x08000000)
    time.sleep(0.5)
    proc = subprocess.Popen(["notepad.exe"])

    hwnd = 0
    deadline = time.time() + 20
    while time.time() < deadline and not hwnd:
        time.sleep(0.3)

        def pick(h, _lparam):
            nonlocal hwnd
            try:
                _tid, pid = win32process.GetWindowThreadProcessId(h)
            except Exception:  # noqa: BLE001
                return
            if pid == proc.pid and win32gui.IsWindowVisible(h) \
                    and win32gui.GetWindowText(h).strip():
                hwnd = h

        win32gui.EnumWindows(pick, None)
    if not hwnd:
        raise SystemExit("记事本没起来")
    if not focus(hwnd):
        log("警告：没能把记事本切到前台，Agent 可能操作到别的窗口")
    time.sleep(0.8)
    return hwnd, proc


def clear_notepad(hwnd: int) -> None:
    """先清空，避免旧内容干扰判断。"""
    import win32api
    focus(hwnd)
    win32api.keybd_event(win32con.VK_CONTROL, 0, 0, 0)
    win32api.keybd_event(0x41, 0, 0, 0)
    win32api.keybd_event(0x41, 0, win32con.KEYEVENTF_KEYUP, 0)
    win32api.keybd_event(win32con.VK_CONTROL, 0, win32con.KEYEVENTF_KEYUP, 0)
    time.sleep(0.1)
    win32api.keybd_event(win32con.VK_DELETE, 0, 0, 0)
    win32api.keybd_event(win32con.VK_DELETE, 0, win32con.KEYEVENTF_KEYUP, 0)
    time.sleep(0.3)


def read_notepad(hwnd: int) -> str:
    """全选复制，从剪贴板读回内容——不依赖记事本是 Win32 还是 UWP。"""
    import win32api

    if not focus(hwnd):
        log("警告：记事本没在前台，复制到的可能是别的窗口")

    try:
        win32clipboard.OpenClipboard()
        win32clipboard.EmptyClipboard()
        win32clipboard.CloseClipboard()
    except Exception:  # noqa: BLE001
        pass

    def tap(mod: int, key: int) -> None:
        win32api.keybd_event(mod, 0, 0, 0)
        win32api.keybd_event(key, 0, 0, 0)
        win32api.keybd_event(key, 0, win32con.KEYEVENTF_KEYUP, 0)
        win32api.keybd_event(mod, 0, win32con.KEYEVENTF_KEYUP, 0)
        time.sleep(0.15)

    tap(win32con.VK_CONTROL, 0x41)      # Ctrl+A
    tap(win32con.VK_CONTROL, 0x43)      # Ctrl+C

    time.sleep(0.3)
    text = ""
    for _ in range(10):
        try:
            win32clipboard.OpenClipboard()
            try:
                text = win32clipboard.GetClipboardData(win32con.CF_UNICODETEXT) or ""
            finally:
                win32clipboard.CloseClipboard()
            break
        except Exception:  # noqa: BLE001
            time.sleep(0.2)
    return text


def main() -> int:
    store = cookie_bridge.load_store()
    log(f"登录态：{len(store or {})} 条 cookie")
    if not store:
        log("没有可用登录态，先手动跑一次客户端")
        return 2

    dapi = api.DoubaoAPI()
    t0 = time.time()
    profile = dapi.start(store)
    log(f"桥接就绪 {time.time() - t0:.1f}s，账号={profile.get('nickname') or profile}")

    hwnd, proc = open_notepad()
    clear_notepad(hwnd)
    log(f"记事本窗口 hwnd={hwnd}，前台={win32gui.GetForegroundWindow() == hwnd}")

    done = {"msg": "", "at": 0.0}

    def on_done(msg: str) -> None:
        done["msg"] = msg
        done["at"] = time.time()

    # 不做任何人为节奏限制：上传通道已带自愈（见 api.upload_image），
    # 请求频率按服务端允许的自然节奏走

    dec = Decider(dapi)
    sess = agent.AgentSession(
        GOAL, dec.decide,
        on_status=lambda t: log("  · " + t),
        on_step=lambda s, d, o, m: log(f"  第{s}步 {m[:120]}"),
        on_done=on_done,
        max_steps=8,
        use_uia=True,
        with_vision=True,
    )
    sess.start()
    deadline = time.time() + 240
    while time.time() < deadline and sess.is_running:
        time.sleep(0.5)
    if sess.is_running:
        sess.cancel()
        log("超时，已叫停")

    log(f"结论：{done['msg'] or '（没等到完成回调）'}")
    text = read_notepad(hwnd)
    log(f"记事本内容：{text!r}")

    ok = "Hello 2026" in text
    log("PASS" if ok else "FAIL")

    try:
        proc.kill()
    except OSError:
        pass
    try:
        dec.close()
    except Exception:  # noqa: BLE001
        pass
    try:
        dapi.close()
    except Exception:  # noqa: BLE001
        pass
    shutil.rmtree(api.PROFILE_DIR, ignore_errors=True)
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
