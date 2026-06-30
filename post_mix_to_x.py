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
SHEET_CREDS_JSON    = os.environ.get("SHEET_CREDENTIALS_JSON", "") or GDRIVE_CREDS_JSON

SPREADSHEET_ID      = os.environ.get("SPREADSHEET_ID", "1QXCzLY_ygvJlCxdgDyh_oYkBxm5jSfDp56jtYQ1FVYw")
SHEET_TAB_NAME      = os.environ.get("SHEET_TAB_NAME", "threads")
CAPTION_COLUMN      = os.environ.get("CAPTION_COLUMN", "A")
THREAD_DELIMITER    = os.environ.get("THREAD_DELIMITER", "---")

SOURCE_FOLDER_ID    = os.environ.get("GDRIVE_SOURCE_FOLDER_ID", "")
CLAIMED_FOLDER_ID   = os.environ.get("GDRIVE_CLAIMED_FOLDER_ID", "")

CSV_PATH            = os.environ.get("POSTS_CSV_PATH", "table.csv")
CAPTION_SOURCE      = os.environ.get("CAPTION_SOURCE", "csv").strip().lower()  # csv|custom — used for image/video posts
CUSTOM_CAPTION_RAW  = os.environ.get("CUSTOM_CAPTION", "")
LINKS_RAW           = os.environ.get("LINKS", "")

POST_COUNT          = int(os.environ.get("POST_COUNT", "5"))
IMAGE_PERCENT       = float(os.environ.get("IMAGE_PERCENT", "40"))
VIDEO_PERCENT       = float(os.environ.get("VIDEO_PERCENT", "30"))
THREAD_PERCENT      = float(os.environ.get("THREAD_PERCENT", "30"))

INTERVAL_MINUTES    = float(os.environ.get("INTERVAL_MINUTES", "10"))
INTERVAL_SECONDS    = int(INTERVAL_MINUTES * 60)
SHUFFLE             = os.environ.get("SHUFFLE_ORDER", "false").lower() == "true"
STORAGE_STATE_PATH  = os.environ.get("X_STORAGE_STATE_PATH", "x_storage_state.json")

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


# ── Credential helper (shared by Drive + Sheets) ──────────────────────────────

def build_creds(creds_json_str, scopes):
    if not creds_json_str.strip():
        sys.exit("Required credentials secret is empty (check GDRIVE_CREDENTIALS_JSON / SHEET_CREDENTIALS_JSON in repo Settings > Secrets).")
    data = json.loads(creds_json_str)
    if data.get("type") == "service_account":
        return SACredentials.from_service_account_info(data, scopes=scopes)
    return UserCredentials(
        token=data.get("token"),
        refresh_token=data.get("refresh_token"),
        token_uri=data.get("token_uri", "https://oauth2.googleapis.com/token"),
        client_id=data.get("client_id"),
        client_secret=data.get("client_secret"),
        scopes=data.get("scopes", scopes),
    )


# ── Google Drive helpers ──────────────────────────────────────────────────────

def build_drive_service():
    dbg("Building Google Drive service…")
    creds = build_creds(GDRIVE_CREDS_JSON, ["https://www.googleapis.com/auth/drive"])
    return build("drive", "v3", credentials=creds)


