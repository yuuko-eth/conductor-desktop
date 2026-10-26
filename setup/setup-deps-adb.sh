#!/usr/bin/env bash
# Install ADB for controlling Android devices via USB/WiFi.
# Run this on the HOST machine (not the phone).
set -euo pipefail

echo "==> Installing ADB (Android Debug Bridge)..."

if command -v apt &>/dev/null; then
  sudo apt update
  sudo apt install -y adb
elif command -v dnf &>/dev/null; then
  sudo dnf install -y android-tools
elif command -v pacman &>/dev/null; then
  sudo pacman -S --noconfirm android-tools
elif command -v brew &>/dev/null; then
  # macOS — install via Android platform-tools
  brew install --cask android-platform-tools
else
  echo "Unsupported package manager. Install Android platform-tools manually:"
  echo "  https://developer.android.com/tools/releases/platform-tools"
  exit 1
fi

echo ""
echo "==> Verifying..."
if command -v adb &>/dev/null; then
  ADB_VER=$(adb version | head -1)
  echo "  adb: OK ($ADB_VER)"
else
  echo "  adb: MISSING"
  exit 1
fi

echo ""
echo "==> Phone setup (one-time):"
echo "  1. Enable Developer Options:"
echo "     Settings → About Phone → tap 'Build Number' 7 times"
echo "  2. Enable USB Debugging:"
echo "     Settings → Developer Options → USB Debugging → ON"
echo "  3. Connect phone via USB cable"
echo "  4. Accept the 'Allow USB debugging?' prompt on the phone"
echo "  5. Verify: adb devices (should show your device serial)"
echo ""
echo "  For WiFi debugging (Android 11+):"
echo "     Settings → Developer Options → Wireless Debugging → ON"
echo "     adb pair <ip>:<pair-port>   # enter pairing code"
echo "     adb connect <ip>:<port>"
echo ""

# Check for connected devices
DEVICES=$(adb devices 2>/dev/null | grep -v "List" | grep -v "^$" | wc -l)
if [[ "$DEVICES" -gt 0 ]]; then
  echo "  Connected devices:"
  adb devices -l | grep -v "List"
else
  echo "  No devices connected yet."
fi

echo ""
echo "==> Done. Set CONDUCTOR_DESKTOP_MOBILE_ANDROID=1 where your MCP client starts conductor-desktop, then restart it."
