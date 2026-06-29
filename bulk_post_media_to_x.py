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
    if not CLAIMED_FOLDER_ID:
        dbg("  GDRIVE_CLAIMED_FOLDER_ID not set — skipping move to claimed folder.")
        return
    dbg(f"  Moving '{file_name}' to claimed folder ({CLAIMED_FOLDER_ID})…")
    file_meta = service.files().get(fileId=file_id, fields="parents").execute()
    previous_parents = ",".join(file_meta.get("parents", []))
    service.files().update(
        fileId=file_id,
        addParents=CLAIMED_FOLDER_ID,
        removeParents=previous_parents,
        fields="id, parents",
    ).execute()
    dbg(f"  Moved '{file_name}' -> claimed folder OK.")


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

# All known selectors for the tweet textbox, in priority order
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

# All known selectors for the post/tweet submit button
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

# Selectors for media upload button (to click and reveal file input)
MEDIA_BUTTON_SELECTORS = [
    '[data-testid="fileInput"]',
    'input[type="file"]',
    '[data-testid="addMedia"]',
    'button[aria-label*="Media"]',
    'button[aria-label*="photo"]',
    'button[aria-label*="image"]',
    'label[for*="file"]',
]

# Preview selectors to confirm media attached
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
        "https://x.com/home",  # fallback: go home then click compose
    ]
    for url in urls[:2]:  # try direct compose URLs first
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
            if "compose" in current or _textbox_visible(page):
                dbg(f"  [NAV] Compose page confirmed.")
                return True
        except RuntimeError:
            raise
        except Exception as e:
            dbg(f"  [NAV] Navigation to {url} failed: {e}")

    # Last resort: go home and click the compose button
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
        # Dump all contenteditable elements for diagnosis
        all_ce = page.locator('[contenteditable="true"]').all()
        dbg(f"  [TYPE] Found {len(all_ce)} contenteditable elements:")
        for el in all_ce:
            try:
                dbg(f"    testid={el.get_attribute('data-testid')} aria={el.get_attribute('aria-label')}")
            except Exception:
                pass
        raise RuntimeError("Could not find tweet textbox with any known selector.")

    # Method 1: click + keyboard.type
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

    # Method 2: fill (works on some contenteditable)
    try:
        textbox.fill(text)
        page.wait_for_timeout(500)
        dbg(f"  [TYPE] Typed via fill ({len(text)} chars).")
        return
    except Exception as e:
        dbg(f"  [TYPE] fill failed: {e} — trying JS innerText…")

    # Method 3: JS injection
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

    # ── Strategy A: set_input_files on hidden file input ─────────────────────
    dbg("  [ATTACH-A] Looking for file input directly…")
    file_inputs = page.locator('input[type="file"]')
    count = file_inputs.count()
    dbg(f"  [ATTACH-A] Found {count} file input(s).")

    if count > 0:
        for idx in range(count):
            inp = file_inputs.nth(idx)
            accept = inp.get_attribute("accept") or ""
            dbg(f"  [ATTACH-A] Input #{idx}: accept='{accept}'")
            # Pick input that accepts images/videos
            if (is_video and ("video" in accept or accept == "")) or \
               (not is_video and ("image" in accept or accept == "")):
                try:
                    inp.set_input_files(media_path)
                    dbg(f"  [ATTACH-A] set_input_files on input #{idx} — success.")
                    if _wait_for_preview(page, upload_timeout, post_index, "A"):
                        return True
                except Exception as e:
                    dbg(f"  [ATTACH-A] set_input_files #{idx} failed: {e}")

        # Try all inputs if targeted one failed
        for idx in range(count):
            try:
                file_inputs.nth(idx).set_input_files(media_path)
                dbg(f"  [ATTACH-A] Fallback: set_input_files on input #{idx}.")
                if _wait_for_preview(page, upload_timeout, post_index, f"A-fallback{idx}"):
                    return True
            except Exception as e:
                dbg(f"  [ATTACH-A] Fallback #{idx} failed: {e}")

    # ── Strategy B: click media toolbar button, then set_input_files ─────────
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
                # After clicking, try file inputs again
                file_inputs2 = page.locator('input[type="file"]')
                if file_inputs2.count() > 0:
                    file_inputs2.first.set_input_files(media_path)
                    dbg("  [ATTACH-B] set_input_files after button click — success.")
                    if _wait_for_preview(page, upload_timeout, post_index, "B"):
                        return True
        except Exception as e:
            dbg(f"  [ATTACH-B] Button {btn_sel} failed: {e}")

    # ── Strategy C: dispatch file via JS FileList ─────────────────────────────
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

    # ── Strategy D: drag-and-drop simulation ─────────────────────────────────
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
            # If progress bar, wait for it to finish
            _wait_for_upload_finish(page, timeout)
            page.wait_for_timeout(1_500)
            screenshot(page, f"p{post_index}_preview_{strategy_label}")
            return True
        except Exception:
            pass

    # Final check: just wait a bit and see if attachments area appears
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
        # Dump all buttons
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

    # Click with fallback methods
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


