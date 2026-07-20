import asyncio
import json
import logging
import os
import random
import re
import threading
import time
from datetime import datetime

import gspread
from playwright_stealth import Stealth

import dm_bot
import paths


def _is_quota_error(exc):
    """True if `exc` is a Sheets 429 quota error (per-minute read/write limit).
    Mirrors app._is_quota_error -- matches the response status, the error's own
    code, or the "[429]" its string form carries, so a slightly different gspread
    version still trips it. str(exc) is guarded because a malformed error object
    can raise there."""
    response = getattr(exc, "response", None)
    if response is not None and getattr(response, "status_code", None) == 429:
        return True
    if getattr(exc, "code", None) == 429:
        return True
    try:
        return "[429]" in str(exc)
    except Exception:
        return False


def _retry_write(fn, *args, **kwargs):
    """Runs one gspread write call, retrying transient 429 quota errors with
    exponential backoff (1/2/4s) before giving up and re-raising. Safe because the
    writes it wraps (update_cells/format/batch_update) all set ABSOLUTE values --
    re-applying an already-applied write is idempotent, and there's no append
    anywhere a retry could double. These run inside Send/Scan worker threads, so
    this re-raises rather than returning an HTTP response; the per-row handler in
    process_team_send_username logs and moves on if retries exhaust. This is the
    write-side counterpart to app.retry_on_sheets_quota (which guards reads), and
    is what keeps a 429 landing right after a confirmed DM from marking a
    delivered message as an 'error'."""
    delay = 1.0
    for attempt in range(4):
        try:
            return fn(*args, **kwargs)
        except gspread.exceptions.APIError as e:
            if not _is_quota_error(e) or attempt == 3:
                raise
            time.sleep(delay)
            delay *= 2


def _ws_update_cells(worksheet, cells):
    return _retry_write(worksheet.update_cells, cells)


def _ws_format(worksheet, ranges, fmt):
    return _retry_write(worksheet.format, ranges, fmt)


def _ss_batch_update(spreadsheet, body):
    return _retry_write(spreadsheet.batch_update, body)


def close_browser():
    """Closes the persistent automation browser, if one is open -- used when
    logging out of a VJ, so switching to a different account starts the next
    Send/Scan with a completely fresh browser window rather than reusing
    whatever session is already open."""
    dm_bot.close_browser()


TEAM_SPREADSHEET_KEY = "16HjwYsar92mw0CyxmDwhRSgKmfuex5d9N6TN2_Riofo"
BLOCK_WIDTH = 10
COL_DATE, COL_UPDATE, COL_NAME, COL_LINK, COL_MESSAGE, COL_SENT, COL_UNSEEN, COL_SEEN, COL_REPLIED, COL_CHAT = range(10)

DATA_HEADER_ROW = 16  # row 16 = table headers (Date/Update/Name/Link/Lady); data starts row 17
DATA_STATUSES = ("sent", "unseen", "seen", "replied", "chat")  # left-to-right table order in the sheet

# A roster tab's title starts with "TEAM<N>"; its paired data tab starts with
# "TEAM<N>DATA" (same digits) -- e.g. "TEAM1/第一组" pairs with
# "TEAM1DATA/第一组数据". Matched by prefix, not exact title, so translating or
# rewording anything after that prefix never breaks team discovery.
TEAM_TAB_PATTERN = re.compile(r"^TEAM(\d+)(DATA)?")


def list_teams(spreadsheet):
    """Discovers every team by pairing worksheets whose titles match
    TEAM_TAB_PATTERN. Tabs that don't match at all (like "RAW DATA") are
    ignored; a roster or data tab with no matching counterpart is skipped.
    Returns [{'id': 'TEAM1', 'label': <roster tab's full title>,
    'roster_title': ..., 'data_title': ...}, ...] sorted by team number."""
    rosters, datas = {}, {}
    for ws in spreadsheet.worksheets():
        m = TEAM_TAB_PATTERN.match(ws.title)
        if not m:
            continue
        num, is_data = m.group(1), m.group(2)
        (datas if is_data else rosters)[num] = ws.title

    teams = []
    for num in sorted(rosters, key=int):
        if num in datas:
            teams.append({
                "id": f"TEAM{num}",
                "label": rosters[num],
                "roster_title": rosters[num],
                "data_title": datas[num],
            })
    return teams


# Every call site (Send, Scan, the Recipients-table poll, the 60s dashboard
# loop) re-resolves team_id -> (roster_worksheet, data_worksheet) from
# scratch, and each of spreadsheet.worksheets() and spreadsheet.worksheet()
# independently calls the Sheets API's fetch_sheet_metadata under the hood --
# so an unresolved lookup costs 3 read-quota units before any actual roster
# data is touched. Cached here since team/tab structure (titles, which tabs
# exist) only changes via a deliberate rename/add, never as a side effect of
# normal sending/scanning/importing -- a few minutes of staleness on that is a
# non-issue, and get_team_sheet_users' actual data read is never cached.
_SPREADSHEET_CACHE_TTL = 300  # seconds
_team_worksheets_cache = {}  # team_id -> {"pair": (worksheet, data_worksheet), "fetched_at": ...}


def get_team_worksheets(spreadsheet, team_id):
    """Resolves a team_id (e.g. 'TEAM1') to its (roster_worksheet,
    data_worksheet) pair. Raises ValueError if the team isn't found."""
    now = time.monotonic()
    cached = _team_worksheets_cache.get(team_id)
    if cached and now - cached["fetched_at"] <= _SPREADSHEET_CACHE_TTL:
        return cached["pair"]
    for team in list_teams(spreadsheet):
        if team["id"] == team_id:
            pair = (spreadsheet.worksheet(team["roster_title"]), spreadsheet.worksheet(team["data_title"]))
            _team_worksheets_cache[team_id] = {"pair": pair, "fetched_at": now}
            return pair
    raise ValueError(f"Team '{team_id}' not found")


def _header_key(raw):
    """Normalizes a header cell to a lookup key -- headers are bilingual, e.g.
    "DATE/ 日期" or "UPDATE/更新", so this takes just the part before the
    slash. A plain "Date" (no slash) still works the same way."""
    return raw.strip().split("/")[0].strip().lower()


def _data_table_layout(data_worksheet):
    """Reads DATA_HEADER_ROW and finds each status table's columns by scanning for
    contiguous non-blank headers starting at a cell reading "Date" -- same
    discover-from-the-sheet approach as get_lady_names()/_lady_block_start_col(),
    so an Update column added to a table is picked up automatically without
    needing to know its exact position in advance."""
    header_row = data_worksheet.row_values(DATA_HEADER_ROW)
    tables = []
    col = 1
    while col <= len(header_row):
        label = _header_key(header_row[col - 1])
        if label == "date":
            fields = {}
            c = col
            while c <= len(header_row) and header_row[c - 1].strip():
                fields[_header_key(header_row[c - 1])] = c
                c += 1
            tables.append(fields)
            col = c
        else:
            col += 1
    if len(tables) != len(DATA_STATUSES):
        raise ValueError(
            f"Expected {len(DATA_STATUSES)} DATA-tab tables, found {len(tables)} in row "
            f"{DATA_HEADER_ROW} of '{data_worksheet.title}' -- check the header row."
        )
    return dict(zip(DATA_STATUSES, tables))


