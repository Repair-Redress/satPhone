#!/usr/bin/env python3
"""
SatPhone SMS Daemon — listens for SMS and replies with thermal images.

Requires:
  - Termux:API add-on  (F-Droid → Termux:API, then: pkg install termux-api)
  - For MMS replies:    Tasker  (see --tasker-help for exact setup)
  - NO Termux:Tasker needed — the daemon talks to Tasker via Android intents.

Usage:
  python sms_daemon.py                           # run daemon
  python sms_daemon.py --handle "+1555..." "therm 44.43 -110.59"
  python sms_daemon.py --tasker-help             # Tasker setup guide
  python sms_daemon.py --test-mms "+1555..."     # test MMS intent
"""

import argparse
import json
import shutil
import subprocess
import sqlite3
import sys
import time
from pathlib import Path
from typing import Optional

import config
from logger import get_logger
from sms import parse_message, HELP_TEXT
from rate_limit import RateLimiter, get_rejection_message
from main import run_pipeline

log = get_logger("satphone.daemon")


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

POLL_INTERVAL = config.SMS_POLL_INTERVAL      # seconds between inbox checks
SMS_FETCH_COUNT = config.SMS_FETCH_COUNT      # messages per poll
TERMUX_API_TIMEOUT = 15                       # seconds for termux-api calls

# Shared storage path where Tasker can access images.
# Created by running `termux-setup-storage` once.
SHARED_IMG_DIR = config.MMS_IMAGE_DIR

# Android intent action that Tasker listens for
MMS_INTENT_ACTION = "com.satphone.SEND_MMS"


# ---------------------------------------------------------------------------
# Termux:API wrappers
# ---------------------------------------------------------------------------

def _run_cmd(args: list[str], timeout: int = TERMUX_API_TIMEOUT) -> Optional[str]:
    """Run a command and return stdout, or None on failure."""
    try:
        result = subprocess.run(
            args, capture_output=True, text=True, timeout=timeout,
        )
        if result.returncode != 0:
            log.error("Command %s failed: %s", args[0], result.stderr.strip())
            return None
        return result.stdout
    except FileNotFoundError:
        log.error("%s not found.", args[0])
        return None
    except subprocess.TimeoutExpired:
        log.error("%s timed out after %ds", args[0], timeout)
        return None


def send_sms(number: str, body: str) -> bool:
    """Send a text SMS via termux-sms-send."""
    result = _run_cmd(["termux-sms-send", "-n", number, body], timeout=30)
    if result is not None:
        log.info("SMS → %s (%d chars)", number, len(body))
        return True
    return False


def list_inbox(count: int = SMS_FETCH_COUNT) -> list[dict]:
    """Fetch recent inbox messages via termux-sms-list."""
    output = _run_cmd(
        ["termux-sms-list", "-l", str(count), "-t", "inbox"],
    )
    if output is None:
        return []
    try:
        messages = json.loads(output)
        return messages if isinstance(messages, list) else []
    except json.JSONDecodeError as e:
        log.error("Bad JSON from termux-sms-list: %s", e)
        return []


def send_mms(number: str, body: str, image_path: Path) -> bool:
    """
    Send MMS by copying image to shared storage and broadcasting an
    Android intent that Tasker picks up.

    Flow:
      1. Copy JPEG to ~/storage/shared/Pictures/SatPhone/ (Tasker can read it)
      2. am broadcast → Tasker "Intent Received" profile fires
      3. Tasker sends MMS with the image attached

    No Termux:Tasker plugin needed — just plain Android intents.
    """
    # Ensure shared storage is available
    if not SHARED_IMG_DIR.parent.exists():
        log.error(
            "Shared storage not found at %s. "
            "Run 'termux-setup-storage' and grant permission.",
            SHARED_IMG_DIR.parent,
        )
        return False

    # Copy image to shared storage so Tasker can access it
    SHARED_IMG_DIR.mkdir(parents=True, exist_ok=True)
    shared_path = SHARED_IMG_DIR / image_path.name
    try:
        shutil.copy2(image_path, shared_path)
        log.info("Image copied to shared storage: %s", shared_path)
    except OSError as e:
        log.error("Failed to copy image to shared storage: %s", e)
        return False

    # Broadcast intent to Tasker
    # Tasker receives this via:  Profile → Event → Intent Received
    #   Action: com.satphone.SEND_MMS
    # Extras become Tasker variables: %recipient, %body, %image
    broadcast_cmd = [
        "am", "broadcast",
        "--user", "0",
        "-a", MMS_INTENT_ACTION,
        "--es", "recipient", number,
        "--es", "body", body,
        "--es", "image", str(shared_path),
    ]

    result = _run_cmd(broadcast_cmd, timeout=10)
    if result is not None:
        log.info("MMS intent broadcast → %s", number)
        return True

    log.error("am broadcast failed — is Tasker installed?")
    return False


# ---------------------------------------------------------------------------
# Message tracking (avoid processing the same SMS twice)
# ---------------------------------------------------------------------------

