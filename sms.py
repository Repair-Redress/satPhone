"""SMS message parsing, validation, help text, and request queue."""

import re
import queue
import threading
from datetime import datetime
from typing import Optional
from dataclasses import dataclass

from logger import get_logger

log = get_logger("satphone.sms")


# ---------------------------------------------------------------------------
# Help text
# ---------------------------------------------------------------------------

HELP_TEXT = (
    "SatPhone - Satellite Thermal Imagery\n"
    "Format: therm <lat> <lon> [date]\n"
    "Examples:\n"
    "  therm 44.43 -110.59\n"
    "  therm 44.43, -110.59\n"
    "  therm 44.43 -110.59 2025-10-06\n"
    "Date is optional (YYYY-MM-DD). Without it, the most recent image is used."
)


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------

# therm <lat> [,] <lon> [YYYY-MM-DD]
_COORD_PATTERN = re.compile(
    r"(?i)^therm\s+"
    r"(-?\d+\.?\d*)"                   # latitude
    r"(?:\s*,\s*|\s+)"                 # comma with optional spaces, or spaces
    r"(-?\d+\.?\d*)"                   # longitude
    r"(?:\s+(\d{4}-\d{2}-\d{2}))?"    # optional date
    r"\s*$"
)

_HELP_PATTERN = re.compile(r"(?i)^therm\s+help\s*$")
_THERM_PREFIX = re.compile(r"(?i)^therm\b")


@dataclass
class ParsedRequest:
    """A validated satellite image request."""
    lat: float
    lon: float
    before_date: Optional[str] = None


def parse_message(body: str) -> tuple[Optional[ParsedRequest], Optional[str]]:
    """
    Parse an SMS message body.

    Returns:
        (request, error_message)
        - Valid request:       (ParsedRequest, None)
        - Help or bad format:  (None, help_text_string)
        - Not a therm message: (None, None)
    """
    body = body.strip()

    # Not addressed to us at all
    if not _THERM_PREFIX.match(body):
        return None, None

    # Explicit help request
    if _HELP_PATTERN.match(body):
        return None, HELP_TEXT

    # Try coordinate parse
    m = _COORD_PATTERN.match(body)
    if not m:
        log.info("Bad format: %s", body)
        return None, HELP_TEXT

    lat = float(m.group(1))
    lon = float(m.group(2))
    date_str = m.group(3)

    # Validate ranges
    if not (-90 <= lat <= 90):
        return None, f"Invalid latitude: {lat}. Must be -90 to 90.\n\n{HELP_TEXT}"
    if not (-180 <= lon <= 180):
        return None, f"Invalid longitude: {lon}. Must be -180 to 180.\n\n{HELP_TEXT}"

    # Validate date if provided
    before_date = None
    if date_str:
        try:
            datetime.strptime(date_str, "%Y-%m-%d")
            before_date = date_str
        except ValueError:
            return None, f"Invalid date: {date_str}. Use YYYY-MM-DD.\n\n{HELP_TEXT}"

    return ParsedRequest(lat=lat, lon=lon, before_date=before_date), None


# ---------------------------------------------------------------------------
# Request queue
# ---------------------------------------------------------------------------

class RequestQueue:
    """
    Bounded queue for processing image requests one at a time.

    Rejects new requests when full instead of blocking.
    """

    def __init__(self, max_size: int = 5):
        self._queue: queue.Queue = queue.Queue(maxsize=max_size)
        self._worker_started = False

    def enqueue(self, sender: str, request: ParsedRequest) -> bool:
        """Add a request. Returns False if the queue is full."""
        try:
            self._queue.put_nowait((sender, request))
            log.info(
                "Queued request from %s: %.2f, %.2f",
                sender, request.lat, request.lon,
            )
            return True
        except queue.Full:
            log.warning("Queue full, rejecting request from %s", sender)
            return False

    def start_worker(self, handler_func):
        """
        Start a single background worker that calls handler_func(sender, request)
        for each queued item.
        """
        if self._worker_started:
            return

        def worker():
            while True:
                sender, request = self._queue.get()
                try:
                    log.debug("Processing queued request from %s", sender)
                    handler_func(sender, request)
                except Exception as e:
                    log.error("Error processing request from %s: %s", sender, e)
                finally:
                    self._queue.task_done()

        thread = threading.Thread(target=worker, daemon=True)
        thread.start()
        self._worker_started = True
        log.info("Request queue worker started")

    @property
    def pending(self) -> int:
        """Number of items waiting in the queue."""
        return self._queue.qsize()
