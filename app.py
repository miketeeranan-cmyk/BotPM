import functools
import json
import logging
import os
import queue
import signal
import socket
import subprocess
import sys
import threading
import time
import traceback
import webbrowser

import gspread
from flask import Flask, Response, jsonify, render_template, request

import dm_bot
import paths
import preflight
import team_bot
import updater
from version import VERSION

logging.getLogger("werkzeug").setLevel(logging.ERROR)

APP_TITLE = "Team Sheet"

# The exe is the desktop app; running from source stays the browser-based dev
# loop it has always been.
DESKTOP_MODE = paths.is_frozen()

# The two dialogs that have to be drawn by the OS rather than by the page --
# one fires before any window exists, the other while it's being torn down.
# Neither can reach i18n.js, and asking the webview for the current locale from
# its own closing handler risks deadlocking the GUI thread, so they say both.
NATIVE_ALREADY_RUNNING = "Team Sheet is already running.\n\nTeam Sheet 已在运行。"
NATIVE_WEBVIEW2_MISSING = (
    "Team Sheet needs the Microsoft Edge WebView2 runtime.\n"
    "Opening the download page -- install it, then start Team Sheet again.\n\n"
    "Team Sheet 需要 Microsoft Edge WebView2 运行时。\n"
    "正在打开下载页面 -- 安装后请重新启动 Team Sheet。"
)
NATIVE_QUIT_CONFIRM = "Quit Team Sheet?\n\n退出 Team Sheet？"
NATIVE_RUN_IN_PROGRESS = (
    "A run is still in progress. Click Stop first, then close.\n\n"
    "运行仍在进行中。请先点击“停止”，再关闭。"
)

if getattr(sys, "frozen", False):
    # PyInstaller extracts bundled data files (e.g. templates/) to sys._MEIPASS at runtime.
    template_folder = os.path.join(sys._MEIPASS, "templates")
    app = Flask(__name__, template_folder=template_folder)
else:
    app = Flask(__name__)

# Only one bot run at a time -- shared across the Send and Scan flows, since
# they'd otherwise fight over the same persistent browser profile.
run_lock = threading.Lock()
run_in_progress = False
stop_event = threading.Event()

# Replaced with a fresh Queue at the start of every run (see team_send/team_scan)
# so a lingering SSE connection from a finished run can't steal events meant for
# the next one -- queue.Queue.get() hands each item to whichever thread is
# waiting on that same object, and old connections never notice the client is
# gone since they're blocked on get(), not on writing to the response.
team_event_queue = queue.Queue()

# Same one-at-a-time + fresh-Queue-per-run shape as the Send/Scan flow above,
# for the same reasons.
update_lock = threading.Lock()
update_event_queue = queue.Queue()

DASHBOARD_REFRESH_INTERVAL = 60  # seconds between full refresh passes
DASHBOARD_REFRESH_TEAM_DELAY = 3  # seconds between each team within a pass, to stay under Sheets' per-minute quota
DASHBOARD_REFRESH_QUOTA_COOLDOWN = 90  # seconds to pause the whole loop after a 429, so it stops adding to the read pressure

TAB_CLOSING_GRACE = 5  # seconds to wait for a ping after a tab reports it's closing, before treating the app as unattended

# Monotonic time of the most recent ping from any open tab. Closing the last tab
# quits the app, but the browser event a tab can report ("I'm being torn down")
# also fires for reloads and for Send <-> Scan navigation, and it can't see
# whether another tab is still open. So a closing report only *schedules* the
# quit: every tab pings while it's open, and a ping arriving after the report
# (a reload that finished loading, or a second tab nobody closed) cancels it.
last_tab_ping = 0.0


def _is_quota_error(exc):
    # Sheets' per-minute read limit surfaces as a gspread APIError carrying a 429
    # -- match on the response status or the error's own code, falling back to the
    # "[429]" its string form includes, so a slightly different gspread version still
    # trips it. str(exc) is guarded because a malformed error object can raise there.
    response = getattr(exc, "response", None)
    if response is not None and getattr(response, "status_code", None) == 429:
        return True
    if getattr(exc, "code", None) == 429:
        return True
    try:
        return "[429]" in str(exc)
    except Exception:
        return False


