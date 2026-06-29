"""
Bulk-posts images/videos from Google Drive to X (Twitter).

Media selection:
  - Pulls files from a Google Drive folder (GDRIVE_SOURCE_FOLDER_ID).
  - 60% images (jpg/jpeg/png/gif/webp), 40% videos (mp4/mov/avi).
  - After a successful post, moves the file to GDRIVE_CLAIMED_FOLDER_ID
    to prevent re-posting.

Caption modes (CAPTION_SOURCE):
  - "csv":    randomly picks one row (Action Caption, Caption, Hashtags)
              from table.csv for EACH post.
  - "custom": uses CUSTOM_CAPTION for ALL posts this run.
              Use \\n in the GitHub UI input for line breaks.

Post text format:
    {caption block}

    {optional link if LINKS provided, else omitted}

Required GitHub Secrets -> env vars:
    GDRIVE_CREDENTIALS_JSON   - full token JSON (as a secret string)
    X_STORAGE_STATE_JSON      - Playwright session JSON

Optional env vars:
    GDRIVE_SOURCE_FOLDER_ID   - Drive folder to pick media from
    GDRIVE_CLAIMED_FOLDER_ID  - Drive folder to move posted files into
    POST_COUNT                - how many posts this run (default: 5)
    LINKS                     - optional comma-separated links, one per post
    CAPTION_SOURCE            - "csv" or "custom" (default: csv)
    CUSTOM_CAPTION            - used when CAPTION_SOURCE=custom
    X_STORAGE_STATE_PATH      - path to saved session (default: x_storage_state.json)
    POSTS_CSV_PATH            - path to caption CSV (default: table.csv)
    INTERVAL_MINUTES          - minutes between posts (default: 10)
    SHUFFLE_ORDER             - "true" to shuffle media order (default: false)
    IMAGE_RATIO               - fraction of posts that are images (default: 0.6)
"""

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

# ── Google Drive helpers ──────────────────────────────────────────────────────

def build_drive_service():
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
    return build("drive", "v3", credentials=creds)


def list_media_in_folder(service, folder_id):
    if not folder_id:
        sys.exit("GDRIVE_SOURCE_FOLDER_ID is not set.")
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
        results.extend(resp.get("files", []))
        page_token = resp.get("nextPageToken")
        if not page_token:
            break
    return results


def select_media(files, count, image_ratio):
    images = [f for f in files if f["mimeType"] in IMAGE_MIMES]
    videos = [f for f in files if f["mimeType"] in VIDEO_MIMES]
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
    return chosen


def download_file(service, file_id, dest_path):
    request = service.files().get_media(fileId=file_id)
    with open(dest_path, "wb") as fh:
        downloader = MediaIoBaseDownload(fh, request)
        done = False
        while not done:
            status, done = downloader.next_chunk()
            if status:
                print(f"    Download {int(status.progress() * 100)}%")


def move_to_claimed(service, file_id):
    if not CLAIMED_FOLDER_ID:
        print("  GDRIVE_CLAIMED_FOLDER_ID not set — skipping move.")
        return
    file_meta = service.files().get(fileId=file_id, fields="parents").execute()
    previous_parents = ",".join(file_meta.get("parents", []))
    service.files().update(
        fileId=file_id,
        addParents=CLAIMED_FOLDER_ID,
        removeParents=previous_parents,
        fields="id, parents",
    ).execute()
    print(f"  Moved {file_id} -> claimed folder.")

# ── Caption helpers ───────────────────────────────────────────────────────────

def load_caption_rows(path):
    with open(path, newline="", encoding="utf-8") as f:
        rows = [r for r in csv.DictReader(f) if r.get("Caption", "").strip()]
    if not rows:
        sys.exit(f"No caption rows found in {path}")
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

def attach_media(page, media_path, mime_type):
    """
    Attach a local file to the X compose box.

    X renders the file input as hidden. The correct approach is:
      1. Use page.locator('input[type="file"]') — do NOT filter by data-testid.
      2. Call set_input_files() directly without clicking (hidden inputs can't be clicked).
      3. Wait for the media preview thumbnail to appear before posting.
    """
    is_video = mime_type in VIDEO_MIMES

    # X exposes a hidden <input type="file"> that accepts both images and videos.
    # There are sometimes multiple; the first one is always the compose-box attachment.
    file_input = page.locator('input[type="file"]').first

    # set_input_files works on hidden inputs — no click needed
    file_input.set_input_files(media_path)
    print("  File attached, waiting for preview…")

    if is_video:
        # For video: wait for the video thumbnail / processing indicator
        # X shows a [data-testid="videoComponent"] or a progress bar while processing
        try:
            # Wait until a video element or thumbnail appears in the composer
            page.wait_for_selector(
                '[data-testid="videoComponent"], [data-testid="attachments"] video, '
                '[data-testid="tweetPhoto"]',
                timeout=60_000,
            )
            print("  Video preview visible.")
        except PWTimeout:
            # Also acceptable: the progress bar disappears
            print("  Warning: video preview selector timed out — checking progress bar…")
            try:
                page.wait_for_selector(
                    '[role="progressbar"]', state="detached", timeout=60_000
                )
                print("  Progress bar gone — continuing.")
            except PWTimeout:
                print("  Warning: could not confirm video upload, posting anyway.")
    else:
        # For images: wait for the thumbnail preview in the compose box
        try:
            page.wait_for_selector(
                '[data-testid="tweetPhoto"], [data-testid="attachments"] img',
                timeout=30_000,
            )
            print("  Image preview visible.")
        except PWTimeout:
            print("  Warning: image preview selector timed out — posting anyway.")


