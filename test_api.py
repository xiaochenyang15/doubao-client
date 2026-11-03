import time

from doubao_client.services import cookies as cookie_bridge
from doubao_client.services.api import DoubaoAPI

cookies, source = cookie_bridge.extract_all()
print(f"cookie 来源 {source}，{len(cookies)} 条，已登录 {cookie_bridge.is_logged_in(cookies)}")

api = DoubaoAPI()
t0 = time.time()
profile = api.start(cookies)
print(f"[启动] {time.time() - t0:.1f}s  账号={profile}")

t0 = time.time()
convs = api.list_conversations()
print(f"[会话] {time.time() - t0:.1f}s  共 {len(convs)} 个")
for c in convs[:5]:
    print("   ", c["id"], c["name"][:26])

if convs:
    t0 = time.time()
    msgs = api.load_messages(convs[3]["id"] if len(convs) > 3 else convs[0]["id"], limit=6)
    print(f"[历史] {time.time() - t0:.1f}s  {len(msgs)} 条")
    for m in msgs[:3]:
        print(f"    {m['role']}: {m['text'][:50]}")

print("\n[对话] 发送「用五个字总结你自己」")
buf = []
conv_id = None
for kind, payload in api.stream_chat("用五个字总结你自己", "0"):
    if kind == "delta":
        buf.append(payload)
        print(payload, end="", flush=True)
    elif kind == "conv":
        conv_id = payload
        print(f"\n(conversation_id={payload})")
    elif kind == "error":
        print(f"\n[错误] {payload}")
        break
    elif kind == "done":
        break
print(f"\n\n[完成] 共 {len(''.join(buf))} 字，会话 {conv_id}")

api.close()
