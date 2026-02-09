#!/usr/bin/env python3
"""
SatPhone SMS Daemon — listens for SMS and replies with thermal images.

Requires:
  - Termux:API add-on  (F-Droid → Termux:API, then: pkg install termux-api)
  - Tasker + AutoInput  (for auto-sending MMS)
  - One-time:           termux-setup-storage  (for MMS image sharing)

Usage:
  python sms_daemon.py                           # run daemon
  python sms_daemon.py --handle "+1555..." "therm 44.43 -110.59"
  python sms_daemon.py --test-mms "+1555..."     # test MMS flow
  python sms_daemon.py --tasker-help             # Tasker setup guide
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

# Shared storage where the messaging app can read images.
# Created by running `termux-setup-storage` once.
SHARED_IMG_DIR = config.MMS_IMAGE_DIR


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


def _copy_to_shared(image_path: Path) -> Optional[Path]:
    """Copy an image to shared storage so other apps can read it."""
    if not SHARED_IMG_DIR.parent.exists():
        log.error(
            "Shared storage not found at %s. "
            "Run 'termux-setup-storage' and grant permission.",
            SHARED_IMG_DIR.parent,
        )
        return None

    SHARED_IMG_DIR.mkdir(parents=True, exist_ok=True)
    shared_path = SHARED_IMG_DIR / image_path.name
    try:
        shutil.copy2(image_path, shared_path)
        log.info("Image → shared storage: %s", shared_path)
        return shared_path
    except OSError as e:
        log.error("Failed to copy image to shared storage: %s", e)
        return None


def send_mms(number: str, body: str, image_path: Path) -> bool:
    """
    Send MMS via Tasker + AutoInput (fully automated).

    Flow:
      1. Copy JPEG to shared storage (messaging app can read it)
      2. Broadcast explicit intent to Tasker (-p targets package)
      3. Tasker composes MMS, AutoInput taps Send

    Requires one-time Tasker setup — see --tasker-help.
    """
    shared_path = _copy_to_shared(image_path)
    if shared_path is None:
        return False

    # Explicit broadcast to Tasker's package.
    # The -p flag makes this an explicit broadcast, which bypasses
    # Android 14's restriction on implicit broadcasts.
    result = _run_cmd([
        "am", "broadcast",
        "--user", "0",
        "-a", "com.satphone.SEND_MMS",
        "-p", "net.dinglisch.android.taskerm",
        "--es", "recipient", number,
        "--es", "body", body,
        "--es", "image", str(shared_path),
    ], timeout=10)

    if result is not None:
        log.info("MMS broadcast → Tasker for %s", number)
        return True

    log.error("MMS broadcast failed")
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
        send_sms(sender, f"{caption}\nImage saved: {image_path.name}")


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
    """Send a test MMS to verify the Tasker broadcast works."""
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

    print(f"\nBroadcasting MMS intent to Tasker for {number}...")
    ok = send_mms(number, "SatPhone MMS test", test_img)
    if ok:
        print("Broadcast sent.")
        print("\nWhat should happen:")
        print("  1. Tasker receives the intent")
        print("  2. Messaging app opens with image + recipient")
        print("  3. AutoInput taps Send automatically")
        print(f"\nIf nothing happened, run: python sms_daemon.py --tasker-help")
    else:
        print("\nBroadcast failed. Try:")
        print("  1. Run: termux-setup-storage  (grant storage permission)")
        print("  2. Check that ~/storage/shared/ exists")
        print("  3. Make sure Tasker is installed")
        print("  4. Run: python sms_daemon.py --tasker-help")


# ---------------------------------------------------------------------------
# Tasker setup guide
# ---------------------------------------------------------------------------

TASKER_HELP = r"""
============================================================
  SatPhone — Tasker + AutoInput Setup for Auto-Send MMS
============================================================

