import json
import os
import random
import sys
import tempfile
import time
from pathlib import Path

import gspread
from google.oauth2.service_account import Credentials as SACredentials
from google.oauth2.credentials import Credentials as UserCredentials
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseDownload
from playwright.sync_api import sync_playwright

# ── Config ────────────────────────────────────────────────────────────────────
GDRIVE_CREDS_JSON   = os.environ.get("GDRIVE_CREDENTIALS_JSON", "")
SHEET_CREDS_JSON    = os.environ.get("SHEET_CREDENTIALS_JSON", GDRIVE_CREDS_JSON)
SPREADSHEET_ID      = os.environ.get("SPREADSHEET_ID", "1QXCzLY_ygvJlCxdgDyh_oYkBxm5jSfDp56jtYQ1FVYw")
SHEET_TAB_NAME      = os.environ.get("SHEET_TAB_NAME", "threads")
CAPTION_COLUMN      = os.environ.get("CAPTION_COLUMN", "A")
THREAD_DELIMITER    = os.environ.get("THREAD_DELIMITER", "---")

SOURCE_FOLDER_ID    = os.environ.get("GDRIVE_SOURCE_FOLDER_ID", "")
CLAIMED_FOLDER_ID   = os.environ.get("GDRIVE_CLAIMED_FOLDER_ID", "")
USE_MEDIA           = os.environ.get("USE_MEDIA", "false").lower() == "true"
IMAGE_RATIO         = float(os.environ.get("IMAGE_RATIO", "0.6"))

STORAGE_STATE_PATH  = os.environ.get("X_STORAGE_STATE_PATH", "x_storage_state.json")
INTERVAL_MINUTES    = float(os.environ.get("INTERVAL_MINUTES", "10"))
INTERVAL_SECONDS    = int(INTERVAL_MINUTES * 60)
MAX_THREADS         = int(os.environ.get("MAX_THREADS", "0"))  # 0 = no cap, post all rows
SHUFFLE             = os.environ.get("SHUFFLE_ORDER", "false").lower() == "true"

IMAGE_MIMES = {"image/jpeg", "image/png", "image/gif", "image/webp"}
VIDEO_MIMES = {"video/mp4", "video/quicktime", "video/x-msvideo", "video/mpeg"}

SCREENSHOT_DIR = Path("debug_screenshots")
SCREENSHOT_DIR.mkdir(exist_ok=True)
step_counter = [0]


def dbg(msg):
    ts = time.strftime("%H:%M:%S")
    print(f"[{ts}] {msg}", flush=True)


def screenshot(page, label):
    step_counter[0] += 1
    name = f"{step_counter[0]:03d}_{label}.png"
    path = SCREENSHOT_DIR / name
    try:
        page.screenshot(path=str(path), full_page=False)
        dbg(f"  📸 Screenshot saved: {path}")
    except Exception as e:
        dbg(f"  📸 Screenshot failed ({label}): {e}")


# ── Google Sheets helpers ─────────────────────────────────────────────────────

def build_sheet_client():
    dbg("Authenticating to Google Sheets…")
    if not SHEET_CREDS_JSON.strip():
        sys.exit("SHEET_CREDENTIALS_JSON / GDRIVE_CREDENTIALS_JSON secret is empty.")
    creds_data = json.loads(SHEET_CREDS_JSON)
    scopes = ["https://www.googleapis.com/auth/spreadsheets",
              "https://www.googleapis.com/auth/drive"]

    if creds_data.get("type") == "service_account":
        creds = SACredentials.from_service_account_info(creds_data, scopes=scopes)
    else:
        creds = UserCredentials(
            token=creds_data.get("token"),
            refresh_token=creds_data.get("refresh_token"),
            token_uri=creds_data.get("token_uri", "https://oauth2.googleapis.com/token"),
            client_id=creds_data.get("client_id"),
            client_secret=creds_data.get("client_secret"),
            scopes=creds_data.get("scopes", scopes),
        )
    gc = gspread.authorize(creds)
    dbg("Sheets client ready.")
    return gc


