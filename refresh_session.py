"""Pulls current stripchat.com cookies from your main Chrome and loads them
into the bot's automation profile. Run this whenever the bot's session
expires (login fails / DMs stop sending) -- log into stripchat.com in your
regular Chrome first, then run this script before running dm_bot.py.
"""
import browser_cookie3
from playwright.sync_api import sync_playwright

import dm_bot

AUTOMATION_PROFILE_DIR = dm_bot.USER_DATA_DIR
TARGET_URL = "https://stripchat.com/"


def import_from_chrome():
    """Copies stripchat.com cookies from the main Chrome into the bot's profile.

    Returns {"ok", "count", "logged_in", "message"}. On Chrome 127+ (Windows)
    this typically finds nothing: cookies there use app-bound encryption that
    browser_cookie3 can't read. Manual login (dm_bot.open_for_login) is the
    dependable path; this is the convenience shortcut when it happens to work.

    The caller must have closed the bot browser first -- this opens the same
    persistent profile, and Chrome allows only one owner of its lock at a time.
    """
    try:
        cj = browser_cookie3.chrome(domain_name="stripchat.com")
    except Exception as e:
        return {"ok": False, "count": 0, "logged_in": False,
                "message": f"Couldn't read Chrome's cookies ({e}). Use Log in instead."}

    cookies = [
        {
            "name": c.name,
            "value": c.value,
            "domain": c.domain,
            "path": c.path or "/",
            "secure": bool(c.secure),
        }
        for c in cj
    ]

    if not cookies:
        return {"ok": False, "count": 0, "logged_in": False,
                "message": "No Stripchat cookies found in Chrome. Log in at stripchat.com in Chrome first, or use Log in."}

    # Force English UI -- the bot's selectors are written against English
    # text/labels, but stripchat.com auto-localizes based on visitor IP.
    cookies.append({
        "name": "localeDomain", "value": "en", "domain": ".stripchat.com", "path": "/", "secure": True,
    })

    with sync_playwright() as p:
        context = p.chromium.launch_persistent_context(
            AUTOMATION_PROFILE_DIR, headless=True, channel="chrome",
            args=dm_bot._LAUNCH_ARGS, ignore_default_args=dm_bot._IGNORE_DEFAULT_ARGS,
        )
        context.add_cookies(cookies)

        page = context.pages[0] if context.pages else context.new_page()
        page.goto(TARGET_URL, wait_until="domcontentloaded", timeout=30000)
        page.wait_for_timeout(2000)
        logged_in = page.locator("text=Log In").count() == 0
        page.close()
        context.close()

    if logged_in:
        return {"ok": True, "count": len(cookies), "logged_in": True,
                "message": f"Imported {len(cookies)} cookies. You're logged in."}
    return {"ok": False, "count": len(cookies), "logged_in": False,
            "message": "Cookies were loaded but the site still shows Log In -- your Chrome session may have expired. Log in there again, or use Log in."}


def refresh():
    result = import_from_chrome()
    print(result["message"])


if __name__ == "__main__":
    refresh()
