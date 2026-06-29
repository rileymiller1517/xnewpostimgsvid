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
    """Timestamped debug print, always flushed immediately."""
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

def post_one(page, text, media_path, mime_type, post_index):
    is_video = mime_type in VIDEO_MIMES
    media_type_label = "VIDEO" if is_video else "IMAGE"

    # ── 1. Navigate ───────────────────────────────────────────────────────────
    dbg(f"  [STEP 1] Navigating to x.com/compose/post…")
    page.goto("https://x.com/compose/post", wait_until="domcontentloaded")
    page.wait_for_timeout(3_000)
    dbg(f"  [STEP 1] Current URL after nav: {page.url}")
    screenshot(page, f"p{post_index}_01_after_nav")

    if "login" in page.url:
        screenshot(page, f"p{post_index}_LOGIN_WALL")
        raise RuntimeError(
            "Redirected to login page — session is expired. "
            "Re-run login_and_save_session.py locally and update X_STORAGE_STATE_JSON secret."
        )

    # ── 2. Find & fill textbox ────────────────────────────────────────────────
    dbg(f"  [STEP 2] Waiting for tweet textbox…")
    try:
        textbox = page.get_by_test_id("tweetTextarea_0")
        textbox.wait_for(state="visible", timeout=15_000)
        dbg(f"  [STEP 2] Textbox found. Clicking and typing caption…")
        textbox.click()
        page.wait_for_timeout(300)
        page.keyboard.type(text, delay=15)
        page.wait_for_timeout(500)
        dbg(f"  [STEP 2] Caption typed ({len(text)} chars).")
        screenshot(page, f"p{post_index}_02_caption_typed")
    except PWTimeout:
        screenshot(page, f"p{post_index}_02_TEXTBOX_TIMEOUT")
        raise RuntimeError("Timed out waiting for tweet textbox — page may not have loaded properly.")

    # ── 3. Find file input ────────────────────────────────────────────────────
    dbg(f"  [STEP 3] Looking for file input (type=file)…")
    file_inputs = page.locator('input[type="file"]')
    count = file_inputs.count()
    dbg(f"  [STEP 3] Found {count} file input(s) on page.")

    if count == 0:
        screenshot(page, f"p{post_index}_03_NO_FILE_INPUT")
        # Dump all input elements for diagnosis
        all_inputs = page.locator("input").all()
        dbg(f"  [STEP 3] All inputs on page ({len(all_inputs)}):")
        for inp in all_inputs:
            try:
                dbg(f"    type={inp.get_attribute('type')} | "
                    f"data-testid={inp.get_attribute('data-testid')} | "
                    f"accept={inp.get_attribute('accept')}")
            except Exception:
                pass
        raise RuntimeError("No file input found on compose page — X may have changed its DOM.")

    file_input = file_inputs.first
    dbg(f"  [STEP 3] Using first file input. accept='{file_input.get_attribute('accept')}'")

    # ── 4. Attach file ────────────────────────────────────────────────────────
    dbg(f"  [STEP 4] Attaching {media_type_label}: {media_path} ({mime_type})…")
    file_input.set_input_files(media_path)
    dbg(f"  [STEP 4] set_input_files() called. Waiting for media preview…")
    screenshot(page, f"p{post_index}_04_after_attach")

    # ── 5. Wait for preview / upload to complete ──────────────────────────────
    dbg(f"  [STEP 5] Waiting for {media_type_label} preview to appear…")
    preview_selectors = (
        '[data-testid="attachments"] video, '
        '[data-testid="videoComponent"], '
        '[data-testid="tweetPhoto"], '
        '[data-testid="attachments"] img, '
        '[data-testid="attachments"] [role="progressbar"]'
    )
    upload_timeout = 90_000 if is_video else 30_000
    try:
        page.wait_for_selector(preview_selectors, timeout=upload_timeout)
        dbg(f"  [STEP 5] Preview/upload indicator appeared.")
        screenshot(page, f"p{post_index}_05_preview_appeared")
    except PWTimeout:
        screenshot(page, f"p{post_index}_05_PREVIEW_TIMEOUT")
        dbg(f"  [STEP 5] WARNING: no preview appeared within timeout — dumping page state…")
        dbg(f"    Page URL: {page.url}")
        dbg(f"    Page title: {page.title()}")
        # Check if attachment area exists at all
        attach_area = page.locator('[data-testid="attachments"]')
        dbg(f"    [data-testid='attachments'] count: {attach_area.count()}")
        dbg(f"  [STEP 5] Proceeding anyway…")

    # If it's a progress bar, wait for it to finish
    progress_bar = page.locator('[role="progressbar"]')
    if progress_bar.count() > 0:
        dbg(f"  [STEP 5b] Progress bar visible — waiting for upload to finish…")
        try:
            page.wait_for_selector('[role="progressbar"]', state="detached",
                                   timeout=upload_timeout)
            dbg(f"  [STEP 5b] Upload complete (progress bar gone).")
        except PWTimeout:
            dbg(f"  [STEP 5b] WARNING: progress bar still visible after timeout — posting anyway.")
        screenshot(page, f"p{post_index}_05b_after_progress")

    page.wait_for_timeout(1_500)

    # ── 6. Check Post button ──────────────────────────────────────────────────
    dbg(f"  [STEP 6] Locating Post button…")
    post_button = page.get_by_test_id("tweetButton")
    try:
        post_button.wait_for(state="visible", timeout=10_000)
        is_disabled = post_button.is_disabled()
        dbg(f"  [STEP 6] Post button visible. Disabled={is_disabled}")
        screenshot(page, f"p{post_index}_06_before_click")
        if is_disabled:
            dbg(f"  [STEP 6] Button is disabled — waiting up to 10s for it to enable…")
            page.wait_for_timeout(10_000)
            is_disabled = post_button.is_disabled()
            dbg(f"  [STEP 6] After wait: Disabled={is_disabled}")
    except PWTimeout:
        screenshot(page, f"p{post_index}_06_BUTTON_TIMEOUT")
        raise RuntimeError("Post button not found/visible.")

    # ── 7. Click Post ─────────────────────────────────────────────────────────
    dbg(f"  [STEP 7] Clicking Post button…")
    post_button.click()
    dbg(f"  [STEP 7] Post button clicked. Waiting for confirmation…")
    screenshot(page, f"p{post_index}_07_after_click")

    # Wait for the compose dialog to close (means post was accepted)
    try:
        page.wait_for_selector('[data-testid="tweetTextarea_0"]',
                               state="detached", timeout=15_000)
        dbg(f"  [STEP 7] Compose box closed — post confirmed sent!")
    except PWTimeout:
        dbg(f"  [STEP 7] Compose box still open after 15s — checking URL…")
        dbg(f"  [STEP 7] URL: {page.url}")
        screenshot(page, f"p{post_index}_07_COMPOSE_STILL_OPEN")

    page.wait_for_timeout(3_000)
    screenshot(page, f"p{post_index}_08_final_state")
    dbg(f"  Post sequence complete for post #{post_index}.")


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
            args=["--no-sandbox", "--disable-setuid-sandbox"],
        )
        context = browser.new_context(
            storage_state=STORAGE_STATE_PATH,
            viewport={"width": 1280, "height": 900},
            user_agent=(
                "Mozilla/5.0 (X11; Linux x86_64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            ),
        )
        page = context.new_page()

        # Log all console messages from the browser
        page.on("console", lambda msg: dbg(f"  [BROWSER {msg.type.upper()}] {msg.text[:200]}"))
        # Log failed network requests
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
