# PyInstaller spec for the Windows desktop build. Built only by CI on a Windows
# runner -- PyInstaller cannot cross-compile, so this cannot produce a working
# exe from macOS.
#
# credentials.json is deliberately absent from datas: the release asset is
# public, and a bundled service account key would be extractable from it.

a = Analysis(
    ["app.py"],
    pathex=[],
    binaries=[],
    datas=[
        ("templates", "templates"),
        ("static", "static"),
    ],
    # Playwright still needs its bundled Node driver even though the browser
    # itself is the PC's installed Chrome (channel="chrome").
    hiddenimports=[
        "playwright",
        "playwright.async_api",
        "playwright.sync_api",
        "playwright_stealth",
        # pywebview resolves its GUI backend at runtime, so PyInstaller can't
        # see these by following imports.
        "webview.platforms.winforms",
        "webview.platforms.edgechromium",
        "clr_loader",
        "pythonnet",
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=["tkinter"],
    noarchive=False,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="TeamSheet",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    runtime_tmpdir=None,
    console=False,  # a desktop program, not a terminal app
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