# Rows 1-14 of a DATA tab hold a hand-built "BOT REPORT DASHBOARD" summary, sitting
# above the DATA_HEADER_ROW tables handled above. It used to be live COUNTA formulas
# reading the roster tab, but those were observed getting stuck on stale results
# (Google Sheets failing to recalculate them even when force-rewritten via the API,
# only recovering after a manual drag-edit inside the referenced range). These
# functions instead compute the same counts in Python (get_team_sheet_users already
# does, per lady) and write them in as plain numbers, so the dashboard no longer
# depends on Sheets' own recalculation at all.
DASHBOARD_TOTAL_ROW = 4
DASHBOARD_VJ_HEADER_ROW = 7
DASHBOARD_VJ_STATUS_ROWS = dict(zip(DATA_STATUSES, range(8, 8 + len(DATA_STATUSES))))
DASHBOARD_VJ_BLOCK_WIDTH = 2


def _dashboard_total_columns(data_worksheet):
    """Maps each status to its TOTAL COUNTS value column in DASHBOARD_TOTAL_ROW,
    discovered from that row's own labels rather than a hardcoded position."""
    row = data_worksheet.row_values(DASHBOARD_TOTAL_ROW)
    cols = {}
    for i, cell in enumerate(row):
        key = _header_key(cell)
        if key in DATA_STATUSES:
            cols[key] = i + 2  # value sits one column right of its label
    return cols


def _dashboard_lady_columns(data_worksheet):
    """Maps each lady name in the PER VJ BREAKDOWN header row to her label column
    -- same discover-from-the-sheet approach as get_lady_names(), but for this
    section's 2-column-per-lady layout instead of the roster's BLOCK_WIDTH=10."""
    row = data_worksheet.row_values(DASHBOARD_VJ_HEADER_ROW)
    cols = {}
    for col_idx in range(0, len(row), DASHBOARD_VJ_BLOCK_WIDTH):
        name = row[col_idx].strip() if col_idx < len(row) else ""
        if name:
            cols[name] = col_idx + 1
    return cols


def update_dashboard_counts(roster_worksheet, data_worksheet):
    """Recomputes SENT/UNSEEN/SEEN/REPLIED/CHAT counts from the roster (same counts
    get_team_sheet_users always returns) and writes them as plain numbers into the
    DATA tab's dashboard block, overwriting whatever was there. No-ops if the sheet
    has no such block."""
    total_cols = _dashboard_total_columns(data_worksheet)
    lady_cols = _dashboard_lady_columns(data_worksheet)
    if not total_cols or not lady_cols:
        return

    totals = {status: 0 for status in DATA_STATUSES}
    cells = []
    for lady_name in get_lady_names(roster_worksheet):
        label_col = lady_cols.get(lady_name.strip())
        if label_col is None:
            continue
        _, stats = get_team_sheet_users(roster_worksheet, lady_name)
        for status in DATA_STATUSES:
            count = stats[status]
            totals[status] += count
            cells.append(gspread.Cell(DASHBOARD_VJ_STATUS_ROWS[status], label_col + 1, count))

    for status, col in total_cols.items():
        cells.append(gspread.Cell(DASHBOARD_TOTAL_ROW, col, totals[status]))

    if cells:
        _ws_update_cells(data_worksheet, cells)


# open_by_key() itself calls fetch_sheet_metadata() just to construct the
# Spreadsheet object -- cached for the same reason as _team_worksheets_cache
# above (see that comment), reusing _SPREADSHEET_CACHE_TTL.
_team_spreadsheet_cache = {"obj": None, "fetched_at": 0}


def get_team_spreadsheet():
    now = time.monotonic()
    if _team_spreadsheet_cache["obj"] is None or now - _team_spreadsheet_cache["fetched_at"] > _SPREADSHEET_CACHE_TTL:
        client = gspread.service_account(filename=dm_bot.CREDENTIALS_FILE)
        _team_spreadsheet_cache["obj"] = client.open_by_key(TEAM_SPREADSHEET_KEY)
        _team_spreadsheet_cache["fetched_at"] = now
    return _team_spreadsheet_cache["obj"]


# Separate spreadsheet ("Stripchat Tracker") holding raw candidate lists, one
# tab per country/shift -- source for the Send page's "Import Recruits" flow.
SOURCE_SPREADSHEET_KEY = "1Ni70MtL5rpX_jD021XWF71dbKwKYZCJub_vSoXTBCBY"
SOURCE_CANDIDATES_LIMIT = 500  # some tabs run past 30k rows; never return more than this per fetch

SUMMARY_TAB_NAME = "Summary"  # archive destination in the tracker workbook (see archive_sent_to_summary)
# Tabs the daily archive never touches. It's the Summary tab itself (never
# archive into/out of the archive) plus any known non-candidate tabs. list_source_tabs
# is unfiltered, so if the workbook grows a tab that holds red text in column A
# but isn't a recruit list, add its title here so its rows are never moved/deleted.
ARCHIVE_SKIP_TABS = {SUMMARY_TAB_NAME}

_source_spreadsheet_cache = {"obj": None, "fetched_at": 0}


def get_source_spreadsheet():
    now = time.monotonic()
    if _source_spreadsheet_cache["obj"] is None or now - _source_spreadsheet_cache["fetched_at"] > _SPREADSHEET_CACHE_TTL:
        client = gspread.service_account(filename=dm_bot.CREDENTIALS_FILE)
        _source_spreadsheet_cache["obj"] = client.open_by_key(SOURCE_SPREADSHEET_KEY)
        _source_spreadsheet_cache["fetched_at"] = now
    return _source_spreadsheet_cache["obj"]


def list_source_tabs(spreadsheet):
    """All tab titles in sheet order, unfiltered -- the user picks which one to
    browse, so this doesn't try to guess which tabs are valid candidate lists."""
    return [ws.title for ws in spreadsheet.worksheets()]


def _username_from_link(link):
    """Extracts the real account handle from a profile link's last path segment
    -- more reliable than the sheet's own Username column, which can hold a
    shortened display nickname while the link still points at the real account
    (seen in practice: Username='Kobe', link='.../user/Kobe_DigBick'). Since
    the link is what navigation/sending actually uses, it should win whenever
    it's present and parseable."""
    if not link:
        return None
    path = link.rstrip("/").split("/")
    return path[-1] if path and path[-1] else None


RED_TEXT_COLOR = {"red": 1, "green": 0, "blue": 0}


def _is_red_color(color):
    """True if a Sheets textFormat.foregroundColor dict is (approximately) pure
    red -- Sheets omits zero-valued color components from the JSON, so a plain
    default/black cell comes back as {} here, not {red:0, green:0, blue:0}."""
    return color.get("red", 0) > 0.5 and color.get("green", 0) < 0.3 and color.get("blue", 0) < 0.3


def _rows_with_red_username(worksheet, rows):
    """Checks which of `rows` (1-based) already have a red-colored Username
    (column A) cell -- one metadata fetch spanning min(rows)..max(rows), not
    one range per row (500 candidate rows would mean 500 ranges in one URL,
    which Google's API rejects outright as malformed) and not the whole
    (possibly 30k-row) tab either. Used to detect candidates already marked as
    imported by mark_source_usernames_imported, from a prior import by any
    team/lady, not just the one currently browsing."""
    if not rows:
        return set()
    lo, hi = min(rows), max(rows)
    range_str = f"'{worksheet.title}'!A{lo}:A{hi}"
    meta = worksheet.spreadsheet.fetch_sheet_metadata(params={
        "ranges": [range_str],
        "includeGridData": "true",
        "fields": "sheets.data.rowData.values.effectiveFormat.textFormat.foregroundColor",
    })
    sheets = meta.get("sheets", [{}])
    data_list = sheets[0].get("data", []) if sheets else []
    row_data = data_list[0].get("rowData", []) if data_list else []
    wanted = set(rows)
    red_rows = set()
    for offset, cell_row in enumerate(row_data):
        row_num = lo + offset
        if row_num not in wanted:
            continue
        values = cell_row.get("values") or [{}]
        cell = values[0] if values else {}
        color = cell.get("effectiveFormat", {}).get("textFormat", {}).get("foregroundColor", {})
        if _is_red_color(color):
            red_rows.add(row_num)
    return red_rows


