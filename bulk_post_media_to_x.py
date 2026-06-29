import csv
import json
import os
import random
import sys
import tempfile
import time
from pathlib import Path

from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseDownload
from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout

# ── Config ────────────────────────────────────────────────────────────────────
GDRIVE_CREDS_JSON  = os.environ.get("GDRIVE_CREDENTIALS_JSON", "")
SOURCE_FOLDER_ID   = os.environ.get("GDRIVE_SOURCE_FOLDER_ID", "")
CLAIMED_FOLDER_ID  = os.environ.get("GDRIVE_CLAIMED_FOLDER_ID", "")
POST_COUNT         = int(os.environ.get("POST_COUNT", "5"))
LINKS_RAW          = os.environ.get("LINKS", "")
CAPTION_SOURCE     = os.environ.get("CAPTION_SOURCE", "csv").strip().lower()
CUSTOM_CAPTION_RAW = os.environ.get("CUSTOM_CAPTION", "")
STORAGE_STATE_PATH = os.environ.get("X_STORAGE_STATE_PATH", "x_storage_state.json")
CSV_PATH           = os.environ.get("POSTS_CSV_PATH", "table.csv")
INTERVAL_MINUTES   = float(os.environ.get("INTERVAL_MINUTES", "10"))
INTERVAL_SECONDS   = int(INTERVAL_MINUTES * 60)
SHUFFLE            = os.environ.get("SHUFFLE_ORDER", "false").lower() == "true"
IMAGE_RATIO        = float(os.environ.get("IMAGE_RATIO", "0.6"))

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


# ── Google Drive helpers ──────────────────────────────────────────────────────

def build_drive_service():
    dbg("Building Google Drive service from GDRIVE_CREDENTIALS_JSON…")
    if not GDRIVE_CREDS_JSON.strip():
        sys.exit("GDRIVE_CREDENTIALS_JSON secret is empty.")
    creds_data = json.loads(GDRIVE_CREDS_JSON)
    creds = Credentials(
        token=creds_data.get("token"),
        refresh_token=creds_data.get("refresh_token"),
        token_uri=creds_data.get("token_uri", "https://oauth2.googleapis.com/token"),
        client_id=creds_data.get("client_id"),
        client_secret=creds_data.get("client_secret"),
        scopes=creds_data.get("scopes", ["https://www.googleapis.com/auth/drive"]),
    )
    svc = build("drive", "v3", credentials=creds)
    dbg("Drive service built OK.")
    return svc


def list_media_in_folder(service, folder_id):
    if not folder_id:
        sys.exit("GDRIVE_SOURCE_FOLDER_ID is not set.")
    dbg(f"Listing media in Drive folder: {folder_id}")
    query = (
        f"'{folder_id}' in parents and trashed=false and ("
        + " or ".join(f"mimeType='{m}'" for m in IMAGE_MIMES | VIDEO_MIMES)
        + ")"
    )
    results, page_token = [], None
    while True:
        resp = service.files().list(
            q=query,
            fields="nextPageToken, files(id, name, mimeType)",
            pageSize=200,
            pageToken=page_token,
        ).execute()
        batch = resp.get("files", [])
        results.extend(batch)
        dbg(f"  Got {len(batch)} files this page, {len(results)} total so far.")
        page_token = resp.get("nextPageToken")
        if not page_token:
            break
    return results


def select_media(files, count, image_ratio):
    images = [f for f in files if f["mimeType"] in IMAGE_MIMES]
    videos = [f for f in files if f["mimeType"] in VIDEO_MIMES]
    dbg(f"Available: {len(images)} images, {len(videos)} videos. Want: {count} posts at {image_ratio:.0%} image ratio.")
    if not images and not videos:
        sys.exit("No image or video files found in source folder.")
    n_images = round(count * image_ratio)
    n_videos = count - n_images
    n_images = min(n_images, len(images))
    n_videos = min(n_videos, len(videos))
    total = n_images + n_videos
    if total < count:
        shortfall = count - total
        extra = min(shortfall, len(images) - n_images)
        n_images += extra
        shortfall -= extra
        if shortfall > 0:
            n_videos += min(shortfall, len(videos) - n_videos)
    chosen = random.sample(images, n_images) + random.sample(videos, n_videos)
    random.shuffle(chosen)
    dbg(f"Selected {len(chosen)} files: {n_images} images + {n_videos} videos.")
    return chosen


