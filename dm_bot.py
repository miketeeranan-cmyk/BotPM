import asyncio
import logging
import random
from datetime import datetime
import gspread
import playwright.async_api
from playwright_stealth import Stealth

logging.basicConfig(
    filename="bot.log",
    level=logging.ERROR,
    format="%(asctime)s %(levelname)s %(message)s",
)

# Pre-authenticated via refresh_session.py -- never used for login directly,
# so it never touches the login form that triggers automation detection.
USER_DATA_DIR = "./automation_session"
SHEET_NAME = "stripchat tracker"
CREDENTIALS_FILE = "credentials.json"

TAB_BATCH_SIZE = 20
RAMP_UP_DELAY_RANGE = (0.5, 1.0)  # stagger between opening tabs within a batch


def get_spreadsheet():
    client = gspread.service_account(filename=CREDENTIALS_FILE)
    return client.open(SHEET_NAME)


def choose_worksheet(spreadsheet):
    """Lists all sheet tabs and lets the user pick one from the terminal."""
    worksheets = spreadsheet.worksheets()

    print("Available sheets:")
    for i, ws in enumerate(worksheets, 1):
        print(f"{i}. {ws.title}")

    while True:
        choice = input("Which sheet would you like to pull data from? (number or name)\n> ").strip()

        if choice.isdigit() and 1 <= int(choice) <= len(worksheets):
            return worksheets[int(choice) - 1]

        for ws in worksheets:
            if ws.title == choice:
                return ws

        print("Invalid selection, try again.")


def get_worksheet_users(worksheet, page, page_size):
    """Returns (users_page, total, sent) for the dashboard's paginated user list.
    already_sent is based on column G, which replace_and_mark_red fills in once a DM goes out.
    Reads the whole sheet in a single API call, then slices to the requested page so the
    dashboard never has to render (or wait on) the full list at once."""
    rows = worksheet.get_all_values()[1:]

    users = []
    sent = 0
    for row in rows:
        username = row[0] if row else ""
        if not username or not username.strip():
            continue
        already_sent = bool(len(row) > 6 and row[6].strip())
        if already_sent:
            sent += 1
        users.append({
            "username": username,
            "level": row[1] if len(row) > 1 else "",
            "already_sent": already_sent,
        })

    total = len(users)
    start = (page - 1) * page_size
    return users[start:start + page_size], total, sent


def replace_and_mark_red(worksheet, username, message):
    """Marks the user's existing row as sent in place (no delete/append,
    so it can't drift to the wrong columns) and logs the message + timestamp."""
    cell = worksheet.find(username)
    if cell is None:
        return

    row_idx = cell.row
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    worksheet.update_cell(row_idx, 7, message)    # G
    worksheet.update_cell(row_idx, 8, timestamp)  # H

    # Color the text red (not the cell background) in columns A-C and E
    red_text = {"textFormat": {"foregroundColor": {"red": 1.0, "green": 0.0, "blue": 0.0}}}
    worksheet.format(f"A{row_idx}:C{row_idx}", red_text)
    worksheet.format(f"E{row_idx}", red_text)


async def type_with_delay(locator, text):
    for char in text:
        await locator.type(char)
        await asyncio.sleep(random.uniform(0.05, 0.15))


def _emit_log(log, text):
    log(text) if log else print(text)


def _emit_status(on_status, username, status):
    if on_status:
        on_status(username, status)