def retry_on_sheets_quota(fn):
    # The read endpoints below are idempotent, so a transient 429 (the dashboard
    # refresh loop, a background poll and a manual action all reading at once) is
    # safe to just wait out and retry. Total wait is bounded (1+2+4s) before we give
    # up and return a calm 429 the UI can show instead of a red 500.
    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        delay = 1.0
        for attempt in range(4):
            try:
                return fn(*args, **kwargs)
            except gspread.exceptions.APIError as e:
                if not _is_quota_error(e):
                    raise
                if attempt == 3:
                    return jsonify({"error": "Google Sheets is rate-limiting reads right now (quota). It'll refresh on its own in a moment."}), 429
                time.sleep(delay)
                delay *= 2
    return wrapper


def _dashboard_refresh_loop():
    # Keeps every team's dashboard numbers correct on their own, with no button
    # click and independent of whether a Send/Scan is running -- runs forever in
    # a daemon thread started at app launch. Refreshing all teams back-to-back
    # with no delay is what produced a 429 quota error earlier, hence the
    # per-team delay; each team's failure is caught and logged separately so one
    # bad team (or a transient API error) never kills the loop.
    while True:
        time.sleep(DASHBOARD_REFRESH_INTERVAL)
        try:
            spreadsheet = team_bot.get_team_spreadsheet()
            for t in team_bot.list_teams(spreadsheet):
                try:
                    worksheet = spreadsheet.worksheet(t["roster_title"])
                    data_worksheet = spreadsheet.worksheet(t["data_title"])
                    team_bot.update_dashboard_counts(worksheet, data_worksheet)
                except Exception as e:
                    logging.error(f"Dashboard auto-refresh failed for {t['id']}: {e}")
                    # Once the per-minute read quota is hit, continuing to read the
                    # remaining teams only deepens it (and starves the user-facing
                    # endpoints of quota). Pause the whole pass to let the window reset.
                    if _is_quota_error(e):
                        time.sleep(DASHBOARD_REFRESH_QUOTA_COOLDOWN)
                        break
                time.sleep(DASHBOARD_REFRESH_TEAM_DELAY)
        except Exception as e:
            logging.error(f"Dashboard auto-refresh pass failed: {e}")
            if _is_quota_error(e):
                time.sleep(DASHBOARD_REFRESH_QUOTA_COOLDOWN)


@app.route("/")
def send_page():
    return render_template("send.html", desktop=DESKTOP_MODE)


@app.route("/scan")
def scan_page():
    return render_template("scan.html", desktop=DESKTOP_MODE)


@app.route("/launch")
def launch_page():
    # The desktop window's first URL: checks for an update before handing over
    # to the app. In browser mode nothing can replace a running exe, so /launch
    # is only ever reached in the desktop app.
    return render_template("launch.html", desktop=DESKTOP_MODE, version=VERSION)


@app.route("/api/update/check")
def update_check():
    return jsonify(updater.check())


@app.route("/api/update/start", methods=["POST"])
def update_start():
    global update_event_queue

    if not update_lock.acquire(blocking=False):
        return jsonify({"error": "An update is already running"}), 409

    url = (request.get_json() or {}).get("url")
    if not url:
        update_lock.release()
        return jsonify({"error": "url is required"}), 400

    run_queue = queue.Queue()
    update_event_queue = run_queue

    def worker():
        try:
            new_exe = updater.download(url, lambda done, total: run_queue.put({"done": done, "total": total}))
            run_queue.put({"applying": True})
            updater.apply_and_restart(new_exe)
            # The helper is waiting for this process (and its exe file lock) to
            # go away before it can swap the file, so quitting *is* the last step
            # of the update. kill_tree=False so we don't take that helper down
            # with us.
            threading.Timer(0.5, lambda: _shutdown(kill_tree=False)).start()
        except Exception as e:
            logging.error(f"Update failed: {e}")
            run_queue.put({"error": str(e)})
        finally:
            update_lock.release()

    threading.Thread(target=worker, daemon=True).start()
    return jsonify({"started": True})


@app.route("/api/update/events")
def update_events():
    # Snapshots the current Queue for the same reason team_events does -- see
    # the comment on team_event_queue.
    run_queue = update_event_queue

    def stream():
        while True:
            yield f"data: {json.dumps(run_queue.get())}\n\n"

    return Response(stream(), mimetype="text/event-stream")


