"""命名位置（锚点）库：按程序记住「名字 → 屏幕坐标」。

衍生自 NVDA 插件 Golden Cursor 26.1 的命名位置存取思路
（<https://github.com/nvda-es/goldenCursor>）：
Copyright (C) 2015-2022 Salah Atair, Wafeeq Taher, Joseph Lee,
Abdel (Abdelkrim Bensaid) 及 NVDA 西语社区等贡献者。
Golden Cursor 以 GNU GPL v2 授权；本文件作为其衍生作品同样以
GNU GPL v2 发布（见项目根 LICENSE / THIRD-PARTY-NOTICES.md）。
"""
from __future__ import annotations

import json
import logging
import os
import re
import threading
import time

from ..core.config import APP_DIR

logger = logging.getLogger("agent")

STORE_PATH = os.path.join(APP_DIR, "agent_anchors.json")
MAX_PER_APP = 60          # 每个程序最多记多少个，超了淘汰用得最少的
MAX_NAME = 40             # 名字太长多半是模型把整句提示词当名字了

_lock = threading.RLock()
_data: dict | None = None

# 名字比对时忽略这些：空白、全角空格、常见中英标点
_NOISE = re.compile(r"[\s\u3000\-_·.,:;!?、，。：；！？()（）\[\]【】{}<>《》\"'“”‘’/\\|~`]+")


def normalize_name(name: str) -> str:
    """名字归一化：去空白标点、转小写。只用于比对，不改写用户给的原名。"""
    return _NOISE.sub("", str(name or "")).strip().lower()


def app_key(exe: str = "", title: str = "") -> str:
    """锚点按程序分区。优先用 exe 名（稳定），拿不到才退回窗口标题。"""
    key = (exe or "").strip().lower()
    if key:
        return key
    return (title or "").strip().lower()[:60]


# --------------------------------------------------------------------------- #
# 存取
# --------------------------------------------------------------------------- #
def _blank() -> dict:
    return {"version": 1, "apps": {}}


def _read() -> dict:
    global _data
    with _lock:
        if _data is not None:
            return _data
        data = _blank()
        try:
            if os.path.exists(STORE_PATH):
                with open(STORE_PATH, encoding="utf-8") as f:
                    loaded = json.load(f)
                if isinstance(loaded, dict) and isinstance(loaded.get("apps"), dict):
                    data = loaded
        except (OSError, json.JSONDecodeError) as exc:
            # 文件坏了不该让 Agent 起不来：当作空的，下一次写入自然覆盖
            logger.warning("锚点文件读不了，按空处理：%s", exc)
        _data = data
        return data


def _write() -> None:
    """原子写：先写临时文件再替换，避免半截文件把库毁了。"""
    data = _read()
    tmp = STORE_PATH + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=1)
        os.replace(tmp, STORE_PATH)
    except OSError as exc:
        logger.warning("锚点存不下来：%s", exc)
        try:
            os.remove(tmp)
        except OSError:
            pass


def reload() -> None:
    """丢掉内存缓存，下次从磁盘读（给测试和手工改文件用）。"""
    global _data
    with _lock:
        _data = None


# --------------------------------------------------------------------------- #
# 查询
# --------------------------------------------------------------------------- #
def entries(app: str) -> dict[str, dict]:
    """某个程序下所有锚点（副本，改它不影响库）。"""
    apps = _read().get("apps") or {}
    bucket = apps.get(app or "") or {}
    return {k: dict(v) for k, v in bucket.items() if isinstance(v, dict)}


def find(app: str, name: str) -> tuple[int, int, str] | None:
    """按名字找锚点，返回 (x, y, 实际名字)；找不到返回 None。

    模型给的名字不会和存的一字不差（「发送」vs「发送按钮」），所以按
    精确 → 前缀/互相包含 的顺序退让，同等条件下取**长度最接近**的那个，
    避免「发送」匹配到更长的「发送并关闭窗口」。
    """
    want = normalize_name(name)
    if not want:
        return None
    bucket = entries(app)
    if not bucket:
        return None
    best_rank: tuple[int, int] | None = None
    best_name = ""
    for real in bucket:
        norm = normalize_name(real)
        if not norm:
            continue
        gap = abs(len(norm) - len(want))
        if norm == want:
            rank = (0, 0)
        elif norm.startswith(want) or want.startswith(norm):
            rank = (1, gap)
        elif want in norm or norm in want:
            rank = (2, gap)
        else:
            continue
        if best_rank is None or rank < best_rank:
            best_rank, best_name = rank, real
    if best_rank is None:
        return None
    return _coords(bucket[best_name], best_name)


