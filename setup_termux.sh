#!/data/data/com.termux/files/usr/bin/bash
# ============================================================
# SatPhone - Termux Setup Script
#
# Run once after cloning:
#   chmod +x setup_termux.sh && ./setup_termux.sh
#
# Strategy:
#   1. Try Termux pre-built Python packages (fastest)
#   2. Fall back to pip with Termux-specific build flags
#   3. Install pure-Python packages normally via pip
# ============================================================

set -e

echo "=== SatPhone Termux Setup ==="
echo ""

# --- 1. Update repos and install core packages ---
echo "[1/6] Updating Termux packages..."
pkg update -y && pkg upgrade -y

echo ""
echo "[2/6] Installing system dependencies..."
pkg install -y \
    python \
    build-essential \
    binutils \
    ninja \
    cmake \
    gdal \
    proj \
    libxml2 \
    libjpeg-turbo \
    libpng \
    libopenblas \
    git

# --- 2. Try tur-repo for pre-built Python packages ---
echo ""
echo "[3/6] Installing Python scientific packages..."

# Try tur-repo first (has pre-compiled numpy/scipy/pillow)
if pkg install -y tur-repo 2>/dev/null; then
    echo "  tur-repo available, trying pre-built packages..."
    pkg install -y python-numpy python-scipy python-pillow 2>/dev/null && {
        echo "  Pre-built packages installed successfully"
        PREBUILT=true
    } || {
        echo "  Pre-built packages not available in tur-repo"
        PREBUILT=false
    }
else
    echo "  tur-repo not available"
    PREBUILT=false
fi

# Fall back to pip with Termux build flags
if [ "$PREBUILT" = false ]; then
    echo "  Building from source with Termux flags..."
    export LDFLAGS="-lm -lcompiler_rt"
    export MATHLIB=m

    pip install --upgrade pip setuptools wheel meson-python meson
    pip install ninja || true  # OK if fails — system ninja is used

    echo "  Installing numpy (this may take a few minutes)..."
    pip install numpy

    echo "  Installing scipy..."
    pip install scipy

    echo "  Installing Pillow..."
    pip install Pillow
fi

# --- 3. Create venv with access to system packages ---
echo ""
echo "[4/6] Setting up Python virtual environment..."
if [ ! -d ".venv" ]; then
    python -m venv --system-site-packages .venv
    echo "  Created .venv (with system site-packages)"
else
    echo "  .venv already exists, skipping"
fi
source .venv/bin/activate

# --- 4. Install remaining packages via pip ---
echo ""
echo "[5/6] Installing remaining Python dependencies..."
pip install --upgrade pip

# rasterio needs GDAL — set config path
export GDAL_CONFIG="$(which gdal-config 2>/dev/null || echo '')"
if [ -z "$GDAL_CONFIG" ]; then
    echo "  WARNING: gdal-config not found. rasterio may fail to build."
    echo "  Try: pkg install gdal"
fi

pip install rasterio pystac-client planetary-computer

# --- 5. Create runtime directories ---
echo ""
echo "[6/6] Creating directories..."
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
" && echo "" && echo "=== Setup Complete ===" || {
    echo ""
    echo "=== Setup had errors ==="
    echo "Some imports failed. Try running the script again,"
    echo "or install failing packages manually with:"
    echo "  LDFLAGS='-lm -lcompiler_rt' pip install <package>"
    exit 1
}

echo ""
echo "To run SatPhone:"
echo "  source .venv/bin/activate"
echo "  python main.py 44.43 -110.59"
echo ""
echo "Or with SMS-style input:"
echo '  python main.py "therm 44.43 -110.59"'
echo ""