@app.route("/api/setup/status")
def setup_status():
    # Two things a PC can be missing that the app can't work around: the Google
    # key (deliberately not shipped in the exe) and Chrome (which the bot drives
    # via channel="chrome"). Surfaced as a page rather than a raw driver error.
    return jsonify({
        "credentials": paths.has_credentials(),
        "credentials_dir": os.path.dirname(paths.credentials_path()),
        "chrome": preflight.find_chrome() is not None,
        "chrome_url": preflight.CHROME_DOWNLOAD_URL,
    })


@app.route("/api/stop", methods=["POST"])
def stop():
    if not run_in_progress:
        return jsonify({"error": "No run in progress"}), 409
    stop_event.set()
    return jsonify({"stopping": True})


def _shutdown(kill_tree=True):
    # Kills this process and (by default) its Chromium children in one go, so
    # the automation browser never outlives the app. Windows has no process
    # groups and no killpg, so taskkill's /T (kill the whole tree) is the direct
    # analog.
    #
    # The update path passes kill_tree=False on purpose: it has just spawned the
    # detached helper that swaps the exe and relaunches, and that helper is a
    # child of this process -- /T would kill it too, leaving the app closed and
    # never reopened. There's no bot browser open during an update (it runs on
    # the launch splash, before any Send/Scan), so nothing needs the tree kill.
    if os.name == "nt":
        args = ["taskkill", "/F"]
        if kill_tree:
            args.append("/T")
        args += ["/PID", str(os.getpid())]
        subprocess.run(args, capture_output=True)
    else:
        os.killpg(os.getpgrp(), signal.SIGTERM)


@app.route("/api/quit", methods=["POST"])
def quit_app():
    # run_in_progress is the authoritative source (not the client's own busy
    # state) since Send and Scan can be open in separate tabs -- one tab has no
    # way to know a run is active in the other.
    if run_in_progress:
        return jsonify({"error": "A run is still in progress. Click Stop first, then Quit."}), 409
    # Delay so this request can return a response before the process is killed.
    threading.Timer(0.5, _shutdown).start()
    return jsonify({"quitting": True})


@app.route("/api/tab/ping", methods=["POST"])
def tab_ping():
    global last_tab_ping
    last_tab_ping = time.monotonic()
    return jsonify({"ok": True})


@app.route("/api/tab/closing", methods=["POST"])
def tab_closing():
    reported_at = time.monotonic()

    def quit_if_unattended():
        if last_tab_ping > reported_at:
            return  # a reload finished, or another tab is still open
        if run_in_progress:
            # Closing the tab shouldn't abandon a send half-delivered. The run
            # keeps going; reopen the URL to watch it, then Quit when it's done.
            logging.warning("Last tab closed while a run is in progress -- staying up.")
            return
        _shutdown()

    threading.Timer(TAB_CLOSING_GRACE, quit_if_unattended).start()
    return jsonify({"closing": True})


@app.route("/api/browser/close", methods=["POST"])
def browser_close():
    # Same cross-tab safety as /api/quit -- Send and Scan can be open in
    # separate tabs, so a tab's own idle button state can't be trusted alone.
    if run_in_progress:
        return jsonify({"error": "A run is in progress -- can't close the browser until it finishes."}), 409
    team_bot.close_browser()
    return jsonify({"closed": True})


@app.route("/api/session/login", methods=["POST"])
def session_login():
    # Opens the bot's own Chrome so the user can log into Stripchat by hand. The
    # persistent profile keeps the session for later runs; closing the browser
    # (via /api/browser/close) is what saves it.
    if run_in_progress:
        return jsonify({"error": "A run is in progress -- finish it before logging in."}), 409
    dm_bot.open_for_login()
    return jsonify({"opened": True})


@app.route("/api/session/import", methods=["POST"])
def session_import():
    # Best-effort shortcut: copy Stripchat cookies from the main Chrome. Usually
    # finds nothing on Chrome 127+ (app-bound encryption), so the response tells
    # the user to fall back to Log in.
    if run_in_progress:
        return jsonify({"error": "A run is in progress -- finish it before importing."}), 409
    import refresh_session
    # The import opens the same persistent profile, so the bot browser must not
    # be holding its lock.
    team_bot.close_browser()
    result = refresh_session.import_from_chrome()
    return jsonify(result)


