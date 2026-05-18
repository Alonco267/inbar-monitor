#!/usr/bin/env bash
# ════════════════════════════════════════════════════════════════════
#  Inbar Grade Monitor — Installer
#
#  HOW TO RUN (first time only):
#    Right-click this file → "Open With" → Terminal
#    Click "Open" on the security popup
#
#  After that: just double-click it.
# ════════════════════════════════════════════════════════════════════

# Change to the folder this script lives in (handles double-click from Finder)
cd "$(dirname "$0")"

clear
echo ""
echo "  ╔══════════════════════════════════════════╗"
echo "  ║    Inbar Grade Monitor — Installer       ║"
echo "  ║    Bar-Ilan University                   ║"
echo "  ╚══════════════════════════════════════════╝"
echo ""

# ── Check for Python 3 ────────────────────────────────────────────────────────
if ! command -v python3 &>/dev/null; then
    echo "  ✗  Python 3 is not installed."
    echo ""
    echo "  Please install it:"
    echo "  1. Open Safari"
    echo "  2. Go to: https://www.python.org/downloads/"
    echo "  3. Click the big yellow 'Download Python' button"
    echo "  4. Open the downloaded file and follow the installer"
    echo "  5. Come back here and double-click Install.command again"
    echo ""
    read -rp "  Press Enter to close..." _
    exit 1
fi

PYTHON_VERSION=$(python3 --version 2>&1)
echo "  ✓  Found $PYTHON_VERSION"
echo ""

# ── Run the main setup script ─────────────────────────────────────────────────
bash "$(dirname "$0")/setup.sh"

EXIT_CODE=$?
echo ""
if [[ $EXIT_CODE -eq 0 ]]; then
    echo "  ══════════════════════════════════════════"
    echo "  ✓  Setup complete! You can close this window."
    echo "  ══════════════════════════════════════════"
else
    echo "  ══════════════════════════════════════════"
    echo "  ✗  Something went wrong (exit code $EXIT_CODE)."
    echo "  ══════════════════════════════════════════"
fi
echo ""
read -rp "  Press Enter to close this window... " _
