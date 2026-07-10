"""Pulls current stripchat.com cookies from your main Chrome and loads them
into the bot's automation profile. Run this whenever the bot's session
expires (login fails / DMs stop sending) -- log into stripchat.com in your
regular Chrome first, then run this script before running dm_bot.py.
"""
import browser_cookie3
from playwright.sync_api import sync_playwright

AUTOMATION_PROFILE_DIR = "./automation_session"
TARGET_URL = "https://stripchat.com/"


def refresh():
    cj = browser_cookie3.chrome(domain_name="stripchat.com")
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
        print("No stripchat.com cookies found in Chrome. Log in at stripchat.com in your regular Chrome first.")
        return

    # Force English UI -- the bot's selectors are written against English
    # text/labels, but stripchat.com auto-localizes based on visitor IP.
    cookies.append({
        "name": "localeDomain", "value": "en", "domain": ".stripchat.com", "path": "/", "secure": True,
    })

    with sync_playwright() as p:
        context = p.chromium.launch_persistent_context(
            AUTOMATION_PROFILE_DIR, headless=True, channel="chrome"
        )
        context.add_cookies(cookies)

        page = context.pages[0] if context.pages else context.new_page()
        page.goto(TARGET_URL, wait_until="domcontentloaded", timeout=30000)
        page.wait_for_timeout(2000)
        logged_in = page.locator("text=Log In").count() == 0
        page.close()
        context.close()

    if logged_in:
        print(f"Session refreshed successfully with {len(cookies)} cookies. You can run dm_bot.py now.")
    else:
        print("Cookies were loaded, but the page still shows a Log In button -- your main Chrome session may have expired. Log in again there and re-run this script.")


if __name__ == "__main__":
    refresh()