@app.route("/api/teams")
@retry_on_sheets_quota
def teams():
    spreadsheet = team_bot.get_team_spreadsheet()
    return jsonify(team_bot.list_teams(spreadsheet))


@app.route("/api/team/<team_id>/ladies")
@retry_on_sheets_quota
def team_ladies(team_id):
    spreadsheet = team_bot.get_team_spreadsheet()
    worksheet, _ = team_bot.get_team_worksheets(spreadsheet, team_id)
    return jsonify(team_bot.get_lady_names(worksheet))


@app.route("/api/team/<team_id>/lady/<lady_name>/users")
@retry_on_sheets_quota
def team_lady_users(team_id, lady_name):
    spreadsheet = team_bot.get_team_spreadsheet()
    worksheet, _ = team_bot.get_team_worksheets(spreadsheet, team_id)
    rows, stats = team_bot.get_team_sheet_users(worksheet, lady_name)
    return jsonify({"rows": rows, "stats": stats})


@app.route("/api/team/<team_id>/dashboard/refresh", methods=["POST"])
@retry_on_sheets_quota
def team_dashboard_refresh(team_id):
    # On-demand version of the same recompute that already runs at the end of
    # every Send/Scan -- lets manual sheet edits (e.g. deleting a row) be
    # reflected without needing to trigger a real run just to refresh the count.
    spreadsheet = team_bot.get_team_spreadsheet()
    worksheet, data_worksheet = team_bot.get_team_worksheets(spreadsheet, team_id)
    team_bot.update_dashboard_counts(worksheet, data_worksheet)
    return jsonify({"refreshed": True})


@app.route("/api/source/tabs")
@retry_on_sheets_quota
def source_tabs():
    spreadsheet = team_bot.get_source_spreadsheet()
    return jsonify(team_bot.list_source_tabs(spreadsheet))


@app.route("/api/source/<tab_name>/candidates")
@retry_on_sheets_quota
def source_candidates(tab_name):
    min_level = request.args.get("min_level", type=int)
    spreadsheet = team_bot.get_source_spreadsheet()
    worksheet = spreadsheet.worksheet(tab_name)
    candidates, total = team_bot.get_source_candidates(worksheet, min_level=min_level)
    return jsonify({"candidates": candidates, "total": total, "limit": team_bot.SOURCE_CANDIDATES_LIMIT})


@app.route("/api/team/<team_id>/lady/<lady_name>/import", methods=["POST"])
def team_lady_import(team_id, lady_name):
    data = request.get_json()
    candidates = data.get("candidates") or []
    if not candidates:
        return jsonify({"error": "candidates is required"}), 400

    spreadsheet = team_bot.get_team_spreadsheet()
    worksheet, _ = team_bot.get_team_worksheets(spreadsheet, team_id)
    result = team_bot.add_candidates_to_roster(worksheet, lady_name, candidates)

    # Doesn't mark the tracker sheet red yet -- only remembers where each
    # candidate came from, so a later successful SEND (not this import) can
    # mark it (see team_bot.process_team_send_username). This way, deselecting
    # a candidate before ever sending to them never touches the tracker sheet.
    source_tab = data.get("source_tab")
    if source_tab:
        team_bot.record_pending_import_sources({
            c["name"]: {"source_tab": source_tab, "row": c["row"]}
            for c in candidates if c.get("name") and c.get("row")
        })

    return jsonify(result)


@app.route("/api/team/<team_id>/lady/<lady_name>/roster/remove", methods=["POST"])
def team_lady_roster_remove(team_id, lady_name):
    # Removal now shifts rows up to close the gap (see remove_candidates_from_roster),
    # so it can't safely run alongside a Send/Scan -- that run captured row
    # numbers up front and writes directly to them throughout; a concurrent
    # shift would move those numbers out from under it mid-run.
    if run_in_progress:
        return jsonify({"error": "A run is in progress -- can't remove roster rows until it finishes."}), 409

    data = request.get_json()
    names = data.get("names") or []
    if not names:
        return jsonify({"error": "names is required"}), 400

    spreadsheet = team_bot.get_team_spreadsheet()
    worksheet, data_worksheet = team_bot.get_team_worksheets(spreadsheet, team_id)
    result = team_bot.remove_candidates_from_roster(worksheet, lady_name, names, data_worksheet=data_worksheet)
    # Drops any not-yet-sent pending source mapping for these names, so a
    # never-sent, later-deselected candidate doesn't leave a stale entry.
    team_bot.discard_pending_import_sources(names)
    return jsonify(result)


