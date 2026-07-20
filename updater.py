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
# The swapped-in exe writes this file (in paths.data_dir()) as its first startup
# action, so apply_update.bat can tell a working new version from a broken one and
# roll back if it never appears. Kept in sync with app._write_update_marker.
MARKER_NAME = "update_ok.flag"

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
    """Streams the new exe next to the current one, reporting bytes as it goes,
    then verifies it's complete before handing it to the swap.

    Written to the install dir rather than a temp dir so the later swap is a
    rename within one filesystem -- a move across volumes can fail part-written.

    A truncated download (connection dropped without raising, or an HTML error
    page saved in place of the exe) would otherwise be swapped in as-is, and a
    corrupt onefile exe can't unpack python3.14.dll -> "Failed to load Python DLL"
    on every launch. So on any integrity failure we delete the partial file and
    raise, which leaves the current working exe untouched -- a failed update must
    never be worse than no update.
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
    _verify_download(target, total)
    return target


def _verify_download(target, total):
    """Raises (after deleting `target`) if the downloaded file isn't a plausible,
    complete Windows exe. `total` is the server's Content-Length, or 0 if it
    didn't send one."""
    def reject(reason):
        try:
            os.remove(target)
        except OSError:
            pass
        raise IOError(f"Update download looks corrupt ({reason}) -- keeping the current version.")

    size = os.path.getsize(target)
    # Primary check: bytes on disk match what the server said it would send.
    # GitHub/S3 always send Content-Length, so a short file is a truncated stream.
    if total and size != total:
        reject(f"got {size} of {total} bytes")
    # Fallbacks when there's no Content-Length to compare against: reject an
    # implausibly small file, or one that isn't a PE binary (e.g. a saved HTML
    # error/redirect page).
    if size < 1_000_000:
        reject(f"only {size} bytes")
    with open(target, "rb") as f:
        if f.read(2) != b"MZ":
            reject("not a Windows executable")


def _install_dir():
    return os.path.dirname(os.path.abspath(sys.executable))


def apply_and_restart(new_exe):
    """Replaces the running exe and relaunches it, with a backup + auto-rollback.

    Windows locks a running exe against being overwritten, so the swap has to
    outlive us: a detached script waits for this process (and its onefile
    bootloader parent) to die, then swaps the file, starts it, and deletes itself.

    The swap is made reversible so a bad update can never strand the machine:
      1. Rename the old exe to TeamSheet.exe.bak (this also *is* the wait for the
         lock to clear -- the rename only succeeds once the exe is free).
      2. Move the (already integrity-checked) new exe into place and launch it.
      3. Wait for the new exe to write its startup marker (see MARKER_NAME). If it
         appears, the new version booted fine -- delete the backup. If it never
         appears within the window, the new exe is broken (e.g. it can't load
         python3.14.dll), so restore the backup and relaunch the old version.
    Every step is logged to update.log so a failed update is diagnosable.
    """
    current = os.path.abspath(sys.executable)
    backup = current + ".bak"
    marker = os.path.join(paths.data_dir(), MARKER_NAME)
    log = os.path.join(paths.data_dir(), "update.log")
    script = os.path.join(paths.data_dir(), "apply_update.bat")
    with open(script, "w", encoding="utf-8") as f:
        f.write(
            "@echo off\r\n"
            "setlocal\r\n"
            f'set "LOG={log}"\r\n'
            f'set "SRC={new_exe}"\r\n'
            f'set "DST={current}"\r\n'
            f'set "BAK={backup}"\r\n'
            f'set "FLAG={marker}"\r\n'
            'echo update started %DATE% %TIME% > "%LOG%"\r\n'
            'del "%FLAG%" >nul 2>&1\r\n'  # clear any stale marker before we relaunch
            # 1. Back up the old exe -- retrying until the rename succeeds, which
            #    is exactly the wait for the running exe (+ its bootloader) to
            #    release the file. Bounded (~40s) so a stuck lock can't loop forever.
            'set /a tries=0\r\n'
            ':backup\r\n'
            'ping -n 2 127.0.0.1 >nul\r\n'
            'move /Y "%DST%" "%BAK%" >>"%LOG%" 2>&1\r\n'
            'if not exist "%DST%" goto swap\r\n'
            'set /a tries+=1\r\n'
            'echo exe still locked, retry %tries% >> "%LOG%"\r\n'
            'if %tries% GEQ 20 goto giveup\r\n'
            'goto backup\r\n'
            # 2. Move the new exe into place and launch it.
            ':swap\r\n'
            'move /Y "%SRC%" "%DST%" >>"%LOG%" 2>&1\r\n'
            'if not exist "%DST%" goto restorefail\r\n'
            'echo swapped ok, launching new version >> "%LOG%"\r\n'
            'start "" "%DST%"\r\n'
            # 3. Wait up to ~30s for the new exe to signal a successful start.
            'set /a waited=0\r\n'
            ':wait\r\n'
            'ping -n 2 127.0.0.1 >nul\r\n'
            'if exist "%FLAG%" goto success\r\n'
            'set /a waited+=1\r\n'
            'if %waited% GEQ 30 goto rollback\r\n'
            'goto wait\r\n'
            ':success\r\n'
            'echo new version started ok >> "%LOG%"\r\n'
            'del "%BAK%" >nul 2>&1\r\n'
            'del "%FLAG%" >nul 2>&1\r\n'
            'del "%~f0"\r\n'
            'exit\r\n'
            # New exe never signaled -- it's broken. Put the old one back.
            ':rollback\r\n'
            'echo new version never signaled start -- rolling back >> "%LOG%"\r\n'
            'move /Y "%BAK%" "%DST%" >>"%LOG%" 2>&1\r\n'
            'start "" "%DST%"\r\n'
            'del "%~f0"\r\n'
            'exit\r\n'
            # The swap-move itself failed after the backup -- restore the old exe.
            ':restorefail\r\n'
            'echo swap failed -- restoring backup >> "%LOG%"\r\n'
            'move /Y "%BAK%" "%DST%" >>"%LOG%" 2>&1\r\n'
            'start "" "%DST%"\r\n'
            'del "%~f0"\r\n'
            'exit\r\n'
            # Never got the lock -- DST is still the untouched old exe; relaunch it.
            ':giveup\r\n'
            'echo gave up: exe stayed locked >> "%LOG%"\r\n'
            'start "" "%DST%"\r\n'
            'del "%~f0"\r\n'
        )
    # DETACHED_PROCESS so the helper survives us exiting a moment later. The
    # caller must NOT kill its own process tree, or this child dies with it.
    subprocess.Popen(
        ["cmd", "/c", script],
        creationflags=0x00000008 | 0x00000200,  # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP
        close_fds=True,
    )
