# -*- mode: python ; coding: utf-8 -*-
import os


a = Analysis(
    [os.path.join(SPECPATH, 'doubao_client', '__main__.py')],
    pathex=[SPECPATH],
    binaries=[],
    datas=[(os.path.join(SPECPATH, 'doubao_client', 'sounds'), 'sounds')],
    # wx.richtext 会连带 import wx._xml / wx._html，PyInstaller 的 wx hook 不收集这两个
    hiddenimports=['win32crypt', 'Crypto.Cipher.AES', 'websocket',
                   # 朗读的兜底后端用 SAPI，走 COM
                   'pythoncom', 'win32com', 'win32com.client',
                   'wx.richtext', 'wx._xml', 'wx._html',
                   # agent 动作层 / 观察层用到的 pywin32 子模块
                   'win32gui', 'win32clipboard', 'win32con', 'win32process',
                   'win32gui_struct',
                   # 代码执行与可视化的标准库
                   'ctypes', 'subprocess', 'threading'],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=['tkinter', 'numpy', 'matplotlib', 'PIL', 'pytest'],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name='doubao_client',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