@app.route("/api/team/send", methods=["POST"])
def team_send():
    global run_in_progress, team_event_queue

    data = request.get_json()
    message = (data.get("message") or "").strip()
    team_id = data.get("team")
    lady_name = data.get("lady")
    dry_run = bool(data.get("dry_run"))
    target = data.get("target") or "new"
    date_filter = data.get("date_filter") or None

    if (not message and not dry_run) or not team_id or not lady_name:
        return jsonify({"error": "team, message, and lady are required"}), 400

    if not run_lock.acquire(blocking=False):
        return jsonify({"error": "A run is already in progress"}), 409

    run_in_progress = True
    stop_event.clear()
    run_queue = queue.Queue()
    team_event_queue = run_queue

    def on_status(username, status):
        run_queue.put({"username": username, "status": status})

    def on_tab_status(username, status):
        run_queue.put({"username": username, "tab_status": status})

    def log(text):
        run_queue.put({"log": text})

    def worker():
        global run_in_progress
        try:
            spreadsheet = team_bot.get_team_spreadsheet()
            worksheet, data_worksheet = team_bot.get_team_worksheets(spreadsheet, team_id)
            team_bot.run_team_send(message, worksheet, data_worksheet, lady_name, log=log, on_status=on_status, on_tab_status=on_tab_status, stop_event=stop_event, dry_run=dry_run, target=target, date_filter=date_filter)
        except Exception as e:
            run_queue.put({"log": f"Error: {e}"})
        finally:
            run_queue.put({"done": True})
            run_in_progress = False
            run_lock.release()

    threading.Thread(target=worker, daemon=True).start()
    return jsonify({"started": True})


@app.route("/api/team/scan", methods=["POST"])
def team_scan():
    global run_in_progress, team_event_queue

    data = request.get_json()
    team_id = data.get("team")
    lady_name = data.get("lady")
    dry_run = bool(data.get("dry_run"))
    date_filter = data.get("date_filter") or None

    if not team_id or not lady_name:
        return jsonify({"error": "team and lady are required"}), 400

    if not run_lock.acquire(blocking=False):
        return jsonify({"error": "A run is already in progress"}), 409

    run_in_progress = True
    stop_event.clear()
    run_queue = queue.Queue()
    team_event_queue = run_queue

    def on_status(username, status):
        run_queue.put({"username": username, "status": status})

    def log(text):
        run_queue.put({"log": text})

    def worker():
        global run_in_progress
        try:
            spreadsheet = team_bot.get_team_spreadsheet()
            worksheet, data_worksheet = team_bot.get_team_worksheets(spreadsheet, team_id)
            team_bot.run_team_scan(worksheet, data_worksheet, lady_name, log=log, on_status=on_status, stop_event=stop_event, dry_run=dry_run, date_filter=date_filter)
        except Exception as e:
            run_queue.put({"log": f"Error: {e}"})
        finally:
            run_queue.put({"done": True})
            run_in_progress = False
            run_lock.release()

    threading.Thread(target=worker, daemon=True).start()
    return jsonify({"started": True})


@app.route("/api/team/events")
def team_events():
    # Snapshot whichever Queue object is current when this connection opens --
    # if a later run replaces the global with a new Queue, this generator keeps
    # reading from the one it started with, so it never competes with a newer
    # run's connection for events (see team_event_queue's definition above).
    run_queue = team_event_queue

    def stream():
        while True:
            event = run_queue.get()
            yield f"data: {json.dumps(event)}\n\n"

    return Response(stream(), mimetype="text/event-stream")


def _free_port():
    # The desktop window is handed whatever URL we end up on, so there's nothing
    # to be gained by insisting on a fixed port -- and a PC where 5050 is taken
    # would otherwise just fail to start.
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def _serve(port):
    threading.Thread(
        target=lambda: app.run(port=port, use_reloader=False, threaded=True),
        daemon=True,
    ).start()
    # Loading the window before the server accepts would show a blank frame.
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", port), 0.2):
                return True
        except OSError:
            time.sleep(0.05)
    return False


