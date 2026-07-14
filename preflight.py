"""Checks the things a PC needs before the app can work, so a missing piece
shows a sentence someone can act on instead of a raw driver error.

All of these no-op off Windows -- the Mac dev machine already has what it needs.
"""
import ctypes
import os
import shutil
import subprocess
import sys

import paths

CHROME_DOWNLOAD_URL = "https://www.google.com/chrome/"
WEBVIEW2_DOWNLOAD_URL = "https://developer.microsoft.com/microsoft-edge/webview2/"

# Microsoft's fixed GUID for the WebView2 Evergreen Runtime. Present under
# EdgeUpdate\Clients once the runtime is installed, which it is out of the box
# on Windows 11 and on any Windows 10 carrying Edge.
_WEBVIEW2_GUID = "{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}"


def find_chrome():
    """The real Google Chrome on this PC, or None.

    dm_bot launches with channel="chrome", i.e. the browser already installed
    here rather than a bundled Chromium -- that's both what keeps the exe small
    and what makes playwright_stealth credible. If it's absent, Playwright
    raises an error naming an internal path, which tells a user nothing.
    """
    if os.name != "nt":
        return shutil.which("google-chrome") or shutil.which("chromium") or (
            "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
            if os.path.exists("/Applications/Google Chrome.app") else None
        )

    for env in ("PROGRAMFILES", "PROGRAMFILES(X86)", "LOCALAPPDATA"):
        base = os.environ.get(env)
        if not base:
            continue
        candidate = os.path.join(base, "Google", "Chrome", "Application", "chrome.exe")
        if os.path.exists(candidate):
            return candidate

    # Installed somewhere non-standard: Chrome registers its own path here.
    try:
        import winreg
        for root in (winreg.HKEY_LOCAL_MACHINE, winreg.HKEY_CURRENT_USER):
            try:
                key = winreg.OpenKey(
                    root, r"SOFTWARE\Clients\StartMenuInternet\Google Chrome\shell\open\command"
                )
                with key:
                    command, _ = winreg.QueryValueEx(key, "")
                path = command.strip('"').split('"')[0]
                if os.path.exists(path):
                    return path
            except OSError:
                continue
    except ImportError:
        pass
    return None


def has_webview2():
    """Whether the WebView2 runtime pywebview renders through is installed."""
    if os.name != "nt":
        return True
    try:
        import winreg
    except ImportError:
        return True

    for root, subkey in (
        (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\WOW6432Node\Microsoft\EdgeUpdate\Clients"),
        (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\EdgeUpdate\Clients"),
        (winreg.HKEY_CURRENT_USER, r"SOFTWARE\Microsoft\EdgeUpdate\Clients"),
    ):
        try:
            key = winreg.OpenKey(root, subkey + "\\" + _WEBVIEW2_GUID)
            with key:
                version, _ = winreg.QueryValueEx(key, "pv")
            if version and version != "0.0.0.0":
                return True
        except OSError:
            continue
    return False


def show_message(title, text):
    """A message when there's no webview to render one in.

    Used only for WebView2 being missing -- every other problem can be shown as
    a normal page inside the app window.
    """
    if os.name == "nt":
        ctypes.windll.user32.MessageBoxW(None, text, title, 0x40)  # MB_ICONINFORMATION
    else:
        print(f"{title}: {text}", file=sys.stderr)


def open_url(url):
    if os.name == "nt":
        os.startfile(url)  # noqa: S606 -- opens the default browser
    else:
        subprocess.run(["open", url], check=False)


class SingleInstance:
    """Stops a second copy from starting.

    Two copies would race over the same automation_session Chrome profile, whose
    lock file lets exactly one Chrome own it -- and double-clicking an exe twice
    is an easy thing to do by accident.
    """

    def __init__(self):
        self._handle = None

    def acquire(self):
        if os.name != "nt":
            return True
        # A named kernel mutex disappears with the process, so a crashed run
        # can't leave a stale lock behind the way a lock file would.
        self._handle = ctypes.windll.kernel32.CreateMutexW(None, False, f"Global\\{paths.APP_NAME}")
        return ctypes.windll.kernel32.GetLastError() != 183  # ERROR_ALREADY_EXISTS