def mark_source_usernames_imported(worksheet, rows):
    """Colors the Username cell (column A) of each given row red in the tracker
    sheet -- a persistent, sheet-visible "already recruited" marker that any
    future browse of this tab (see get_source_candidates/_rows_with_red_username)
    picks back up, regardless of which team/lady does the importing. One-way:
    removing a candidate from a roster later doesn't un-mark it here."""
    if not rows:
        return
    ranges = [f"A{row}" for row in rows]
    _ws_format(worksheet, ranges, {"textFormat": {"foregroundColor": RED_TEXT_COLOR}})


# The tracker sheet's Username cell doesn't turn red at import time -- only
# once a message is actually SENT to that person (see process_team_send_username
# below), so deselecting/removing a just-imported candidate before ever sending
# to them leaves the tracker sheet untouched. The roster's own 10-column block
# has no spare column to carry "which tracker tab/row this came from" through
# to send time, so that's tracked here instead, in a small local file keyed by
# name (names are already treated as the unique account identifier elsewhere
# in this module, e.g. get_team_sheet_users' row entries).
PENDING_IMPORT_SOURCE_FILE = paths.data_file("pending_import_sources.json")
_pending_import_source_lock = threading.Lock()


def _load_pending_import_sources():
    if not os.path.exists(PENDING_IMPORT_SOURCE_FILE):
        return {}
    with open(PENDING_IMPORT_SOURCE_FILE) as f:
        return json.load(f)


def _save_pending_import_sources(data):
    with open(PENDING_IMPORT_SOURCE_FILE, "w") as f:
        json.dump(data, f)


def record_pending_import_sources(entries):
    """entries: {name: {"source_tab": ..., "row": ...}}. Called at import time
    (add_candidates_to_roster) to remember where each candidate came from, so
    a later successful send can mark that row red (see
    pop_pending_import_source / mark_source_usernames_imported)."""
    if not entries:
        return
    with _pending_import_source_lock:
        data = _load_pending_import_sources()
        data.update(entries)
        _save_pending_import_sources(data)


def pop_pending_import_source(name):
    """Removes and returns {"source_tab", "row"} for `name` if it's still
    pending (never sent, never discarded), else None. Popped rather than just
    read, since a candidate should only ever get marked red once, on their
    first successful send."""
    with _pending_import_source_lock:
        data = _load_pending_import_sources()
        entry = data.pop(name, None)
        if entry is not None:
            _save_pending_import_sources(data)
        return entry


def discard_pending_import_sources(names):
    """Called on roster removal (undoing an import) so a name that's checked,
    unchecked, and never sent doesn't leave a stale pending entry behind."""
    if not names:
        return
    with _pending_import_source_lock:
        data = _load_pending_import_sources()
        changed = False
        for name in names:
            if data.pop(name, None) is not None:
                changed = True
        if changed:
            _save_pending_import_sources(data)


def _shift_pending_sources_after_delete(source_tab, deleted_rows):
    """After the archive deletes `deleted_rows` (1-based) from `source_tab`, the
    rows below each deletion shift up -- so the stored row numbers of any
    NOT-yet-sent pending candidate on that tab (pending_import_sources.json,
    keyed name -> {source_tab, row}) go stale. Re-point each surviving pending
    entry: subtract the number of deleted rows above it. An already-sent (red)
    row shouldn't still be pending, but if one is, its mapping is now meaningless
    -- drop it -- so a future send can never mark the wrong (shifted) row red."""
    if not deleted_rows:
        return
    deleted = sorted(deleted_rows)
    deleted_set = set(deleted)
    with _pending_import_source_lock:
        data = _load_pending_import_sources()
        changed = False
        for name, entry in list(data.items()):
            if entry.get("source_tab") != source_tab:
                continue
            row = entry.get("row")
            if row in deleted_set:
                del data[name]
                changed = True
                continue
            above = sum(1 for d in deleted if d < row)
            if above:
                entry["row"] = row - above
                changed = True
        if changed:
            _save_pending_import_sources(data)


def _get_or_create_summary(spreadsheet):
    try:
        return spreadsheet.worksheet(SUMMARY_TAB_NAME)
    except gspread.exceptions.WorksheetNotFound:
        return spreadsheet.add_worksheet(title=SUMMARY_TAB_NAME, rows=1000, cols=26)


def archive_sent_to_summary():
    """Daily housekeeping (see app's noon scheduler): across every tracker tab,
    move each already-sent recruit -- a row whose Username cell (column A) is red,
    marked by mark_source_usernames_imported on a successful send -- into the
    Summary tab, then delete it from its source tab so the active lists stay
    short. The full row is copied, and the archived name stays red in Summary.

    Per-tab and resilient: one tab's failure is logged and skipped, never
    aborting the rest (mirrors the dashboard refresh loop). Returns
    {tab_title: rows_archived} for logging. Runs on its own thread, off the
    request path, so it just raises/logs rather than returning HTTP responses.

    Ordering within a tab matters: copy to Summary FIRST, delete from the source
    SECOND. If the delete fails after the copy, the worst case is a row that's in
    both places (still red in the source, so next run re-copies it) -- an
    idempotent double, not a lost record. The reverse order could drop a row
    entirely if the Summary write failed after the delete."""
    spreadsheet = get_source_spreadsheet()
    summary_ws = _get_or_create_summary(spreadsheet)
    results = {}
    for worksheet in spreadsheet.worksheets():
        title = worksheet.title
        if title in ARCHIVE_SKIP_TABS:
            continue
        try:
            values = worksheet.get_all_values()
            if len(values) < 2:
                continue  # header only (or empty) -- nothing to archive

            # Check every data row's column-A format in one metadata fetch. A sent
            # row is always red on column A, even when the Username cell is blank
            # (name came from the link), so we check all rows, not just non-blank ones.
            data_rows = list(range(2, len(values) + 1))
            red_rows = sorted(_rows_with_red_username(worksheet, data_rows))
            if not red_rows:
                continue

            # Copy first. Compute the destination range ONCE (Summary's current
            # height + 1) and write with update_cells at fixed absolute positions,
            # so a retry after a 429 re-writes the SAME cells instead of appending
            # again -- append_rows would double on retry.
            start_row = len(summary_ws.get_all_values()) + 1
            cells = []
            for offset, src_row in enumerate(red_rows):
                row_values = values[src_row - 1]  # get_all_values is 0-based
                for col_idx, val in enumerate(row_values):
                    if val == "":
                        continue  # destination rows are fresh/empty; blanks stay blank
                    cells.append(gspread.Cell(start_row + offset, col_idx + 1, val))
            if cells:
                _ws_update_cells(summary_ws, cells)

            # Delete second: whole rows (all columns), bottom-to-top in one batch,
            # reusing the shift-preserving helper across the tab's full width.
            _delete_rows_shifting_up(
                worksheet,
                [(row, 1, worksheet.col_count) for row in red_rows],
            )

            # The deletes shifted rows up -- fix any not-yet-sent pending mappings
            # for this tab so a later send still marks the right row.
            _shift_pending_sources_after_delete(title, red_rows)

            results[title] = len(red_rows)
            logging.info(f"Archived {len(red_rows)} sent row(s) from '{title}' to '{SUMMARY_TAB_NAME}'.")
        except Exception as e:
            logging.error(f"Archive failed for tab '{title}': {e}")
    return results


