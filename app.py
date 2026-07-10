import json
import logging
import os
import queue
import sys
import threading
import webbrowser

from flask import Flask, Response, jsonify, render_template, request

import dm_bot

logging.getLogger("werkzeug").setLevel(logging.ERROR)

if getattr(sys, "frozen", False):
    # PyInstaller extracts bundled data files (e.g. templates/) to sys._MEIPASS at runtime.
    template_folder = os.path.join(sys._MEIPASS, "templates")
    app = Flask(__name__, template_folder=template_folder)
else:
    app = Flask(__name__)

# Only one bot run at a time.
run_lock = threading.Lock()
run_in_progress = False
event_queue = queue.Queue()
stop_event = threading.Event()


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/worksheets")
def list_worksheets():
    spreadsheet = dm_bot.get_spreadsheet()
    titles = [ws.title for ws in spreadsheet.worksheets()]
    return jsonify(titles)


PAGE_SIZE = 200


@app.route("/api/worksheet/<name>/users")
def worksheet_users(name):
    page = request.args.get("page", default=1, type=int)
    spreadsheet = dm_bot.get_spreadsheet()
    worksheet = spreadsheet.worksheet(name)
    users, total, sent = dm_bot.get_worksheet_users(worksheet, page, PAGE_SIZE)
    return jsonify({
        "users": users,
        "total": total,
        "sent": sent,
        "page": page,
        "page_size": PAGE_SIZE,
        "total_pages": max(1, -(-total // PAGE_SIZE)),
    })


@app.route("/api/start", methods=["POST"])
def start():
    global run_in_progress

    data = request.get_json()
    message = (data.get("message") or "").strip()
    worksheet_name = data.get("worksheet")

    if not message or not worksheet_name:
        return jsonify({"error": "message and worksheet are required"}), 400

    if not run_lock.acquire(blocking=False):
        return jsonify({"error": "A run is already in progress"}), 409

    run_in_progress = True
    stop_event.clear()

    def on_status(username, status):
        event_queue.put({"username": username, "status": status})

    def log(text):
        event_queue.put({"log": text})

    def worker():
        global run_in_progress
        try:
            spreadsheet = dm_bot.get_spreadsheet()
            worksheet = spreadsheet.worksheet(worksheet_name)
            dm_bot.run(message, worksheet, log=log, on_status=on_status, stop_event=stop_event)
        except Exception as e:
            event_queue.put({"log": f"Error: {e}"})
        finally:
            event_queue.put({"done": True})
            run_in_progress = False
            run_lock.release()

    threading.Thread(target=worker, daemon=True).start()
    return jsonify({"started": True})


@app.route("/api/stop", methods=["POST"])
def stop():
    if not run_in_progress:
        return jsonify({"error": "No run in progress"}), 409
    stop_event.set()
    return jsonify({"stopping": True})


@app.route("/api/events")
def events():
    def stream():
        while True:
            event = event_queue.get()
            yield f"data: {json.dumps(event)}\n\n"

    return Response(stream(), mimetype="text/event-stream")


if __name__ == "__main__":
    # Port 5000 collides with macOS's AirPlay Receiver service, so use 5050 instead.
    webbrowser.open("http://127.0.0.1:5050")
    app.run(debug=True, use_reloader=False, port=5050)
