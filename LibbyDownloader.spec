# -*- mode: python ; coding: utf-8 -*-


a = Analysis(
    ['libby_dl.py'],
    pathex=[],
    binaries=[],
    datas=[],
    hiddenimports=[],
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
    a.binaries,
    a.datas,
    [],
    name='LibbyDownloader',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    # UPX-compressed PyInstaller binaries are a well-known antivirus
    # false-positive trigger (the packed structure resembles common
    # malware packing) -- disabled to reduce that risk, at the cost of a
    # larger file. Doesn't fix Windows SmartScreen's separate "unknown
    # publisher" warning; only a paid code-signing certificate does that.
    upx=False,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