def get_source_candidates(worksheet, min_level=None, limit=SOURCE_CANDIDATES_LIMIT):
    """Reads a tracker tab shaped like 'Username | Level | Timestamp | | link'
    (row 1 header, data from row 2). Columns are fixed by position rather than
    matched by header text, since header wording varies across tabs (e.g. one
    tab's Username header is blank even though the column itself isn't).
    Tolerant of short/malformed rows -- skipped rather than raising. Returns
    (candidates, total_matches): candidates is capped at `limit`, but
    total_matches counts everyone matching min_level so the caller can tell the
    user their filter matched more than is shown. Each candidate also carries
    its sheet `row` (so a later import can mark it red) and `already_imported`
    (whether its Username cell is already red from a prior import -- see
    mark_source_usernames_imported)."""
    values = worksheet.get_all_values()
    candidates = []
    total = 0
    for row_num, row in enumerate(values[1:], start=2):
        sheet_name = row[0].strip() if len(row) > 0 else ""
        link = row[4].strip() if len(row) > 4 else ""
        name = _username_from_link(link) or sheet_name
        if not name:
            continue
        level_raw = row[1].strip() if len(row) > 1 else ""
        try:
            level = int(level_raw)
        except ValueError:
            level = None
        if min_level is not None and (level is None or level < min_level):
            continue
        total += 1
        if len(candidates) < limit:
            candidates.append({"name": name, "level": level, "link": link, "row": row_num})

    red_rows = _rows_with_red_username(worksheet, [c["row"] for c in candidates])
    for c in candidates:
        c["already_imported"] = c["row"] in red_rows

    return candidates, total


def get_lady_names(worksheet):
    """Reads row 1, returning the name found at the start of each BLOCK_WIDTH-col
    block. Dynamic rather than hardcoded so a renamed/added lady doesn't need a
    code change."""
    header_row = worksheet.row_values(1)
    names = []
    for col_idx in range(0, len(header_row), BLOCK_WIDTH):
        name = header_row[col_idx].strip() if col_idx < len(header_row) else ""
        if name:
            names.append(name)
    return names


def _lady_block_start_col(worksheet, lady_name):
    """1-based column where `lady_name`'s block begins, or None if not found
    (case-insensitive match against row 1)."""
    header_row = worksheet.row_values(1)
    target = lady_name.strip().lower()
    for col_idx in range(0, len(header_row), BLOCK_WIDTH):
        name = header_row[col_idx].strip() if col_idx < len(header_row) else ""
        if name and name.lower() == target:
            return col_idx + 1
    return None


def get_team_sheet_users(worksheet, lady_name):
    """One get_all_values() call, sliced to lady_name's 10-col block, data starting
    row 3. Returns (rows, stats):
      rows: [{row, date, update, name, link, message, sent, unseen, seen, replied, chat}, ...]
            (sent/unseen/seen/replied/chat are bools: cell non-empty)
      stats: {total, sent, chat, unseen, seen, replied} counts for the UI panel.
    Raises ValueError if lady_name isn't found in row 1."""
    block_start_col = _lady_block_start_col(worksheet, lady_name)
    if block_start_col is None:
        raise ValueError(f"Lady '{lady_name}' not found in {worksheet.title}")

    values = worksheet.get_all_values()
    idx = block_start_col - 1  # 0-based index into each row

    def cell(row, offset):
        i = idx + offset
        return row[i].strip() if len(row) > i and row[i] else ""

    rows = []
    stats = {"total": 0, "sent": 0, "chat": 0, "unseen": 0, "seen": 0, "replied": 0}
    for row_num, row in enumerate(values[2:], start=3):
        name = cell(row, COL_NAME)
        if not name:
            continue

        message = cell(row, COL_MESSAGE)
        # MESSAGE is an append-only "{date}: {msg}" log (one entry per send, see
        # _append_message) -- counting entries gives how many times we've actually
        # sent this person something, with no extra chat inspection needed. Counts
        # lines starting with a date prefix, not just any non-blank line, since the
        # sent message itself can contain embedded newlines (a multi-line textarea
        # message) that would otherwise inflate this past the real send count.
        send_count = len(re.findall(r"^\d{4}-\d{2}-\d{2}: ", message, re.MULTILINE)) if message else 0

        entry = {
            "row": row_num,
            "date": cell(row, COL_DATE),
            "update": cell(row, COL_UPDATE),
            "name": name,
            "link": cell(row, COL_LINK),
            "message": message,
            "sent": bool(cell(row, COL_SENT)),
            "unseen": bool(cell(row, COL_UNSEEN)),
            "seen": bool(cell(row, COL_SEEN)),
            "replied": bool(cell(row, COL_REPLIED)),
            "chat": bool(cell(row, COL_CHAT)),
            "send_count": send_count,
            "exhausted": send_count >= 2,  # sent 2+ times with no useful engagement -- stop offering resend
        }
        rows.append(entry)
        stats["total"] += 1
        for key in ("sent", "chat", "unseen", "seen", "replied"):
            if entry[key]:
                stats[key] += 1

    return rows, stats


def add_candidates_to_roster(worksheet, lady_name, candidates, import_date=None):
    """Appends {name, link} candidates after a lady's existing roster rows,
    skipping any name already present. Writes NAME+LINK+DATE -- DATE is
    stamped with import_date (today, unless given) so a never-contacted row
    can be filtered/sent by "date pulled"; mark_team_sent overwrites it with
    the send date the moment the row is actually sent, which is also the
    moment it stops being "new"-eligible, so the two meanings never collide.
    Message/status columns stay blank until that row gets sent normally.
    Always appends after the last existing row rather than backfilling gaps,
    since (unlike the DATA tab) roster rows are never cleared once written.
    Raises ValueError if lady_name isn't found in row 1."""
    block_start_col = _lady_block_start_col(worksheet, lady_name)
    if block_start_col is None:
        raise ValueError(f"Lady '{lady_name}' not found in {worksheet.title}")

    import_date = import_date or datetime.now().strftime("%Y-%m-%d")
    date_col = block_start_col + COL_DATE
    name_col = block_start_col + COL_NAME
    link_col = block_start_col + COL_LINK
    existing_values = worksheet.col_values(name_col)[2:]  # data starts row 3
    existing_names = {v.strip() for v in existing_values if v.strip()}

    cells = []
    added = 0
    skipped_duplicate = 0
    row = 3 + len(existing_values)
    for candidate in candidates:
        name = (candidate.get("name") or "").strip()
        if not name:
            continue
        if name in existing_names:
            skipped_duplicate += 1
            continue
        cells.append(gspread.Cell(row, date_col, import_date))
        cells.append(gspread.Cell(row, name_col, name))
        cells.append(gspread.Cell(row, link_col, candidate.get("link") or ""))
        existing_names.add(name)
        added += 1
        row += 1

    if cells:
        _ws_update_cells(worksheet, cells)
    return {"added": added, "skipped_duplicate": skipped_duplicate}


