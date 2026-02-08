"""STAC search and satellite data fetching."""

import time
from dataclasses import dataclass
from typing import Optional

import numpy as np
import pystac_client
import planetary_computer
import rasterio
from rasterio.windows import from_bounds
from rasterio.enums import Resampling
from rasterio.warp import transform_bounds

from config import (
    STAC_URL, area_bbox,
    SCENE_CLOUD_MAX, LOCAL_CLEAR_MIN, STAC_CANDIDATES,
)
from logger import get_logger

log = get_logger("satphone.thermal")


@dataclass
class SceneInfo:
    """Metadata for a selected Landsat scene."""
    id: str
    datetime: str
    cloud_cover: Optional[float]
    local_clear: float
    sensor: str
    bbox_4326: list[float]
    thermal_url: str
    qa_pixel_url: Optional[str] = None


def _get_catalog():
    """Open the Planetary Computer STAC catalog (shared helper)."""
    return pystac_client.Client.open(
        STAC_URL,
        modifier=planetary_computer.sign_inplace,
    )


# GDAL environment for remote reads: timeouts + retries at the HTTP layer
_GDAL_ENV = {
    "GDAL_HTTP_TIMEOUT": "30",
    "GDAL_HTTP_MAX_RETRY": "3",
    "GDAL_HTTP_RETRY_DELAY": "2",
    "GDAL_DISABLE_READDIR_ON_OPEN": "EMPTY_DIR",
    "CPL_VSIL_CURL_ALLOWED_EXTENSIONS": ".tif,.tiff",
}


# ---------------------------------------------------------------------------
# Retry helper
# ---------------------------------------------------------------------------

def _retry(fn, max_attempts: int = 3, backoff: float = 2.0, label: str = ""):
    """Call fn() with exponential backoff retries on failure."""
    last_err = None
    for attempt in range(1, max_attempts + 1):
        try:
            return fn()
        except Exception as e:
            last_err = e
            if attempt < max_attempts:
                delay = backoff ** (attempt - 1)
                log.warning(
                    "  %s attempt %d/%d failed (%s), retrying in %.0fs...",
                    label, attempt, max_attempts, e, delay,
                )
                time.sleep(delay)
            else:
                log.error("  %s failed after %d attempts: %s", label, max_attempts, e)
    raise last_err


# ---------------------------------------------------------------------------
# QA pixel helpers
# ---------------------------------------------------------------------------

def _qa_clear_mask(qa: np.ndarray) -> np.ndarray:
    """
    Decode Landsat C2 QA_PIXEL band into a boolean clear-sky mask.

    Excludes: fill, dilated cloud, cloud, cloud shadow.
    """
    fill = (qa & (1 << 0)) > 0
    dilated_cloud = (qa & (1 << 1)) > 0
    cloud = (qa & (1 << 3)) > 0
    cloud_shadow = (qa & (1 << 4)) > 0
    return ~(fill | dilated_cloud | cloud | cloud_shadow)


def _fetch_qa_for_bbox(qa_url: str, bbox_4326: list) -> np.ndarray:
    """Windowed read of QA_PIXEL band for a bbox. Returns raw uint16 array."""
    with rasterio.Env(**_GDAL_ENV):
        with rasterio.open(qa_url) as src:
            bbox_native = transform_bounds("EPSG:4326", src.crs, *bbox_4326)
            window = from_bounds(*bbox_native, transform=src.transform)
            return src.read(1, window=window)


def _check_local_clear(item, bbox_4326: list) -> float:
    """
    Quick check: what percentage of our window is cloud-free?

    Returns clear percentage (0-100).  If QA data is unavailable, returns
    100.0 (assume clear) since we already filter by scene-level cloud cover.
    """
    if "qa_pixel" not in item.assets:
        log.info("  No QA_PIXEL asset, assuming clear")
        return 100.0

    qa_url = item.assets["qa_pixel"].href
    qa = _fetch_qa_for_bbox(qa_url, bbox_4326)
    clear = _qa_clear_mask(qa)
    return 100.0 * clear.sum() / clear.size


