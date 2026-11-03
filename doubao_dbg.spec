# -*- mode: python ; coding: utf-8 -*-
import os


a = Analysis(
    [os.path.join(SPECPATH, 'doubao_client', '__main__.py')],
    pathex=[SPECPATH],
    binaries=[],
    datas=[],
    hiddenimports=['win32crypt', 'Crypto.Cipher.AES', 'websocket',
                   # wx.richtext 会连带 import wx._xml / wx._html，
                   # PyInstaller 的 wx hook 不收集这两个扩展
                   'wx.richtext', 'wx._xml', 'wx._html'],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=['tkinter', 'numpy', 'matplotlib', 'PIL'],
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
    name='doubao_dbg',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