def download_file(service, file_id, file_name, dest_path):
    dbg(f"  Downloading '{file_name}' (id={file_id}) -> {dest_path}")
    request = service.files().get_media(fileId=file_id)
    with open(dest_path, "wb") as fh:
        downloader = MediaIoBaseDownload(fh, request)
        done = False
        while not done:
            status, done = downloader.next_chunk()
            if status:
                pct = int(status.progress() * 100)
                dbg(f"    Download progress: {pct}%")
    size_kb = Path(dest_path).stat().st_size // 1024
    dbg(f"  Download complete. File size: {size_kb} KB at {dest_path}")


def move_to_claimed(service, file_id, file_name):
    """
    Move a file from the source folder to the claimed folder.
    This is called immediately after a post is confirmed (or assumed) sent,
    so the same file is never picked again in future runs.
    """
    if not CLAIMED_FOLDER_ID:
        dbg("  GDRIVE_CLAIMED_FOLDER_ID not set — skipping move to claimed folder.")
        return
    dbg(f"  Moving '{file_name}' to claimed folder ({CLAIMED_FOLDER_ID})…")
    try:
        file_meta = service.files().get(fileId=file_id, fields="parents").execute()
        previous_parents = ",".join(file_meta.get("parents", []))
        service.files().update(
            fileId=file_id,
            addParents=CLAIMED_FOLDER_ID,
            removeParents=previous_parents,
            fields="id, parents",
        ).execute()
        dbg(f"  ✓ Moved '{file_name}' -> claimed folder OK.")
    except Exception as e:
        # Log but don't crash — the post was already sent
        dbg(f"  ⚠ WARNING: Failed to move '{file_name}' to claimed folder: {e}")
        dbg(f"  ⚠ This file may be re-posted in a future run. Move it manually if needed.")


# ── Caption helpers ───────────────────────────────────────────────────────────

def load_caption_rows(path):
    with open(path, newline="", encoding="utf-8") as f:
        rows = [r for r in csv.DictReader(f) if r.get("Caption", "").strip()]
    if not rows:
        sys.exit(f"No caption rows found in {path}")
    dbg(f"Loaded {len(rows)} caption rows from {path}")
    return rows


def build_text_csv(row, link=""):
    parts = [row["Action Caption"].strip(), row["Caption"].strip(),
             "", row["Hashtags"].strip()]
    text = "\n".join(parts)
    if link:
        text += f"\n\n{link}"
    return text


def build_text_custom(raw, link=""):
    text = raw.replace("\\n", "\n").strip()
    if link:
        text += f"\n\n{link}"
    return text


# ── X / Playwright posting ────────────────────────────────────────────────────

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

