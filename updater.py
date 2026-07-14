"""Checks GitHub for a newer release, downloads it, and swaps the exe.

The repo is public, so the releases API needs no token -- which is exactly why
credentials.json must never be bundled into the asset this downloads.
"""
import json
import logging
import os
import subprocess
import sys
import urllib.error
import urllib.request

from packaging.version import InvalidVersion, Version

import paths
from version import VERSION

REPO = "miketeeranan-cmyk/BotPM"
LATEST_RELEASE_URL = f"https://api.github.com/repos/{REPO}/releases/latest"
ASSET_NAME = "TeamSheet.exe"
RELEASES_PAGE = f"https://github.com/{REPO}/releases/latest"

_TIMEOUT = 15


def _get_json(url):
    request = urllib.request.Request(url, headers={
        "Accept": "application/vnd.github+json",
        "User-Agent": f"TeamSheet/{VERSION}",
    })
    with urllib.request.urlopen(request, timeout=_TIMEOUT) as response:
        return json.load(response)


def check():
    """Returns {"available", "current", "latest", "url", "size"}.

    Never raises: a failed check (offline, GitHub down, rate limited) must let
    the app start normally rather than block someone from working.
    """
    result = {"available": False, "current": VERSION, "latest": None, "url": None, "size": 0}
    if not paths.is_frozen():
        return result  # running from source; there's nothing to replace
    try:
        release = _get_json(LATEST_RELEASE_URL)
        latest = (release.get("tag_name") or "").lstrip("v")
        asset = next((a for a in release.get("assets", []) if a.get("name") == ASSET_NAME), None)
        if not latest or asset is None:
            return result
        result["latest"] = latest
        result["url"] = asset.get("browser_download_url")
        result["size"] = asset.get("size") or 0
        result["available"] = Version(latest) > Version(VERSION)
    except (InvalidVersion, urllib.error.URLError, OSError, ValueError, KeyError) as e:
        logging.error(f"Update check failed: {e}")
    return result


def download(url, on_progress):
    """Streams the new exe next to the current one, reporting bytes as it goes.

    Written to the install dir rather than a temp dir so the later swap is a
    rename within one filesystem -- a move across volumes can fail part-written.
    """
    target = os.path.join(_install_dir(), ASSET_NAME + ".new")
    request = urllib.request.Request(url, headers={"User-Agent": f"TeamSheet/{VERSION}"})
    with urllib.request.urlopen(request, timeout=_TIMEOUT) as response:
        total = int(response.headers.get("Content-Length") or 0)
        done = 0
        with open(target, "wb") as f:
            while True:
                chunk = response.read(64 * 1024)
                if not chunk:
                    break
                f.write(chunk)
                done += len(chunk)
                on_progress(done, total)
    return target


def _install_dir():
    return os.path.dirname(os.path.abspath(sys.executable))


def apply_and_restart(new_exe):
    """Replaces the running exe and relaunches it.

    Windows locks a running exe against being overwritten, so the swap has to
    outlive us: a detached script waits for this PID to die, moves the new file
    over the old, starts it, and deletes itself.
    """
    current = os.path.abspath(sys.executable)
    script = os.path.join(paths.data_dir(), "apply_update.bat")
    with open(script, "w", encoding="utf-8") as f:
        f.write(
            "@echo off\r\n"
            "setlocal\r\n"
            f':wait\r\n'
            f'tasklist /FI "PID eq {os.getpid()}" 2>nul | find "{os.getpid()}" >nul\r\n'
            'if not errorlevel 1 (\r\n'
            '  ping -n 2 127.0.0.1 >nul\r\n'
            '  goto wait\r\n'
            ')\r\n'
            f'move /Y "{new_exe}" "{current}" >nul\r\n'
            f'start "" "{current}"\r\n'
            'del "%~f0"\r\n'
        )
    # DETACHED_PROCESS: the script has to survive us exiting a moment later.
    subprocess.Popen(
        ["cmd", "/c", script],
        creationflags=0x00000008 | 0x00000200,  # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP
        close_fds=True,
    )