def _on_closing(window):
    # run_in_progress rather than any client-side state: the window can be
    # showing Scan while a Send is running, and closing mid-run would abandon
    # messages half-delivered.
    if run_in_progress:
        preflight.show_message(APP_TITLE, NATIVE_RUN_IN_PROGRESS)
        return False
    return window.create_confirmation_dialog(APP_TITLE, NATIVE_QUIT_CONFIRM)


def _run_desktop():
    import webview

    instance = preflight.SingleInstance()
    if not instance.acquire():
        preflight.show_message(APP_TITLE, NATIVE_ALREADY_RUNNING)
        return

    # Checked before creating the window, because without WebView2 there is no
    # window to render the bad news in.
    if not preflight.has_webview2():
        preflight.show_message(APP_TITLE, NATIVE_WEBVIEW2_MISSING)
        preflight.open_url(preflight.WEBVIEW2_DOWNLOAD_URL)
        return

    port = _free_port()
    if not _serve(port):
        preflight.show_message(APP_TITLE, "Team Sheet failed to start.\n\nTeam Sheet 启动失败。")
        return

    window = webview.create_window(
        APP_TITLE, f"http://127.0.0.1:{port}/launch",
        width=1280, height=860, min_size=(1024, 700),
    )
    window.events.closing += lambda: _on_closing(window)
    webview.start()
    # Reached once the window is gone: the run's Chrome children are ours to
    # clean up, and they don't die with the window on their own.
    _shutdown()


def _run_browser():
    # Port 5000 collides with macOS's AirPlay Receiver service, so use 5050 instead.
    webbrowser.open("http://127.0.0.1:5050")
    app.run(debug=True, use_reloader=False, port=5050)


def _selftest():
    """Proves a built exe can actually start, without opening a window.

    Exists because v1.0.0 shipped an exe that died on `import team_bot` --
    playwright_stealth reads its .js files at import time and PyInstaller hadn't
    bundled them. CI only checked the file existed, so a green build shipped a
    crash. team_bot/updater are imported at the top of this module, so reaching
    this function at all has already run that same import chain -- the rest just
    confirms the data files those imports rely on are actually present.
    """
    lines = [f"version: {VERSION}", f"frozen: {paths.is_frozen()}", f"data_dir: {paths.data_dir()}"]
    code = 0
    try:
        import playwright_stealth
        stealth_js = os.path.join(os.path.dirname(playwright_stealth.__file__), "js", "utils.js")
        assert os.path.exists(stealth_js), f"playwright_stealth js missing: {stealth_js}"
        lines.append("playwright_stealth js: found")

        assert os.path.isdir(app.template_folder), f"templates missing: {app.template_folder}"
        lines.append(f"templates: {app.template_folder}")

        lines.append(f"chrome: {'found' if preflight.find_chrome() else 'not found'}")
        lines.append("RESULT: OK")
    except Exception:
        lines.append("RESULT: FAIL")
        lines.append(traceback.format_exc())
        code = 1

    report = "\n".join(lines)
    # A windowed (console=False) exe has no stdout on Windows -- sys.stdout is
    # None and print() would raise -- so the file is the real channel CI reads.
    try:
        with open(paths.data_file("selftest.log"), "w", encoding="utf-8") as f:
            f.write(report)
    except Exception:
        pass
    try:
        print(report)
    except Exception:
        pass
    return code


def _main():
    threading.Thread(target=_dashboard_refresh_loop, daemon=True).start()
    if DESKTOP_MODE:
        _run_desktop()
    else:
        _run_browser()


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        sys.exit(_selftest())
    try:
        _main()
    except Exception:
        # A windowed exe has no console, so an unhandled error would otherwise
        # vanish into an uncopyable dialog. Write it down and point the user at
        # the file, so the next failure is one message rather than a screenshot.
        report = traceback.format_exc()
        try:
            log_path = paths.data_file("crash.log")
            with open(log_path, "w", encoding="utf-8") as f:
                f.write(report)
        except Exception:
            log_path = "(could not write crash.log)"
        preflight.show_message(
            APP_TITLE,
            f"Team Sheet hit an error and can't start.\n{log_path}\n\n"
            f"Team Sheet 启动时出错，无法启动。\n{log_path}\n\n{report}",
        )
        raise
