import asyncio
import logging
import random
import threading
import urllib.parse
import playwright.async_api

import paths

logging.basicConfig(
    filename=paths.data_file("bot.log"),
    level=logging.ERROR,
    format="%(asctime)s %(levelname)s %(message)s",
)

# Pre-authenticated via refresh_session.py -- never used for login directly,
# so it never touches the login form that triggers automation detection.
USER_DATA_DIR = paths.data_file("automation_session")
CREDENTIALS_FILE = paths.credentials_path()

TAB_BATCH_SIZE = 5
RAMP_UP_DELAY_RANGE = (0.5, 1.0)  # stagger between opening tabs within a batch

HISTORY_CHECK_TIMEOUT = 8.0  # max seconds to wait for existing chat history to render
HISTORY_CHECK_INTERVAL = 0.5

SEND_CONFIRM_TIMEOUT = 15.0  # max seconds to wait for a NEW delivery checkmark after clicking Send
SEND_CONFIRM_INTERVAL = 0.3

# The browser is launched once and reused across Start/Stop cycles (Stop no longer
# tears it down), so a single background thread runs one persistent asyncio event
# loop that owns the Playwright driver + browser context across separate runs.
_loop = None
_loop_thread = None
_playwright = None
_browser_context = None
_anchor_page = None

_STOPPED = object()  # sentinel: an awaited step was cut short by stop_event


def _loop_worker(loop):
    asyncio.set_event_loop(loop)
    loop.run_forever()


def _ensure_loop():
    global _loop, _loop_thread
    if _loop is None:
        _loop = asyncio.new_event_loop()
        _loop_thread = threading.Thread(target=_loop_worker, args=(_loop,), daemon=True)
        _loop_thread.start()
    return _loop


# Launch Chrome without the automation fingerprint so it behaves like a normal
# browser. Playwright otherwise passes --enable-automation (the "Chrome is being
# controlled by automated test software" banner) and sets navigator.webdriver,
# which sign-in pages use to refuse login with "this browser may not be secure"
# -- so the user couldn't log in at all. Dropping that default and disabling the
# AutomationControlled blink feature removes both signals. It also makes the bot
# itself less obviously automated during sends.
_LAUNCH_ARGS = ["--disable-blink-features=AutomationControlled"]
_IGNORE_DEFAULT_ARGS = ["--enable-automation"]


async def _ensure_context():
    global _playwright, _browser_context, _anchor_page
    if _browser_context is None:
        _playwright = await playwright.async_api.async_playwright().start()
        _browser_context = await _playwright.chromium.launch_persistent_context(
            USER_DATA_DIR, headless=False, channel="chrome",
            args=_LAUNCH_ARGS, ignore_default_args=_IGNORE_DEFAULT_ARGS,
        )
        # Kept open for the whole run and never used for a DM -- callers switch
        # back to it after every new tab so the browser window stays on the first
        # page instead of jumping to whichever tab just opened.
        _anchor_page = _browser_context.pages[0] if _browser_context.pages else await _browser_context.new_page()

        # Warms DNS/TLS/CDN caches once, up front -- observed in practice: the
        # first batch of concurrent tabs in a fresh context pays this cost all at
        # once and can blow prepare_and_type_dm's 15s textarea wait, while every later batch
        # (already warm) renders fast. A single sequential visit here means the
        # real batches never pay that cost.
        try:
            await _anchor_page.goto("https://stripchat.com", wait_until="load", timeout=30000)
        except Exception:
            pass  # best-effort warm-up -- a slow/failed warm-up shouldn't block the run
    return _browser_context, _anchor_page


async def _close_browser_async():
    global _playwright, _browser_context, _anchor_page
    if _browser_context is not None:
        await _browser_context.close()
        _browser_context = None
        _anchor_page = None
    if _playwright is not None:
        await _playwright.stop()
        _playwright = None


def close_browser():
    """Closes the persistent browser, if one is open."""
    if _loop is None:
        return
    future = asyncio.run_coroutine_threadsafe(_close_browser_async(), _loop)
    future.result()


