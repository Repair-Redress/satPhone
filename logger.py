"""Logging configuration for SatPhone."""

import logging
import threading
from logging.handlers import RotatingFileHandler

from config import LOG_FILE, LOG_MAX_BYTES, LOG_BACKUP_COUNT

_initialized = False
_init_lock = threading.Lock()


def get_logger(name: str = "satphone") -> logging.Logger:
    """
    Get a configured logger.

    Handlers are attached only to the root 'satphone' logger.
    Child loggers (satphone.thermal, satphone.imaging, etc.) inherit
    them via propagation, so messages are never duplicated.
    """
    global _initialized

    with _init_lock:
        if not _initialized:
            _initialized = True
            root = logging.getLogger("satphone")
            root.setLevel(logging.DEBUG)

            # Console handler -- INFO level, simple format
            ch = logging.StreamHandler()
            ch.setLevel(logging.INFO)
            ch.setFormatter(logging.Formatter("%(message)s"))
            root.addHandler(ch)

            # File handler -- DEBUG level, full timestamps
            LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
            fh = RotatingFileHandler(
                LOG_FILE,
                maxBytes=LOG_MAX_BYTES,
                backupCount=LOG_BACKUP_COUNT,
            )
            fh.setLevel(logging.DEBUG)
            fh.setFormatter(
                logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s")
            )
            root.addHandler(fh)

    return logging.getLogger(name)