def _delete_rows_shifting_up(worksheet, removals):
    """removals: [(row, col_start, col_end), ...], 1-based inclusive. Deletes
    each row's cells within its column range and shifts everything below it
    (in that same column range only) up to fill the gap -- a structural
    deleteRange, not a value rewrite, so cell formatting (e.g. mark_team_dead's
    strikethrough) moves with the shift instead of being stranded at the old
    row. Scoped to col_start..col_end so this never disturbs a neighboring
    lady's block or another status table sharing the same row numbers.
    Processed bottom-to-top in one batch_update call so multiple removals in
    the same call never drift against each other's row numbers."""
    if not removals:
        return
    requests = [
        {
            "deleteRange": {
                "range": {
                    "sheetId": worksheet.id,
                    "startRowIndex": row - 1,
                    "endRowIndex": row,
                    "startColumnIndex": col_start - 1,
                    "endColumnIndex": col_end,
                },
                "shiftDimension": "ROWS",
            }
        }
        for row, col_start, col_end in sorted(removals, reverse=True)
    ]
    _ss_batch_update(worksheet.spreadsheet, {"requests": requests})


def remove_candidates_from_roster(worksheet, lady_name, names, data_worksheet=None):
    """Undoes an import: for each row whose NAME matches, within lady_name's
    block, deletes its 10 columns (DATE..CHAT) and shifts later rows up to
    close the gap immediately -- no permanent holes left behind. This also
    clears any activity (message history, sent/seen/etc) recorded against
    that row since the import, since it's a full undo, not just a name/link
    clear. If data_worksheet is given, also removes each name from every
    DATA-tab status table it's listed in (mirrors mark_team_dead) and
    recomputes the dashboard counts, so a removed candidate disappears from
    the dashboard/data sheet immediately too, not just the roster. Raises
    ValueError if lady_name isn't found in row 1."""
    block_start_col = _lady_block_start_col(worksheet, lady_name)
    if block_start_col is None:
        raise ValueError(f"Lady '{lady_name}' not found in {worksheet.title}")

    name_col = block_start_col + COL_NAME
    name_values = worksheet.col_values(name_col)[2:]  # data starts row 3
    wanted = {n.strip() for n in names if n and n.strip()}

    rows_to_remove = [3 + offset for offset, value in enumerate(name_values) if value.strip() in wanted]

    _delete_rows_shifting_up(
        worksheet,
        [(row, block_start_col, block_start_col + BLOCK_WIDTH - 1) for row in rows_to_remove],
    )

    if data_worksheet is not None:
        data_layout = _data_table_layout(data_worksheet)
        for name in wanted:
            for status in data_layout:
                row = _find_data_row(data_worksheet, data_layout, status, name)
                if row:
                    _remove_data_row(data_worksheet, data_layout, status, row)
        update_dashboard_counts(worksheet, data_worksheet)

    return {"removed": len(rows_to_remove)}


def _find_data_row(data_worksheet, layout, status, name):
    """Scans the target status table's NAME column from row 17 down (each status
    table lives in its own column range, so this can't use a sheet-wide find())."""
    cols = layout[status]
    name_values = data_worksheet.col_values(cols["name"])[DATA_HEADER_ROW:]
    for offset, value in enumerate(name_values):
        if value.strip() == name:
            return DATA_HEADER_ROW + 1 + offset
    return None


def _next_empty_data_row(data_worksheet, layout, status):
    """Returns the first blank row (not just the last), so rows freed up by
    _remove_data_row get reused instead of leaving permanent holes."""
    cols = layout[status]
    name_values = data_worksheet.col_values(cols["name"])[DATA_HEADER_ROW:]
    for offset, value in enumerate(name_values):
        if not value.strip():
            return DATA_HEADER_ROW + 1 + offset
    return DATA_HEADER_ROW + 1 + len(name_values)


def _remove_data_row(data_worksheet, layout, status, row):
    """Deletes a status table's row and shifts later rows in that same table
    up to close the gap immediately -- scoped to just this table's column
    range (via _delete_rows_shifting_up), so the other tables sitting
    side-by-side on the same row numbers are untouched."""
    cols = layout[status]
    col_start, col_end = min(cols.values()), max(cols.values())
    _delete_rows_shifting_up(data_worksheet, [(row, col_start, col_end)])


def sync_data_status(data_worksheet, layout, status, entry, lady_name, date_str, update_str=None):
    """Moves `entry`'s DATA-tab row into the `status` table, removing it from
    whichever other status table it was previously in -- so each table always
    reflects current status only, never stale duplicates. `date_str` fills that
    table's DATE column; `update_str`, if given and the table has an Update
    column, fills that too."""
    name = entry["name"]
    for other_status in layout:
        if other_status == status:
            continue
        existing_row = _find_data_row(data_worksheet, layout, other_status, name)
        if existing_row:
            _remove_data_row(data_worksheet, layout, other_status, existing_row)

    cols = layout[status]
    row = _find_data_row(data_worksheet, layout, status, name)
    if row is None:
        row = _next_empty_data_row(data_worksheet, layout, status)

    cells = [
        gspread.Cell(row, cols["date"], date_str),
        gspread.Cell(row, cols["name"], name),
        gspread.Cell(row, cols["link"], entry.get("link", "")),
        gspread.Cell(row, cols["lady"], lady_name),
    ]
    if "update" in cols and update_str is not None:
        cells.append(gspread.Cell(row, cols["update"], update_str))
    _ws_update_cells(data_worksheet, cells)


def _append_message(existing, date_str, message):
    """Keeps a running log in the MESSAGE cell instead of overwriting it, so the
    full outreach history for a recipient stays visible in one place."""
    entry_line = f"{date_str}: {message}"
    return f"{existing}\n{entry_line}" if existing else entry_line


def mark_team_sent(worksheet, row, block_start_col, existing_message, message, date_str, data_worksheet, data_layout,
                    entry, lady_name, clear_status=False):
    """Writes DATE + an appended MESSAGE line + SENT="X" in one batched
    update_cells call, and records the send in the DATA tab's Sent table (moving
    them out of whichever other table they were previously in, e.g. Unseen, on
    a follow-up). date_str is computed once for the whole run (the date Send
    was pressed), not per-row. Doesn't touch UPDATE -- that's Scan's job (mirrors
    mark_team_status, which never touches DATE for the same reason: each column
    only ever gets written by the one flow it actually means). clear_status=True
    (follow-up sends) also blanks UNSEEN/SEEN/REPLIED, since a fresh message
    makes the old read-state stale until the next Scan resolves it."""
    cells = [
        gspread.Cell(row, block_start_col + COL_DATE, date_str),
        gspread.Cell(row, block_start_col + COL_MESSAGE, _append_message(existing_message, date_str, message)),
        gspread.Cell(row, block_start_col + COL_SENT, "X"),
    ]
    if clear_status:
        cells += [
            gspread.Cell(row, block_start_col + COL_UNSEEN, ""),
            gspread.Cell(row, block_start_col + COL_SEEN, ""),
            gspread.Cell(row, block_start_col + COL_REPLIED, ""),
        ]
    _ws_update_cells(worksheet, cells)
    sync_data_status(data_worksheet, data_layout, "sent", entry, lady_name, date_str, update_str=None)


def mark_team_chat(worksheet, row, block_start_col, data_worksheet, data_layout, entry, lady_name, date_str,
                    update_date=None):
    """Marks CHAT and stamps DATE -- called either when existing history was found
    before sending (date_str = today, since that's effectively first contact; Send
    never passes update_date, matching mark_team_sent leaving UPDATE for Scan), or
    when a Scan finds a conversation that's grown into a real back-and-forth
    (date_str = the row's original send date, so DATE isn't overwritten; the scan
    caller passes update_date = today, since that's Scan's actual job). Clears SENT
    -- CHAT/SENT/UNSEEN/SEEN/REPLIED are mutually exclusive on the roster, mirroring
    how sync_data_status keeps a name in exactly one DATA-tab status table at a time."""
    cells = [
        gspread.Cell(row, block_start_col + COL_SENT, ""),
        gspread.Cell(row, block_start_col + COL_CHAT, "X"),
        gspread.Cell(row, block_start_col + COL_DATE, date_str),
    ]
    if update_date is not None:
        cells.append(gspread.Cell(row, block_start_col + COL_UPDATE, update_date))
    _ws_update_cells(worksheet, cells)
    sync_data_status(data_worksheet, data_layout, "chat", entry, lady_name, date_str, update_str=update_date)