def confirm_post_sent(page, post_index):
    """Wait for compose box to close or URL to change — confirms post was sent."""
    dbg("  [CONFIRM] Waiting for compose box to close…")
    screenshot(page, f"p{post_index}_after_click")

    # Method 1: compose textbox disappears
    try:
        page.wait_for_selector('[data-testid="tweetTextarea_0"]',
                               state="detached", timeout=20_000)
        dbg("  [CONFIRM] ✓ Compose box closed — post confirmed!")
        page.wait_for_timeout(3_000)
        screenshot(page, f"p{post_index}_final_state")
        return True
    except PWTimeout:
        dbg("  [CONFIRM] Compose box still open — checking alternative signals…")

    # Method 2: URL changed away from /compose
    page.wait_for_timeout(3_000)
    current_url = page.url
    dbg(f"  [CONFIRM] Current URL: {current_url}")
    if "compose" not in current_url:
        dbg("  [CONFIRM] ✓ URL changed — post likely sent.")
        screenshot(page, f"p{post_index}_final_state")
        return True

    # Method 3: toast/notification appeared
    try:
        page.wait_for_selector(
            '[data-testid="toast"], [aria-label*="sent"], [aria-label*="posted"]',
            timeout=5_000,
        )
        dbg("  [CONFIRM] ✓ Toast notification appeared — post sent!")
        screenshot(page, f"p{post_index}_final_state")
        return True
    except Exception:
        pass

    screenshot(page, f"p{post_index}_confirm_uncertain")
    dbg("  [CONFIRM] ⚠ Could not confirm post was sent — marking uncertain.")
    return False