async def _open_for_login_async():
    _, anchor = await _ensure_context()
    try:
        await anchor.bring_to_front()
        await anchor.goto("https://stripchat.com/", wait_until="domcontentloaded", timeout=30000)
    except Exception:
        pass  # the window is open regardless; the user can navigate it


def open_for_login():
    """Opens the bot's own persistent Chrome so the user can log in by hand.

    The session lives in USER_DATA_DIR and survives across runs, so this is only
    needed once (or whenever it expires). It's the reliable way in on Windows,
    where reading the main Chrome's cookies is blocked by app-bound encryption.
    Closing the browser afterwards (close_browser) is what persists the login.
    """
    _ensure_loop()
    future = asyncio.run_coroutine_threadsafe(_open_for_login_async(), _loop)
    future.result()


async def _await_or_stop(awaitable, stop_event, poll_interval=0.2):
    """Runs awaitable to completion, but bails out early (returning _STOPPED) the
    moment stop_event fires, instead of blocking on Playwright's own much longer
    timeout -- this is what makes Stop take effect in ~0.2s instead of up to 15s.
    Uses asyncio.wait's timeout (rather than sleep-then-check) so a fast awaitable
    still returns as soon as it's done, without waiting out a full poll interval."""
    if not stop_event:
        return await awaitable
    task = asyncio.ensure_future(awaitable)
    while True:
        if stop_event.is_set():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            return _STOPPED
        done, _ = await asyncio.wait({task}, timeout=poll_interval)
        if task in done:
            return task.result()


async def type_with_delay(locator, text):
    for char in text:
        await locator.type(char)
        await asyncio.sleep(random.uniform(0.05, 0.15))


def _emit_log(log, text):
    # Always print too, not just when there's no `log` callback -- the callback
    # only reaches the browser's on-screen Log panel via SSE, which is gone
    # once the run ends, so the console/log file is the only place to
    # after-the-fact diagnose what a run actually did.
    print(text)
    if log:
        log(text)


def _emit_status(on_status, username, status):
    if on_status:
        on_status(username, status)


async def _wait_for_settled_message_count(page, stop_event=None):
    """Polls the chat's message count -- via the 'base-message-wrapper' class,
    shared by both our own and the counterpart's message bubbles, matched
    exactly so it doesn't also catch the nested 'base-message-wrapper-inner'
    div -- until two consecutive polls agree, so a conversation that's still
    rendering isn't undercounted. A single check right after the panel opens
    isn't reliable once several tabs are loading concurrently -- slower tabs
    need more time for history to render -- so this polls up to
    HISTORY_CHECK_TIMEOUT and returns as soon as the count stabilizes."""
    chat_container = page.locator(".content-messages__scroll-container")
    elapsed = 0.0
    last_count = -1
    stable_polls = 0
    while elapsed < HISTORY_CHECK_TIMEOUT:
        if stop_event and stop_event.is_set():
            return max(last_count, 0)
        try:
            count = await chat_container.locator(".base-message-wrapper").count()
        except Exception:
            count = last_count if last_count >= 0 else 0  # container not found yet, keep polling
        if count == last_count:
            stable_polls += 1
            if stable_polls >= 2:
                return count
        else:
            stable_polls = 0
        last_count = count
        await asyncio.sleep(HISTORY_CHECK_INTERVAL)
        elapsed += HISTORY_CHECK_INTERVAL
    return max(last_count, 0)


def _normalize_profile_url(url):
    """The session is authenticated only on the apex stripchat.com host. A sheet
    link pointing at a geo subdomain (e.g. th.stripchat.com, imported from a
    region-redirected scrape) loads a profile where Send PM is clickable but the
    PM panel silently never opens -- the textarea never enters the DOM, so
    prepare_and_type_dm's textarea wait always times out. Rewrite any *.stripchat.com host
    back to the apex host, preserving the path, so PM opens against the authed
    session. Non-stripchat.com hosts (and None) are returned unchanged."""
    if not url:
        return url
    parts = urllib.parse.urlsplit(url)
    if parts.hostname and parts.hostname.endswith(".stripchat.com") and parts.hostname != "stripchat.com":
        parts = parts._replace(netloc="stripchat.com")
    return urllib.parse.urlunsplit(parts)


