# SatPhone

Satellite thermal imagery delivered via SMS. Runs on a phone in Termux.

Text a location, get back a thermal satellite image — pulled from Landsat on Microsoft Planetary Computer, processed on-device, and sent back as MMS.

## How It Works

```
User texts: "therm 44.43 -110.59"
  → Geocode / validate coordinates
  → STAC search (Planetary Computer, Landsat C2 L2)
  → Windowed COG read (~500KB vs 100MB full scene)
  → Thermal colormap + overlay
  → JPEG compressed for MMS (<100KB)
  → Reply with image
```

## Quick Start

### Desktop / Development

```bash
git clone https://github.com/YOUR_USER/satphone.git
cd satphone
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
python main.py 44.43 -110.59
```

### Termux (Android)

```bash
git clone https://github.com/YOUR_USER/satphone.git
cd satphone
chmod +x setup_termux.sh
./setup_termux.sh
source .venv/bin/activate
python main.py 44.43 -110.59
```

## Usage

```bash
# Direct coordinates
python main.py <lat> <lon>
python main.py <lat> <lon> <YYYY-MM-DD>

# SMS-style message
python main.py "therm 44.43 -110.59"
python main.py "therm 44.43 -110.59 2025-10-06"
python main.py "therm help"
```

## Project Structure

```
main.py           Entry point and pipeline orchestrator
config.py         All configuration constants and paths
thermal.py        STAC search and satellite data fetching
imaging.py        Thermal colormap, overlay, image processing
sms.py            SMS message parsing, validation, request queue
rate_limit.py     Rate limiting, abuse protection, credit system
logger.py         Logging configuration (rotating file + console)
setup_termux.sh   One-command Termux setup
```

## Data Sources

- **Satellite imagery:** [Microsoft Planetary Computer](https://planetarycomputer.microsoft.com/) (free, no API key)
- **Collection:** Landsat Collection 2 Level 2 (`landsat-c2-l2`)
- **Thermal band:** `lwir11` (Band 10 thermal infrared)
- **Land/water mask:** ESA WorldCover v2.0

## Target Hardware

Designed to run on constrained Android devices via Termux:

- Moto G Power 2024 5G (4GB RAM, MediaTek Dimensity 7020)
- Windowed COG reads keep memory usage low (~500KB per request vs 100MB full scene)
- Pipeline completes in ~15-25s on-device

## Configuration

All constants live in `config.py`. Key settings:

| Setting | Default | Description |
|---------|---------|-------------|
| `AREA_SIZE_KM` | 4 | Size of the image area (km × km) |
| `SCENE_CLOUD_MAX` | 85 | Max scene-level cloud cover % |
| `LOCAL_CLEAR_MIN` | 50 | Min locally clear pixels % |
| `STAC_CANDIDATES` | 10 | Number of scenes to check |
| `JPEG_QUALITY` | 85 | Output JPEG quality |
| `DEBUG` | False | Enable tracemalloc profiling |

## Persistence

Log files (`satphone.log`), the database (`satphone.db`), and generated images (`thermal_output/`) are all gitignored — they persist locally through `git pull` updates and are never overwritten by upstream changes.

## License

MIT