def load_thread_rows(gc):
    sh = gc.open_by_key(SPREADSHEET_ID)
    ws = sh.worksheet(SHEET_TAB_NAME)
    dbg(f"Opened sheet '{SHEET_TAB_NAME}'.")
    col_index = ord(CAPTION_COLUMN.upper()) - ord("A") + 1
    values = ws.col_values(col_index)

    rows = []
    for i, raw in enumerate(values, start=1):
        if i == 1:
            continue  # header row ("captions")
        text = (raw or "").strip()
        if text:
            rows.append({"row_number": i, "text": text})
    dbg(f"Loaded {len(rows)} non-empty thread row(s) from column {CAPTION_COLUMN}.")
    return ws, rows


def delete_row(ws, row_number):
    dbg(f"  Deleting row {row_number} from sheet and saving…")
    try:
        ws.delete_rows(row_number)
        dbg(f"  ✓ Row {row_number} deleted (sheet auto-saves on each API write).")
    except Exception as e:
        dbg(f"  ⚠ WARNING: Failed to delete row {row_number}: {e}")
        dbg(f"  ⚠ This thread may be re-posted in a future run unless removed manually.")


def split_into_tweets(text, delimiter):
    parts = [p.strip() for p in text.split(delimiter)]
    parts = [p for p in parts if p]
    return parts if parts else [text.strip()]


# ── Google Drive media helpers (only used when USE_MEDIA=true) ───────────────

def build_drive_service():
    creds_data = json.loads(GDRIVE_CREDS_JSON)
    scopes = ["https://www.googleapis.com/auth/drive"]
    if creds_data.get("type") == "service_account":
        creds = SACredentials.from_service_account_info(creds_data, scopes=scopes)
    else:
        creds = UserCredentials(
            token=creds_data.get("token"),
            refresh_token=creds_data.get("refresh_token"),
            token_uri=creds_data.get("token_uri", "https://oauth2.googleapis.com/token"),
            client_id=creds_data.get("client_id"),
            client_secret=creds_data.get("client_secret"),
            scopes=creds_data.get("scopes", scopes),
        )
    return build("drive", "v3", credentials=creds)


def list_media_in_folder(service, folder_id):
    if not folder_id:
        return []
    query = (
        f"'{folder_id}' in parents and trashed=false and ("
        + " or ".join(f"mimeType='{m}'" for m in IMAGE_MIMES | VIDEO_MIMES)
        + ")"
    )
    results, page_token = [], None
    while True:
        resp = service.files().list(
            q=query, fields="nextPageToken, files(id, name, mimeType)",
            pageSize=200, pageToken=page_token,
        ).execute()
        results.extend(resp.get("files", []))
        page_token = resp.get("nextPageToken")
        if not page_token:
            break
    return results


def pick_one_media(files, image_ratio):
    images = [f for f in files if f["mimeType"] in IMAGE_MIMES]
    videos = [f for f in files if f["mimeType"] in VIDEO_MIMES]
    if not images and not videos:
        return None
    if images and videos:
        return random.choice(images) if random.random() < image_ratio else random.choice(videos)
    return random.choice(images or videos)


def download_file(service, file_id, file_name, dest_path):
    dbg(f"  Downloading '{file_name}' -> {dest_path}")
    request = service.files().get_media(fileId=file_id)
    with open(dest_path, "wb") as fh:
        downloader = MediaIoBaseDownload(fh, request)
        done = False
        while not done:
            _, done = downloader.next_chunk()
    dbg(f"  Download complete.")


def move_to_claimed(service, file_id, file_name):
    if not CLAIMED_FOLDER_ID:
        return
    try:
        meta = service.files().get(fileId=file_id, fields="parents").execute()
        prev = ",".join(meta.get("parents", []))
        service.files().update(
            fileId=file_id, addParents=CLAIMED_FOLDER_ID,
            removeParents=prev, fields="id, parents",
        ).execute()
        dbg(f"  ✓ Moved '{file_name}' -> claimed folder.")
    except Exception as e:
        dbg(f"  ⚠ WARNING: Failed to move '{file_name}' to claimed folder: {e}")


# ── X / Playwright posting (same robust selectors/strategies as bulk script) ──

