"""
Rate limiting, abuse protection, and credit system for SatPhone.

Features:
- Per-phone rate limits (per minute, hour, day)
- Automatic blocking of abusive senders
- Manual blocklist
- Credit system integration ready

Usage:
    from rate_limit import RateLimiter

    limiter = RateLimiter()

    allowed, reason = limiter.check(phone_number)
    if allowed:
        limiter.log_request(phone_number)
"""

import sqlite3
import time
import threading
from contextlib import contextmanager
from pathlib import Path
from dataclasses import dataclass
from typing import Optional

from config import DB_PATH
from logger import get_logger

log = get_logger("satphone.ratelimit")


# === CONFIGURATION ===

@dataclass
class RateLimitConfig:
    per_minute: int = 2
    per_hour: int = 10
    per_day: int = 30
    abuse_threshold: int = 10  # limit hits before auto-block
    max_queue_size: int = 5


DEFAULT_CONFIG = RateLimitConfig()


# === DATABASE ===

@contextmanager
def _connect(db_path: Path):
    """Context manager for SQLite connections. Always closes on exit."""
    conn = sqlite3.connect(db_path)
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


class RateLimiter:
    """
    Rate limiter with SQLite-backed persistence. Thread-safe.

    Note: check() and log_request() are separate calls. If multiple
    threads call the handler concurrently, a small TOCTOU window exists.
    Use RequestQueue (sms.py) to serialize request processing.
    """

    def __init__(self, db_path: Optional[Path] = None, config: RateLimitConfig = DEFAULT_CONFIG):
        self.db_path = db_path or DB_PATH
        self.config = config
        self._lock = threading.Lock()
        self._init_db()

    def _init_db(self):
        with _connect(self.db_path) as conn:
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS requests (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    phone TEXT NOT NULL,
                    timestamp REAL NOT NULL
                );

                CREATE INDEX IF NOT EXISTS idx_requests_phone_time
                ON requests(phone, timestamp);

                CREATE TABLE IF NOT EXISTS rate_limit_hits (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    phone TEXT NOT NULL,
                    timestamp REAL NOT NULL,
                    reason TEXT
                );

                CREATE TABLE IF NOT EXISTS blocklist (
                    phone TEXT PRIMARY KEY,
                    reason TEXT,
                    blocked_at REAL,
                    expires_at REAL  -- NULL = permanent
                );

                CREATE TABLE IF NOT EXISTS users (
                    phone TEXT PRIMARY KEY,
                    credits INTEGER DEFAULT 0,
                    created_at REAL,
                    last_request REAL
                );
            """)

    def check(self, phone: str) -> tuple[bool, str]:
        """
        Check if a request is allowed.

        Returns (allowed, reason).  Reasons: ok, blocked, slow_down,
        hourly_limit, daily_limit.
        """
        now = time.time()

        with self._lock:
            with _connect(self.db_path) as conn:
                # Check blocklist
                row = conn.execute("""
                    SELECT 1 FROM blocklist
                    WHERE phone = ?
                    AND (expires_at IS NULL OR expires_at > ?)
                """, (phone, now)).fetchone()

                if row is not None:
                    return False, "blocked"

                counts = conn.execute("""
                    SELECT
                        SUM(CASE WHEN timestamp > ? THEN 1 ELSE 0 END),
                        SUM(CASE WHEN timestamp > ? THEN 1 ELSE 0 END),
                        SUM(CASE WHEN timestamp > ? THEN 1 ELSE 0 END)
                    FROM requests WHERE phone = ?
                """, (now - 60, now - 3600, now - 86400, phone)).fetchone()

                per_min = counts[0] or 0
                per_hour = counts[1] or 0
                per_day = counts[2] or 0

        if per_min >= self.config.per_minute:
            self._log_limit_hit(phone, "per_minute")
            return False, "slow_down"
        if per_hour >= self.config.per_hour:
            self._log_limit_hit(phone, "per_hour")
            return False, "hourly_limit"
        if per_day >= self.config.per_day:
            self._log_limit_hit(phone, "per_day")
            return False, "daily_limit"

        return True, "ok"

    def is_blocked(self, phone: str) -> bool:
        with self._lock:
            with _connect(self.db_path) as conn:
                row = conn.execute("""
                    SELECT 1 FROM blocklist
                    WHERE phone = ?
                    AND (expires_at IS NULL OR expires_at > ?)
                """, (phone, time.time())).fetchone()
                return row is not None

    def log_request(self, phone: str):
        """Log a successful request."""
        now = time.time()
        with self._lock:
            with _connect(self.db_path) as conn:
                conn.execute(
                    "INSERT INTO requests (phone, timestamp) VALUES (?, ?)",
                    (phone, now),
                )
                conn.execute("""
                    INSERT INTO users (phone, credits, created_at, last_request)
                    VALUES (?, 0, ?, ?)
                    ON CONFLICT(phone) DO UPDATE SET last_request = ?
                """, (phone, now, now, now))
                conn.execute(
                    "DELETE FROM requests WHERE timestamp < ?",
                    (now - 604800,),
                )

    def _log_limit_hit(self, phone: str, reason: str):
        now = time.time()
        with self._lock:
            with _connect(self.db_path) as conn:
                conn.execute(
                    "INSERT INTO rate_limit_hits (phone, timestamp, reason) VALUES (?, ?, ?)",
                    (phone, now, reason),
                )
                hits = conn.execute("""
                    SELECT COUNT(*) FROM rate_limit_hits
                    WHERE phone = ? AND timestamp > ?
                """, (phone, now - 3600)).fetchone()[0]

                if hits >= self.config.abuse_threshold:
                    conn.execute("""
                        INSERT OR REPLACE INTO blocklist (phone, reason, blocked_at, expires_at)
                        VALUES (?, ?, ?, ?)
                    """, (phone, "auto_block_abuse", now, now + 86400))
                    log.warning("Auto-blocked %s for abuse", phone)

                conn.execute(
                    "DELETE FROM rate_limit_hits WHERE timestamp < ?",
                    (now - 86400,),
                )

    def block(self, phone: str, reason: str = "manual", duration_hours: Optional[float] = None):
        now = time.time()
        expires = now + (duration_hours * 3600) if duration_hours else None
        with self._lock:
            with _connect(self.db_path) as conn:
                conn.execute("""
                    INSERT OR REPLACE INTO blocklist (phone, reason, blocked_at, expires_at)
                    VALUES (?, ?, ?, ?)
                """, (phone, reason, now, expires))

    def unblock(self, phone: str):
        with self._lock:
            with _connect(self.db_path) as conn:
                conn.execute("DELETE FROM blocklist WHERE phone = ?", (phone,))

    def get_stats(self, phone: str) -> dict:
        now = time.time()
        with self._lock:
            with _connect(self.db_path) as conn:
                counts = conn.execute("""
                    SELECT
                        SUM(CASE WHEN timestamp > ? THEN 1 ELSE 0 END),
                        SUM(CASE WHEN timestamp > ? THEN 1 ELSE 0 END),
                        SUM(CASE WHEN timestamp > ? THEN 1 ELSE 0 END),
                        COUNT(*)
                    FROM requests WHERE phone = ?
                """, (now - 60, now - 3600, now - 86400, phone)).fetchone()

                user = conn.execute(
                    "SELECT credits, created_at FROM users WHERE phone = ?",
                    (phone,),
                ).fetchone()

        return {
            "requests_per_minute": counts[0] or 0,
            "requests_per_hour": counts[1] or 0,
            "requests_per_day": counts[2] or 0,
            "total_requests": counts[3] or 0,
            "credits": user[0] if user else 0,
            "member_since": user[1] if user else None,
            "limits": {
                "per_minute": self.config.per_minute,
                "per_hour": self.config.per_hour,
                "per_day": self.config.per_day,
            },
        }


# === CREDIT SYSTEM ===

class CreditManager:
    """Manage user credits for paid requests. Thread-safe."""

    def __init__(self, db_path: Optional[Path] = None):
        self.db_path = db_path or DB_PATH
        self._lock = threading.Lock()

    def get_credits(self, phone: str) -> int:
        with self._lock:
            with _connect(self.db_path) as conn:
                row = conn.execute(
                    "SELECT credits FROM users WHERE phone = ?", (phone,)
                ).fetchone()
                return row[0] if row else 0

    def use_credit(self, phone: str) -> bool:
        with self._lock:
            with _connect(self.db_path) as conn:
                cursor = conn.execute(
                    "UPDATE users SET credits = credits - 1 WHERE phone = ? AND credits > 0",
                    (phone,),
                )
                return cursor.rowcount > 0

    def add_credits(self, phone: str, amount: int):
        now = time.time()
        with self._lock:
            with _connect(self.db_path) as conn:
                conn.execute("""
                    INSERT INTO users (phone, credits, created_at, last_request)
                    VALUES (?, ?, ?, NULL)
                    ON CONFLICT(phone) DO UPDATE SET credits = credits + ?
                """, (phone, amount, now, amount))


# === RESPONSE MESSAGES ===

RATE_LIMIT_RESPONSES = {
    "blocked": None,  # silent ignore
    "slow_down": "Please wait a moment between requests.",
    "hourly_limit": "Hourly limit reached ({used}/{limit}). Try again later.",
    "daily_limit": "Daily limit reached ({used}/{limit}). Try again tomorrow.",
    "queue_full": "Service is busy. Please try again in a minute.",
    "no_credits": "No credits remaining. Text BUY for more.",
}


def get_rejection_message(reason: str, stats: dict = None) -> Optional[str]:
    """Get user-friendly rejection message."""
    msg = RATE_LIMIT_RESPONSES.get(reason)
    if msg and stats:
        if reason == "hourly_limit":
            msg = msg.format(
                used=stats["requests_per_hour"], limit=stats["limits"]["per_hour"]
            )
        elif reason == "daily_limit":
            msg = msg.format(
                used=stats["requests_per_day"], limit=stats["limits"]["per_day"]
            )
    return msg


# === OVERLOAD ALERTING ===

class OverloadMonitor:
    """Detect when the service is being overwhelmed and send alerts. Thread-safe."""

    def __init__(
        self,
        db_path: Optional[Path] = None,
        alert_phone: str = None,
        alert_threshold_per_minute: int = 20,
        alert_cooldown_minutes: int = 15,
    ):
        self.db_path = db_path or DB_PATH
        self.alert_phone = alert_phone
        self.threshold = alert_threshold_per_minute
        self.cooldown = alert_cooldown_minutes * 60
        self._last_alert = 0
        self._lock = threading.Lock()
        self._init_db()

    def _init_db(self):
        with _connect(self.db_path) as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS incoming_log (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    phone TEXT,
                    timestamp REAL
                )
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS alerts (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp REAL,
                    reason TEXT,
                    details TEXT
                )
            """)

    def log_incoming(self, phone: str):
        now = time.time()
        with self._lock:
            with _connect(self.db_path) as conn:
                conn.execute(
                    "INSERT INTO incoming_log (phone, timestamp) VALUES (?, ?)",
                    (phone, now),
                )
                conn.execute(
                    "DELETE FROM incoming_log WHERE timestamp < ?",
                    (now - 3600,),
                )

    def check_overload(self) -> tuple[bool, dict]:
        now = time.time()
        with self._lock:
            with _connect(self.db_path) as conn:
                stats = conn.execute("""
                    SELECT
                        COUNT(*) as total,
                        COUNT(DISTINCT phone) as unique_senders
                    FROM incoming_log
                    WHERE timestamp > ?
                """, (now - 60,)).fetchone()

        total = stats[0] or 0
        unique = stats[1] or 0

        return total >= self.threshold, {
            "messages_per_minute": total,
            "unique_senders": unique,
            "threshold": self.threshold,
            "is_attack": unique <= 3 and total > 10,
        }

    def should_alert(self) -> bool:
        return (time.time() - self._last_alert) > self.cooldown

    def send_alert(self, stats: dict, send_sms_func) -> bool:
        if not self.alert_phone:
            log.warning("No alert phone configured")
            return False

        if not self.should_alert():
            return False

        if stats.get("is_attack"):
            msg = (
                f"ATTACK DETECTED: {stats['messages_per_minute']} msgs/min "
                f"from {stats['unique_senders']} sender(s)"
            )
        else:
            msg = (
                f"High traffic: {stats['messages_per_minute']} msgs/min "
                f"from {stats['unique_senders']} senders"
            )

        try:
            send_sms_func(self.alert_phone, msg)
            self._last_alert = time.time()

            with self._lock:
                with _connect(self.db_path) as conn:
                    conn.execute(
                        "INSERT INTO alerts (timestamp, reason, details) VALUES (?, ?, ?)",
                        (time.time(), "overload", str(stats)),
                    )

            log.info("Alert sent to %s", self.alert_phone)
            return True
        except Exception as e:
            log.error("Failed to send alert: %s", e)
            return False

    def get_attack_sources(self, limit: int = 10) -> list[tuple[str, int]]:
        with self._lock:
            with _connect(self.db_path) as conn:
                rows = conn.execute("""
                    SELECT phone, COUNT(*) as count
                    FROM incoming_log
                    WHERE timestamp > ?
                    GROUP BY phone
                    ORDER BY count DESC
                    LIMIT ?
                """, (time.time() - 3600, limit)).fetchall()
                return rows


def create_sms_handler(alert_phone: str, send_sms_func, process_func):
    """
    Create a hardened SMS handler with overload protection.

    Order of operations:
    1. Log incoming (fast)
    2. Check overload (fast)
    3. Send alert if needed (before heavy processing)
    4. Rate limit check
    5. Process request (slow)
    """
    monitor = OverloadMonitor(alert_phone=alert_phone)
    limiter = RateLimiter()

    def handler(sender: str, body: str):
        monitor.log_incoming(sender)

        is_overloaded, stats = monitor.check_overload()

        if is_overloaded:
            monitor.send_alert(stats, send_sms_func)
            if stats.get("is_attack"):
                return

        allowed, reason = limiter.check(sender)
        if not allowed:
            msg = get_rejection_message(reason, limiter.get_stats(sender))
            if msg:
                send_sms_func(sender, msg)
            return

        limiter.log_request(sender)
        process_func(sender, body)

    return handler
