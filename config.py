"""SatPhone configuration constants."""

import math
from pathlib import Path

# Project root (resolved so CWD doesn't matter)
PROJECT_DIR = Path(__file__).resolve().parent

# STAC catalog
STAC_URL = "https://planetarycomputer.microsoft.com/api/stac/v1"

# Output
OUTPUT_DIR = PROJECT_DIR / "thermal_output"

# Area of interest
AREA_SIZE_KM = 4


def area_bbox(lat: float, lon: float) -> list[float]:
    """
    Build a [west, south, east, north] bbox centered on (lat, lon).

    Uses latitude-corrected longitude span for constant real-world width.
    """
    lat_deg = AREA_SIZE_KM / 111.0
    lon_deg = AREA_SIZE_KM / (111.0 * math.cos(math.radians(lat)))
    return [lon - lon_deg, lat - lat_deg, lon + lon_deg, lat + lat_deg]


# Image
UPSCALE_FACTOR = 4
JPEG_QUALITY = 85

# Cloud filtering
SCENE_CLOUD_MAX = 85       # scene-level cloud %, relaxed since we check locally
LOCAL_CLEAR_MIN = 50        # minimum % of clear pixels in our window to accept
STAC_CANDIDATES = 10        # number of candidate scenes to fetch from STAC

# Thermal conversion: Landsat C2 L2 surface temperature
# T(K) = DN * scale + offset
THERMAL_SCALE = 0.00341802
THERMAL_OFFSET = 149.0

# Database
_TERMUX_DB = Path("/data/data/com.termux/files/home/satphone.db")
DB_PATH = _TERMUX_DB if _TERMUX_DB.parent.exists() else PROJECT_DIR / "satphone.db"

# SMS daemon
SMS_POLL_INTERVAL = 5          # seconds between inbox checks
SMS_FETCH_COUNT = 5            # messages to read per poll
MESSAGING_PACKAGE = "com.google.android.apps.messaging"  # Google Messages

# Shared storage path for MMS images (Tasker can read files here).
# Created by `termux-setup-storage`; falls back to project dir on desktop.
_TERMUX_SHARED = Path.home() / "storage" / "shared"
MMS_IMAGE_DIR = (
    (_TERMUX_SHARED / "Pictures" / "SatPhone")
    if _TERMUX_SHARED.exists()
    else OUTPUT_DIR
)

# Logging
LOG_FILE = PROJECT_DIR / "satphone.log"
LOG_MAX_BYTES = 5 * 1024 * 1024  # 5 MB per file
LOG_BACKUP_COUNT = 3

# Debug
DEBUG = False
