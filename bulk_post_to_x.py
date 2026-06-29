"""
Bulk-posts a list of links to X, one every N minutes, all within a single
GitHub Actions run.

Two caption modes, set via CAPTION_SOURCE:
  - "csv":    randomly picks one row (Action Caption, Caption, Hashtags)
              from table.csv for EACH link.
  - "custom": uses one CUSTOM_CAPTION text for ALL links posted this run.
              Type \n (backslash-n) in the workflow input wherever you want
              a line break -- the GitHub UI box is single-line, so this is
              how multi-line text gets in.

Final post text is always:
    {caption block}

    {link}

Required env vars:
    LINKS                 - comma-separated list of links (required)
    CAPTION_SOURCE         - "csv" or "custom" (default: csv)
    CUSTOM_CAPTION         - used when CAPTION_SOURCE=custom
    X_STORAGE_STATE_PATH   - path to saved session (default: x_storage_state.json)
    POSTS_CSV_PATH         - path to caption CSV (default: table.csv)
    INTERVAL_MINUTES       - minutes between posts (default: 10)
    SHUFFLE_ORDER          - "true" to post links in random order (default: false)
"""

import csv
import os
import random
import sys
import time
from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout

LINKS_RAW = os.environ.get("LINKS", "")
CAPTION_SOURCE = os.environ.get("CAPTION_SOURCE", "csv").strip().lower()
CUSTOM_CAPTION_RAW = os.environ.get("CUSTOM_CAPTION", "")
STORAGE_STATE_PATH = os.environ.get("X_STORAGE_STATE_PATH", "x_storage_state.json")
CSV_PATH = os.environ.get("POSTS_CSV_PATH", "table.csv")
INTERVAL_MINUTES = float(os.environ.get("INTERVAL_MINUTES", "10"))
INTERVAL_SECONDS = int(INTERVAL_MINUTES * 60)
SHUFFLE = os.environ.get("SHUFFLE_ORDER", "false").lower() == "true"


def parse_links(raw):
    links = [l.strip() for l in raw.split(",") if l.strip()]
    if not links:
        sys.exit("No links provided. Set the LINKS input (comma-separated).")
    return links


def load_caption_rows(path):
    with open(path, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        rows = [r for r in reader if r.get("Caption", "").strip()]
    if not rows:
        sys.exit(f"No caption rows found in {path}")
    return rows


def build_text_csv_mode(row, link):
    action_caption = row["Action Caption"].strip()
    caption = row["Caption"].strip()
    hashtags = row["Hashtags"].strip()
    return f"{action_caption}\n{caption}\n\n{hashtags}\n\n{link}"


def build_text_custom_mode(custom_caption, link):
    text = custom_caption.replace("\\n", "\n").strip()
    return f"{text}\n\n{link}"


def post_one(page, text):
    page.goto("https://x.com/compose/post", wait_until="domcontentloaded")

    if "login" in page.url:
        raise RuntimeError(
            "Session looks expired (redirected to login). "
            "Re-run login_and_save_session.py locally and refresh the secret."
        )

    textbox = page.get_by_test_id("tweetTextarea_0")
    textbox.wait_for(state="visible", timeout=15000)
    textbox.click()
    textbox.fill(text)

    try:
        page.wait_for_selector('[data-testid="card.wrapper"]', timeout=12000)
    except PWTimeout:
        print("Warning: link preview card didn't render in time, posting anyway.")

    post_button = page.get_by_test_id("tweetButton")
    post_button.wait_for(state="visible", timeout=10000)
    post_button.click()
    page.wait_for_timeout(4000)


def main():
    links = parse_links(LINKS_RAW)
    if SHUFFLE:
        random.shuffle(links)

    if CAPTION_SOURCE == "custom":
        if not CUSTOM_CAPTION_RAW.strip():
            sys.exit("CAPTION_SOURCE=custom but CUSTOM_CAPTION is empty.")
        caption_rows = None
    elif CAPTION_SOURCE == "csv":
        caption_rows = load_caption_rows(CSV_PATH)
    else:
        sys.exit(f"Unknown CAPTION_SOURCE '{CAPTION_SOURCE}', expected 'csv' or 'custom'.")

    est_minutes = (len(links) - 1) * INTERVAL_MINUTES
    print(f"Posting {len(links)} link(s), mode={CAPTION_SOURCE}, spacing={INTERVAL_MINUTES} min.")
    print(f"Estimated total run time: ~{est_minutes:.0f} minutes.")
    if est_minutes > 350:
        print("WARNING: close to/over GitHub's 6-hour job limit; run may be killed mid-batch.")

    with sync_playwright() as p:
        device = p.devices["iPhone 13"]
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(storage_state=STORAGE_STATE_PATH, **device)
        page = context.new_page()

        for i, link in enumerate(links, start=1):
            if CAPTION_SOURCE == "custom":
                text = build_text_custom_mode(CUSTOM_CAPTION_RAW, link)
            else:
                row = random.choice(caption_rows)
                text = build_text_csv_mode(row, link)

            print(f"\n[{i}/{len(links)}] Posting:\n{text}\n")
            try:
                post_one(page, text)
                print(f"[{i}/{len(links)}] Done.")
            except Exception as e:
                print(f"[{i}/{len(links)}] FAILED: {e}")

            if i < len(links):
                print(f"Sleeping {INTERVAL_SECONDS}s before next post...")
                time.sleep(INTERVAL_SECONDS)

        browser.close()

    print("\nAll links processed.")


if __name__ == "__main__":
    main()