def mark_team_status(worksheet, row, block_start_col, status, data_worksheet, data_layout, entry, lady_name,
                      sent_date, update_date):
    """status: 'unseen' | 'seen' | 'replied' -- mutually exclusive, so this batches
    all three TEAM1 cells (X in the matching column, blank in the other two) and
    mirrors the change into the DATA tab with DATE = when we sent them
    (sent_date). update_date always stamps TEAM1's UPDATE cell and the DATA
    tab's Update column with today's date -- it's a "we ran the system on this
    person today" marker, written on every scan regardless of whether the
    status actually changed. Also always clears SENT and CHAT -- a no-op for the
    normal case (already not-SENT, already not-CHAT), but required when a row
    that's still SENT (first scan after a send) or came in as CHAT (see
    _run_team_scan_async) gets reclassified here: without this, SENT/CHAT would
    stay "X" forever alongside the new status. SENT/CHAT/UNSEEN/SEEN/REPLIED are
    mutually exclusive on the roster, matching how sync_data_status keeps a name
    in exactly one DATA-tab status table at a time; _is_send_eligible's 'new'
    check and the Scan eligibility filter below both account for SENT no longer
    staying lit once a row resolves to unseen/seen/replied."""
    cells = [
        gspread.Cell(row, block_start_col + COL_SENT, ""),
        gspread.Cell(row, block_start_col + COL_CHAT, ""),
        gspread.Cell(row, block_start_col + COL_UNSEEN, "X" if status == "unseen" else ""),
        gspread.Cell(row, block_start_col + COL_SEEN, "X" if status == "seen" else ""),
        gspread.Cell(row, block_start_col + COL_REPLIED, "X" if status == "replied" else ""),
        gspread.Cell(row, block_start_col + COL_UPDATE, update_date),
    ]
    _ws_update_cells(worksheet, cells)
    sync_data_status(data_worksheet, data_layout, status, entry, lady_name, sent_date, update_str=update_date)


def mark_team_dead(worksheet, row, block_start_col, data_worksheet, data_layout, entry, lady_name):
    """Clears SENT/UNSEEN/SEEN/REPLIED/CHAT and strikes through NAME + the
    MESSAGE log in red -- called instead of resending once a follow-up target
    has already had 2 sends with no reply (see get_team_sheet_users'
    'exhausted'). DATE and the MESSAGE text itself are left alone -- struck
    through, not erased -- so this reads as "we tried, they went dark," and
    _is_send_eligible's message-not-empty check keeps it from ever being
    treated as a fresh 'new' lead again. Removes the row from every DATA-tab
    status table, since it no longer belongs in any of them."""
    cells = [
        gspread.Cell(row, block_start_col + COL_SENT, ""),
        gspread.Cell(row, block_start_col + COL_UNSEEN, ""),
        gspread.Cell(row, block_start_col + COL_SEEN, ""),
        gspread.Cell(row, block_start_col + COL_REPLIED, ""),
        gspread.Cell(row, block_start_col + COL_CHAT, ""),
    ]
    _ws_update_cells(worksheet, cells)

    name_a1 = gspread.utils.rowcol_to_a1(row, block_start_col + COL_NAME)
    message_a1 = gspread.utils.rowcol_to_a1(row, block_start_col + COL_MESSAGE)
    _ws_format(
        worksheet,
        [name_a1, message_a1],
        {"textFormat": {"strikethrough": True, "foregroundColor": RED_TEXT_COLOR}},
    )

    name = entry["name"]
    for status in data_layout:
        existing_row = _find_data_row(data_worksheet, data_layout, status, name)
        if existing_row:
            _remove_data_row(data_worksheet, data_layout, status, existing_row)


async def _run_in_tab_batches(items, process_one, log=None, stop_event=None, batch_size=None, inter_batch_delay=0):
    """Runs process_one(item) across `items` in sequential batches of
    `batch_size` (defaults to dm_bot.TAB_BATCH_SIZE) tabs -- a batch's tabs
    must all finish (including closing their tab) before the next batch
    opens, so at most batch_size tabs are ever open at once. Shared here
    since both the send and scan flows need it. inter_batch_delay, if given,
    is an extra pause after a batch finishes before the next one starts."""
    if batch_size is None:
        batch_size = dm_bot.TAB_BATCH_SIZE
    for start in range(0, len(items), batch_size):
        if stop_event and stop_event.is_set():
            dm_bot._emit_log(log, "Stopped by user.")
            break

        if start > 0 and inter_batch_delay:
            await asyncio.sleep(inter_batch_delay)

        batch = items[start:start + batch_size]
        dm_bot._emit_log(log, f"--- Starting batch of {len(batch)} tabs ---")
        tasks = []
        for i, item in enumerate(batch):
            if stop_event and stop_event.is_set():
                break
            if i > 0:
                await asyncio.sleep(random.uniform(*dm_bot.RAMP_UP_DELAY_RANGE))
            tasks.append(asyncio.create_task(process_one(item)))

        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)


def _mark_pending_source_red(username, log=None):
    """Called as soon as a real (non-dry-run) send starts processing username
    (see process_team_send_username) -- not gated on the eventual result -- and
    pops username's pending import source, if any, and colors its Username
    cell red in the tracker sheet. A no-op if username has no pending entry
    (imported before this feature existed, already marked from an earlier run,
    or never imported through the app at all). Failures are logged, not
    raised, since a tracker-sheet marking problem shouldn't block a send."""
    source = pop_pending_import_source(username)
    if not source:
        return
    try:
        source_worksheet = get_source_spreadsheet().worksheet(source["source_tab"])
        mark_source_usernames_imported(source_worksheet, [source["row"]])
    except Exception as e:
        logging.error(f"Failed to mark {username}'s tracker sheet row red: {e}")
        dm_bot._emit_log(log, f"[{username}] Couldn't mark the tracker sheet: {e}")