# ---------------------------------------------------------------------------
# STAC search with local cloud check
# ---------------------------------------------------------------------------

def search_stac(lat: float, lon: float, before_date: str = None) -> SceneInfo:
    """
    Search for the most recent Landsat scene that is locally cloud-free.

    Fetches up to STAC_CANDIDATES scenes (relaxed cloud filter), then
    checks each one's QA_PIXEL band over our specific window.  Returns
    the first scene with >= LOCAL_CLEAR_MIN % clear pixels.
    """
    start = time.time()

    def _do_search():
        search_kwargs = {
            "collections": ["landsat-c2-l2"],
            "bbox": bbox,
            "query": {"eo:cloud_cover": {"lt": SCENE_CLOUD_MAX}},
            "max_items": STAC_CANDIDATES,
            "sortby": ["-properties.datetime"],
        }

        if before_date:
            search_kwargs["datetime"] = f"../{before_date}"

        search = _get_catalog().search(**search_kwargs)
        return list(search.items())

    bbox = area_bbox(lat, lon)

    items = _retry(_do_search, max_attempts=3, label="STAC search")

    if not items:
        raise ValueError(f"No scenes found for {lat}, {lon}")

    elapsed_search = time.time() - start
    log.info("  STAC returned %d candidates (%.2fs)", len(items), elapsed_search)

    # Check each candidate's local cloud cover via QA_PIXEL
    for i, item in enumerate(items):
        props = item.properties
        scene_cloud = props.get("eo:cloud_cover")
        scene_date = props.get("datetime", "?")[:10]
        cloud_str = f"{scene_cloud:.0f}" if scene_cloud is not None else "?"

        try:
            local_clear = _check_local_clear(item, bbox)
        except Exception as e:
            log.warning(
                "  [%d/%d] %s  Failed to check cloud cover: %s",
                i + 1, len(items), scene_date, e,
            )
            continue

        status = "CLEAR" if local_clear >= LOCAL_CLEAR_MIN else "cloudy"
        log.info(
            "  [%d/%d] %s  scene=%s%%  local=%.0f%% clear  %s",
            i + 1, len(items), scene_date, cloud_str, local_clear, status,
        )

        if local_clear >= LOCAL_CLEAR_MIN:
            # Locate thermal band
            thermal_asset = None
            for key in ["lwir11", "st_b10", "ST_B10"]:
                if key in item.assets:
                    thermal_asset = item.assets[key]
                    break
            if not thermal_asset:
                log.warning("  No thermal band, skipping")
                continue

            info = SceneInfo(
                id=item.id,
                datetime=props.get("datetime", "unknown"),
                cloud_cover=scene_cloud,
                local_clear=local_clear,
                sensor=props.get("platform", "unknown"),
                bbox_4326=bbox,
                thermal_url=thermal_asset.href,
                qa_pixel_url=(
                    item.assets["qa_pixel"].href
                    if "qa_pixel" in item.assets
                    else None
                ),
            )

            elapsed = time.time() - start
            log.info("  Selected: %s (%.2fs total)", info.id, elapsed)
            return info

    raise ValueError(
        f"No locally clear scenes found for {lat}, {lon} "
        f"(checked {len(items)} candidates, need {LOCAL_CLEAR_MIN}% clear)"
    )


# ---------------------------------------------------------------------------
# Data fetching
# ---------------------------------------------------------------------------