async def prepare_and_type_dm(page, username, message, log=None, dry_run=False, stop_event=None, url=None, allow_reply_history=False):
    """Site-specific navigation + history checking + typing -- the half of a DM
    that's safe to run in several tabs at once. Navigates to
    `url` if given (some sheets store a full profile link that isn't just
    stripchat.com/{username}), otherwise falls back to the default.
    allow_reply_history=False (first contact, the default): any existing message
    blocks sending -- someone else already has a relationship with this person.
    allow_reply_history=True (a follow-up send): only a real back-and-forth (3 or
    more messages) blocks sending -- our own earlier message sitting there
    alone shouldn't stop the follow-up that's the whole point of this call.
    Returns "typed" (the message sits in the box, ready for submit_dm), "dry_run",
    "skipped" (history blocks this send), or "stopped_early" (stop requested
    before any typing started)."""
    if stop_event and stop_event.is_set():
        return "stopped_early"

    if await _await_or_stop(page.goto(_normalize_profile_url(url) or f"https://stripchat.com/{username}"), stop_event) is _STOPPED:
        return "stopped_early"

    # 1. Click the profile's Send PM button
    pm_button = page.locator("#user-actions-send-pm, button[aria-label='Send PM']")
    if await _await_or_stop(pm_button.first.wait_for(state="visible", timeout=15000), stop_event) is _STOPPED:
        return "stopped_early"
    await pm_button.first.click()

    # 2. Wait for previous chat history to load from the server, if any
    _emit_log(log, f"[{username}] Checking for previous chat history...")
    message_count = await _wait_for_settled_message_count(page, stop_event=stop_event)
    history_limit = 2 if allow_reply_history else 0  # 3+ messages counts as a real conversation
    if message_count > history_limit:
        _emit_log(log, f"[{username}] Previous messages found! Skipping user.")
        return "skipped" # Returning "skipped" tells the loop to close the tab and skip

    if stop_event and stop_event.is_set():
        return "stopped_early"

    if dry_run:
        _emit_log(log, f"[{username}] Dry run: PM panel opened, no history found. Not sending.")
        return "dry_run" # Stop before touching the message box -- nothing typed, nothing sent

    # 3. Locate the precise Text Input Box
    chat_input = page.locator("textarea[placeholder='Private message...']")
    if await _await_or_stop(chat_input.first.wait_for(state="visible", timeout=15000), stop_event) is _STOPPED:
        return "stopped_early"
    await chat_input.first.click()

    # 4. Type the message with human delay. Once started, always finish it so a
    # half-typed message never sits in the box -- Stop only holds back the Send click.
    await type_with_delay(chat_input.first, message)
    return "typed"


async def _log_send_failure_snapshot(page, username, bubbles_before, icons_before, log=None):
    """Dumps what the PM panel actually looked like when confirmation timed out,
    to bot.log and the on-screen Log panel. Without this a failed confirmation is
    indistinguishable from a failed send -- and telling those apart after the fact
    is the whole difficulty here. The svg class list is what reveals a site-side
    icon rename; a textarea that still holds our text is the clearest sign the
    message genuinely never left. Best-effort: any failure gathering this is
    swallowed so it never replaces the real timeout error."""
    try:
        state = await page.evaluate(
            """() => {
                const container = document.querySelector('.content-messages__scroll-container');
                const bubbles = document.querySelectorAll('.base-message-wrapper');
                const box = document.querySelector("textarea[placeholder='Private message...']");
                const svgs = new Set();
                for (const svg of document.querySelectorAll('svg')) svgs.add(svg.getAttribute('class') || '(none)');
                return {
                    container: !!container,
                    bubbles: bubbles.length,
                    bubblesInContainer: container ? container.querySelectorAll('.base-message-wrapper').length : -1,
                    lastBubbleClass: bubbles.length ? (bubbles[bubbles.length - 1].getAttribute('class') || '') : '(none)',
                    svgClasses: [...svgs],
                    textareaLength: box ? box.value.length : -1,
                };
            }"""
        )
        detail = (
            f"[{username}] Send confirmation timed out. bubbles {bubbles_before}->{state['bubbles']} "
            f"(in container: {state['bubblesInContainer']}, container present: {state['container']}), "
            f"icons before: {icons_before}, last bubble class: {state['lastBubbleClass']!r}, "
            f"textarea chars left: {state['textareaLength']}, svg classes: {state['svgClasses']}"
        )
    except Exception as e:
        detail = f"[{username}] Send confirmation timed out; couldn't read page state: {e}"
    logging.error(detail)
    _emit_log(log, detail)


