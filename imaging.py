"""Image processing, colormap, overlay burn-ins, and ASCII terminal preview."""

import time
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageFont

from config import UPSCALE_FACTOR, JPEG_QUALITY, THERMAL_SCALE, THERMAL_OFFSET
from logger import get_logger

# Avoid circular import -- use TYPE_CHECKING guard
from typing import TYPE_CHECKING
if TYPE_CHECKING:
    from thermal import SceneInfo

log = get_logger("satphone.imaging")


# ---------------------------------------------------------------------------
# Thermal colormap
# ---------------------------------------------------------------------------

# Color ramp stops: (normalized_position, R, G, B)
_COLORMAP_STOPS = np.array([
    [0.00,  80,  80,  85],   # mid gray (coldest)
    [0.70, 180, 180, 185],   # light gray
    [0.82, 205, 180, 185],   # pink
    [0.91, 240, 160,  60],   # orange
    [1.00, 255, 210,  80],   # gold (hottest)
])


def apply_thermal_colormap(data: np.ndarray) -> Image.Image:
    """
    Apply thermal colormap with gamma expansion for hot-end detail.

    Gradient: mid gray -> light gray -> pink -> orange -> gold (hottest)
    """
    norm = data.astype(np.float32) / 255.0

    # Gamma expansion stretches the hot end for more visual detail
    hot_threshold = 0.70
    hot_region = np.clip((norm - hot_threshold) / (1.0 - hot_threshold), 0, 1)
    norm = np.where(
        norm > hot_threshold,
        hot_threshold + np.power(hot_region, 0.6) * (1.0 - hot_threshold),
        norm,
    )

    # Interpolate each RGB channel across the color ramp
    positions = _COLORMAP_STOPS[:, 0]
    rgb = np.stack([
        np.interp(norm, positions, _COLORMAP_STOPS[:, ch]).clip(0, 255).astype(np.uint8)
        for ch in (1, 2, 3)
    ], axis=-1)

    return Image.fromarray(rgb)


# ---------------------------------------------------------------------------
# Color-ramp sampling helper (used by overlay)
# ---------------------------------------------------------------------------

def _sample_ramp_colors(num_steps: int) -> list[tuple[int, int, int]]:
    """Return a list of RGB tuples sampling the colormap at even intervals."""
    values = np.linspace(0, 255, num_steps).astype(np.uint8)
    row = values.reshape(1, -1)
    img = apply_thermal_colormap(row)
    pixels = np.array(img)[0]
    return [tuple(int(c) for c in px) for px in pixels]


# ---------------------------------------------------------------------------
# Overlay / burn-in
# ---------------------------------------------------------------------------

def _draw_text(draw, pos, text, font, fill=(255, 255, 255)):
    """Draw plain text onto the image."""
    draw.text(pos, text, fill=fill, font=font)