def list_media_in_folder(service, folder_id, mimes):
    if not folder_id or not mimes:
        return []
    dbg(f"Listing media in Drive folder: {folder_id} (mimes={mimes})")
    query = (
        f"'{folder_id}' in parents and trashed=false and ("
        + " or ".join(f"mimeType='{m}'" for m in mimes)
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
    dbg(f"  Found {len(results)} file(s).")
    return results


def download_file(service, file_id, file_name, dest_path):
    dbg(f"  Downloading '{file_name}' -> {dest_path}")
    request = service.files().get_media(fileId=file_id)
    with open(dest_path, "wb") as fh:
        downloader = MediaIoBaseDownload(fh, request)
        done = False
        while not done:
            status, done = downloader.next_chunk()
            if status:
                dbg(f"    Download progress: {int(status.progress()*100)}%")
    dbg("  Download complete.")


def move_to_claimed(service, file_id, file_name):
    if not CLAIMED_FOLDER_ID:
        dbg("  GDRIVE_CLAIMED_FOLDER_ID not set — skipping move.")
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
        dbg(f"  ⚠ WARNING: Failed to move '{file_name}': {e}")


# ── Google Sheets helpers (threads) ───────────────────────────────────────────

def build_sheet_client():
    dbg("Authenticating to Google Sheets…")
    creds = build_creds(SHEET_CREDS_JSON, [
        "https://www.googleapis.com/auth/spreadsheets",
        "https://www.googleapis.com/auth/drive",
    ])
    return gspread.authorize(creds)


def load_thread_rows(gc):
    sh = gc.open_by_key(SPREADSHEET_ID)
    try:
        ws = sh.worksheet(SHEET_TAB_NAME)
    except gspread.exceptions.WorksheetNotFound:
        available = [w.title for w in sh.worksheets()]
        dbg(f"Tab '{SHEET_TAB_NAME}' not found. Available tabs: {available}")
        sys.exit(
            f"SHEET_TAB_NAME '{SHEET_TAB_NAME}' does not exist in this spreadsheet. "
            f"Available tabs: {available}. Set sheet_tab_name to one of these when running the workflow."
        )
    col_index = ord(CAPTION_COLUMN.upper()) - ord("A") + 1
    values = ws.col_values(col_index)
    rows = []
    for i, raw in enumerate(values, start=1):
        if i == 1:
            continue
        text = (raw or "").strip()
        if text:
            rows.append({"row_number": i, "text": text})
    dbg(f"Loaded {len(rows)} thread row(s) from sheet column {CAPTION_COLUMN}.")
    return ws, rows


def delete_row(ws, row_number):
    dbg(f"  Deleting row {row_number} from sheet…")
    try:
        ws.delete_rows(row_number)
        dbg(f"  ✓ Row {row_number} deleted and saved.")
    except Exception as e:
        dbg(f"  ⚠ WARNING: Failed to delete row {row_number}: {e}")


def split_into_tweets(text, delimiter):
    parts = [p.strip() for p in text.split(delimiter)]
    parts = [p for p in parts if p]
    return parts if parts else [text.strip()]


# ── Caption helpers (for image/video posts) ──────────────────────────────────

def load_caption_rows(path):
    import csv
    with open(path, newline="", encoding="utf-8") as f:
        rows = [r for r in csv.DictReader(f) if r.get("Caption", "").strip()]
    if not rows:
        sys.exit(f"No caption rows found in {path}")
    dbg(f"Loaded {len(rows)} caption rows from {path}")
    return rows


def build_text_csv(row, link=""):
    parts = [row["Action Caption"].strip(), row["Caption"].strip(), "", row["Hashtags"].strip()]
    text = "\n".join(parts)
    if link:
        text += f"\n\n{link}"
    return text


def build_text_custom(raw, link=""):
    text = raw.replace("\\n", "\n").strip()
    if link:
        text += f"\n\n{link}"
    return text


# ── X / Playwright shared selectors ───────────────────────────────────────────

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


def navigate_to_compose(page, post_index, attempt=1):
    for url in ["https://x.com/compose/post", "https://twitter.com/compose/tweet"]:
        dbg(f"  [NAV] Attempt {attempt}: navigating to {url}")
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=30_000)
            page.wait_for_timeout(3_000)
            current = page.url
            screenshot(page, f"p{post_index}_nav_attempt{attempt}")
            if "login" in current or "signin" in current:
                raise RuntimeError("Redirected to login — session expired.")
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


def type_into_textbox(page, locator, text):
    locator.scroll_into_view_if_needed()
    locator.click()
    page.wait_for_timeout(400)
    page.keyboard.type(text, delay=15)
    page.wait_for_timeout(400)


def attach_media_robust(page, media_path, mime_type, post_index):
    is_video = mime_type in VIDEO_MIMES
    upload_timeout = 120_000 if is_video else 45_000
    file_inputs = page.locator('input[type="file"]')
    count = file_inputs.count()
    dbg(f"  [ATTACH] Found {count} file input(s).")
    if count > 0:
        for idx in range(count):
            try:
                file_inputs.nth(idx).set_input_files(media_path)
                if _wait_for_preview(page, upload_timeout, post_index, f"slot{idx}"):
                    return True
            except Exception as e:
                dbg(f"  [ATTACH] input #{idx} failed: {e}")

    dbg("  [ATTACH] Trying media toolbar button…")
    for btn_sel in ['[data-testid="addMedia"]', '[aria-label*="edia"]', '[aria-label*="hoto"]']:
        try:
            btn = page.locator(btn_sel).first
            if btn.is_visible():
                btn.click()
                page.wait_for_timeout(1_000)
                fi2 = page.locator('input[type="file"]')
                if fi2.count() > 0:
                    fi2.first.set_input_files(media_path)
                    if _wait_for_preview(page, upload_timeout, post_index, "toolbar"):
                        return True
        except Exception as e:
            dbg(f"  [ATTACH] toolbar btn {btn_sel} failed: {e}")

    screenshot(page, f"p{post_index}_attach_failed")
    raise RuntimeError("All media attachment strategies failed.")