def _init_tracking():
    """Create the processed-messages table if needed."""
    conn = sqlite3.connect(str(config.DB_PATH))
    conn.execute("""
        CREATE TABLE IF NOT EXISTS processed_sms (
            sms_id   TEXT PRIMARY KEY,
            sender   TEXT,
            body     TEXT,
            ts       REAL
        )
    """)
    conn.execute(
        "DELETE FROM processed_sms WHERE ts < ?", (time.time() - 604800,),
    )
    conn.commit()
    conn.close()


def _already_processed(sms_id: str) -> bool:
    conn = sqlite3.connect(str(config.DB_PATH))
    row = conn.execute(
        "SELECT 1 FROM processed_sms WHERE sms_id = ?", (sms_id,),
    ).fetchone()
    conn.close()
    return row is not None


def _mark_processed(sms_id: str, sender: str = "", body: str = ""):
    conn = sqlite3.connect(str(config.DB_PATH))
    conn.execute(
        "INSERT OR IGNORE INTO processed_sms (sms_id, sender, body, ts) "
        "VALUES (?, ?, ?, ?)",
        (sms_id, sender, body[:200], time.time()),
    )
    conn.commit()
    conn.close()


# ---------------------------------------------------------------------------
# Core message handler
# ---------------------------------------------------------------------------

def handle_message(sender: str, body: str, limiter: RateLimiter):
    """
    Process one incoming SMS through the full pipeline:
    parse → rate-limit → fetch → reply.
    """
    request, error = parse_message(body)

    # Not a therm message — silently ignore
    if request is None and error is None:
        return

    # Help text or bad format
    if error:
        send_sms(sender, error)
        return

    # Rate limit check
    allowed, reason = limiter.check(sender)
    if not allowed:
        msg = get_rejection_message(reason, limiter.get_stats(sender))
        if msg:  # "blocked" returns None → silent ignore
            send_sms(sender, msg)
        return

    # Count this request
    limiter.log_request(sender)

    # Acknowledge (pipeline takes 15-25s on the phone)
    send_sms(
        sender,
        f"Fetching thermal image for {request.lat:.2f}, {request.lon:.2f}... "
        f"(this takes ~20s)",
    )

    # Run the heavy pipeline
    try:
        image_path = run_pipeline(request.lat, request.lon, request.before_date)
    except Exception as e:
        log.error("Pipeline failed for %s: %s", sender, e, exc_info=True)
        send_sms(sender, f"Sorry, couldn't fetch that image. Error: {e}")
        return

    # Send MMS with the image (falls back to text-only if MMS fails)
    caption = f"Thermal: {request.lat:.2f}, {request.lon:.2f}"
    if not send_mms(sender, caption, image_path):
        send_sms(sender, f"{caption}\nImage saved but MMS failed. "
                         f"Check --tasker-help for setup.")


# ---------------------------------------------------------------------------
# Daemon loop
# ---------------------------------------------------------------------------

def daemon_loop():
    """Poll termux-sms-list and process new messages."""
    log.info("=" * 50)
    log.info("SatPhone SMS daemon starting")
    log.info("Poll interval: %ds | DB: %s", POLL_INTERVAL, config.DB_PATH)
    log.info("Shared image dir: %s", SHARED_IMG_DIR)
    log.info("=" * 50)

    _init_tracking()
    limiter = RateLimiter()

    # Verify termux-api is available
    test = _run_cmd(["termux-sms-list", "-l", "1"])
    if test is None:
        log.error("termux-sms-list not working. Is Termux:API installed?")
        log.error("  1. Install the Termux:API app from F-Droid")
        log.error("  2. pkg install termux-api")
        log.error("  3. Grant SMS permissions to Termux:API")
        sys.exit(1)

    # Verify shared storage
    if not SHARED_IMG_DIR.parent.exists():
        log.warning(
            "Shared storage not available at %s", SHARED_IMG_DIR.parent,
        )
        log.warning("MMS will fail. Run: termux-setup-storage")

    log.info("Waiting for SMS...")

    while True:
        try:
            messages = list_inbox()
            for msg in messages:
                sms_id = str(msg.get("_id", ""))
                if not sms_id or _already_processed(sms_id):
                    continue

                sender = msg.get("number", "").strip()
                body = msg.get("body", "").strip()

                if not sender or not body:
                    _mark_processed(sms_id, sender, body)
                    continue

                log.info("New SMS from %s: %s", sender, body[:80])
                _mark_processed(sms_id, sender, body)

                handle_message(sender, body, limiter)

        except KeyboardInterrupt:
            log.info("Daemon stopped (Ctrl-C)")
            break
        except Exception as e:
            log.error("Daemon loop error: %s", e, exc_info=True)

        time.sleep(POLL_INTERVAL)


# ---------------------------------------------------------------------------
# Single-message mode (called via --handle)
# ---------------------------------------------------------------------------

def handle_one(sender: str, body: str):
    """Process a single message and exit."""
    _init_tracking()
    limiter = RateLimiter()
    handle_message(sender, body, limiter)


# ---------------------------------------------------------------------------
# Test MMS intent (--test-mms)
# ---------------------------------------------------------------------------

