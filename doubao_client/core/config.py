"""应用配置：路径解析与 config.json 的读写。

打包为 exe 后 ``__file__`` 指向 PyInstaller 的临时解包目录，进程退出即被
清除；因此冻结状态下必须使用 exe 所在目录，配置与登录态才能持久保存。
开发状态下应用目录为仓库根目录（即本文件向上两级）。
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

FROZEN = bool(getattr(sys, "frozen", False))

if FROZEN:
    APP_DIR = os.path.dirname(os.path.abspath(sys.executable))
else:
    # doubao_client/core/config.py -> 仓库根目录
    APP_DIR = str(Path(__file__).resolve().parents[2])

CONFIG_PATH = os.path.join(APP_DIR, "config.json")

# 首次运行时生成；build.py 组装发行包时也使用该默认值
DEFAULT_CONFIG: dict = {"device_id": "7687366431038539314",
                        "web_id": "7702742599338001983",
                        # 自动 pip 安装使用的镜像源；空串表示官方 PyPI。
                        # 全局生效：会话内运行代码与 AI Agent 的 run_code 均读取该值
                        "pip_mirror": "https://pypi.tuna.tsinghua.edu.cn/simple"}


class AppConfig:
    """config.json 的面向对象封装。

    一个实例对应一次磁盘读取；``update`` 方法执行读-改-写流程。
    写入失败时不抛出异常，仅返回 False：配置无法持久化不应影响功能运行。
    """

    def __init__(self, path: str = CONFIG_PATH):
        self.path = path
        self._data: dict = {}
        self.reload()

    def reload(self) -> "AppConfig":
        """从磁盘重新读取；文件损坏或缺失时回退到默认值。"""
        data: dict = {}
        if os.path.exists(self.path):
            try:
                with open(self.path, encoding="utf-8") as f:
                    loaded = json.load(f)
                if isinstance(loaded, dict):
                    data = loaded
            except (json.JSONDecodeError, OSError):
                data = {}
        if not data:
            data = dict(DEFAULT_CONFIG)
            self.save(data)
        self._data = {**DEFAULT_CONFIG, **data}
        return self

    def get(self, key: str, default=None):
        return self._data.get(key, default)

    def set(self, key: str, value) -> None:
        self._data[key] = value

    def save(self, data: dict | None = None) -> bool:
        """写入磁盘；失败返回 False，不抛出异常。"""
        if data is not None:
            self._data = data
        try:
            with open(self.path, "w", encoding="utf-8") as f:
                json.dump(self._data, f, indent=2, ensure_ascii=False)
            return True
        except OSError:
            return False

    def update(self, **changes) -> dict:
        """读-改-写一步完成，调用方无需重复这三步操作。"""
        self._data.update(changes)
        self.save()
        return self._data


_shared: AppConfig | None = None


def instance() -> AppConfig:
    """进程级共享实例（每次调用都重新读取磁盘，保证各处取到最新值）。"""
    global _shared
    if _shared is None:
        _shared = AppConfig()
    return _shared.reload()


def load_config() -> dict:
    """读取配置字典；文件损坏或字段缺失时回退默认值，不使启动失败。"""
    return dict(instance()._data)


def save_config(cfg: dict) -> bool:
    return instance().save(cfg)


def update_config(**changes) -> dict:
    return instance().update(**changes)