def _wait_for_preview(page, timeout, post_index, label):
    for sel in PREVIEW_SELECTORS:
        try:
            page.wait_for_selector(sel, timeout=timeout // len(PREVIEW_SELECTORS))
            try:
                page.wait_for_selector('[role="progressbar"]', state="detached", timeout=timeout)
            except Exception:
                pass
            page.wait_for_timeout(1_200)
            screenshot(page, f"p{post_index}_preview_{label}")
            return True
        except Exception:
            continue
    return False


def add_thread_tweet_box(page, post_index, tweet_index):
    dbg(f"  [THREAD] Adding tweet #{tweet_index + 1}…")
    btn = find_element_multi(page, ADD_THREAD_TWEET_SELECTORS, "add-thread-tweet button", timeout=10_000)
    if btn is None:
        screenshot(page, f"p{post_index}_no_add_button")
        raise RuntimeError("Could not find the 'Add post' (+) button to extend the thread.")
    btn.click()
    page.wait_for_timeout(800)
    locator = page.locator(f'[data-testid="tweetTextarea_{tweet_index}"]').first
    try:
        locator.wait_for(state="visible", timeout=10_000)
        return locator
    except Exception:
        all_ce = page.locator('div[contenteditable="true"][data-testid]')
        return all_ce.nth(all_ce.count() - 1)


def click_post_button(page, post_index):
    post_btn = find_element_multi(page, POST_BUTTON_SELECTORS, "post button", timeout=15_000)
    if post_btn is None:
        screenshot(page, f"p{post_index}_no_post_button")
        raise RuntimeError("Post button not found.")
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


def post_with_network_confirmation(page, post_index, click_timeout=25_000):
    result = {"sent": None, "tweet_id": None}

    def on_response(response):
        if "CreateTweet" not in response.url:
            return
        try:
            data = response.json()
        except Exception:
            return
        if isinstance(data, dict) and data.get("errors"):
            dbg(f"  [NET-CONFIRM] CreateTweet errors: {data['errors']}")
            result["sent"] = False
            return
        try:
            tweet_id = data["data"]["create_tweet"]["tweet_results"]["result"]["rest_id"]
            dbg(f"  [NET-CONFIRM] CreateTweet succeeded, tweet_id={tweet_id}")
            result["sent"] = True
            result["tweet_id"] = tweet_id
        except (KeyError, TypeError):
            result["sent"] = False

    page.on("response", on_response)
    try:
        click_post_button(page, post_index)
        waited = 0
        while waited < click_timeout:
            page.wait_for_timeout(500)
            waited += 500
            if result["sent"] is False:
                break
        page.wait_for_timeout(1_500)
    finally:
        page.remove_listener("response", on_response)

    screenshot(page, f"p{post_index}_after_click")
    if result["sent"]:
        return True, result["tweet_id"]
    return False, None


def post_one_job(page, job, post_index, max_attempts=3):
    """
    job = {
      "kind": "image" | "video" | "thread",
      "tweets": [str, ...],          # 1+ tweets (thread or single-tweet image/video caption)
      "media_path": str|None,
      "mime_type": str|None,
    }
    """
    for attempt in range(1, max_attempts + 1):
        dbg(f"  ─── [{job['kind'].upper()}] attempt {attempt}/{max_attempts} ───")
        try:
            navigate_to_compose(page, post_index, attempt)

            first_box = find_element_multi(page, TEXTBOX_SELECTORS, "textbox", timeout=20_000)
            if first_box is None:
                raise RuntimeError("Could not find first tweet textbox.")
            type_into_textbox(page, first_box, job["tweets"][0])
            screenshot(page, f"p{post_index}_a{attempt}_tweet1_typed")

            if job["media_path"]:
                attach_media_robust(page, job["media_path"], job["mime_type"], post_index)

            for idx in range(1, len(job["tweets"])):
                box = add_thread_tweet_box(page, post_index, idx)
                type_into_textbox(page, box, job["tweets"][idx])
                screenshot(page, f"p{post_index}_a{attempt}_tweet{idx+1}_typed")

            sent, tweet_id = post_with_network_confirmation(page, post_index)
            if sent:
                dbg(f"  Post #{post_index} SUCCESS on attempt {attempt} (tweet_id={tweet_id}).")
                return True
            dbg(f"  Post #{post_index}: not confirmed — retrying.")
            if attempt < max_attempts:
                page.wait_for_timeout(5_000)

        except RuntimeError as e:
            dbg(f"  [ATTEMPT {attempt}] RuntimeError: {e}")
            if any(k in str(e).lower() for k in ("login", "session", "restriction", "graduated-access")):
                raise
            if attempt < max_attempts:
                page.wait_for_timeout(10_000)
            else:
                return False
        except Exception as e:
            dbg(f"  [ATTEMPT {attempt}] Unexpected error: {e}")
            try:
                screenshot(page, f"p{post_index}_a{attempt}_exception")
            except Exception:
                pass
            if attempt < max_attempts:
                page.wait_for_timeout(10_000)
            else:
                return False
    return False


# ── Job planning: turn percentages into a concrete list of posts ─────────────

def compute_type_counts(post_count, image_pct, video_pct, thread_pct):
    total_pct = image_pct + video_pct + thread_pct
    if total_pct <= 0:
        sys.exit("All three percentages (image/video/thread) are 0 — nothing to post.")
    img_frac = image_pct / total_pct
    vid_frac = video_pct / total_pct
    thr_frac = thread_pct / total_pct

    n_images = round(post_count * img_frac)
    n_videos = round(post_count * vid_frac)
    n_threads = post_count - n_images - n_videos
    if n_threads < 0:
        n_threads = 0

    # zero-out types explicitly disabled by the user (percent == 0)
    if image_pct <= 0:
        n_images = 0
    if video_pct <= 0:
        n_videos = 0
    if thread_pct <= 0:
        n_threads = 0

    dbg(f"Normalized mix -> images:{img_frac:.0%} video:{vid_frac:.0%} thread:{thr_frac:.0%}")
    dbg(f"Planned counts -> images:{n_images} videos:{n_videos} threads:{n_threads} (target total {post_count})")
    return n_images, n_videos, n_threads


def main():
    dbg("=" * 60)
    dbg("Starting post_mix_to_x.py")
    dbg(f"POST_COUNT      : {POST_COUNT}")
    dbg(f"IMAGE_PERCENT   : {IMAGE_PERCENT}")
    dbg(f"VIDEO_PERCENT   : {VIDEO_PERCENT}")
    dbg(f"THREAD_PERCENT  : {THREAD_PERCENT}")
    dbg(f"INTERVAL_MINUTES: {INTERVAL_MINUTES}")
    dbg(f"SPREADSHEET_ID  : {SPREADSHEET_ID}")
    dbg("=" * 60)

    n_images, n_videos, n_threads = compute_type_counts(
        POST_COUNT, IMAGE_PERCENT, VIDEO_PERCENT, THREAD_PERCENT
    )

    drive_service = None
    if n_images or n_videos:
        drive_service = build_drive_service()

    image_files, video_files = [], []
    if n_images:
        image_files = list_media_in_folder(drive_service, SOURCE_FOLDER_ID, IMAGE_MIMES)
        if len(image_files) < n_images:
            dbg(f"⚠ Only {len(image_files)} image(s) available — capping image posts.")
            n_images = len(image_files)
    if n_videos:
        video_files = list_media_in_folder(drive_service, SOURCE_FOLDER_ID, VIDEO_MIMES)
        if len(video_files) < n_videos:
            dbg(f"⚠ Only {len(video_files)} video(s) available — capping video posts.")
            n_videos = len(video_files)

    caption_rows = None
    if n_images or n_videos:
        if CAPTION_SOURCE == "custom":
            if not CUSTOM_CAPTION_RAW.strip():
                sys.exit("CAPTION_SOURCE=custom but CUSTOM_CAPTION is empty.")
        else:
            caption_rows = load_caption_rows(CSV_PATH)
    links = [l.strip() for l in LINKS_RAW.split(",") if l.strip()]

    gc, ws, thread_rows = None, None, []
    if n_threads:
        gc = build_sheet_client()
        ws, thread_rows = load_thread_rows(gc)
        if len(thread_rows) < n_threads:
            dbg(f"⚠ Only {len(thread_rows)} thread row(s) available — capping thread posts.")
            n_threads = len(thread_rows)
        if SHUFFLE:
            random.shuffle(thread_rows)
        thread_rows = thread_rows[:n_threads]

    chosen_images = random.sample(image_files, n_images) if n_images else []
    chosen_videos = random.sample(video_files, n_videos) if n_videos else []

    jobs = []
    for f in chosen_images:
        jobs.append({"kind": "image", "drive_file": f})
    for f in chosen_videos:
        jobs.append({"kind": "video", "drive_file": f})
    for row in thread_rows:
        jobs.append({"kind": "thread", "sheet_row": row})

    if SHUFFLE:
        random.shuffle(jobs)

    if not jobs:
        dbg("No jobs to run after capping against availability. Exiting.")
        return

    dbg(f"Final plan: {len(jobs)} post(s) -> images:{n_images} videos:{n_videos} threads:{n_threads}")

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

        for i, job in enumerate(jobs, start=1):
            dbg("")
            dbg(f"{'='*50}")
            dbg(f"POST {i}/{len(jobs)}  kind={job['kind']}")
            dbg(f"{'='*50}")

            media_path, mime_type, drive_file = None, None, None
            tweets = []
            row_number = None
            tmp_to_clean = None

            try:
                if job["kind"] in ("image", "video"):
                    drive_file = job["drive_file"]
                    mime_type = drive_file["mimeType"]
                    link = links[i - 1] if i - 1 < len(links) else ""
                    if CAPTION_SOURCE == "custom":
                        text = build_text_custom(CUSTOM_CAPTION_RAW, link)
                    else:
                        text = build_text_csv(random.choice(caption_rows), link)
                    tweets = [text]

                    ext = Path(drive_file["name"]).suffix or (".mp4" if job["kind"] == "video" else ".jpg")
                    with tempfile.NamedTemporaryFile(suffix=ext, delete=False) as tmp:
                        media_path = tmp.name
                    tmp_to_clean = media_path
                    download_file(drive_service, drive_file["id"], drive_file["name"], media_path)

                else:  # thread
                    row = job["sheet_row"]
                    row_number = row["row_number"]
                    tweets = split_into_tweets(row["text"], THREAD_DELIMITER)

                for j, t in enumerate(tweets, start=1):
                    dbg(f"  Tweet {j}: {t[:80]}{'…' if len(t) > 80 else ''}")

                job_payload = {"kind": job["kind"], "tweets": tweets, "media_path": media_path, "mime_type": mime_type}
                posted = post_one_job(page, job_payload, post_index=i)

                if posted:
                    dbg(f"POST {i}/{len(jobs)}: SUCCESS ✓")
                    results_summary.append((i, job["kind"], "SUCCESS"))
                    if job["kind"] in ("image", "video"):
                        move_to_claimed(drive_service, drive_file["id"], drive_file["name"])
                    else:
                        delete_row(ws, row_number)
                else:
                    dbg(f"POST {i}/{len(jobs)}: FAILED after retries.")
                    results_summary.append((i, job["kind"], "FAILED"))

            except RuntimeError as e:
                dbg(f"POST {i}/{len(jobs)}: FATAL — {e}")
                results_summary.append((i, job["kind"], f"FATAL: {e}"))
                if any(k in str(e).lower() for k in ("login", "session", "restriction", "graduated-access")):
                    dbg("Session expired or account restricted — aborting run.")
                    break

            except Exception as e:
                dbg(f"POST {i}/{len(jobs)}: EXCEPTION — {e}")
                results_summary.append((i, job["kind"], f"EXCEPTION: {e}"))

            finally:
                if tmp_to_clean:
                    try:
                        Path(tmp_to_clean).unlink(missing_ok=True)
                    except Exception:
                        pass

            if i < len(jobs):
                dbg(f"Sleeping {INTERVAL_SECONDS}s ({INTERVAL_MINUTES} min) before next post…")
                time.sleep(INTERVAL_SECONDS)

        browser.close()

    dbg("")
    dbg("=" * 60)
    dbg("Run complete. Summary:")
    for idx, kind, status in results_summary:
        dbg(f"  Post {idx:>2} [{kind:<6}]: {status}")
    dbg(f"Debug screenshots saved in: {SCREENSHOT_DIR.resolve()}")
    dbg("=" * 60)


if __name__ == "__main__":
    main()