def post_one(page, text, media_path, mime_type, post_index, max_attempts=3):
    """Post one piece of media with full retry logic."""
    is_video = mime_type in VIDEO_MIMES
    media_type_label = "VIDEO" if is_video else "IMAGE"

    for attempt in range(1, max_attempts + 1):
        dbg(f"  ─── Post attempt {attempt}/{max_attempts} ───")
        try:
            # ── 1. Navigate ───────────────────────────────────────────────────
            dbg(f"  [STEP 1] Navigating to compose page…")
            navigate_to_compose(page, post_index, attempt)

            # ── 2. Type caption ───────────────────────────────────────────────
            dbg(f"  [STEP 2] Typing caption…")
            type_text_robust(page, text, post_index)
            screenshot(page, f"p{post_index}_a{attempt}_caption_typed")
            dbg(f"  [STEP 2] Caption typed ({len(text)} chars).")

            # ── 3. Attach media ───────────────────────────────────────────────
            dbg(f"  [STEP 3] Attaching {media_type_label}: {media_path}")
            attach_media_robust(page, media_path, mime_type, post_index)
            dbg(f"  [STEP 3] {media_type_label} attached successfully.")

            # ── 4. Click Post ─────────────────────────────────────────────────
            dbg(f"  [STEP 4] Clicking Post button…")
            click_post_button(page, post_index)

            # ── 5. Confirm ────────────────────────────────────────────────────
            dbg(f"  [STEP 5] Confirming post sent…")
            sent = confirm_post_sent(page, post_index)
            if sent:
                dbg(f"  Post #{post_index} SUCCESS on attempt {attempt}.")
                return True
            else:
                dbg(f"  Post #{post_index} unconfirmed on attempt {attempt} — retrying.")
                page.wait_for_timeout(5_000)

        except RuntimeError as e:
            dbg(f"  [ATTEMPT {attempt}] RuntimeError: {e}")
            if "login" in str(e).lower() or "session" in str(e).lower():
                raise  # don't retry auth failures
            if attempt < max_attempts:
                dbg(f"  Waiting 10s before retry…")
                page.wait_for_timeout(10_000)
            else:
                raise
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
                raise

    raise RuntimeError(f"All {max_attempts} attempts failed for post #{post_index}.")


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
            # Accept all permissions that X might request
            permissions=["notifications"],
        )

        # Remove automation fingerprints
        context.add_init_script("""
            Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
            Object.defineProperty(navigator, 'plugins', { get: () => [1, 2, 3, 4, 5] });
        """)

        page = context.new_page()

        # Log browser events
        page.on("console", lambda msg: dbg(f"  [BROWSER {msg.type.upper()}] {msg.text[:200]}"))
        page.on("requestfailed", lambda req: dbg(f"  [NET FAIL] {req.url[:100]}"))

        dbg("Browser launched. Starting post loop…")

        for i, media_file in enumerate(chosen, start=1):
            dbg("")
            dbg(f"{'='*50}")
            dbg(f"POST {i}/{len(chosen)}: {media_file['name']} ({media_file['mimeType']})")
            dbg(f"{'='*50}")

            link = links[i - 1] if i - 1 < len(links) else ""
            if CAPTION_SOURCE == "custom":
                text = build_text_custom(CUSTOM_CAPTION_RAW, link)
            else:
                row = random.choice(caption_rows)
                text = build_text_csv(row, link)

            dbg(f"Caption ({len(text)} chars):")
            for line in text.split("\n"):
                dbg(f"  | {line}")

            ext = Path(media_file["name"]).suffix or (
                ".mp4" if media_file["mimeType"] in VIDEO_MIMES else ".jpg"
            )

            with tempfile.NamedTemporaryFile(suffix=ext, delete=False) as tmp:
                tmp_path = tmp.name

            success = False
            try:
                download_file(service, media_file["id"], media_file["name"], tmp_path)
                post_one(page, text, tmp_path, media_file["mimeType"], post_index=i)
                dbg(f"POST {i}/{len(chosen)}: SUCCESS ✓")
                success = True
            except Exception as e:
                dbg(f"POST {i}/{len(chosen)}: FAILED — {e}")
                try:
                    screenshot(page, f"p{i}_EXCEPTION")
                except Exception:
                    pass
            finally:
                Path(tmp_path).unlink(missing_ok=True)
                dbg(f"  Temp file cleaned up.")

            if success:
                move_to_claimed(service, media_file["id"], media_file["name"])
            else:
                dbg(f"  Skipping move-to-claimed (post failed).")

            if i < len(chosen):
                dbg(f"Sleeping {INTERVAL_SECONDS}s ({INTERVAL_MINUTES} min) before next post…")
                time.sleep(INTERVAL_SECONDS)

        dbg("Closing browser…")
        browser.close()

    dbg("")
    dbg("All posts processed.")
    dbg(f"Debug screenshots saved in: {SCREENSHOT_DIR.resolve()}")


if __name__ == "__main__":
    main()