MEDIA_BUTTON_SELECTORS = [
    '[data-testid="fileInput"]',
    'input[type="file"]',
    '[data-testid="addMedia"]',
    'button[aria-label*="Media"]',
    'button[aria-label*="photo"]',
    'button[aria-label*="image"]',
    'label[for*="file"]',
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
    """Try multiple selectors in order; return the first visible one."""
    for sel in selectors:
        try:
            el = page.locator(sel).first
            el.wait_for(state="visible", timeout=timeout // len(selectors))
            dbg(f"    ✓ Found {label} via: {sel}")
            return el
        except Exception:
            dbg(f"    ✗ Selector failed for {label}: {sel}")
    return None


def navigate_to_compose(page, post_index, attempt=1):
    """Navigate to the compose page with retries."""
    urls = [
        "https://x.com/compose/post",
        "https://twitter.com/compose/tweet",
        "https://x.com/home",
    ]
    for url in urls[:2]:
        dbg(f"  [NAV] Attempt {attempt}: navigating to {url}")
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=30_000)
            page.wait_for_timeout(3_000)
            current = page.url
            dbg(f"  [NAV] Current URL: {current}")
            screenshot(page, f"p{post_index}_nav_attempt{attempt}")

            if "login" in current or "signin" in current:
                raise RuntimeError(
                    "Redirected to login — session expired. "
                    "Re-run login_and_save_session.py and update X_STORAGE_STATE_JSON."
                )
            if "graduated-access" in current:
                raise RuntimeError(
                    "Account redirected to graduated-access restriction page. "
                    "This is an X account-level posting restriction, not a script bug — "
                    "check Settings > Account > Account access in a normal browser."
                )
            if "compose" in current or _textbox_visible(page):
                dbg(f"  [NAV] Compose page confirmed.")
                return True
        except RuntimeError:
            raise
        except Exception as e:
            dbg(f"  [NAV] Navigation to {url} failed: {e}")

    dbg("  [NAV] Trying home → compose button fallback…")
    try:
        page.goto("https://x.com/home", wait_until="domcontentloaded", timeout=30_000)
        page.wait_for_timeout(3_000)
        compose_btn = page.locator(
            'a[href="/compose/post"], '
            '[data-testid="SideNav_NewTweet_Button"], '
            '[aria-label="Post"], '
            'a[aria-label*="compose"]'
        ).first
        compose_btn.wait_for(state="visible", timeout=10_000)
        compose_btn.click()
        page.wait_for_timeout(3_000)
        screenshot(page, f"p{post_index}_nav_home_fallback")
        if _textbox_visible(page):
            dbg("  [NAV] Home fallback worked.")
            return True
    except Exception as e:
        dbg(f"  [NAV] Home fallback failed: {e}")

    return False


def _textbox_visible(page):
    for sel in TEXTBOX_SELECTORS[:3]:
        try:
            el = page.locator(sel).first
            if el.is_visible():
                return True
        except Exception:
            pass
    return False


def type_text_robust(page, text, post_index):
    """Find textbox and type text using multiple methods."""
    dbg("  [TYPE] Looking for tweet textbox…")
    textbox = find_element_multi(page, TEXTBOX_SELECTORS, "textbox", timeout=20_000)

    if textbox is None:
        screenshot(page, f"p{post_index}_type_no_textbox")
        all_ce = page.locator('[contenteditable="true"]').all()
        dbg(f"  [TYPE] Found {len(all_ce)} contenteditable elements:")
        for el in all_ce:
            try:
                dbg(f"    testid={el.get_attribute('data-testid')} aria={el.get_attribute('aria-label')}")
            except Exception:
                pass
        raise RuntimeError("Could not find tweet textbox with any known selector.")

    try:
        textbox.scroll_into_view_if_needed()
        textbox.click()
        page.wait_for_timeout(500)
        page.keyboard.type(text, delay=15)
        page.wait_for_timeout(500)
        dbg(f"  [TYPE] Typed via keyboard.type ({len(text)} chars).")
        return
    except Exception as e:
        dbg(f"  [TYPE] keyboard.type failed: {e} — trying fill…")

    try:
        textbox.fill(text)
        page.wait_for_timeout(500)
        dbg(f"  [TYPE] Typed via fill ({len(text)} chars).")
        return
    except Exception as e:
        dbg(f"  [TYPE] fill failed: {e} — trying JS innerText…")

    try:
        page.evaluate(
            """(args) => {
                const el = document.querySelector(args.sel);
                if (el) {
                    el.focus();
                    el.innerText = args.text;
                    el.dispatchEvent(new Event('input', { bubbles: true }));
                    el.dispatchEvent(new Event('change', { bubbles: true }));
                }
            }""",
            {"sel": TEXTBOX_SELECTORS[0], "text": text},
        )
        page.wait_for_timeout(500)
        dbg(f"  [TYPE] Typed via JS innerText.")
        return
    except Exception as e:
        dbg(f"  [TYPE] JS innerText failed: {e}")
        raise RuntimeError("All text-input methods failed.")


def attach_media_robust(page, media_path, mime_type, post_index):
    """Attach a file using multiple strategies."""
    is_video = mime_type in VIDEO_MIMES
    upload_timeout = 120_000 if is_video else 45_000

    dbg("  [ATTACH-A] Looking for file input directly…")
    file_inputs = page.locator('input[type="file"]')
    count = file_inputs.count()
    dbg(f"  [ATTACH-A] Found {count} file input(s).")

    if count > 0:
        for idx in range(count):
            inp = file_inputs.nth(idx)
            accept = inp.get_attribute("accept") or ""
            dbg(f"  [ATTACH-A] Input #{idx}: accept='{accept}'")
            if (is_video and ("video" in accept or accept == "")) or \
               (not is_video and ("image" in accept or accept == "")):
                try:
                    inp.set_input_files(media_path)
                    dbg(f"  [ATTACH-A] set_input_files on input #{idx} — success.")
                    if _wait_for_preview(page, upload_timeout, post_index, "A"):
                        return True
                except Exception as e:
                    dbg(f"  [ATTACH-A] set_input_files #{idx} failed: {e}")

        for idx in range(count):
            try:
                file_inputs.nth(idx).set_input_files(media_path)
                dbg(f"  [ATTACH-A] Fallback: set_input_files on input #{idx}.")
                if _wait_for_preview(page, upload_timeout, post_index, f"A-fallback{idx}"):
                    return True
            except Exception as e:
                dbg(f"  [ATTACH-A] Fallback #{idx} failed: {e}")

    dbg("  [ATTACH-B] Clicking media toolbar button to reveal file input…")
    media_btn_selectors = [
        '[data-testid="addMedia"]',
        '[aria-label*="edia"]',
        '[aria-label*="hoto"]',
        'button:has([data-testid="addMedia"])',
        'div[role="button"]:has([data-testid="addMedia"])',
        'label[data-testid="fileInput"]',
    ]
    for btn_sel in media_btn_selectors:
        try:
            btn = page.locator(btn_sel).first
            if btn.is_visible():
                btn.click()
                page.wait_for_timeout(1_000)
                dbg(f"  [ATTACH-B] Clicked button: {btn_sel}")
                file_inputs2 = page.locator('input[type="file"]')
                if file_inputs2.count() > 0:
                    file_inputs2.first.set_input_files(media_path)
                    dbg("  [ATTACH-B] set_input_files after button click — success.")
                    if _wait_for_preview(page, upload_timeout, post_index, "B"):
                        return True
        except Exception as e:
            dbg(f"  [ATTACH-B] Button {btn_sel} failed: {e}")

    dbg("  [ATTACH-C] Trying JS FileList dispatch…")
    try:
        with open(media_path, "rb") as f:
            file_bytes = f.read()
        import base64
        b64 = base64.b64encode(file_bytes).decode()
        file_name = Path(media_path).name

        js_result = page.evaluate(
            """async (args) => {
                const byteChars = atob(args.b64);
                const byteNums = new Array(byteChars.length);
                for (let i = 0; i < byteChars.length; i++) {
                    byteNums[i] = byteChars.charCodeAt(i);
                }
                const byteArr = new Uint8Array(byteNums);
                const blob = new Blob([byteArr], { type: args.mimeType });
                const file = new File([blob], args.fileName, { type: args.mimeType });

                const input = document.querySelector('input[type="file"]');
                if (!input) return 'no-input';

                const dt = new DataTransfer();
                dt.items.add(file);
                const nativeInputValueSetter = Object.getOwnPropertyDescriptor(
                    window.HTMLInputElement.prototype, 'files'
                );
                input.files = dt.files;
                input.dispatchEvent(new Event('change', { bubbles: true }));
                input.dispatchEvent(new Event('input', { bubbles: true }));
                return 'ok';
            }""",
            {"b64": b64, "mimeType": mime_type, "fileName": file_name},
        )
        dbg(f"  [ATTACH-C] JS result: {js_result}")
        if js_result == "ok":
            if _wait_for_preview(page, upload_timeout, post_index, "C"):
                return True
    except Exception as e:
        dbg(f"  [ATTACH-C] JS FileList failed: {e}")

    dbg("  [ATTACH-D] Trying drag-and-drop simulation…")
    try:
        with open(media_path, "rb") as f:
            file_bytes = f.read()
        import base64
        b64 = base64.b64encode(file_bytes).decode()

        js_result = page.evaluate(
            """async (args) => {
                const byteChars = atob(args.b64);
                const byteNums = new Array(byteChars.length);
                for (let i = 0; i < byteChars.length; i++) {
                    byteNums[i] = byteChars.charCodeAt(i);
                }
                const byteArr = new Uint8Array(byteNums);
                const blob = new Blob([byteArr], { type: args.mimeType });
                const file = new File([blob], args.fileName, { type: args.mimeType });

                const dt = new DataTransfer();
                dt.items.add(file);

                const target = document.querySelector(
                    '[data-testid="tweetTextarea_0EditorContainer"], '
                    + '[data-testid="tweetTextarea_0"], '
                    + 'div[contenteditable="true"]'
                );
                if (!target) return 'no-target';

                const events = ['dragenter', 'dragover', 'drop'];
                for (const evtName of events) {
                    const evt = new DragEvent(evtName, {
                        bubbles: true,
                        cancelable: true,
                        dataTransfer: dt,
                    });
                    target.dispatchEvent(evt);
                    await new Promise(r => setTimeout(r, 100));
                }
                return 'ok';
            }""",
            {"b64": b64, "mimeType": mime_type, "fileName": Path(media_path).name},
        )
        dbg(f"  [ATTACH-D] JS drag result: {js_result}")
        if js_result == "ok":
            if _wait_for_preview(page, upload_timeout, post_index, "D"):
                return True
    except Exception as e:
        dbg(f"  [ATTACH-D] Drag-drop simulation failed: {e}")

    screenshot(page, f"p{post_index}_attach_all_failed")
    raise RuntimeError("All media attachment strategies failed.")


def _wait_for_preview(page, timeout, post_index, strategy_label):
    """Wait for a media preview to appear. Returns True on success."""
    dbg(f"  [PREVIEW-{strategy_label}] Waiting for media preview (timeout={timeout}ms)…")
    for sel in PREVIEW_SELECTORS:
        try:
            page.wait_for_selector(sel, timeout=timeout // len(PREVIEW_SELECTORS))
            dbg(f"  [PREVIEW-{strategy_label}] Preview appeared: {sel}")
            _wait_for_upload_finish(page, timeout)
            page.wait_for_timeout(1_500)
            screenshot(page, f"p{post_index}_preview_{strategy_label}")
            return True
        except Exception:
            pass

    page.wait_for_timeout(3_000)
    attach = page.locator('[data-testid="attachments"]')
    if attach.count() > 0:
        dbg(f"  [PREVIEW-{strategy_label}] Attachments area found (fallback check).")
        _wait_for_upload_finish(page, timeout)
        screenshot(page, f"p{post_index}_preview_{strategy_label}_fallback")
        return True

    dbg(f"  [PREVIEW-{strategy_label}] No preview detected.")
    screenshot(page, f"p{post_index}_no_preview_{strategy_label}")
    return False


def _wait_for_upload_finish(page, timeout):
    """Wait for any progress bar to disappear."""
    try:
        progress = page.locator('[role="progressbar"]')
        if progress.count() > 0:
            dbg("  [UPLOAD] Progress bar visible — waiting for completion…")
            page.wait_for_selector('[role="progressbar"]', state="detached", timeout=timeout)
            dbg("  [UPLOAD] Upload complete.")
    except Exception as e:
        dbg(f"  [UPLOAD] Progress bar wait: {e}")


def click_post_button(page, post_index):
    """Click the post/tweet button with multiple selector fallbacks."""
    dbg("  [POST-BTN] Locating Post button…")
    post_btn = find_element_multi(page, POST_BUTTON_SELECTORS, "post button", timeout=15_000)

    if post_btn is None:
        screenshot(page, f"p{post_index}_no_post_button")
        all_btns = page.locator("button").all()
        dbg(f"  [POST-BTN] All buttons ({len(all_btns)}):")
        for b in all_btns[:20]:
            try:
                dbg(f"    testid={b.get_attribute('data-testid')} text={b.inner_text()[:40]}")
            except Exception:
                pass
        raise RuntimeError("Post button not found with any known selector.")

    is_disabled = post_btn.is_disabled()
    dbg(f"  [POST-BTN] Found. Disabled={is_disabled}")
    screenshot(page, f"p{post_index}_before_post_click")

    if is_disabled:
        dbg("  [POST-BTN] Button disabled — waiting up to 15s…")
        for wait_round in range(3):
            page.wait_for_timeout(5_000)
            is_disabled = post_btn.is_disabled()
            dbg(f"  [POST-BTN] After {(wait_round+1)*5}s: Disabled={is_disabled}")
            if not is_disabled:
                break

    try:
        post_btn.click()
        dbg("  [POST-BTN] Clicked via .click().")
    except Exception as e:
        dbg(f"  [POST-BTN] .click() failed: {e} — trying JS click…")
        try:
            page.evaluate(
                """(sel) => {
                    for (const s of sel) {
                        const el = document.querySelector(s);
                        if (el) { el.click(); return s; }
                    }
                    return null;
                }""",
                POST_BUTTON_SELECTORS,
            )
            dbg("  [POST-BTN] JS click executed.")
        except Exception as e2:
            dbg(f"  [POST-BTN] JS click also failed: {e2}")
            raise RuntimeError("Could not click Post button.")


# ── Network-based post confirmation (replaces old DOM-guessing version) ───────
#
# Instead of inferring success from whether the compose box visually closes
# (which is timing-sensitive and was causing false negatives -> retries ->
# duplicate posts), we listen for the actual CreateTweet GraphQL response.
# This tells us definitively whether a post was created, with no guessing,
# and eliminates the duplicate-posting bug from the previous version.

def post_with_network_confirmation(page, post_index, click_timeout=20_000):
    """
    Clicks the Post button while listening for the CreateTweet network
    response. Returns (sent: bool, tweet_id: str|None).

    sent=True  -> CreateTweet API confirmed a new tweet id. Safe to mark posted.
    sent=False -> CreateTweet errored, or no matching response was observed
                  within the timeout. Safe to retry — nothing was created.
    """
    result = {"sent": None, "tweet_id": None}

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
            tweet_id = (
                data["data"]["create_tweet"]["tweet_results"]["result"]["rest_id"]
            )
            dbg(f"  [NET-CONFIRM] CreateTweet succeeded, tweet_id={tweet_id}")
            result["sent"] = True
            result["tweet_id"] = tweet_id
        except (KeyError, TypeError):
            dbg("  [NET-CONFIRM] CreateTweet response shape unexpected — treating as unconfirmed.")
            result["sent"] = False

    page.on("response", on_response)
    try:
        click_post_button(page, post_index)
        dbg("  [NET-CONFIRM] Waiting for CreateTweet network response…")
        waited = 0
        poll_step = 500
        while result["sent"] is None and waited < click_timeout:
            page.wait_for_timeout(poll_step)
            waited += poll_step
    finally:
        page.remove_listener("response", on_response)

    screenshot(page, f"p{post_index}_after_click")

    if result["sent"] is None:
        dbg(f"  [NET-CONFIRM] No CreateTweet response within {click_timeout}ms — NOT sent.")
        screenshot(page, f"p{post_index}_confirm_not_sent")
        return False, None

    if result["sent"]:
        page.wait_for_timeout(1_500)
        screenshot(page, f"p{post_index}_final_state")
        return True, result["tweet_id"]

    screenshot(page, f"p{post_index}_confirm_failed")
    return False, None


def post_one(page, text, media_path, mime_type, post_index, max_attempts=3):
    """
    Post one piece of media with retry on clear failure only.
    Returns True if posted (confirmed via network response), False if
    clearly failed all attempts. Does NOT raise — caller decides what to do.
    """
    is_video = mime_type in VIDEO_MIMES
    media_type_label = "VIDEO" if is_video else "IMAGE"

    for attempt in range(1, max_attempts + 1):
        dbg(f"  ─── Post attempt {attempt}/{max_attempts} ───")
        try:
            dbg(f"  [STEP 1] Navigating to compose page…")
            navigate_to_compose(page, post_index, attempt)

            dbg(f"  [STEP 2] Typing caption…")
            type_text_robust(page, text, post_index)
            screenshot(page, f"p{post_index}_a{attempt}_caption_typed")
            dbg(f"  [STEP 2] Caption typed ({len(text)} chars).")

            dbg(f"  [STEP 3] Attaching {media_type_label}: {media_path}")
            attach_media_robust(page, media_path, mime_type, post_index)
            dbg(f"  [STEP 3] {media_type_label} attached successfully.")

            dbg(f"  [STEP 4] Clicking Post button and confirming via network…")
            sent, tweet_id = post_with_network_confirmation(page, post_index)

            if sent:
                dbg(f"  Post #{post_index} SUCCESS on attempt {attempt} (tweet_id={tweet_id}).")
                return True
            else:
                # CreateTweet did not confirm success — nothing was created,
                # so it's safe to retry from a fresh compose page.
                dbg(f"  Post #{post_index}: not confirmed via network — retrying.")
                if attempt < max_attempts:
                    dbg(f"  Waiting 5s before retry…")
                    page.wait_for_timeout(5_000)

        except RuntimeError as e:
            dbg(f"  [ATTEMPT {attempt}] RuntimeError: {e}")
            if "login" in str(e).lower() or "session" in str(e).lower():
                raise
            if "graduated-access" in str(e).lower() or "restriction" in str(e).lower():
                raise
            if attempt < max_attempts:
                dbg(f"  Waiting 10s before retry…")
                page.wait_for_timeout(10_000)
            else:
                dbg(f"  All {max_attempts} attempts failed with RuntimeError.")
                return False
        except Exception as e:
            dbg(f"  [ATTEMPT {attempt}] Unexpected error: {e}")
            try:
                screenshot(page, f"p{post_index}_a{attempt}_exception")
            except Exception:
                pass
            if attempt < max_attempts:
                dbg(f"  Waiting 10s before retry…")
                page.wait_for_timeout(10_000)
            else:
                dbg(f"  All {max_attempts} attempts failed with unexpected error.")
                return False

    dbg(f"  All {max_attempts} attempts exhausted for post #{post_index}.")
    return False


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    dbg("=" * 60)
    dbg("Starting bulk_post_media_to_x.py (DEBUG MODE)")
    dbg(f"CAPTION_SOURCE   : {CAPTION_SOURCE}")
    dbg(f"POST_COUNT       : {POST_COUNT}")
    dbg(f"IMAGE_RATIO      : {IMAGE_RATIO}")
    dbg(f"INTERVAL_MINUTES : {INTERVAL_MINUTES}")
    dbg(f"SOURCE_FOLDER_ID : {SOURCE_FOLDER_ID or '(not set)'}")
    dbg(f"CLAIMED_FOLDER_ID: {CLAIMED_FOLDER_ID or '(not set)'}")
    dbg(f"STORAGE_STATE    : {STORAGE_STATE_PATH}")
    dbg(f"LINKS_RAW        : {LINKS_RAW[:80] or '(none)'}")
    dbg("=" * 60)

    if CAPTION_SOURCE == "custom":
        if not CUSTOM_CAPTION_RAW.strip():
            sys.exit("CAPTION_SOURCE=custom but CUSTOM_CAPTION is empty.")
        caption_rows = None
        dbg(f"Custom caption set ({len(CUSTOM_CAPTION_RAW)} chars).")
    elif CAPTION_SOURCE == "csv":
        caption_rows = load_caption_rows(CSV_PATH)
    else:
        sys.exit(f"Unknown CAPTION_SOURCE '{CAPTION_SOURCE}'.")

    links = [l.strip() for l in LINKS_RAW.split(",") if l.strip()]
    dbg(f"Links provided: {len(links)}")

    dbg("Connecting to Google Drive…")
    service = build_drive_service()
    all_files = list_media_in_folder(service, SOURCE_FOLDER_ID)
    dbg(f"Total media files found in source folder: {len(all_files)}")

    if not all_files:
        sys.exit("Source folder is empty or no images/videos found. Nothing to post.")

    for f in all_files[:10]:
        dbg(f"  File: {f['name']} | mimeType: {f['mimeType']} | id: {f['id']}")
    if len(all_files) > 10:
        dbg(f"  … and {len(all_files) - 10} more.")

    chosen = select_media(all_files, POST_COUNT, IMAGE_RATIO)
    if SHUFFLE:
        random.shuffle(chosen)

    # Track which file IDs we've already claimed this run, defensively.
    claimed_this_run: set = set()

    n_images = sum(1 for f in chosen if f["mimeType"] in IMAGE_MIMES)
    n_videos = len(chosen) - n_images
    dbg(f"Will post {len(chosen)} file(s): {n_images} image(s) + {n_videos} video(s).")

    est = (len(chosen) - 1) * INTERVAL_MINUTES
    dbg(f"Estimated run time: ~{est:.0f} min.")
    if est > 350:
        dbg("WARNING: estimated time close to GitHub's 6-hour job limit!")

    dbg("Launching Chromium (headless desktop)…")
    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=True,
            args=[
                "--no-sandbox",
                "--disable-setuid-sandbox",
                "--disable-blink-features=AutomationControlled",
                "--disable-web-security",
                "--disable-features=IsolateOrigins,site-per-process",
            ],
        )
        context = browser.new_context(
            storage_state=STORAGE_STATE_PATH,
            viewport={"width": 1280, "height": 900},
            user_agent=(
                "Mozilla/5.0 (X11; Linux x86_64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            ),
            permissions=["notifications"],
        )

        context.add_init_script("""
            Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
            Object.defineProperty(navigator, 'plugins', { get: () => [1, 2, 3, 4, 5] });
        """)

        page = context.new_page()

        page.on("console", lambda msg: dbg(f"  [BROWSER {msg.type.upper()}] {msg.text[:200]}"))
        page.on("requestfailed", lambda req: dbg(f"  [NET FAIL] {req.url[:100]}"))

        dbg("Browser launched. Starting post loop…")

        results_summary = []

        for i, media_file in enumerate(chosen, start=1):
            file_id   = media_file["id"]
            file_name = media_file["name"]
            mime_type = media_file["mimeType"]

            dbg("")
            dbg(f"{'='*50}")
            dbg(f"POST {i}/{len(chosen)}: {file_name} ({mime_type})")
            dbg(f"{'='*50}")

            if file_id in claimed_this_run:
                dbg(f"  ⚠ File '{file_name}' already claimed this run — skipping.")
                results_summary.append((i, file_name, "SKIPPED_DUPLICATE"))
                continue

            link = links[i - 1] if i - 1 < len(links) else ""
            if CAPTION_SOURCE == "custom":
                text = build_text_custom(CUSTOM_CAPTION_RAW, link)
            else:
                row = random.choice(caption_rows)
                text = build_text_csv(row, link)

            dbg(f"Caption ({len(text)} chars):")
            for line in text.split("\n"):
                dbg(f"  | {line}")

            ext = Path(file_name).suffix or (
                ".mp4" if mime_type in VIDEO_MIMES else ".jpg"
            )

            with tempfile.NamedTemporaryFile(suffix=ext, delete=False) as tmp:
                tmp_path = tmp.name

            posted = False
            try:
                download_file(service, file_id, file_name, tmp_path)

                posted = post_one(page, text, tmp_path, mime_type, post_index=i)

                if posted:
                    dbg(f"POST {i}/{len(chosen)}: SUCCESS ✓")
                    results_summary.append((i, file_name, "SUCCESS"))

                    # Move to claimed IMMEDIATELY after a confirmed post, so
                    # even if the script crashes mid-run the file won't be
                    # re-posted in the next workflow run.
                    claimed_this_run.add(file_id)
                    move_to_claimed(service, file_id, file_name)
                else:
                    dbg(f"POST {i}/{len(chosen)}: FAILED after all retries.")
                    results_summary.append((i, file_name, "FAILED"))

            except RuntimeError as e:
                dbg(f"POST {i}/{len(chosen)}: FATAL — {e}")
                results_summary.append((i, file_name, f"FATAL: {e}"))
                # If session expired or account is restricted, abort the run —
                # retrying will not help in either case.
                msg = str(e).lower()
                if "login" in msg or "session" in msg or "graduated-access" in msg or "restriction" in msg:
                    dbg("Session expired or account restricted — aborting run.")
                    try:
                        screenshot(page, f"p{i}_FATAL")
                    except Exception:
                        pass
                    break
                try:
                    screenshot(page, f"p{i}_FATAL")
                except Exception:
                    pass

            except Exception as e:
                dbg(f"POST {i}/{len(chosen)}: EXCEPTION — {e}")
                results_summary.append((i, file_name, f"EXCEPTION: {e}"))
                try:
                    screenshot(page, f"p{i}_EXCEPTION")
                except Exception:
                    pass

            finally:
                try:
                    Path(tmp_path).unlink(missing_ok=True)
                    dbg(f"  Temp file cleaned up.")
                except Exception:
                    pass

            if i < len(chosen):
                dbg(f"Sleeping {INTERVAL_SECONDS}s ({INTERVAL_MINUTES} min) before next post…")
                time.sleep(INTERVAL_SECONDS)

        dbg("Closing browser…")
        browser.close()

    dbg("")
    dbg("=" * 60)
    dbg("Run complete. Summary:")
    for idx, name, status in results_summary:
        dbg(f"  Post {idx:>2}: [{status:^20}] {name}")
    dbg(f"Debug screenshots saved in: {SCREENSHOT_DIR.resolve()}")
    dbg("=" * 60)


if __name__ == "__main__":
    main()