async def process_team_send_username(context, stealth, worksheet, data_worksheet, data_layout, entry, block_start_col,
                                      lady_name, message, run_date, send_gate, anchor_page=None, log=None, on_status=None,
                                      on_tab_status=None, dry_run=False, clear_status=False):
    """One row's send flow in its own tab. Navigating, history checking and typing
    all run concurrently across a batch's tabs; the send itself queues on
    send_gate, so only one tab submits at a time. Runs to completion once started
    -- stop_event is deliberately not passed into dm_bot, so Stop only ever
    prevents a *new* tab from starting (see _run_in_tab_batches); a tab that's
    already sending always finishes and closes normally."""
    username = entry["name"]
    dm_bot._emit_log(log, f"--- Processing: {username} ---")
    dm_bot._emit_status(on_status, username, "processing")
    # Marks the tracker sheet the moment a real (non-dry-run) send starts
    # processing this candidate -- not gated on the eventual result (sent,
    # skipped, error, etc.), so it reflects "this account got worked this run,"
    # not "this specific message definitely landed."
    if not dry_run:
        await asyncio.to_thread(_mark_pending_source_red, username, log)
    user_page = await context.new_page()
    dm_bot._emit_status(on_tab_status, username, "open")
    await stealth.apply_stealth_async(user_page)
    if anchor_page and not anchor_page.is_closed():
        await anchor_page.bring_to_front()

    try:
        # clear_status is already "this is a follow-up, not a first contact" --
        # reused here so a follow-up isn't blocked by our own earlier message
        # sitting in the chat (see dm_bot.prepare_and_type_dm's allow_reply_history).
        result = await dm_bot.prepare_and_type_dm(user_page, username, message, log=log, dry_run=dry_run, url=entry.get("link") or None, allow_reply_history=clear_status)

        if result == "typed":
            # Wait for our turn, send, then close before releasing -- so the next
            # tab's send starts only once this tab is gone. The sheet write below
            # deliberately stays outside the queue: it never touches the page, so
            # making the next send wait on a Sheets round-trip would only add
            # dead time between sends.
            async with send_gate:
                result = await dm_bot.submit_dm(user_page, username, log=log)
                if not user_page.is_closed():
                    await user_page.close()

        if result == "sent":
            await asyncio.to_thread(mark_team_sent, worksheet, entry["row"], block_start_col, entry.get("message", ""), message, run_date, data_worksheet, data_layout, entry, lady_name, clear_status)
            dm_bot._emit_log(log, f"[{username}] Marked SENT in {worksheet.title}.")
            dm_bot._emit_status(on_status, username, "sent")
        elif result == "dry_run":
            dm_bot._emit_status(on_status, username, "dry_run")
        elif result == "skipped":
            await asyncio.to_thread(mark_team_chat, worksheet, entry["row"], block_start_col, data_worksheet, data_layout, entry, lady_name, run_date)
            dm_bot._emit_log(log, f"[{username}] Marked CHAT (existing history).")
            dm_bot._emit_status(on_status, username, "chat")

    except Exception as e:
        logging.error(f"Failed to send team DM to {username}: {str(e)}")
        dm_bot._emit_log(log, f"Error processing {username}, skipping to next...")
        dm_bot._emit_status(on_status, username, "error")
    finally:
        if not user_page.is_closed():
            await user_page.close()
        dm_bot._emit_status(on_tab_status, username, "closed")


def _matches_followup_target(entry, target):
    """The status part of a follow-up match, without the exhaustion check --
    factored out so _run_team_send_async's dead-account detection (exhausted
    rows that WOULD match) can share it with _is_send_eligible (not-exhausted
    rows that DO match)."""
    if target == "unseen_followup":
        return entry["unseen"] and not entry["chat"] and not entry["replied"]
    if target == "seen_followup":
        return entry["seen"] and not entry["chat"] and not entry["replied"]
    return False


def _is_send_eligible(entry, target, date_filter):
    """target: 'new' (never contacted, the default) | 'unseen_followup' | 'seen_followup'.
    date_filter, if given, additionally requires an exact match against the
    date column that target's rows actually carry: for 'new' that's DATE (the
    date the row was pulled/imported, see add_candidates_to_roster), since
    UPDATE is never stamped on a never-contacted row; for follow-up targets
    it's UPDATE (the date it was last scanned), letting a follow-up target
    just the people who were checked on a specific day. Follow-up targets also
    exclude 'exhausted' rows (2+ sends already, see get_team_sheet_users) --
    someone who hasn't engaged after that many tries isn't offered again (see
    mark_team_dead, which handles those instead of silently dropping them). The
    'new' target also excludes any row with existing MESSAGE history, even if
    its status boxes are empty -- that's what a dead-marked row looks like after
    mark_team_dead clears them, and it must never look like a fresh lead again.
    Checks all five status boxes, not just SENT/CHAT -- mark_team_status now
    clears SENT/CHAT once a row resolves to unseen/seen/replied (they're mutually
    exclusive there), so a resolved row must still be excluded via its own box."""
    if target in ("unseen_followup", "seen_followup"):
        ok = _matches_followup_target(entry, target) and not entry["exhausted"]
    else:
        ok = (not entry["sent"] and not entry["chat"] and not entry["unseen"]
              and not entry["seen"] and not entry["replied"] and not entry["message"])
    if ok and date_filter:
        ok = (entry["date"] if target == "new" else entry["update"]) == date_filter
    return ok


async def _run_team_send_async(message, worksheet, data_worksheet, lady_name, log=None, on_status=None,
                                on_tab_status=None, stop_event=None, dry_run=False, target="new",
                                date_filter=None):
    block_start_col = _lady_block_start_col(worksheet, lady_name)
    if block_start_col is None:
        raise ValueError(f"Lady '{lady_name}' not found in {worksheet.title}")

    rows, _ = get_team_sheet_users(worksheet, lady_name)
    data_layout = _data_table_layout(data_worksheet)

    # Dedupe by name -- concurrent tabs for the same user would otherwise both
    # send and both write to the same row. Rows that match a follow-up target's
    # status but are exhausted (2+ sends, no reply) go to `dead` instead of
    # `eligible` -- see mark_team_dead.
    seen_names = set()
    eligible = []
    dead = []
    for entry in rows:
        if entry["name"] in seen_names:
            continue
        if _is_send_eligible(entry, target, date_filter):
            seen_names.add(entry["name"])
            eligible.append(entry)
        elif (target != "new" and entry["exhausted"] and _matches_followup_target(entry, target)
              and (not date_filter or entry["update"] == date_filter)):
            seen_names.add(entry["name"])
            dead.append(entry)

    for entry in dead:
        if not dry_run:
            await asyncio.to_thread(mark_team_dead, worksheet, entry["row"], block_start_col, data_worksheet, data_layout, entry, lady_name)
        dm_bot._emit_log(log, f"[{entry['name']}] 2 sends with no reply -- marking dead account instead of resending.")
        dm_bot._emit_status(on_status, entry["name"], "dead")

    try:
        if not eligible:
            dm_bot._emit_log(log, "No eligible users to message for this lady.")
            return

        dm_bot._emit_log(log, f"Found {len(eligible)} users to message.")

        context, anchor_page = await dm_bot._ensure_context()
        stealth = Stealth()
        run_date = datetime.now().strftime("%Y-%m-%d")
        clear_status = target != "new"
        # One queue for the whole run, not per batch -- tabs type concurrently but
        # line up here to send, in the order they finish typing.
        send_gate = asyncio.Lock()

        async def process_one(entry):
            await process_team_send_username(
                context, stealth, worksheet, data_worksheet, data_layout, entry, block_start_col, lady_name,
                message, run_date, send_gate, anchor_page=anchor_page, log=log, on_status=on_status,
                on_tab_status=on_tab_status, dry_run=dry_run, clear_status=clear_status,
            )

        await _run_in_tab_batches(eligible, process_one, log=log, stop_event=stop_event)
    finally:
        # Runs on every call, including "nothing eligible" -- so re-running Send
        # after manually editing/clearing the sheet still refreshes the dashboard
        # even when there's nobody left to message.
        await asyncio.to_thread(update_dashboard_counts, worksheet, data_worksheet)


def run_team_send(message, worksheet, data_worksheet, lady_name, log=None, on_status=None, on_tab_status=None,
                   stop_event=None, dry_run=False, target="new", date_filter=None):
    loop = dm_bot._ensure_loop()
    future = asyncio.run_coroutine_threadsafe(
        _run_team_send_async(message, worksheet, data_worksheet, lady_name, log, on_status, on_tab_status,
                              stop_event, dry_run, target, date_filter),
        loop,
    )
    future.result()