async def submit_dm(page, username, log=None, stop_event=None):
    """Sends the message prepare_and_type_dm already typed into `page`, then waits
    for delivery confirmation. Split from the typing half so a caller can serialize
    just this part while several tabs type at once (see team_bot's send queue).
    Returns "sent", or "stopped_writing" (stop requested after typing finished --
    message was never sent)."""
    if stop_event and stop_event.is_set():
        _emit_log(log, f"[{username}] Stopped after writing -- not sending.")
        return "stopped_writing"

    # Snapshot the thread BEFORE we send, so confirmation can be tied to the
    # message we're about to send rather than to anything already on screen (the
    # original wait on `checkmark.last` was satisfied instantly by an EARLIER
    # message's checkmark). Two independent signals are snapshotted because
    # either one alone misses real sends:
    #  - the message bubbles themselves, counted PAGE-WIDE and not under
    #    .content-messages__scroll-container: that container holds rendered
    #    history, and on a first contact (no history at all) it may not be in the
    #    DOM when the panel opens, so anything scoped under it matches nothing
    #    forever and every delivered first message reads as a failure.
    #  - the delivery icons, matching icon-read as well as icon-check-4: the site
    #    swaps check for read once the message is read (see team_bot's
    #    _read_message_status), so a recipient who reads instantly can mean
    #    icon-check-4 is never observed by a 0.3s poll.
    bubbles = page.locator(".base-message-wrapper")
    icons = page.locator("svg.icon-check-4, svg.icon-read")
    try:
        bubbles_before = await bubbles.count()
    except Exception:
        bubbles_before = 0
    try:
        icons_before = await icons.count()
    except Exception:
        icons_before = 0

    # 5. Locate and click the exact Send Button
    send_button = page.locator("button[aria-label='Send']")
    await send_button.first.wait_for(state="visible", timeout=5000)
    await send_button.first.click()

    # 6. Wait for confirmation that our message joined the thread. On timeout we
    # raise, so the caller (process_team_send_username) treats it as not-sent --
    # status "error" -- rather than silently marking the roster row SENT for a
    # message that may never have gone out.
    elapsed = 0.0
    own_bubble_polls = 0  # consecutive polls seeing our new bubble
    while elapsed < SEND_CONFIRM_TIMEOUT:
        try:
            if await icons.count() > icons_before:
                _emit_log(log, f"[{username}] Message sent (checkmark confirmed).")
                return "sent"
            if await bubbles.count() > bubbles_before:
                cls = await bubbles.last.get_attribute("class") or ""
                # Must be ours, not an incoming message that landed mid-send, and
                # must still be there on the next poll -- an optimistic render the
                # server then rejects disappears again, and would otherwise count
                # as a send that never happened.
                if "counterpart-base-message" not in cls:
                    own_bubble_polls += 1
                    if own_bubble_polls >= 2:
                        _emit_log(log, f"[{username}] Message sent (appeared in thread).")
                        return "sent"
                else:
                    own_bubble_polls = 0
            else:
                own_bubble_polls = 0
        except Exception:
            pass  # DOM mid-rerender; keep polling
        await asyncio.sleep(SEND_CONFIRM_INTERVAL)
        elapsed += SEND_CONFIRM_INTERVAL

    await _log_send_failure_snapshot(page, username, bubbles_before, icons_before, log)
    raise playwright.async_api.TimeoutError(
        f"No delivery confirmation for {username} within {SEND_CONFIRM_TIMEOUT}s -- not marking SENT."
    )
