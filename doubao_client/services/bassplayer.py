"""BASS 音频播放：直接 ctypes 调 bass.dll，不再自己解音频容器。

为什么不用现成的播放器
----------------------
豆包语音合成回来的是 ADTS 裸 AAC 流（`fff1` 同步头 + Lavc 编码器标记），
它有两种尴尬：

* Python 标准库没有解码器；
* 系统那几个放声音的接口（`winsound.PlaySound` / `wx.media` / `PlaySound`）
  只认文件路径，不认内存里的裸数据——落盘再放会让「按句播放」多出成百次
  磁盘往返，也容易留下垃圾文件。

BASS 正好补这两块：`BASS_StreamCreateFile` 的 `mem` 模式能从内存建流，
`bass_aac.dll` 插件负责解 AAC。核心 DLL 只有 167 KB，无需安装、无需注册表，
可以直接随程序分发。

初始化只做一次
--------------
BASS 是**进程级**的：第二次 `BASS_Init` 会直接失败（实测错误码 14）。所以
初始化放在模块级、加锁做一次，所有 `Player` 共用同一个库句柄。建流、播放、
停止这些调用 BASS 本身是线程安全的，跟着谁调都行。
"""

from __future__ import annotations

import ctypes
import logging
import os
import struct
import threading
import time
from ctypes import wintypes as wt

from ..core.config import APP_DIR

logger = logging.getLogger("bass")

# ---- BASS 常量（bass.h 里挑出来的，只留用得上的）----
BASS_ACTIVE_PLAYING = 1
BASS_ATTRIB_VOL = 2


def dll_dir() -> str:
    """DLL 目录：按解释器位数挑 x64 / x86，认不到就退回 x64。"""
    bits = struct.calcsize("P") * 8
    base = os.path.join(APP_DIR, "bin", "bass")
    for name in (f"x{bits}", "x64", "x86"):
        p = os.path.join(base, name)
        if os.path.isfile(os.path.join(p, "bass.dll")):
            return p
    return os.path.join(base, f"x{bits}")


# 进程级状态：BASS 只初始化一次
_lock = threading.RLock()
_lib = None
_init = {"tried": False, "ok": False, "error": ""}


def _bind(lib) -> None:
    lib.BASS_Init.argtypes = [ctypes.c_int, wt.DWORD, wt.DWORD,
                              ctypes.c_void_p, ctypes.c_void_p]
    lib.BASS_Init.restype = wt.BOOL
    lib.BASS_ErrorGetCode.restype = ctypes.c_int
    lib.BASS_PluginLoad.argtypes = [ctypes.c_char_p, wt.DWORD]
    lib.BASS_PluginLoad.restype = ctypes.c_void_p
    lib.BASS_StreamCreateFile.argtypes = [wt.BOOL, ctypes.c_void_p,
                                          ctypes.c_ulonglong,
                                          ctypes.c_ulonglong, wt.DWORD]
    lib.BASS_StreamCreateFile.restype = wt.DWORD
    lib.BASS_ChannelPlay.argtypes = [wt.DWORD, wt.BOOL]
    lib.BASS_ChannelPlay.restype = wt.BOOL
    lib.BASS_ChannelStop.argtypes = [wt.DWORD]
    lib.BASS_ChannelStop.restype = wt.BOOL
    lib.BASS_ChannelIsActive.argtypes = [wt.DWORD]
    lib.BASS_ChannelIsActive.restype = wt.DWORD
    lib.BASS_ChannelGetLength.argtypes = [wt.DWORD, wt.DWORD]
    lib.BASS_ChannelGetLength.restype = ctypes.c_ulonglong
    lib.BASS_ChannelBytes2Seconds.argtypes = [wt.DWORD, ctypes.c_ulonglong]
    lib.BASS_ChannelBytes2Seconds.restype = ctypes.c_double
    lib.BASS_ChannelSetAttribute.argtypes = [wt.DWORD, wt.DWORD, ctypes.c_float]
    lib.BASS_ChannelSetAttribute.restype = wt.BOOL
    lib.BASS_StreamFree.argtypes = [wt.DWORD]
    lib.BASS_StreamFree.restype = wt.BOOL


