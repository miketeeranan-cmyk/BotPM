# PyInstaller spec for the Windows desktop build. Built only by CI on a Windows
# runner -- PyInstaller cannot cross-compile, so this cannot produce a working
# exe from macOS.
#
# credentials.json is deliberately absent from datas: the release asset is
# public, and a bundled service account key would be extractable from it.

from PyInstaller.utils.hooks import collect_all, collect_data_files

# playwright_stealth reads all ~20 of its evasion .js files at *import* time
# (stealth.py builds a module-level dict of from_file(...) calls), so without
# them the app dies on `import team_bot` before anything runs. Playwright's own
# Node driver needs no help here -- it ships a PyInstaller hook.
datas = [("templates", "templates"), ("static", "static")]
datas += collect_data_files("playwright_stealth")

# pywebview's Windows backend goes through pythonnet, which carries .NET
# assemblies PyInstaller won't find by following imports. No-ops off Windows,
# where these aren't installed (see the markers in requirements.txt).
binaries = []
hiddenimports = []
for package in ("pythonnet", "clr_loader"):
    try:
        pkg_datas, pkg_binaries, pkg_hidden = collect_all(package)
    except Exception:
        continue
    datas += pkg_datas
    binaries += pkg_binaries
    hiddenimports += pkg_hidden

a = Analysis(
    ["app.py"],
    pathex=[],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports + [
        "playwright",
        "playwright.async_api",
        "playwright.sync_api",
        "playwright_stealth",
        # pywebview resolves its GUI backend at runtime, so PyInstaller can't
        # see these by following imports.
        "webview.platforms.winforms",
        "webview.platforms.edgechromium",
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
    icon="static/icon.ico",
)
