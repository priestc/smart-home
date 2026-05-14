#!/usr/bin/env bash
# Install arduino-cli and all dependencies needed to compile the ESP32 relay firmware.
# Safe to run multiple times — skips steps that are already complete.
#
# Usage: bash setup.sh

set -euo pipefail

# ── arduino-cli ──────────────────────────────────────────────────────────────

if command -v arduino-cli &>/dev/null; then
    echo "arduino-cli already installed: $(arduino-cli version --short 2>/dev/null || arduino-cli version)"
else
    echo "Installing arduino-cli..."

    # Pick an install destination the current user can write to without sudo.
    # Prefer /usr/local/bin (system-wide) if writable, else ~/.local/bin.
    if [ -w /usr/local/bin ]; then
        BINDIR=/usr/local/bin
    else
        BINDIR="$HOME/.local/bin"
        mkdir -p "$BINDIR"
        # Warn if this isn't on PATH yet
        case ":$PATH:" in
            *":$BINDIR:"*) ;;
            *) echo "Note: add $BINDIR to your PATH (e.g. add 'export PATH=\"\$HOME/.local/bin:\$PATH\"' to ~/.bashrc or ~/.zshrc)" ;;
        esac
    fi

    curl -fsSL https://raw.githubusercontent.com/arduino/arduino-cli/master/install.sh | BINDIR="$BINDIR" sh
    echo "arduino-cli installed to $BINDIR"
fi

# ── Board manager URL ────────────────────────────────────────────────────────

ESP32_URL="https://raw.githubusercontent.com/espressif/arduino-esp32/gh-pages/package_esp32_index.json"

arduino-cli config init --overwrite &>/dev/null || true

# Add the ESP32 URL only if it isn't already present
if ! arduino-cli config dump | grep -q "$ESP32_URL"; then
    arduino-cli config add board_manager.additional_urls "$ESP32_URL"
fi

# ── Board index ──────────────────────────────────────────────────────────────

echo "Updating board index..."
arduino-cli core update-index

# ── ESP32 core ───────────────────────────────────────────────────────────────

if arduino-cli core list 2>/dev/null | grep -q "esp32:esp32"; then
    echo "ESP32 core already installed."
else
    echo "Installing ESP32 Arduino core (this takes a few minutes the first time)..."
    arduino-cli core install esp32:esp32
fi

# ── ArduinoJson library ──────────────────────────────────────────────────────

if arduino-cli lib list 2>/dev/null | grep -q "ArduinoJson"; then
    echo "ArduinoJson already installed."
else
    echo "Installing ArduinoJson..."
    arduino-cli lib install "ArduinoJson"
fi

# ── Done ─────────────────────────────────────────────────────────────────────

echo ""
echo "All done. Run ./build.sh to compile the firmware."
