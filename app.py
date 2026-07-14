import functools
import json
import logging
import os
import queue
import signal
import sys
import threading
import time
import webbrowser

import gspread
from flask import Flask, Response, jsonify, render_template, request

import team_bot

logging.getLogger("werkzeug").setLevel(logging.ERROR)

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

DASHBOARD_REFRESH_INTERVAL = 60  # seconds between full refresh passes
DASHBOARD_REFRESH_TEAM_DELAY = 3  # seconds between each team within a pass, to stay under Sheets' per-minute quota
DASHBOARD_REFRESH_QUOTA_COOLDOWN = 90  # seconds to pause the whole loop after a 429, so it stops adding to the read pressure


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
    return render_template("send.html")


@app.route("/scan")
def scan_page():
    return render_template("scan.html")


@app.route("/api/stop", methods=["POST"])
def stop():
    if not run_in_progress:
        return jsonify({"error": "No run in progress"}), 409
    stop_event.set()
    return jsonify({"stopping": True})


@app.route("/api/quit", methods=["POST"])
def quit_app():
    # run_in_progress is the authoritative source (not the client's own busy
    # state) since Send and Scan can be open in separate tabs -- one tab has no
    # way to know a run is active in the other.
    if run_in_progress:
        return jsonify({"error": "A run is still in progress. Click Stop first, then Quit."}), 409
    # Delay so this request can return a response before the process (and its
    # Chromium child processes, sharing the same process group) are killed.
    threading.Timer(0.5, lambda: os.killpg(os.getpgrp(), signal.SIGTERM)).start()
    return jsonify({"quitting": True})


@app.route("/api/browser/close", methods=["POST"])
def browser_close():
    # Same cross-tab safety as /api/quit -- Send and Scan can be open in
    # separate tabs, so a tab's own idle button state can't be trusted alone.
    if run_in_progress:
        return jsonify({"error": "A run is in progress -- can't close the browser until it finishes."}), 409
    team_bot.close_browser()
    return jsonify({"closed": True})


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


if __name__ == "__main__":
    threading.Thread(target=_dashboard_refresh_loop, daemon=True).start()
    # Port 5000 collides with macOS's AirPlay Receiver service, so use 5050 instead.
    webbrowser.open("http://127.0.0.1:5050")
    app.run(debug=True, use_reloader=False, port=5050)