WHAT THIS DOES
──────────────
  The daemon (Termux) handles everything: polling SMS, processing
  satellite images, sending text replies. When an image is ready,
  it broadcasts an Android intent to Tasker. Tasker opens the
  messaging app with the MMS pre-composed, and AutoInput taps
  the Send button automatically. The whole thing takes ~2 seconds.

  Daemon (Termux)                  Tasker + AutoInput
  ───────────────                  ──────────────────
  polls SMS inbox
  parses & processes request
  copies image to shared storage
  am broadcast ──────────────→    Intent Received fires
                                   1. Compose MMS
                                   2. Wait 2 seconds
                                   3. AutoInput taps Send
                                   4. Go Home
                                       ↓
                                   MMS sent from your SIM #

INSTALL (one-time)
──────────────────
  1. Tasker        — Play Store (~$3.99)
  2. AutoInput     — Play Store (free w/ ads, or $1.49)
     After install: Settings → Accessibility → AutoInput → ON
  3. In Termux:    termux-setup-storage   (tap Allow)

TASKER SETUP (step by step)
───────────────────────────

STEP 1: Create the Task
╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌
  Open Tasker → TASKS tab → tap + → name it: SatPhone MMS

  Add 4 actions (tap + for each):

  ┌─ Action 1: Compose MMS ─────────────────────────────────┐
  │  System → Send Intent                                   │
  │    Action:     android.intent.action.SENDTO              │
  │    Data:       smsto:%recipient                          │
  │    Extra:      sms_body:%body                            │
  │    Extra:      android.intent.extra.STREAM:file://%image │
  │    Mime Type:  image/jpeg                                │
  │    Target:     Activity                                  │
  └──────────────────────────────────────────────────────────┘

  ┌─ Action 2: Wait for messaging app to load ──────────────┐
  │  Task → Wait                                            │
  │    Seconds: 2                                           │
  └──────────────────────────────────────────────────────────┘

  ┌─ Action 3: Tap the Send button ─────────────────────────┐
  │  Plugin → AutoInput → Action                            │
  │    Type:   Tap                                          │
  │    Value:  Send SMS                                     │
  │  (If that doesn't match, try: "Send", "send message",  │
  │   or use AutoInput UI Query to find the right label)    │
  └──────────────────────────────────────────────────────────┘

  ┌─ Action 4: Return to background ────────────────────────┐
  │  App → Go Home                                          │
  │    Page: 1                                              │
  └──────────────────────────────────────────────────────────┘

STEP 2: Create the Profile
╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌
  PROFILES tab → tap + → Event → System → Intent Received

    Action:  com.satphone.SEND_MMS
    (leave Cat, Cat Regex, Scheme, Data, etc. all blank)

  Tap back → link it to task "SatPhone MMS"
  Make sure the profile toggle is ON.

STEP 3: Test
╌╌╌╌╌╌╌╌╌╌╌╌
  In Termux:
    python sms_daemon.py --test-mms "+1YOURNUMBER"

  You should see:
    1. Messaging app flashes open with image + recipient
    2. AutoInput taps Send
    3. Phone goes back to home screen
    4. MMS arrives on the target phone

FINDING THE SEND BUTTON LABEL
──────────────────────────────
  The Send button label varies by messaging app:
    Google Messages:   "Send SMS"  or  "Send message"
    Samsung Messages:  "Send"
    Other:             varies

  To find yours:
    1. Open your messaging app, compose a message
    2. Open Tasker → TASKS → create a temp task
    3. Add action: Plugin → AutoInput → UI Query
    4. Run it — it lists all UI elements on screen
    5. Find the Send button's "text" or "desc" value
    6. Use that value in Action 3 above

RUNNING THE DAEMON
──────────────────
  source .venv/bin/activate
  python sms_daemon.py              # foreground
  nohup python sms_daemon.py &      # background
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
        help='Test MMS flow: --test-mms "+15551234567"',
    )
    parser.add_argument(
        "--tasker-help", action="store_true",
        help="Print Tasker + AutoInput setup guide",
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