def load() -> bool:
    """加载并初始化 BASS（只会真正做一次）。失败原因看 error()。"""
    global _lib
    with _lock:
        if _init["tried"]:
            return _init["ok"]
        _init["tried"] = True
        _init["ok"] = False
        folder = dll_dir()
        core = os.path.join(folder, "bass.dll")
        if not os.path.isfile(core):
            _init["error"] = "缺少 bin/bass/x64/bass.dll"
            return False
        try:
            if hasattr(os, "add_dll_directory"):
                os.add_dll_directory(folder)
            lib = ctypes.WinDLL(core)
            _bind(lib)
        except Exception as exc:  # noqa: BLE001
            _init["error"] = f"加载 bass.dll 失败：{exc}"
            return False
        if not lib.BASS_Init(-1, 44100, 0, None, None):
            code = lib.BASS_ErrorGetCode()
            _init["error"] = {
                3: "没有可用的声卡设备",
                8: "声卡已被其它程序独占",
            }.get(code, f"声卡没初始化起来（BASS 错误码 {code}）")
            return False
        # AAC 解码插件：没有它就只能放 MP3/WAV，而豆包这条流是 AAC
        aac = os.path.join(folder, "bass_aac.dll")
        if os.path.isfile(aac):
            if not lib.BASS_PluginLoad(aac.encode("utf-8"), 0):
                logger.warning("bass_aac.dll 加载失败（错误码 %s）",
                               lib.BASS_ErrorGetCode())
        else:
            logger.warning("缺 bass_aac.dll：豆包的 AAC 音频放不了")
        _lib = lib
        _init["ok"] = True
        return True


def error() -> str:
    return _init["error"]


class Player:
    """一个够用的播放器：放一段内存音频，等它放完，随时能停。"""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._stream = 0
        self._buf = None          # 建流时传进去的内存必须一直活着
        self._seconds = 0.0
        self._volume = 1.0
        self._error = ""

    # ---------- 播放 ----------
    def play(self, data: bytes) -> bool:
        """把一段内存音频（AAC/MP3/WAV）丢给声卡，立刻返回。"""
        if not data:
            return False
        if not load():
            self._error = error()
            return False
        lib = _lib
        with self._lock:
            self._free()
            buf = ctypes.create_string_buffer(data, len(data))
            handle = lib.BASS_StreamCreateFile(
                True, ctypes.cast(buf, ctypes.c_void_p), 0, len(data), 0)
            if not handle:
                self._error = f"解不了这段音频（BASS 错误码 {lib.BASS_ErrorGetCode()}）"
                logger.warning(self._error)
                return False
            self._buf = buf
            self._stream = handle
            lib.BASS_ChannelSetAttribute(handle, BASS_ATTRIB_VOL, self._volume)
            length = lib.BASS_ChannelGetLength(handle, 0)
            self._seconds = (lib.BASS_ChannelBytes2Seconds(handle, length)
                             if length else 0.0)
            if not lib.BASS_ChannelPlay(handle, True):
                self._error = f"播放失败（BASS 错误码 {lib.BASS_ErrorGetCode()}）"
                logger.warning(self._error)
                self._free()
                return False
            return True

    def wait(self, stop: threading.Event | None = None,
             timeout: float | None = None) -> None:
        """等这一段放完（或被叫停）。"""
        deadline = time.time() + timeout if timeout else None
        while self.is_playing():
            if stop is not None and stop.is_set():
                return
            if deadline and time.time() > deadline:
                return
            time.sleep(0.03)

    def stop(self) -> None:
        with self._lock:
            if self._stream and _lib is not None:
                try:
                    _lib.BASS_ChannelStop(self._stream)
                except Exception:  # noqa: BLE001
                    pass
            self._free()

    def _free(self) -> None:
        if self._stream and _lib is not None:
            try:
                _lib.BASS_StreamFree(self._stream)
            except Exception:  # noqa: BLE001
                pass
        self._stream = 0
        self._buf = None
        self._seconds = 0.0

    def is_playing(self) -> bool:
        with self._lock:
            if not self._stream or _lib is None:
                return False
            try:
                return _lib.BASS_ChannelIsActive(self._stream) == BASS_ACTIVE_PLAYING
            except Exception:  # noqa: BLE001
                return False

    def set_volume(self, value: float) -> None:
        self._volume = max(0.0, min(1.0, float(value)))
        with self._lock:
            if self._stream and _lib is not None:
                try:
                    _lib.BASS_ChannelSetAttribute(
                        self._stream, BASS_ATTRIB_VOL, self._volume)
                except Exception:  # noqa: BLE001
                    pass

    @property
    def seconds(self) -> float:
        """当前这段音频多长（秒）。"""
        return self._seconds

    def error(self) -> str:
        return self._error or error()

    def available(self) -> bool:
        return load()
