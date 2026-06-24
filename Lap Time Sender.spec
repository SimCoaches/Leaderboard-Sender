# -*- mode: python ; coding: utf-8 -*-

from PyInstaller.utils.hooks import collect_all
import PyQt6.sip

qrcode_datas, qrcode_binaries, qrcode_hiddenimports = collect_all('qrcode')
sip_binary = PyQt6.sip.__file__

a = Analysis(
    ['lap_time_sender.py'],
    pathex=[],
    binaries=[
        (sip_binary, "PyQt6")
    ] + qrcode_binaries,
    datas=[('resources', 'resources')] + qrcode_datas,
    hiddenimports=qrcode_hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='Lap Time Sender',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=['resources\\icon.png'],
)
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name='Lap Time Sender',
)
