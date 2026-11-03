from __future__ import annotations

import os
import sys

try:
    import winsound
    _HAVE = True
except Exception:  # noqa: BLE001
    _HAVE = False

_ENABLED = True


def _sounds_dir() -> str:
    base = getattr(sys, "_MEIPASS", None)
    if base:
        return os.path.join(base, "sounds")
    here = os.path.dirname(os.path.abspath(__file__))
    # agent/sounds.py -> 项目根
    return os.path.join(os.path.dirname(here), "sounds")


def set_enabled(value: bool) -> None:
    """总开关（界面将来可放一个「音效」勾选项）。"""
    global _ENABLED
    _ENABLED = bool(value)


def is_enabled() -> bool:
    return _ENABLED


def available() -> list[str]:
    """返回当前可播放的音效名（便于界面列出/校验）。"""
    d = _sounds_dir()
    out = []
    if os.path.isdir(d):
        for fn in sorted(os.listdir(d)):
            if fn.endswith(".wav"):
                out.append(fn[:-4])
    return out


def play(name: str) -> None:
    """播放 sounds/<name>.wav；文件不存在或任何异常都静默跳过。"""
    if not _ENABLED or not _HAVE:
        return
    try:
        path = os.path.join(_sounds_dir(), f"{name}.wav")
        if os.path.isfile(path):
            # SND_ASYNC：后台播放，不阻塞 Agent 工作线程
            winsound.PlaySound(path, winsound.SND_FILENAME | winsound.SND_ASYNC)
    except Exception:  # noqa: BLE001
        pass