def draw_overlay(
    img: Image.Image,
    lat: float | None,
    lon: float | None,
    scene_info: "SceneInfo",
    stretch_info: dict,
) -> Image.Image:
    """
    Draw burn-in overlay in the bottom-left corner:
      triangle + N  (north arrow)
      lat lon
      datetime UTC
      Land  <min>K       <max>K
      [color ramp bar]
      Water <min>K       <max>K
    """
    draw = ImageDraw.Draw(img)
    img_w, img_h = img.size

    # Font sizing -- small relative to image
    font_size = max(11, img_w // 35)
    try:
        font = ImageFont.load_default(size=font_size)
    except TypeError:
        font = ImageFont.load_default()

    small_size = max(9, font_size - 2)
    try:
        small_font = ImageFont.load_default(size=small_size)
    except TypeError:
        small_font = font

    # Parse scene datetime
    dt_str = getattr(scene_info, "datetime", "unknown")
    try:
        dt = datetime.fromisoformat(dt_str.replace("Z", "+00:00"))
        dt_formatted = dt.strftime("%Y-%m-%d %H:%M:%S UTC")
    except (ValueError, AttributeError):
        dt_formatted = dt_str

    # Coordinate strings
    lat_str = f"{lat:.2f}" if lat is not None else "?"
    lon_str = f"{lon:.2f}" if lon is not None else "?"

    # Convert raw DN stretch values to Celsius
    def _dn_to_c(dn):
        return dn * THERMAL_SCALE + THERMAL_OFFSET - 273.15

    land_lo_c = _dn_to_c(stretch_info.get("land_low", 0))
    land_hi_c = _dn_to_c(stretch_info.get("land_high", 0))
    water_lo_c = _dn_to_c(stretch_info.get("water_low", 0))
    water_hi_c = _dn_to_c(stretch_info.get("water_high", 0))

    # Layout constants
    margin = max(8, img_w // 50)
    line_h = font_size + 3
    ramp_height = max(10, font_size * 2 // 3)
    gap = max(4, font_size // 4)  # space between min text and ramp bar

    # Measure the widest min-temp label to set ramp start offset
    land_min_txt = f"Land   {land_lo_c:.0f}\u00b0C"
    water_min_txt = f"Water  {water_lo_c:.0f}\u00b0C"
    land_min_bbox = draw.textbbox((0, 0), land_min_txt, font=small_font)
    water_min_bbox = draw.textbbox((0, 0), water_min_txt, font=small_font)
    label_w = max(land_min_bbox[2] - land_min_bbox[0], water_min_bbox[2] - water_min_bbox[0])
    ramp_x = margin + label_w + gap  # ramp starts after min label
    ramp_width = max(80, img_w // 4)

    # Total height of the overlay block
    # Items: triangle+N, coords, datetime, land label, ramp, water label
    arrow_h = font_size + line_h  # triangle + N together
    total_h = arrow_h + 3 * line_h + line_h + ramp_height + 4 + line_h + margin
    y = img_h - total_h - margin // 2
    x = margin

    # -- North arrow (drawn as a filled triangle polygon) --
    tri_size = font_size
    tri_cx = x + tri_size // 2
    tri_top = y
    tri_bot = y + tri_size
    draw.polygon(
        [(tri_cx, tri_top), (tri_cx - tri_size // 3, tri_bot), (tri_cx + tri_size // 3, tri_bot)],
        fill=(255, 255, 255),
    )
    y += tri_size + 2
    _draw_text(draw, (x, y), "N", font)
    y += line_h

    # -- Coordinates --
    _draw_text(draw, (x, y), f"{lat_str} {lon_str}", small_font)
    y += line_h

    # -- Datetime --
    _draw_text(draw, (x, y), dt_formatted, small_font)
    y += line_h

    # -- Land label + max on same row --
    land_max_txt = f"{land_hi_c:.0f}\u00b0C"
    _draw_text(draw, (x, y), land_min_txt, small_font)
    _draw_text(draw, (ramp_x + ramp_width + gap, y), land_max_txt, small_font)
    y += line_h

    # -- Color ramp bar (aligned to start after min labels) --
    colors = _sample_ramp_colors(ramp_width)
    for i, color in enumerate(colors):
        draw.rectangle([ramp_x + i, y, ramp_x + i + 1, y + ramp_height], fill=color)
    y += ramp_height + 4

    # -- Water label + max --
    water_max_txt = f"{water_hi_c:.0f}\u00b0C"
    _draw_text(draw, (x, y), water_min_txt, small_font)
    _draw_text(draw, (ramp_x + ramp_width + gap, y), water_max_txt, small_font)

    return img


# ---------------------------------------------------------------------------
# Main processing entry point
# ---------------------------------------------------------------------------

def process_thermal_image(
    data: np.ndarray,
    output_path: Path,
    scene_info: "SceneInfo",
    water_mask: np.ndarray | None = None,
    clear_mask: np.ndarray | None = None,
    lat: float | None = None,
    lon: float | None = None,
) -> Path:
    """
    Normalize, colormap, upscale, overlay, and save a thermal image.

    Args:
        data: Raw thermal DN array.
        output_path: Where to save the JPEG.
        scene_info: Scene metadata.
        water_mask: Boolean (True = water), or None.
        clear_mask: Boolean (True = clear sky), from QA_PIXEL, or None.
        lat, lon: Coordinates for overlay text.

    Returns the output Path.
    """
    start = time.time()
    data_f = data.astype(float)
    valid_data = data_f > 0

    # Combine valid pixels with clear-sky mask when available
    if clear_mask is not None and clear_mask.shape == data.shape:
        cloud_excluded = clear_mask.sum() < clear_mask.size
        if cloud_excluded:
            cloudy_pct = 100 * (~clear_mask & valid_data).sum() / valid_data.sum()
            log.info("  Excluding %.1f%% cloudy pixels from stretch", cloudy_pct)
        usable = valid_data & clear_mask
    else:
        usable = valid_data

    stretch_info: dict = {}

    if water_mask is not None and water_mask.shape == data.shape:
        # Pure-numpy morphology (avoids scipy dependency)
        def _morph(mask: np.ndarray, iterations: int, dilate: bool) -> np.ndarray:
            """Binary dilation (dilate=True) or erosion via 3×3 cross kernel."""
            out = mask.copy()
            for _ in range(iterations):
                shifted = np.zeros_like(out)
                for dr, dc in [(-1, 0), (1, 0), (0, -1), (0, 1), (0, 0)]:
                    sliced = out[
                        max(dr, 0): out.shape[0] + min(dr, 0) or None,
                        max(dc, 0): out.shape[1] + min(dc, 0) or None,
                    ]
                    target = shifted[
                        max(-dr, 0): shifted.shape[0] + min(-dr, 0) or None,
                        max(-dc, 0): shifted.shape[1] + min(-dc, 0) or None,
                    ]
                    if dilate:
                        target |= sliced
                    else:
                        target &= sliced
                if not dilate:
                    # Erosion: start from all-True, AND each shift
                    eroded = np.ones_like(out)
                    for dr, dc in [(-1, 0), (1, 0), (0, -1), (0, 1), (0, 0)]:
                        sliced = out[
                            max(dr, 0): out.shape[0] + min(dr, 0) or None,
                            max(dc, 0): out.shape[1] + min(dc, 0) or None,
                        ]
                        target = eroded[
                            max(-dr, 0): eroded.shape[0] + min(-dr, 0) or None,
                            max(-dc, 0): eroded.shape[1] + min(-dc, 0) or None,
                        ]
                        target &= sliced
                    out = eroded
                else:
                    out = shifted
            return out

        water_dilated = _morph(water_mask, iterations=2, dilate=True)
        water_eroded = _morph(water_mask, iterations=2, dilate=False)

        pure_water_mask = water_eroded & usable
        pure_land_mask = ~water_dilated & usable
        mixed_mask = (water_dilated & ~water_eroded) & valid_data

        mixed_pct = 100 * mixed_mask.sum() / valid_data.sum()
        log.info("  Mixed shoreline pixels: %.1f%%", mixed_pct)

        if pure_land_mask.any():
            land_low = np.percentile(data_f[pure_land_mask], 0.5)
            land_high = np.percentile(data_f[pure_land_mask], 99.9)
            log.info("  Land stretch: %.0f - %.0f", land_low, land_high)
        else:
            land_low = np.percentile(data_f[usable], 0.5)
            land_high = np.percentile(data_f[usable], 99.5)

        if pure_water_mask.any():
            water_low = np.percentile(data_f[pure_water_mask], 0.5)
            water_high = np.percentile(data_f[pure_water_mask], 99.9)
            log.info("  Water stretch: %.0f - %.0f", water_low, water_high)
        else:
            water_low, water_high = land_low, land_high

        stretch_info = {
            "land_low": land_low,
            "land_high": land_high,
            "water_low": water_low,
            "water_high": water_high,
        }

        # Normalize each zone independently (guard against uniform temperature)
        normalized = np.zeros_like(data_f)
        land_range = land_high - land_low
        water_range = water_high - water_low
        if land_range > 0 and pure_land_mask.any():
            normalized[pure_land_mask] = (
                (data_f[pure_land_mask] - land_low) / land_range * 255
            )
        if water_range > 0 and pure_water_mask.any():
            normalized[pure_water_mask] = (
                (data_f[pure_water_mask] - water_low) / water_range * 255
            )

        # Blend shoreline transition zone
        if mixed_mask.any() and land_range > 0 and water_range > 0:
            land_norm = (data_f - land_low) / land_range * 255
            water_norm = (data_f - water_low) / water_range * 255
            blend_weight = water_mask.astype(float)
            blended = water_norm * blend_weight + land_norm * (1 - blend_weight)
            normalized[mixed_mask] = blended[mixed_mask]

        normalized = np.clip(normalized, 0, 255).astype(np.uint8)
    else:
        # Single stretch from center region, using only clear pixels
        h, w = data.shape
        cy, cx = h // 2, w // 2
        margin = max(h, w) // 4
        margin = max(margin, 10)
        center_slice = (slice(cy - margin, cy + margin), slice(cx - margin, cx + margin))
        center_usable = usable[center_slice]
        center_data = data_f[center_slice]
        valid_center = center_data[center_usable]

        if valid_center.size == 0:
            # Fall back to all usable pixels if center is all clouds
            valid_center = data_f[usable]
        if valid_center.size == 0:
            raise ValueError("No valid clear thermal data")

        p_low = np.percentile(valid_center, 0.5)
        p_high = np.percentile(valid_center, 99.5)
        log.info("  Stretch: %.0f - %.0f", p_low, p_high)

        stretch_info = {
            "land_low": p_low,
            "land_high": p_high,
            "water_low": p_low,
            "water_high": p_high,
        }

        p_range = p_high - p_low
        if p_range > 0:
            normalized = np.clip(
                (data_f - p_low) / p_range * 255, 0, 255
            ).astype(np.uint8)
        else:
            normalized = np.full_like(data_f, 128, dtype=np.uint8)

    # Apply colormap
    img = apply_thermal_colormap(normalized)

    # Upscale
    new_size = (img.width * UPSCALE_FACTOR, img.height * UPSCALE_FACTOR)
    img = img.resize(new_size, Image.Resampling.BILINEAR)
    log.info("  Upscaled %dx: %sx%s", UPSCALE_FACTOR, img.width, img.height)

    # Burn-in overlay
    img = draw_overlay(img, lat, lon, scene_info, stretch_info)

    # Save
    output_path.parent.mkdir(parents=True, exist_ok=True)
    img.save(output_path, "JPEG", quality=JPEG_QUALITY, optimize=True)

    file_size_kb = output_path.stat().st_size / 1024
    elapsed = time.time() - start
    log.info("  Saved: %s (%.1f KB, %.2fs)", output_path, file_size_kb, elapsed)

    return output_path


# ---------------------------------------------------------------------------
# ASCII terminal preview
# ---------------------------------------------------------------------------

def ascii_preview(img: Image.Image, width: int = 30):
    """Print an ANSI true-color preview of the image center (no border)."""
    w, h = img.size
    crop_size = min(w, h) // 2
    left = (w - crop_size) // 2
    top = (h - crop_size) // 2
    center = img.crop((left, top, left + crop_size, top + crop_size))

    height = width // 2
    preview = center.resize((width, height), Image.Resampling.BILINEAR)
    pixels = np.array(preview)

    print()
    print("  Preview (center):")
    for row in pixels:
        line = "  "
        for r, g, b in row:
            line += f"\033[48;2;{r};{g};{b}m \033[0m"
        print(line)
    print()