TEXTBOX_SELECTORS = [
    '[data-testid="tweetTextarea_0"]',
    '[data-testid="tweetTextarea_0EditorContainer"] div[contenteditable="true"]',
    'div[contenteditable="true"][data-testid]',
    'div[contenteditable="true"][aria-label]',
    'div[contenteditable="true"]',
    '[aria-label="Post text"]',
    '[aria-label="Tweet text"]',
    '[placeholder="What is happening?!"]',
    '[placeholder*="happening"]',
]

POST_BUTTON_SELECTORS = [
    '[data-testid="tweetButton"]',
    '[data-testid="tweetButtonInline"]',
    'button[data-testid*="tweet"]',
    'div[data-testid="tweetButton"]',
    'button:has-text("Post")',
    'button:has-text("Tweet")',
    '[aria-label="Post"]',
    '[aria-label="Tweet"]',
]

ADD_THREAD_TWEET_SELECTORS = [
    '[data-testid="addButton"]',
    'div[aria-label="Add post"]',
    'button[aria-label="Add post"]',
]

PREVIEW_SELECTORS = [
    '[data-testid="attachments"] video',
    '[data-testid="videoComponent"]',
    '[data-testid="tweetPhoto"]',
    '[data-testid="attachments"] img',
    '[data-testid="attachments"] [role="progressbar"]',
    '[data-testid="attachments"]',
    'img[src*="blob:"]',
    'video[src*="blob:"]',
]


def find_element_multi(page, selectors, label, timeout=15_000):
    for sel in selectors:
        try:
            el = page.locator(sel).first
            el.wait_for(state="visible", timeout=timeout // len(selectors))
            dbg(f"    ✓ Found {label} via: {sel}")
            return el
        except Exception:
            dbg(f"    ✗ Selector failed for {label}: {sel}")
    return None


def _textbox_visible(page):
    for sel in TEXTBOX_SELECTORS[:3]:
        try:
            if page.locator(sel).first.is_visible():
                return True
        except Exception:
            pass
    return False


def navigate_to_compose(page, thread_index, attempt=1):
    urls = ["https://x.com/compose/post", "https://twitter.com/compose/tweet"]
    for url in urls:
        dbg(f"  [NAV] Attempt {attempt}: navigating to {url}")
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=30_000)
            page.wait_for_timeout(3_000)
            current = page.url
            screenshot(page, f"t{thread_index}_nav_attempt{attempt}")
            if "login" in current or "signin" in current:
                raise RuntimeError("Redirected to login — session expired. Re-run login_and_save_session.py.")
            if "graduated-access" in current:
                raise RuntimeError("Account redirected to graduated-access restriction page.")
            if "compose" in current or _textbox_visible(page):
                return True
        except RuntimeError:
            raise
        except Exception as e:
            dbg(f"  [NAV] Navigation to {url} failed: {e}")

    dbg("  [NAV] Trying home → compose button fallback…")
    try:
        page.goto("https://x.com/home", wait_until="domcontentloaded", timeout=30_000)
        page.wait_for_timeout(3_000)
        btn = page.locator(
            'a[href="/compose/post"], [data-testid="SideNav_NewTweet_Button"], '
            '[aria-label="Post"], a[aria-label*="compose"]'
        ).first
        btn.wait_for(state="visible", timeout=10_000)
        btn.click()
        page.wait_for_timeout(3_000)
        if _textbox_visible(page):
            return True
    except Exception as e:
        dbg(f"  [NAV] Home fallback failed: {e}")
    return False


def type_into_textbox(page, textbox_locator, text):
    textbox_locator.scroll_into_view_if_needed()
    textbox_locator.click()
    page.wait_for_timeout(400)
    page.keyboard.type(text, delay=15)
    page.wait_for_timeout(400)


