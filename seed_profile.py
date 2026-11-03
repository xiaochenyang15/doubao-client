import json
import subprocess
import time
import urllib.request

import websocket

from doubao_client.services import cookies as cookie_bridge
from doubao_client.services.api import PORTABLE_CHROME, PROFILE_DIR

PORT = 9223


def main():
    cookies, source = cookie_bridge.extract_all()
    print("来源:", source, "条数:", len(cookies))
    if not cookies:
        raise SystemExit("没有可用的登录态")

    proc = subprocess.Popen(
        [PORTABLE_CHROME, f"--remote-debugging-port={PORT}",
         f"--user-data-dir={PROFILE_DIR}", "--remote-allow-origins=*",
         "--no-first-run", "--no-default-browser-check",
         "--window-position=-32000,-32000", "about:blank"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    targets = None
    for _ in range(60):
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{PORT}/json/list", timeout=3) as r:
                targets = json.loads(r.read().decode())
            if targets:
                break
        except Exception:  # noqa: BLE001
            time.sleep(0.5)
    if not targets:
        raise SystemExit("调试端口未就绪")

    page = next(t for t in targets if t["type"] == "page")
    ws = websocket.create_connection(page["webSocketDebuggerUrl"], timeout=60,
                                     max_size=32 * 1024 * 1024, suppress_origin=True)

    def send(method, **params):
        nonlocal mid
        mid += 1
        ws.send(json.dumps({"id": mid, "method": method, "params": params}))
        while True:
            msg = json.loads(ws.recv())
            if msg.get("id") == mid:
                return msg.get("result", {})

    mid = 0
    send("Network.enable")
    send("Page.enable")
    exp = int(time.time()) + 86400 * 60
    payload = [{"name": n, "value": m["value"], "domain": m["domain"],
                "path": m.get("path") or "/", "secure": True, "expires": exp}
               for n, m in cookies.items() if m.get("value")]
    send("Network.setCookies", cookies=payload)
    send("Page.navigate", url="https://www.doubao.com/chat/")
    print("已注入并打开，等待写入…")
    time.sleep(8)
    try:
        send("Browser.close")
    except Exception:  # noqa: BLE001
        proc.terminate()
    time.sleep(3)

    got, src = cookie_bridge.extract_portable()
    print("便携包登录态:", src, "条数:", len(got),
          "已登录:", cookie_bridge.is_logged_in(got))


if __name__ == "__main__":
    main()
