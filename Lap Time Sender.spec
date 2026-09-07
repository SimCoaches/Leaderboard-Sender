# -*- mode: python ; coding: utf-8 -*-

from PyInstaller.utils.hooks import collect_all
import PyQt6.sip
from pathlib import Path

qrcode_datas, qrcode_binaries, qrcode_hiddenimports = collect_all('qrcode')
sip_binary = PyQt6.sip.__file__

a = Analysis(
    ['lap_time_sender.py'],
    pathex=[],
    binaries=[
        (sip_binary, "PyQt6")
    ] + qrcode_binaries,
    datas=[('resources', 'resources')] + qrcode_datas,
    hiddenimports=qrcode_hiddenimports + ['websocket'],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    optimize=0,
)

# Qt uses the Windows ICU shim. Some build machines also put a third-party
# ICU runtime on PATH (for example Poppler); PyInstaller can collect that DLL
# and place it ahead of System32, which makes Qt6Core fail at startup.
a.binaries = [
    entry for entry in a.binaries
    if not (
        Path(entry[0]).name.lower().startswith('icu')
        and Path(entry[0]).name.lower().endswith('.dll')
    )
]
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
