#!/usr/bin/env python3
"""
SatPhone SMS Daemon — listens for SMS and replies with thermal images.

Requires:
  - Termux:API add-on  (F-Droid → Termux:API, then: pkg install termux-api)
  - For MMS replies:    Tasker + Termux:Tasker  (see --tasker-help)

Usage:
  python sms_daemon.py                           # run daemon
  python sms_daemon.py --handle "+1555..." "therm 44.43 -110.59"
  python sms_daemon.py --tasker-help             # Tasker setup guide
"""

import argparse
import json
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
MMS_OUTBOX = config.MMS_OUTBOX_DIR            # Tasker watches this directory
TERMUX_API_TIMEOUT = 15                       # seconds for termux-api calls


# ---------------------------------------------------------------------------
# Termux:API wrappers
# ---------------------------------------------------------------------------

def _run_termux(args: list[str], timeout: int = TERMUX_API_TIMEOUT) -> Optional[str]:
    """Run a termux-api command and return stdout, or None on failure."""
    try:
        result = subprocess.run(
            args, capture_output=True, text=True, timeout=timeout,
        )
        if result.returncode != 0:
            log.error("Command %s failed: %s", args[0], result.stderr.strip())
            return None
        return result.stdout
    except FileNotFoundError:
        log.error(
            "%s not found. Install Termux:API: pkg install termux-api", args[0],
        )
        return None
    except subprocess.TimeoutExpired:
        log.error("%s timed out after %ds", args[0], timeout)
        return None


def send_sms(number: str, body: str) -> bool:
    """Send a text SMS via termux-sms-send."""
    # SMS body limit is ~160 chars per segment; termux handles splitting
    result = _run_termux(["termux-sms-send", "-n", number, body], timeout=30)
    if result is not None:
        log.info("SMS → %s (%d chars)", number, len(body))
        return True
    return False


def list_inbox(count: int = SMS_FETCH_COUNT) -> list[dict]:
    """Fetch recent inbox messages via termux-sms-list."""
    output = _run_termux(
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
    Queue an MMS for Tasker to send.

    Writes a JSON file to .mms_outbox/ which Tasker watches.
    Returns True if the file was written successfully.

    Tasker setup: see --tasker-help flag.
    """
    MMS_OUTBOX.mkdir(parents=True, exist_ok=True)

    payload = {
        "to": number,
        "body": body,
        "image": str(image_path.resolve()),
        "queued_at": time.time(),
    }

    outbox_file = MMS_OUTBOX / f"{int(time.time() * 1000)}.json"
    try:
        outbox_file.write_text(json.dumps(payload))
        log.info("MMS queued → %s (%s)", number, outbox_file.name)
        return True
    except OSError as e:
        log.error("Failed to write MMS outbox file: %s", e)
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
    # Prune entries older than 7 days
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

    # Send MMS with the image (falls back to SMS if MMS outbox fails)
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
    log.info("MMS outbox: %s", MMS_OUTBOX)
    log.info("=" * 50)

    _init_tracking()
    limiter = RateLimiter()

    # Verify termux-api is available
    test = _run_termux(["termux-sms-list", "-l", "1"])
    if test is None:
        log.error("termux-sms-list not working. Is Termux:API installed?")
        log.error("  Install: pkg install termux-api")
        log.error("  Also install the Termux:API app from F-Droid.")
        sys.exit(1)

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
# Single-message mode (for Tasker → Termux:Tasker integration)
# ---------------------------------------------------------------------------

def handle_one(sender: str, body: str):
    """Process a single message and exit. Called by Tasker."""
    _init_tracking()
    limiter = RateLimiter()
    handle_message(sender, body, limiter)


# ---------------------------------------------------------------------------
# Tasker setup guide
# ---------------------------------------------------------------------------

TASKER_HELP = """\
=== Tasker Setup for SatPhone MMS ===

SatPhone uses Tasker to send MMS (images) because Android doesn't allow
background apps to send MMS directly. Text-only replies work without Tasker.

INSTALL:
  1. Tasker          (Play Store, ~$3.99)
  2. Termux:Tasker   (F-Droid — lets Tasker run Termux scripts)
  3. Grant Termux:Tasker the "Run commands in Termux" permission

--- Option A: Let Tasker trigger the script (recommended) ---

  Profile: "SatPhone SMS Received"
    Trigger:  Event → Phone → Received Text
              Type: SMS
              Sender: *
              Content: therm*
    Task:     "Process SatPhone"
              1. Termux → Run Command:
                 Command:  cd ~/satphone && source .venv/bin/activate && \\
                           python sms_daemon.py --handle "%SMSRF" "%SMSRB"
                 In Terminal: OFF

  This lets Tasker call the script immediately when an SMS arrives
  (no polling delay). The daemon mode is not needed with this option.

--- Option B: Daemon polls + Tasker sends MMS ---

  Run the daemon in Termux:
    cd ~/satphone && source .venv/bin/activate
    nohup python sms_daemon.py &

  Create a Tasker profile to send queued MMS:

  Profile: "SatPhone MMS Sender"
    Trigger:  Event → File → File Modified
              Path: /data/data/com.termux/files/home/satphone/.mms_outbox
              File Filter: *.json
    Task:     "Send SatPhone MMS"
              1. Read File: %triggered_path → %json
              2. JSON Read: %json → %to, %body, %image
              3. Send MMS:
                 Number: %to
                 Message: %body
                 Attachment: %image
              4. Delete File: %triggered_path

--- Notes ---
  • Option A is simpler (one Tasker profile, no daemon needed).
  • Option B is more resilient (daemon handles retries, queuing).
  • You can combine both: Tasker triggers for speed, daemon as fallback.
  • Grant Termux:API notification access for termux-sms-list to work.
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
        help='Process a single SMS: --handle "+15551234567" "therm 44.43 -110.59"',
    )
    parser.add_argument(
        "--tasker-help", action="store_true",
        help="Print Tasker setup instructions for MMS",
    )

    args = parser.parse_args()

    if args.tasker_help:
        print(TASKER_HELP)
        return

    if args.handle:
        sender, body = args.handle
        handle_one(sender, body)
        return

    # Default: run the polling daemon
    daemon_loop()


if __name__ == "__main__":
    main()