def post_one(page, text, media_path, mime_type):
    page.goto("https://x.com/compose/post", wait_until="domcontentloaded")
    # Extra settle time — the compose page JS can be slow to initialise
    page.wait_for_timeout(2_000)

    if "login" in page.url:
        raise RuntimeError(
            "Session expired (redirected to login). "
            "Re-run login_and_save_session.py and refresh X_STORAGE_STATE_JSON."
        )

    # ── Type caption ──────────────────────────────────────────────────────────
    textbox = page.get_by_test_id("tweetTextarea_0")
    textbox.wait_for(state="visible", timeout=15_000)
    textbox.click()
    # Use type() instead of fill() so X's React state registers every keystroke
    page.keyboard.type(text, delay=20)
    page.wait_for_timeout(500)

    # ── Attach media ──────────────────────────────────────────────────────────
    attach_media(page, media_path, mime_type)

    # ── Small settle before posting ───────────────────────────────────────────
    page.wait_for_timeout(1_500)

    # ── Click Post ────────────────────────────────────────────────────────────
    post_button = page.get_by_test_id("tweetButton")
    post_button.wait_for(state="visible", timeout=10_000)
    post_button.wait_for(state="enabled", timeout=10_000)
    post_button.click()

    # Wait for the compose overlay to close (confirms the post went through)
    try:
        page.wait_for_selector('[data-testid="tweetTextarea_0"]',
                               state="detached", timeout=15_000)
    except PWTimeout:
        pass  # Some X versions navigate away instead of detaching
    page.wait_for_timeout(3_000)


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    if CAPTION_SOURCE == "custom":
        if not CUSTOM_CAPTION_RAW.strip():
            sys.exit("CAPTION_SOURCE=custom but CUSTOM_CAPTION is empty.")
        caption_rows = None
    elif CAPTION_SOURCE == "csv":
        caption_rows = load_caption_rows(CSV_PATH)
    else:
        sys.exit(f"Unknown CAPTION_SOURCE '{CAPTION_SOURCE}'.")

    links = [l.strip() for l in LINKS_RAW.split(",") if l.strip()]

    print("Connecting to Google Drive…")
    service = build_drive_service()
    all_files = list_media_in_folder(service, SOURCE_FOLDER_ID)
    print(f"Found {len(all_files)} media file(s) in source folder.")

    if not all_files:
        sys.exit("Source folder is empty — nothing to post.")

    chosen = select_media(all_files, POST_COUNT, IMAGE_RATIO)
    if SHUFFLE:
        random.shuffle(chosen)

    n_images = sum(1 for f in chosen if f["mimeType"] in IMAGE_MIMES)
    n_videos = len(chosen) - n_images
    print(f"Selected {len(chosen)} file(s): {n_images} image(s), {n_videos} video(s).")
    print(f"Interval: {INTERVAL_MINUTES} min  |  Caption: {CAPTION_SOURCE}")

    est = (len(chosen) - 1) * INTERVAL_MINUTES
    print(f"Estimated run time: ~{est:.0f} min.")
    if est > 350:
        print("WARNING: may exceed GitHub's 6-hour job limit.")

    with sync_playwright() as p:
        # Use a desktop viewport — mobile viewports can hide the file input entirely
        browser = p.chromium.launch(headless=True)
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

        for i, media_file in enumerate(chosen, start=1):
            link = links[i - 1] if i - 1 < len(links) else ""

            if CAPTION_SOURCE == "custom":
                text = build_text_custom(CUSTOM_CAPTION_RAW, link)
            else:
                row = random.choice(caption_rows)
                text = build_text_csv(row, link)

            ext = Path(media_file["name"]).suffix or (
                ".mp4" if media_file["mimeType"] in VIDEO_MIMES else ".jpg"
            )

            print(f"\n[{i}/{len(chosen)}] {media_file['name']} ({media_file['mimeType']})")
            print(f"  Caption: {text[:80].replace(chr(10), ' ')}…")

            with tempfile.NamedTemporaryFile(suffix=ext, delete=False) as tmp:
                tmp_path = tmp.name

            success = False
            try:
                print("  Downloading from Drive…")
                download_file(service, media_file["id"], tmp_path)
                size_kb = Path(tmp_path).stat().st_size // 1024
                print(f"  Downloaded: {size_kb} KB")

                print("  Posting to X…")
                post_one(page, text, tmp_path, media_file["mimeType"])
                print(f"[{i}/{len(chosen)}] Posted OK")
                success = True

            except Exception as e:
                print(f"[{i}/{len(chosen)}] FAILED: {e}")
            finally:
                Path(tmp_path).unlink(missing_ok=True)

            if success:
                move_to_claimed(service, media_file["id"])

            if i < len(chosen):
                print(f"  Sleeping {INTERVAL_SECONDS}s before next post…")
                time.sleep(INTERVAL_SECONDS)

        browser.close()

    print("\nAll posts processed.")


if __name__ == "__main__":
    main()