def test_mms(number: str):
    """Send a test MMS intent to verify Tasker is set up correctly."""
    # Create a small test image
    try:
        from PIL import Image as PILImage
        test_img = config.OUTPUT_DIR / "test_mms.jpg"
        config.OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
        img = PILImage.new("RGB", (100, 100), color=(255, 100, 50))
        img.save(test_img, "JPEG")
        log.info("Created test image: %s", test_img)
    except Exception as e:
        log.error("Failed to create test image: %s", e)
        return

    ok = send_mms(number, "SatPhone MMS test", test_img)
    if ok:
        print(f"Test MMS intent sent to Tasker for {number}")
        print("If Tasker is set up correctly, you should receive an MMS.")
        print("If not, run: python sms_daemon.py --tasker-help")
    else:
        print("MMS failed. Check the log and run --tasker-help for setup.")


# ---------------------------------------------------------------------------
# Tasker setup guide
# ---------------------------------------------------------------------------

TASKER_HELP = r"""
============================================================
  SatPhone — Tasker Setup for MMS (no Termux:Tasker needed)
============================================================

The daemon handles everything: polling SMS, processing images,
sending text replies. Tasker's only job is sending the MMS,
because Android doesn't let background apps send MMS directly.

PREREQUISITES
─────────────
1. Tasker               (Play Store, ~$3.99)
2. Termux:API app       (F-Droid — you probably already have this)
3. Run once in Termux:  termux-setup-storage
   (grants access to shared storage for passing images to Tasker)

HOW IT WORKS
────────────
  Daemon (Termux)             Tasker (Android)
  ─────────────────           ──────────────────
  polls SMS inbox
  parses "therm ..."
  fetches satellite image
  copies JPEG to shared       ← image lands in
    storage                     /storage/.../SatPhone/
  broadcasts intent ──────→   receives intent
                               reads %recipient, %body, %image
                               sends MMS with image attached

TASKER SETUP (step by step)
───────────────────────────

STEP 1:  Create the Task  (what Tasker does when triggered)
╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌

  a. Open Tasker → tap TASKS tab at the top
  b. Tap  +  (bottom right) → name it:  Send SatPhone MMS
  c. Inside the task, tap  +  to add an action:

     ─── Action 1: Send Intent ───
     • Category:  System  →  Send Intent
     • Fill in EXACTLY:
         Action:          android.intent.action.SENDTO
         Data:            smsto:%recipient
         Extra:           sms_body:%body
         Extra:           android.intent.extra.STREAM:file://%image
         Package:         (leave blank)
         Class:           (leave blank)
         Mime Type:       image/jpeg
         Target:          Activity

     • Tap the back arrow to save the action.

  d. That's the only action needed. Tap back to save the task.

STEP 2:  Create the Profile  (what triggers the task)
╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌

  a. Tap PROFILES tab at the top
  b. Tap  +  (bottom right)
  c. Select:  Event
  d. Select:  System  →  Intent Received
  e. Fill in:
         Action:   com.satphone.SEND_MMS
         (leave everything else blank)
  f. Tap back arrow
  g. Tasker asks which Task to link → select "Send SatPhone MMS"

STEP 3:  Enable and test
╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌

  a. Make sure the profile toggle is ON (green checkmark)
  b. In Termux, run:
       python sms_daemon.py --test-mms "+1YOURNUMBER"
  c. Tasker should fire, opening your messaging app with the
     image attached and the recipient filled in.
  d. Tap Send.

NOTES
─────
• The "Send Intent" approach opens your messaging app with
  everything pre-filled. On most phones you just tap Send.
  This is the most reliable cross-device approach.

• If you want FULLY automated MMS (no tap needed), install
  the Tasker plugin "AutoShare" (free on Play Store) and
  replace the Send Intent action with:
    AutoShare → Send MMS
    Number: %recipient,  Text: %body,  File: %image

• The daemon handles SMS text replies on its own (help text,
  rate limit messages, "Processing..." acknowledgment).
  Tasker is ONLY needed for the image MMS.

• Run the daemon in Termux:
    source .venv/bin/activate
    python sms_daemon.py              # foreground
    nohup python sms_daemon.py &      # background

TEST COMMAND
────────────
  python sms_daemon.py --test-mms "+15551234567"

  This sends a dummy image through the full MMS flow so you can
  verify Tasker is receiving the intent and composing the MMS.
"""


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="SatPhone SMS daemon",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--handle", nargs=2, metavar=("SENDER", "BODY"),
        help='Process one SMS: --handle "+15551234567" "therm 44.43 -110.59"',
    )
    parser.add_argument(
        "--test-mms", metavar="NUMBER",
        help='Test MMS sending: --test-mms "+15551234567"',
    )
    parser.add_argument(
        "--tasker-help", action="store_true",
        help="Print Tasker setup instructions for MMS",
    )

    args = parser.parse_args()

    if args.tasker_help:
        print(TASKER_HELP)
        return

    if args.test_mms:
        test_mms(args.test_mms)
        return

    if args.handle:
        sender, body = args.handle
        handle_one(sender, body)
        return

    # Default: run the polling daemon
    daemon_loop()


if __name__ == "__main__":
    main()
