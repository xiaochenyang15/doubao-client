# -*- coding: utf-8 -*-
"""缺库自动安装链路的回归测试。

盯住这几个历史 bug：
1. 一段代码缺好几个包时只装第一个、轮次不够就放弃 → 必须批量装；
2. 装包超时上限 180s，慢网上正常下载被当成失败杀掉 → 下限 600s；
3. 镜像源写死 → 走全局配置且镜像优先；
4. 装包全程没有提示、不能取消 → 流式输出回调 + 取消事件。
"""
from __future__ import annotations

import io
import os
import subprocess
import sys
import tempfile
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from doubao_client.core import runner  # noqa: E402

PASS = 0
FAIL = 0


def check(name: str, cond: bool, extra: str = ""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ok    {name}")
    else:
        FAIL += 1
        print(f"  FAIL  {name}  {extra}")


print("[1] 常量与配置")
check("装包尝试上限足够（>=6 轮，缺多个包也装得完）",
      runner.MAX_INSTALL_ROUNDS >= 6, str(runner.MAX_INSTALL_ROUNDS))
check("装包单次超时下限 >= 600s（慢网正常下载不被误杀）",
      runner.INSTALL_PER_TRY >= 600, str(runner.INSTALL_PER_TRY))
from doubao_client.core.config import DEFAULT_CONFIG  # noqa: E402
check("DEFAULT_CONFIG 有 pip_mirror 全局键",
      "pip_mirror" in DEFAULT_CONFIG, str(DEFAULT_CONFIG))

print("\n[2] pip_mirror() 读全局配置")
real_load = runner.load_config
try:
    runner.load_config = lambda: {"pip_mirror": "http://my-mirror/simple"}
    check("配置里的镜像被读出", runner.pip_mirror() == "http://my-mirror/simple")
    runner.load_config = lambda: {"pip_mirror": "  "}
    check("空白配置退回空串（= 官方 PyPI）", runner.pip_mirror() == "")
    runner.load_config = lambda: {}
    check("缺键不炸", runner.pip_mirror() == "")
finally:
    runner.load_config = real_load

print("\n[3] _missing_modules 一次挖全")
out = ("ModuleNotFoundError: No module named 'pil'\n"
       "ModuleNotFoundError: No module named 'requests'\n"
       "ModuleNotFoundError: No module named 'pil'\n"
       "ModuleNotFoundError: No module named 'a.b.c'\n")
mods = runner._missing_modules(out)
check("三个顶层模块（去重、点号取顶层）", mods == ["pil", "requests", "a"],
      str(mods))

print("\n[4] 缺多个包：批量一次装全（旧版只装第一个）")
execs: list[int] = []
installs: list[list[str]] = []
# runner 已重构为 Interpreter / CodeRunner 两个实例：执行与安装分别挂在
# code_runner / interpreter 上，模块级 run() 只是转发到默认实例。
REAL_RUNNER = runner.code_runner
REAL_INTERP = runner.interpreter
real_exec, real_inst = REAL_RUNNER._exec_once, REAL_INTERP.pip_install
real_py_cache, real_info_cache = REAL_INTERP._py_cache, REAL_INTERP._info_cache
REAL_INTERP._py_cache = "python.exe"
REAL_INTERP._info_cache = {"path": "python.exe", "version": "3.13",
                           "bundled": False, "writable": True}


def fake_exec(script, wd, timeout, on_output=None, cancel=None):
    execs.append(1)
    if len(execs) == 1:
        return 1, ("ModuleNotFoundError: No module named 'pil'\n"
                   "ModuleNotFoundError: No module named 'requests'\n"), False, False
    return 0, "done\n", False, False


def fake_install(pkgs, timeout, on_output=None, cancel=None):
    installs.append(list(pkgs))
    return True, ""

tmp = tempfile.mkdtemp(prefix="runner_test_")
try:
    REAL_RUNNER._exec_once = fake_exec
    REAL_INTERP.pip_install = fake_install
    res = runner.run("print(1)", tmp)
    check("一条 pip 命令装两个包（别名 pil→pillow）",
          installs == [["pillow", "requests"]], str(installs))
    check("装完重跑成功", res.ok and len(execs) == 2,
          f"ok={res.ok} execs={len(execs)}")
    check("installed 记录全", res.installed == ["pillow", "requests"],
          str(res.installed))

    print("\n[5] 缺三个包也只用一轮装完（轮次留给真实失败重试）")
    execs.clear(); installs.clear()
    def fake_exec3(script, wd, timeout, on_output=None, cancel=None):
        execs.append(1)
        if len(execs) == 1:
            return 1, ("No module named 'pandas'\n"
                       "No module named 'numpy'\n"
                       "No module named 'openpyxl'\n"), False, False
        return 0, "ok\n", False, False
    REAL_RUNNER._exec_once = fake_exec3
    res = runner.run("print(1)", tmp)
    check("一次装齐 pandas/numpy/openpyxl",
          installs == [["pandas", "numpy", "openpyxl"]], str(installs))
    check("重跑成功", res.ok, res.summary())

    print("\n[6] 安装失败要把原因和换源建议写进输出")
    execs.clear()
    def fake_install_fail(pkgs, timeout, on_output=None, cancel=None):
        return False, "ERROR: Could not find a version"
    REAL_INTERP.pip_install = fake_install_fail
    res = runner.run("print(1)", tmp)
    check("输出含失败原因", "自动安装" in res.output and "失败" in res.output)
    check("提示换镜像源", "镜像源" in res.output, res.output[-200:])

    print("\n[7] 取消：预置位 → 不开跑；跑一半 → 杀进程")
    execs.clear()
    ev = threading.Event()
    ev.set()
    res = runner.run("print(1)", tmp, cancel=ev)
    check("预置取消 → cancelled 且没执行", res.cancelled and not execs,
          f"cancelled={res.cancelled} execs={len(execs)}")
    check("summary 说清是取消", "取消" in res.summary(), res.summary())
finally:
    REAL_RUNNER._exec_once, REAL_INTERP.pip_install = real_exec, real_inst
    REAL_INTERP._py_cache, REAL_INTERP._info_cache = real_py_cache, real_info_cache

print("\n[8] _pump：真子进程的取消与逐行输出")
proc = subprocess.Popen(
    [sys.executable, "-c", "import time; time.sleep(30)"],
    stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
    text=True, encoding="utf-8", errors="replace")
ev = threading.Event()
threading.Timer(0.6, ev.set).start()
t0 = time.time()
out, timed_out, cancelled = runner._pump(proc, 60, cancel=ev)
check("取消后立刻返回（<10s）", cancelled and not timed_out and time.time() - t0 < 10,
      f"cancelled={cancelled} timed_out={timed_out}")
check("进程被杀掉", proc.poll() is not None)

lines: list[str] = []
proc = subprocess.Popen(
    [sys.executable, "-c", "print('行A');print('行B')"],
    stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
    text=True, encoding="utf-8", errors="replace")
out, timed_out, cancelled = runner._pump(proc, 30, on_output=lines.append)
check("逐行回调都到齐", "行A" in lines and "行B" in lines, str(lines))
check("未超时未取消", not timed_out and not cancelled)

print("\n[9] pip_install：镜像优先 + 批量 + 可取消")
calls: list[list[str]] = []


class FakeProc:
    def __init__(self, cmd, rc=0, out="Requirement already satisfied\n"):
        self.cmd = cmd
        self.returncode = rc
        self.stdout = io.StringIO(out)

    def wait(self, timeout=None):
        return self.returncode

    def poll(self):
        return self.returncode

    def kill(self):
        self.returncode = -9


real_popen = runner.subprocess.Popen
real_load = runner.load_config
real_info = REAL_INTERP._info_cache
real_py = REAL_INTERP._py_cache
try:
    # 预置解释器画像，免得 info() 内部的 subprocess.run 撞上打桩的 Popen
    REAL_INTERP._py_cache = "python.exe"
    REAL_INTERP._info_cache = {"path": "python.exe", "version": "3.13",
                               "bundled": False, "writable": True}
    runner.load_config = lambda: {"pip_mirror": "http://cfg-mirror/simple"}
    def fake_popen(cmd, **kw):
        calls.append(list(cmd))
        return FakeProc(cmd)
    runner.subprocess.Popen = fake_popen
    ok, err = REAL_INTERP.pip_install(["pillow", "requests"], 300)
    check("成功", ok and not err)
    check("第一条尝试就用配置里的镜像",
          calls and calls[0][calls[0].index("-i") + 1] == "http://cfg-mirror/simple",
          str(calls[0]))
    check("一条命令带全部包",
          calls and "pillow" in calls[0] and "requests" in calls[0], str(calls[0]))

    calls.clear()
    def fail_popen(cmd, **kw):
        calls.append(list(cmd))
        return FakeProc(cmd, rc=1, out="ERROR: No matching distribution found\n")
    runner.subprocess.Popen = fail_popen
    ok, err = REAL_INTERP.pip_install(["nope"], 300)
    check("全失败时报错", not ok and "No matching distribution" in err)
    check("失败后退回官方 PyPI 兜底",
          any("-i" not in c for c in calls), str([c[:6] for c in calls]))

    calls.clear()
    ev = threading.Event()
    ev.set()
    ok, err = REAL_INTERP.pip_install(["pillow"], 300, cancel=ev)
    check("取消事件置位 → 返回已取消", not ok and err == "已取消", err)
finally:
    runner.subprocess.Popen = real_popen
    runner.load_config = real_load
    REAL_INTERP._info_cache = real_info
    REAL_INTERP._py_cache = real_py

print(f"\n{'=' * 50}\n通过 {PASS} 项，失败 {FAIL} 项")
sys.exit(1 if FAIL else 0)
