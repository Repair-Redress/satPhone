#!/data/data/com.termux/files/usr/bin/bash
# ============================================================
# SatPhone - Termux Setup Script
#
# Run once after cloning:
#   chmod +x setup_termux.sh && ./setup_termux.sh
#
# This installs all system packages and Python dependencies
# needed to run SatPhone on Termux (Android).
# ============================================================

set -e

echo "=== SatPhone Termux Setup ==="
echo ""

# --- 1. Update package repos ---
echo "[1/5] Updating Termux packages..."
pkg update -y && pkg upgrade -y

# --- 2. Install system dependencies ---
echo ""
echo "[2/5] Installing system packages..."
# python:        runtime
# gdal:          geospatial raster I/O (required by rasterio)
# libpng:        PNG support for Pillow
# libjpeg-turbo: JPEG support for Pillow (MMS output)
# proj:          coordinate transforms (required by rasterio)
# libxml2:       XML parsing (used by pystac-client)
# git:           for pulling updates
pkg install -y \
    python \
    gdal \
    libpng \
    libjpeg-turbo \
    proj \
    libxml2 \
    git

# --- 3. Create Python virtual environment ---
echo ""
echo "[3/5] Setting up Python virtual environment..."
if [ ! -d ".venv" ]; then
    python -m venv .venv
    echo "Created .venv"
else
    echo ".venv already exists, skipping"
fi
source .venv/bin/activate

# --- 4. Install Python packages ---
echo ""
echo "[4/5] Installing Python dependencies..."
pip install --upgrade pip
pip install -r requirements.txt

# --- 5. Create runtime directories ---
echo ""
echo "[5/5] Creating directories..."
mkdir -p thermal_output

echo ""
echo "=== Setup Complete ==="
echo ""
echo "To run SatPhone:"
echo "  source .venv/bin/activate"
echo "  python main.py 44.43 -110.59"
echo ""
echo "Or with SMS-style input:"
echo '  python main.py "therm 44.43 -110.59"'
echo ""