def _coords(entry: dict, real: str) -> tuple[int, int, str] | None:
    try:
        return int(entry.get("x")), int(entry.get("y")), real
    except (TypeError, ValueError):
        return None


def find_any(name: str) -> tuple[str, str] | None:
    """在所有程序里找同名锚点，返回 (程序, 名字)。只用来给报错补一句提示。"""
    want = normalize_name(name)
    if not want:
        return None
    for app in (_read().get("apps") or {}):
        if find(app, name) is not None:
            bucket = entries(app)
            for real in bucket:
                if normalize_name(real) == want:
                    return app, real
            return app, name
    return None


def describe(app: str, limit: int = 12) -> list[str]:
    """给提示词看的锚点清单：用得多的排前面。"""
    bucket = entries(app)
    if not bucket:
        return []
    ordered = sorted(bucket.items(),
                     key=lambda kv: (-int(kv[1].get("hits") or 0),
                                     -float(kv[1].get("updated") or 0)))
    lines = []
    for name, entry in ordered[:limit]:
        coords = _coords(entry, name)
        if coords is None:
            continue
        x, y, _ = coords
        hits = int(entry.get("hits") or 0)
        mark = "（自动记的）" if entry.get("auto") else ""
        lines.append(f"「{name}」x={x} y={y}，用过 {hits} 次{mark}")
    return lines


# --------------------------------------------------------------------------- #
# 写入 / 删除
# --------------------------------------------------------------------------- #
def _resolve_existing(bucket: dict, name: str) -> str:
    """这次要写的名字是不是库里已有的那一条（该就地更新而不是新增）。

    **只认同名**（去空白标点、不分大小写），写的时候不做模糊合并：两个名字长得像
    不代表是同一个控件——「第1项」和「第10项」、「发送」和「发送消息」都是反例，
    错合并比多一条记录麻烦得多。模糊匹配只放在 ``find()`` 里，那是「读」的时候猜。
    """
    want = normalize_name(name)
    if not want:
        return name
    for key in bucket:
        if normalize_name(key) == want:
            return key
    return name


def remember(app: str, name: str, x: int, y: int,
             note: str = "", auto: bool = False,
             bump: bool = True) -> str:
    """记一条锚点，返回最终采用的名字。

    同名（归一化后相同）就地更新并保留原来的写法，不同名一律另存——多一条记录
    不碍事，``find()`` 会按精确→前缀的顺序挑最合适的那个。"""
    app = (app or "").strip().lower()
    clean = str(name or "").strip()[:MAX_NAME]
    if not app or not clean:
        return ""
    with _lock:
        data = _read()
        apps = data.setdefault("apps", {})
        bucket = apps.setdefault(app, {})
        real = _resolve_existing(bucket, clean)
        old = bucket.get(real) or {}
        bucket[real] = {
            "x": int(x),
            "y": int(y),
            "hits": int(old.get("hits") or 0) + (1 if bump else 0),
            "note": str(note or old.get("note") or "")[:80],
            "auto": bool(auto and old.get("auto", True)),
            "updated": round(time.time(), 1),
        }
        _evict(bucket)
        _write()
    return real


def _evict(bucket: dict) -> None:
    """超上限就淘汰：先丢自动记的、用得最少的，还不够再丢手记的。"""
    if len(bucket) <= MAX_PER_APP:
        return

    def rank(kv):
        entry = kv[1]
        return (0 if entry.get("auto") else 1,
                int(entry.get("hits") or 0),
                float(entry.get("updated") or 0))

    for key, _ in sorted(bucket.items(), key=rank)[: len(bucket) - MAX_PER_APP]:
        bucket.pop(key, None)


def forget(app: str, name: str = "") -> int:
    """删掉一个名字（name 为空则清空该程序的全部锚点），返回删了几条。"""
    app = (app or "").strip().lower()
    with _lock:
        apps = _read().setdefault("apps", {})
        bucket = apps.get(app)
        if not bucket:
            return 0
        if not name:
            count = len(bucket)
            apps.pop(app, None)
            _write()
            return count
        want = normalize_name(name)
        gone = [k for k in bucket if normalize_name(k) == want]
        for key in gone:
            bucket.pop(key, None)
        if gone:
            _write()
        return len(gone)