async def _open_pm(page, username, stop_event=None, url=None):
    """Navigates to the profile and opens the PM panel -- just the first two steps
    of dm_bot.prepare_and_type_dm, duplicated here rather than extracted out of it,
    since scan never types or sends anything and this keeps that path untouched."""
    if stop_event and stop_event.is_set():
        return False
    if await dm_bot._await_or_stop(page.goto(dm_bot._normalize_profile_url(url) or f"https://stripchat.com/{username}"), stop_event) is dm_bot._STOPPED:
        return False
    pm_button = page.locator("#user-actions-send-pm, button[aria-label='Send PM']")
    if await dm_bot._await_or_stop(pm_button.first.wait_for(state="visible", timeout=15000), stop_event) is dm_bot._STOPPED:
        return False
    await pm_button.first.click()
    return True


async def _endpoints_are_counterpart(chat):
    """Checks whether the FIRST and LAST rendered messages (chronological DOM
    order) are both from the counterpart -- a more precise signal than a bare
    count() over the whole container, and a safety net for it:
    counterpart-base-message matches via a partial class name (a CSS-module
    hash suffix that can change, per the comment below), so a real counterpart
    message could in principle go undetected by that count() while still
    being identifiable individually here. Used to catch e.g. a recipient who
    messaged first and still hasn't gotten a reply -- that should read as
    'replied', not fall through to seen/unseen just because the count() check
    happened to miss it."""
    messages = chat.locator(".base-message-wrapper")
    count = await messages.count()
    if count == 0:
        return False

    async def is_counterpart(i):
        cls = await messages.nth(i).get_attribute("class") or ""
        return "counterpart-base-message" in cls

    return await is_counterpart(0) and await is_counterpart(count - 1)


async def _read_message_status(page, stop_event=None):
    """Returns 'chat' | 'replied' | 'seen' | 'unseen' for the currently open PM
    panel. A conversation that's reached 3 or more total messages (either side)
    is treated as a real back-and-forth and always wins as 'chat', regardless
    of who sent last -- counted via dm_bot._wait_for_settled_message_count(),
    which polls the shared 'base-message-wrapper' class (present on both our
    own and the counterpart's bubbles) until the count stabilizes, so a
    conversation still rendering isn't undercounted. Otherwise, replied/seen/
    unseen checked in that order because a replied conversation also shows a
    read icon on our earlier message -- replied must win. Uses partial class
    matching for counterpart-message since that class has a CSS-module hash
    suffix that can change -- _endpoints_are_counterpart is a second, more
    precise check for the same signal, in case that partial match ever misses
    a real counterpart message."""
    chat = page.locator(".content-messages__scroll-container")
    if await dm_bot._wait_for_settled_message_count(page, stop_event=stop_event) >= 3:
        return "chat"
    if await chat.locator('[class*="counterpart-base-message"]').count() > 0:
        return "replied"
    if await _endpoints_are_counterpart(chat):
        return "replied"
    if await chat.locator("svg.icon-read").count() > 0:
        return "seen"
    return "unseen"


async def process_team_scan_username(context, stealth, worksheet, data_worksheet, data_layout, entry, block_start_col,
                                      lady_name, scan_date, anchor_page=None, log=None, on_status=None, dry_run=False,
                                      stop_event=None):
    username = entry["name"]
    dm_bot._emit_log(log, f"--- Scanning: {username} ---")
    dm_bot._emit_status(on_status, username, "processing")
    user_page = await context.new_page()
    await stealth.apply_stealth_async(user_page)
    if anchor_page and not anchor_page.is_closed():
        await anchor_page.bring_to_front()

    try:
        opened = await _open_pm(user_page, username, stop_event=stop_event, url=entry.get("link") or None)
        if not opened:
            dm_bot._emit_log(log, f"[{username}] Stopped before status could be read.")
            dm_bot._emit_status(on_status, username, "error")
            return

        status = await _read_message_status(user_page, stop_event=stop_event)
        dm_bot._emit_log(log, f"[{username}] Status: {status}.")

        if not dry_run:
            if status == "chat":
                await asyncio.to_thread(mark_team_chat, worksheet, entry["row"], block_start_col, data_worksheet, data_layout, entry, lady_name, entry["date"], update_date=scan_date)
            else:
                # Update always stamps today's date -- it just means "we ran a
                # scan on this person today," regardless of whether the status
                # came back the same as last time.
                await asyncio.to_thread(mark_team_status, worksheet, entry["row"], block_start_col, status, data_worksheet, data_layout, entry, lady_name, entry["date"], scan_date)

        dm_bot._emit_status(on_status, username, "dry_run" if dry_run else status)

    except Exception as e:
        logging.error(f"Failed to scan status for {username}: {str(e)}")
        dm_bot._emit_log(log, f"Error scanning {username}, skipping to next...")
        dm_bot._emit_status(on_status, username, "error")
    finally:
        if not user_page.is_closed():
            await user_page.close()


async def _run_team_scan_async(worksheet, data_worksheet, lady_name, log=None, on_status=None, stop_event=None,
                                dry_run=False, date_filter=None):
    block_start_col = _lady_block_start_col(worksheet, lady_name)
    if block_start_col is None:
        raise ValueError(f"Lady '{lady_name}' not found in {worksheet.title}")

    rows, _ = get_team_sheet_users(worksheet, lady_name)
    # CHAT rows are included here, not just SENT -- the Send flow's history check
    # marks CHAT on *any* existing message (a coarse "someone already has a
    # relationship with this person" gate), while _read_message_status below only
    # calls it "chat" at 3+ messages. A CHAT row might really just be one old
    # message that should be replied/seen/unseen instead, so it needs the same
    # rescan chance a SENT row gets -- if the real count turns out to be 3+ it
    # stays/gets reconfirmed CHAT, otherwise it's reclassified. UNSEEN/SEEN rows
    # are included too since mark_team_status now clears SENT/CHAT once a row
    # resolves to one of those (mutually exclusive with SENT/CHAT, matching
    # sync_data_status's one-table-at-a-time DATA tab) -- without checking those
    # boxes directly, a resolved row would never come up for rescanning again.
    # REPLIED is still a terminal state and stays excluded. date_filter, if given,
    # further limits this to whichever day's batch was sent (entry's own DATE,
    # not UPDATE), so a specific day's sends can be rescanned alone.
    eligible = [
        e for e in rows
        if (e["sent"] or e["chat"] or e["unseen"] or e["seen"]) and not e["replied"]
        and (not date_filter or e["date"] == date_filter)
    ]

    try:
        if not eligible:
            dm_bot._emit_log(log, "No sent users pending a status scan for this lady.")
            return

        dm_bot._emit_log(log, f"Scanning {len(eligible)} sent users.")

        context, anchor_page = await dm_bot._ensure_context()
        stealth = Stealth()
        scan_date = datetime.now().strftime("%Y-%m-%d")
        data_layout = _data_table_layout(data_worksheet)

        async def process_one(entry):
            await process_team_scan_username(
                context, stealth, worksheet, data_worksheet, data_layout, entry, block_start_col, lady_name,
                scan_date, anchor_page=anchor_page, log=log, on_status=on_status,
                dry_run=dry_run, stop_event=stop_event,
            )

        await _run_in_tab_batches(eligible, process_one, log=log, stop_event=stop_event)
    finally:
        # Runs on every call, including "nothing to scan" -- so re-running Scan
        # after manually editing/clearing the sheet still refreshes the dashboard.
        await asyncio.to_thread(update_dashboard_counts, worksheet, data_worksheet)


def run_team_scan(worksheet, data_worksheet, lady_name, log=None, on_status=None, stop_event=None, dry_run=False,
                   date_filter=None):
    loop = dm_bot._ensure_loop()
    future = asyncio.run_coroutine_threadsafe(
        _run_team_scan_async(worksheet, data_worksheet, lady_name, log, on_status, stop_event, dry_run, date_filter),
        loop,
    )
    future.result()
