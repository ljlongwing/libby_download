# -*- mode: python ; coding: utf-8 -*-

# One-file build:  pyinstaller FabulyDownloader.spec
# Produces dist/FabulyDownloader(.exe) -- a self-contained console app.
# No Playwright/Chromium (Fabuly needs no browser).  ffmpeg is NOT bundled;
# it is only needed for --mp3 and is picked up from PATH or --ffmpeg.

a = Analysis(
    ['fabuly_dl.py'],
    pathex=[],
    binaries=[],
    # LibriVox catalogue (~19k titles) -- read at runtime from next to the exe.
    datas=[('librivox.db', '.')],
    hiddenimports=[],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=['playwright', 'requests'],
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
    name='FabulyDownloader',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    # UPX-compressed PyInstaller binaries are a well-known antivirus
    # false-positive trigger -- disabled to reduce that risk, at the cost
    # of a larger file.  (Does not silence Windows SmartScreen's separate
    # "unknown publisher" prompt; only code-signing does that.)
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