async def send_dm(page, username, message, log=None):
    """Site-specific navigation + DM execution with history checking."""
    await page.goto(f"https://stripchat.com/{username}")

    # 1. Click the profile's Send PM button
    pm_button = page.locator("#user-actions-send-pm, button[aria-label='Send PM']")
    await pm_button.first.wait_for(state="visible", timeout=15000)
    await pm_button.first.click()

    # 2. Wait 3 seconds to let previous chat history load from the server
    _emit_log(log, f"[{username}] Checking for previous chat history...")
    await asyncio.sleep(3)

    # Check if there is already text inside the chat box container
    try:
        chat_container = page.locator(".content-messages__scroll-container")
        chat_text = await chat_container.first.inner_text()

        if chat_text and chat_text.strip():
            _emit_log(log, f"[{username}] Previous messages found! Skipping user.")
            return False # Returning False tells the loop to close the tab and skip
    except Exception:
        pass # If the container isn't found, assume it's empty and proceed

    # 3. Locate the precise Text Input Box
    chat_input = page.locator("textarea[placeholder='Private message...']")
    await chat_input.first.wait_for(state="visible", timeout=15000)
    await chat_input.first.click()

    # 4. Type the message with human delay
    await type_with_delay(chat_input.first, message)

    # 5. Locate and click the exact Send Button
    send_button = page.locator("button[aria-label='Send']")
    await send_button.first.wait_for(state="visible", timeout=5000)
    await send_button.first.click()

    # 6. Wait for the delivery checkmark next to the message we just sent
    checkmark = page.locator(".content-messages__scroll-container svg.icon-check-4")
    await checkmark.last.wait_for(state="visible", timeout=10000)
    _emit_log(log, f"[{username}] Message sent (checkmark confirmed).")
    return True


async def process_username(context, stealth, worksheet, username, message, log=None, on_status=None):
    """Runs one username's full DM flow in its own tab, as part of a concurrent batch."""
    _emit_log(log, f"--- Processing: {username} ---")
    _emit_status(on_status, username, "processing")
    user_page = await context.new_page()
    try:
        await stealth.apply_stealth_async(user_page)

        # send_dm returns True if sent, False if skipped due to history
        was_sent = await send_dm(user_page, username, message, log=log)

        if was_sent:
            await asyncio.to_thread(replace_and_mark_red, worksheet, username, message)
            _emit_log(log, f"[{username}] Marked red in Google Sheets.")
            _emit_status(on_status, username, "sent")
        else:
            _emit_status(on_status, username, "skipped")

    except Exception as e:
        logging.error(f"Failed to send DM to {username}: {str(e)}")
        _emit_log(log, f"Error processing {username}, skipping to next...")
        _emit_status(on_status, username, "error")
    finally:
        if not user_page.is_closed():
            await user_page.close()


async def _run_async(message, worksheet, log=None, on_status=None, stop_event=None):
    all_usernames = [name for name in worksheet.col_values(1)[1:] if name and name.strip()]

    if not all_usernames:
        _emit_log(log, "No usernames found in the Google Sheet.")
        return

    _emit_log(log, f"Found {len(all_usernames)} usernames to message.")

    async with playwright.async_api.async_playwright() as p:
        context = await p.chromium.launch_persistent_context(
            USER_DATA_DIR, headless=False, channel="chrome"
        )

        stealth = Stealth()

        try:
            for batch_start in range(0, len(all_usernames), TAB_BATCH_SIZE):
                if stop_event and stop_event.is_set():
                    _emit_log(log, "Stopped by user.")
                    break

                batch = all_usernames[batch_start:batch_start + TAB_BATCH_SIZE]
                _emit_log(log, f"--- Starting batch of {len(batch)} tabs ---")

                tasks = []
                for i, username in enumerate(batch):
                    if stop_event and stop_event.is_set():
                        break
                    if i > 0:
                        await asyncio.sleep(random.uniform(*RAMP_UP_DELAY_RANGE))
                    tasks.append(asyncio.create_task(
                        process_username(context, stealth, worksheet, username, message, log=log, on_status=on_status)
                    ))

                await asyncio.gather(*tasks)
        finally:
            await context.close()


def run(message, worksheet, log=None, on_status=None, stop_event=None):
    asyncio.run(_run_async(message, worksheet, log, on_status, stop_event))


if __name__ == "__main__":
    try:
        print("=== Stripchat Auto-DM Bot ===")
        # Dynamically ask for the message in the terminal before booting the browser
        custom_message = input("What message would you like to send to these users? \n> ")

        if not custom_message.strip():
            print("You must enter a message. Exiting script.")
        else:
            spreadsheet = get_spreadsheet()
            worksheet = choose_worksheet(spreadsheet)
            run(custom_message, worksheet)
            
    except Exception as e:
        print(f"Error: {e}")
        logging.exception("Runtime exception during bot execution")