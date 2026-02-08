#!/usr/bin/env python3
"""
SatPhone - Satellite Thermal Imagery SMS Service

Usage:
    python main.py 44.43 -110.59                    # Direct lat/lon
    python main.py 44.43 -110.59 2025-10-06         # With date filter
    python main.py "therm 44.43 -110.59"            # SMS-style message
    python main.py "therm help"                      # Show help text
"""

import sys
import time
from pathlib import Path

from PIL import Image as PILImage

from config import OUTPUT_DIR, AREA_SIZE_KM, DEBUG
from logger import get_logger
from thermal import search_stac, fetch_thermal_data, fetch_clear_mask, fetch_worldcover
from imaging import process_thermal_image, ascii_preview
from sms import parse_message, HELP_TEXT

log = get_logger("satphone")


def run_pipeline(lat: float, lon: float, before_date: str = None) -> Path:
    """
    Run the full thermal image pipeline.

    Returns the path to the output JPEG.
    """
    if DEBUG:
        import tracemalloc
        tracemalloc.start()

    total_start = time.time()

    try:
        log.info("")
        log.info("Satellite Thermal Image Fetcher")
        log.info("Location: %.4f, %.4f", lat, lon)
        log.info("Area: %dkm x %dkm", AREA_SIZE_KM, AREA_SIZE_KM)
        if before_date:
            log.info("Before date: %s", before_date)
        log.info("-" * 50)

        # Step 1: STAC search (with local cloud check via QA_PIXEL)
        log.info("")
        log.info("[1/5] Searching STAC (checking local cloud cover)...")
        scene_info = search_stac(lat, lon, before_date)

        # Step 2: Fetch thermal data
        log.info("")
        log.info("[2/5] Fetching thermal data...")
        data, crop_info = fetch_thermal_data(scene_info)

        # Step 3: Fetch clear-sky mask
        log.info("")
        log.info("[3/5] Fetching clear-sky mask...")
        clear_mask = fetch_clear_mask(scene_info, crop_info)

        # Step 4: Fetch WorldCover mask
        log.info("")
        log.info("[4/5] Fetching land/water mask...")
        water_mask = fetch_worldcover(scene_info.bbox_4326, crop_info)

        # Step 5: Process and save
        log.info("")
        log.info("[5/5] Processing image...")
        timestamp = int(time.time())
        output_path = OUTPUT_DIR / f"thermal_{lat:.2f}_{lon:.2f}_{timestamp}.jpg"
        process_thermal_image(
            data, output_path, scene_info, water_mask,
            clear_mask=clear_mask, lat=lat, lon=lon,
        )
    finally:
        total_time = time.time() - total_start
        if DEBUG:
            import tracemalloc
            _current_mem, peak_mem = tracemalloc.get_traced_memory()
            tracemalloc.stop()

    mem_str = f" | Peak memory: {peak_mem / 1024 / 1024:.1f} MB" if DEBUG else ""
    log.info("")
    log.info("Complete in %.1fs%s | Output: %s", total_time, mem_str, output_path)

    # Terminal preview
    ascii_preview(PILImage.open(output_path))

    return output_path


def main():
    args = sys.argv[1:]

    if not args:
        print(HELP_TEXT)
        sys.exit(0)

    # Single quoted argument -> treat as SMS message body
    if len(args) == 1 and args[0].lower().startswith("therm"):
        request, error = parse_message(args[0])
        if error:
            print(error)
            sys.exit(0)
        if request is None:
            print("Not a therm message.")
            sys.exit(1)
        run_pipeline(request.lat, request.lon, request.before_date)
    else:
        # Direct lat lon [date] arguments
        try:
            lat = float(args[0])
            lon = float(args[1])
            before_date = args[2] if len(args) >= 3 else None
        except (ValueError, IndexError):
            print("Usage: python main.py <lat> <lon> [YYYY-MM-DD]")
            print('       python main.py "therm <lat> <lon> [YYYY-MM-DD]"')
            sys.exit(1)
        run_pipeline(lat, lon, before_date)


if __name__ == "__main__":
    main()
