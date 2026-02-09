#!/data/data/com.termux/files/usr/bin/bash
# ============================================================
# SatPhone - Termux Setup Script
#
# Run once after cloning:
#   chmod +x setup_termux.sh && ./setup_termux.sh
#
# Strategy:
#   Heavy native packages (numpy, scipy, Pillow) are installed
#   via Termux's package manager (pre-compiled for Android).
#   A venv with --system-site-packages makes them available.
#   Remaining pure-Python packages install via pip normally.
# ============================================================

set -e

echo "=== SatPhone Termux Setup ==="
echo ""

# --- 1. Update and add tur-repo for pre-built Python packages ---
echo "[1/5] Updating Termux packages..."
pkg update -y && pkg upgrade -y
pkg install -y tur-repo

# --- 2. Install system + pre-compiled Python packages ---
echo ""
echo "[2/5] Installing system packages..."
pkg install -y \
    python \
    python-numpy \
    python-scipy \
    python-pillow \
    gdal \
    proj \
    libxml2 \
    libjpeg-turbo \
    libpng \
    build-essential \
    git

# --- 3. Create venv with access to system packages ---
echo ""
echo "[3/5] Setting up Python virtual environment..."
if [ ! -d ".venv" ]; then
    python -m venv --system-site-packages .venv
    echo "Created .venv (with system site-packages)"
else
    echo ".venv already exists, skipping"
fi
source .venv/bin/activate

# --- 4. Install remaining Python packages via pip ---
# numpy, scipy, Pillow are already satisfied by system packages.
# pip will skip them and only install what's missing.
echo ""
echo "[4/5] Installing remaining Python dependencies..."
pip install --upgrade pip
pip install rasterio pystac-client planetary-computer

# --- 5. Create runtime directories ---
echo ""
echo "[5/5] Creating directories..."
mkdir -p thermal_output

# --- Verify ---
echo ""
echo "Verifying imports..."
python -c "
import numpy, scipy, PIL, rasterio, pystac_client, planetary_computer
print('  numpy', numpy.__version__)
print('  scipy', scipy.__version__)
print('  Pillow', PIL.__version__)
print('  rasterio', rasterio.__version__)
print('All dependencies OK')
"

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