def attach_media_to_current_tweet(page, media_path, mime_type, label, timeout=120_000):
    file_inputs = page.locator('input[type="file"]')
    count = file_inputs.count()
    if count == 0:
        dbg(f"  [ATTACH-{label}] No file input found.")
        return False
    try:
        file_inputs.last.set_input_files(media_path)
    except Exception as e:
        dbg(f"  [ATTACH-{label}] set_input_files failed: {e}")
        return False

    for sel in PREVIEW_SELECTORS:
        try:
            page.wait_for_selector(sel, timeout=timeout // len(PREVIEW_SELECTORS))
            try:
                page.wait_for_selector('[role="progressbar"]', state="detached", timeout=timeout)
            except Exception:
                pass
            page.wait_for_timeout(1_000)
            dbg(f"  [ATTACH-{label}] Media attached and preview confirmed.")
            return True
        except Exception:
            continue
    dbg(f"  [ATTACH-{label}] No preview detected after attach.")
    return False


def add_thread_tweet_box(page, thread_index, tweet_index):
    """Click the '+' button to add another tweet to the thread, return the new textbox locator."""
    dbg(f"  [THREAD] Adding tweet #{tweet_index + 1} to thread…")
    btn = find_element_multi(page, ADD_THREAD_TWEET_SELECTORS, "add-thread-tweet button", timeout=10_000)
    if btn is None:
        screenshot(page, f"t{thread_index}_no_add_button")
        raise RuntimeError("Could not find the 'Add post' (+) button to extend the thread.")
    btn.click()
    page.wait_for_timeout(800)
    sel = f'[data-testid="tweetTextarea_{tweet_index}"]'
    locator = page.locator(sel).first
    try:
        locator.wait_for(state="visible", timeout=10_000)
        return locator
    except Exception:
        # fall back to generic contenteditable scan, pick the last one
        all_ce = page.locator('div[contenteditable="true"][data-testid]')
        return all_ce.nth(all_ce.count() - 1)


def click_post_button(page, thread_index):
    post_btn = find_element_multi(page, POST_BUTTON_SELECTORS, "post button", timeout=15_000)
    if post_btn is None:
        screenshot(page, f"t{thread_index}_no_post_button")
        raise RuntimeError("Post button not found with any known selector.")
    if post_btn.is_disabled():
        for _ in range(3):
            page.wait_for_timeout(5_000)
            if not post_btn.is_disabled():
                break
    try:
        post_btn.click()
    except Exception:
        page.evaluate(
            """(sel) => { for (const s of sel) { const el = document.querySelector(s); if (el) { el.click(); return s; } } return null; }""",
            POST_BUTTON_SELECTORS,
        )


def post_with_network_confirmation(page, thread_index, click_timeout=25_000):
    """
    Confirms via the CreateTweet GraphQL response (single tweet) — for true
    multi-tweet threads, X fires CreateTweet once per tweet in the thread
    chain; we wait for the LAST one (the response carrying the final tweet
    in the chain) before declaring success, since that's what closes compose.
    """
    result = {"sent": None, "tweet_id": None, "responses_seen": 0}

    def on_response(response):
        if "CreateTweet" not in response.url:
            return
        try:
            data = response.json()
        except Exception:
            return
        if isinstance(data, dict) and data.get("errors"):
            dbg(f"  [NET-CONFIRM] CreateTweet returned errors: {data['errors']}")
            result["sent"] = False
            return
        try:
            tweet_id = data["data"]["create_tweet"]["tweet_results"]["result"]["rest_id"]
            result["responses_seen"] += 1
            dbg(f"  [NET-CONFIRM] CreateTweet #{result['responses_seen']} succeeded, tweet_id={tweet_id}")
            result["sent"] = True
            result["tweet_id"] = tweet_id
        except (KeyError, TypeError):
            dbg("  [NET-CONFIRM] Unexpected CreateTweet response shape — unconfirmed.")
            result["sent"] = False

    page.on("response", on_response)
    try:
        click_post_button(page, thread_index)
        dbg("  [NET-CONFIRM] Waiting for CreateTweet network response(s)…")
        waited = 0
        # give threads extra time since multiple CreateTweet calls fire in sequence
        while waited < click_timeout:
            page.wait_for_timeout(500)
            waited += 500
            if result["sent"] is False:
                break
        page.wait_for_timeout(1_500)  # settle window for any trailing chain calls
    finally:
        page.remove_listener("response", on_response)

    screenshot(page, f"t{thread_index}_after_click")
    if result["sent"]:
        return True, result["tweet_id"]
    return False, None


def post_thread(page, tweets, media_path, mime_type, thread_index, max_attempts=3):
    for attempt in range(1, max_attempts + 1):
        dbg(f"  ─── Thread attempt {attempt}/{max_attempts} ({len(tweets)} tweet(s)) ───")
        try:
            navigate_to_compose(page, thread_index, attempt)

            first_box = find_element_multi(page, TEXTBOX_SELECTORS, "textbox", timeout=20_000)
            if first_box is None:
                raise RuntimeError("Could not find first tweet textbox.")
            type_into_textbox(page, first_box, tweets[0])
            screenshot(page, f"t{thread_index}_a{attempt}_tweet1_typed")

            if media_path:
                ok = attach_media_to_current_tweet(page, media_path, mime_type, "first")
                if not ok:
                    dbg("  Media attach unconfirmed — continuing anyway (text-only fallback risk).")

            for idx in range(1, len(tweets)):
                box = add_thread_tweet_box(page, thread_index, idx)
                type_into_textbox(page, box, tweets[idx])
                screenshot(page, f"t{thread_index}_a{attempt}_tweet{idx+1}_typed")

            sent, tweet_id = post_with_network_confirmation(page, thread_index)
            if sent:
                dbg(f"  Thread #{thread_index} SUCCESS on attempt {attempt} (last tweet_id={tweet_id}).")
                return True
            dbg(f"  Thread #{thread_index}: not confirmed via network — retrying.")
            if attempt < max_attempts:
                page.wait_for_timeout(5_000)

        except RuntimeError as e:
            dbg(f"  [ATTEMPT {attempt}] RuntimeError: {e}")
            if "login" in str(e).lower() or "session" in str(e).lower() or "restriction" in str(e).lower() or "graduated-access" in str(e).lower():
                raise
            if attempt < max_attempts:
                page.wait_for_timeout(10_000)
            else:
                return False
        except Exception as e:
            dbg(f"  [ATTEMPT {attempt}] Unexpected error: {e}")
            try:
                screenshot(page, f"t{thread_index}_a{attempt}_exception")
            except Exception:
                pass
            if attempt < max_attempts:
                page.wait_for_timeout(10_000)
            else:
                return False
    return False


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    dbg("=" * 60)
    dbg("Starting post_threads_from_sheet_to_x.py")
    dbg(f"SPREADSHEET_ID   : {SPREADSHEET_ID}")
    dbg(f"SHEET_TAB_NAME   : {SHEET_TAB_NAME}")
    dbg(f"CAPTION_COLUMN   : {CAPTION_COLUMN}")
    dbg(f"THREAD_DELIMITER : '{THREAD_DELIMITER}'")
    dbg(f"USE_MEDIA        : {USE_MEDIA}")
    dbg(f"IMAGE_RATIO      : {IMAGE_RATIO}")
    dbg(f"INTERVAL_MINUTES : {INTERVAL_MINUTES}")
    dbg(f"MAX_THREADS      : {MAX_THREADS or '(no cap — all rows)'}")
    dbg("=" * 60)

    gc = build_sheet_client()
    ws, rows = load_thread_rows(gc)
    if not rows:
        dbg("No thread rows found in sheet. Nothing to do.")
        return

    if SHUFFLE:
        random.shuffle(rows)
    if MAX_THREADS > 0:
        rows = rows[:MAX_THREADS]

    drive_service = None
    media_files = []
    if USE_MEDIA:
        if not SOURCE_FOLDER_ID:
            dbg("USE_MEDIA=true but GDRIVE_SOURCE_FOLDER_ID not set — disabling media.")
        else:
            drive_service = build_drive_service()
            media_files = list_media_in_folder(drive_service, SOURCE_FOLDER_ID)
            dbg(f"Found {len(media_files)} media file(s) available for attaching.")

    dbg(f"Will attempt {len(rows)} thread(s) this run.")
    est = (len(rows) - 1) * INTERVAL_MINUTES if len(rows) > 1 else 0
    dbg(f"Estimated run time: ~{est:.0f} min.")

    results_summary = []

    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=True,
            args=[
                "--no-sandbox", "--disable-setuid-sandbox",
                "--disable-blink-features=AutomationControlled",
                "--disable-web-security",
                "--disable-features=IsolateOrigins,site-per-process",
            ],
        )
        context = browser.new_context(
            storage_state=STORAGE_STATE_PATH,
            viewport={"width": 1280, "height": 900},
            user_agent=(
                "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
            ),
            permissions=["notifications"],
        )
        context.add_init_script("""
            Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
            Object.defineProperty(navigator, 'plugins', { get: () => [1,2,3,4,5] });
        """)
        page = context.new_page()
        page.on("console", lambda msg: dbg(f"  [BROWSER {msg.type.upper()}] {msg.text[:200]}"))
        page.on("requestfailed", lambda req: dbg(f"  [NET FAIL] {req.url[:100]}"))

        for i, row in enumerate(rows, start=1):
            row_number = row["row_number"]
            tweets = split_into_tweets(row["text"], THREAD_DELIMITER)
            dbg("")
            dbg(f"{'='*50}")
            dbg(f"THREAD {i}/{len(rows)}  (sheet row {row_number}, {len(tweets)} tweet(s))")
            dbg(f"{'='*50}")
            for j, t in enumerate(tweets, start=1):
                dbg(f"  Tweet {j}: {t[:80]}{'…' if len(t) > 80 else ''}")

            media_path, mime_type, media_file = None, None, None
            if USE_MEDIA and media_files:
                media_file = pick_one_media(media_files, IMAGE_RATIO)
                if media_file:
                    ext = Path(media_file["name"]).suffix or (
                        ".mp4" if media_file["mimeType"] in VIDEO_MIMES else ".jpg"
                    )
                    with tempfile.NamedTemporaryFile(suffix=ext, delete=False) as tmp:
                        media_path = tmp.name
                    mime_type = media_file["mimeType"]
                    download_file(drive_service, media_file["id"], media_file["name"], media_path)

            posted = False
            try:
                posted = post_thread(page, tweets, media_path, mime_type, thread_index=i)

                if posted:
                    dbg(f"THREAD {i}/{len(rows)}: SUCCESS ✓ — removing row {row_number} from sheet.")
                    delete_row(ws, row_number)
                    results_summary.append((i, row_number, "SUCCESS"))
                    if media_file:
                        media_files = [f for f in media_files if f["id"] != media_file["id"]]
                        move_to_claimed(drive_service, media_file["id"], media_file["name"])
                else:
                    dbg(f"THREAD {i}/{len(rows)}: FAILED after retries — row {row_number} left in sheet.")
                    results_summary.append((i, row_number, "FAILED"))

            except RuntimeError as e:
                dbg(f"THREAD {i}/{len(rows)}: FATAL — {e}")
                results_summary.append((i, row_number, f"FATAL: {e}"))
                msg = str(e).lower()
                if "login" in msg or "session" in msg or "graduated-access" in msg or "restriction" in msg:
                    dbg("Session expired or account restricted — aborting run.")
                    break

            except Exception as e:
                dbg(f"THREAD {i}/{len(rows)}: EXCEPTION — {e}")
                results_summary.append((i, row_number, f"EXCEPTION: {e}"))

            finally:
                if media_path:
                    try:
                        Path(media_path).unlink(missing_ok=True)
                    except Exception:
                        pass

            if i < len(rows):
                dbg(f"Sleeping {INTERVAL_SECONDS}s ({INTERVAL_MINUTES} min) before next thread…")
                time.sleep(INTERVAL_SECONDS)

        browser.close()

    dbg("")
    dbg("=" * 60)
    dbg("Run complete. Summary:")
    for idx, row_number, status in results_summary:
        dbg(f"  Thread {idx:>2} (row {row_number:>3}): [{status}]")
    dbg(f"Debug screenshots saved in: {SCREENSHOT_DIR.resolve()}")
    dbg("=" * 60)


if __name__ == "__main__":
    main()