def fetch_thermal_data(scene_info: SceneInfo) -> tuple[np.ndarray, dict]:
    """
    Fetch thermal data via windowed COG read.

    Returns:
        (data, crop_info) -- square-cropped thermal array and crop metadata.
    """
    start = time.time()
    url = scene_info.thermal_url
    bbox_4326 = scene_info.bbox_4326

    log.info("  URL: %s...", url[:70])

    def _do_read():
        with rasterio.Env(**_GDAL_ENV):
            with rasterio.open(url) as src:
                log.info("  Source CRS: %s", src.crs)
                log.info("  Source size: %sx%s", src.width, src.height)

                bbox_native = transform_bounds("EPSG:4326", src.crs, *bbox_4326)
                window = from_bounds(*bbox_native, transform=src.transform)

                log.info(
                    "  Window size: %sx%s native pixels",
                    int(window.width),
                    int(window.height),
                )

                return src.read(1, window=window)

    data = _retry(_do_read, max_attempts=3, label="Thermal COG read")

    # Square crop (center)
    h, w = data.shape
    size = min(h, w)
    y_off = (h - size) // 2
    x_off = (w - size) // 2
    crop_info = {"y_off": y_off, "x_off": x_off, "size": size, "orig_shape": (h, w)}

    if h != w:
        data = data[y_off : y_off + size, x_off : x_off + size]
        log.info("  Cropped to square: %s", data.shape)

    elapsed = time.time() - start
    log.info("  Fetched: %s (%.2fs)", data.shape, elapsed)
    return data, crop_info


def fetch_clear_mask(scene_info: SceneInfo, crop_info: dict) -> np.ndarray | None:
    """
    Fetch QA_PIXEL and return a boolean clear-sky mask, aligned with
    the thermal data (same window, same square crop).
    """
    qa_url = scene_info.qa_pixel_url
    if not qa_url:
        return None

    start = time.time()
    bbox_4326 = scene_info.bbox_4326

    qa = _retry(
        lambda: _fetch_qa_for_bbox(qa_url, bbox_4326),
        max_attempts=3,
        label="QA_PIXEL read",
    )

    # Apply same square crop
    y_off = crop_info["y_off"]
    x_off = crop_info["x_off"]
    size = crop_info["size"]
    if qa.shape != (size, size):
        qa = qa[y_off : y_off + size, x_off : x_off + size]

    clear = _qa_clear_mask(qa)
    clear_pct = 100 * clear.sum() / clear.size

    elapsed = time.time() - start
    log.info("  Clear pixels: %.1f%% (%.2fs)", clear_pct, elapsed)

    return clear


def fetch_worldcover(bbox_4326: list, crop_info: dict) -> np.ndarray | None:
    """
    Fetch ESA WorldCover land cover data and create a water mask.

    Returns:
        Boolean array (True = water) or None if data unavailable.
    """
    start = time.time()

    def _do_search():
        search = _get_catalog().search(
            collections=["esa-worldcover"],
            bbox=bbox_4326,
            query={"esa_worldcover:product_version": {"eq": "2.0.0"}},
            max_items=1,
        )
        return list(search.items())

    try:
        items = _retry(_do_search, max_attempts=2, label="WorldCover STAC search")
    except Exception as e:
        log.warning("  WorldCover search failed: %s, skipping land/water mask", e)
        return None

    if not items:
        log.warning("  No WorldCover data found, skipping land/water mask")
        return None

    item = items[0]

    # Guard against missing "map" asset
    if "map" not in item.assets:
        log.warning("  WorldCover item missing 'map' asset, skipping land/water mask")
        return None

    map_url = item.assets["map"].href

    def _do_read():
        with rasterio.Env(**_GDAL_ENV):
            with rasterio.open(map_url) as src:
                bbox_native = transform_bounds("EPSG:4326", src.crs, *bbox_4326)
                window = from_bounds(*bbox_native, transform=src.transform)

                orig_shape = crop_info["orig_shape"]
                return src.read(
                    1,
                    window=window,
                    out_shape=orig_shape,
                    resampling=Resampling.nearest,
                )

    try:
        data = _retry(_do_read, max_attempts=2, label="WorldCover COG read")
    except Exception as e:
        log.warning("  WorldCover read failed: %s, skipping land/water mask", e)
        return None

    # Apply the same square crop used on thermal data
    y_off = crop_info["y_off"]
    x_off = crop_info["x_off"]
    size = crop_info["size"]
    data = data[y_off : y_off + size, x_off : x_off + size]

    water_mask = data == 80
    water_pct = 100 * water_mask.sum() / water_mask.size

    elapsed = time.time() - start
    log.info("  Water coverage: %.1f%% (%.2fs)", water_pct, elapsed)

    return water_mask
