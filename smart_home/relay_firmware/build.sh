#!/usr/bin/env bash
# Build the ESP32 relay firmware using arduino-cli.
# Outputs .bin files into this directory (relay_firmware/).
#
# Usage: ./build.sh

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SKETCH_DIR="$SCRIPT_DIR/esp32_relay"
FQBN="esp32:esp32:esp32"

# ── arduino-cli ─────────────────────────────────────────────────────────────

if ! command -v arduino-cli &>/dev/null; then
    echo "arduino-cli not found."
    echo "Install it with:"
    echo "  curl -fsSL https://raw.githubusercontent.com/arduino/arduino-cli/master/install.sh | sh"
    echo "  # then move the binary to a directory in \$PATH, e.g. /usr/local/bin"
    exit 1
fi

# ── ESP32 board core ────────────────────────────────────────────────────────

if ! arduino-cli core list 2>/dev/null | grep -q "esp32:esp32"; then
    echo "Installing ESP32 Arduino core (this may take a few minutes)..."
    arduino-cli config init --overwrite &>/dev/null || true
    arduino-cli config add board_manager.additional_urls \
        https://raw.githubusercontent.com/espressif/arduino-esp32/gh-pages/package_esp32_index.json
    arduino-cli core update-index
    arduino-cli core install esp32:esp32
fi

# ── ArduinoJson library ──────────────────────────────────────────────────────

if ! arduino-cli lib list 2>/dev/null | grep -q "ArduinoJson"; then
    echo "Installing ArduinoJson..."
    arduino-cli lib install "ArduinoJson"
fi

# ── Compile ──────────────────────────────────────────────────────────────────

echo "Compiling $SKETCH_DIR ..."
arduino-cli compile \
    --fqbn "$FQBN" \
    --output-dir "$SCRIPT_DIR" \
    "$SKETCH_DIR"

echo ""
echo "Build complete. Firmware files:"
ls -lh "$SCRIPT_DIR"/*.bin
