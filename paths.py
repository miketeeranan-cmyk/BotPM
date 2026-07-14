"""Where the app's writable files and credentials live.

Run from source, everything stays in the project directory -- the layout this
repo has always had, so the Mac dev loop is unchanged. Frozen into the Windows
exe there is no project directory to write to: a onefile bundle unpacks to a
temp dir that Windows deletes on exit, so anything written beside the code is
silently lost between runs. Writable state goes to %LOCALAPPDATA%\\TeamSheet
instead.
"""
import os
import sys

APP_NAME = "TeamSheet"
CREDENTIALS_NAME = "credentials.json"


def is_frozen():
    return getattr(sys, "frozen", False)


def data_dir():
    if not is_frozen():
        return os.path.dirname(os.path.abspath(__file__))
    if os.name == "nt":
        base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
    else:
        base = os.path.expanduser("~/Library/Application Support")
    directory = os.path.join(base, APP_NAME)
    os.makedirs(directory, exist_ok=True)
    return directory


def data_file(name):
    return os.path.join(data_dir(), name)


def _credentials_dirs():
    # Beside the exe first: that's where teammates are told to drop the file,
    # and it's the one location they can find without knowing about AppData.
    if is_frozen():
        return [os.path.dirname(os.path.abspath(sys.executable)), data_dir()]
    return [data_dir()]


def credentials_path():
    # Deliberately never bundled into the exe -- the GitHub release is public,
    # and a service account key inside it would be extractable by anyone who
    # downloaded it. The file is handed to teammates privately instead.
    for directory in _credentials_dirs():
        candidate = os.path.join(directory, CREDENTIALS_NAME)
        if os.path.exists(candidate):
            return candidate
    return os.path.join(_credentials_dirs()[0], CREDENTIALS_NAME)


def has_credentials():
    return os.path.exists(credentials_path())
