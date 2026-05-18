#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────────────────────
# Inbar Grade Monitor — one-command installer
# Usage:  bash setup.sh
# ─────────────────────────────────────────────────────────────────────────────
set -euo pipefail

WORK_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV_DIR="$WORK_DIR/venv"
VENV_PYTHON="$VENV_DIR/bin/python"
SCRIPT_PATH="$WORK_DIR/monitor.py"
LAUNCH_AGENTS="$HOME/Library/LaunchAgents"
BOT_PLIST_SRC="$WORK_DIR/launchd/com.inbar.bot.plist"
MON_PLIST_SRC="$WORK_DIR/launchd/com.inbar.monitor.plist"
BOT_PLIST_DST="$LAUNCH_AGENTS/com.inbar.bot.plist"
MON_PLIST_DST="$LAUNCH_AGENTS/com.inbar.monitor.plist"
LOG_DIR="$WORK_DIR/logs"

GREEN='\033[0;32m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
NC='\033[0m'

step() { echo -e "${GREEN}▶ $*${NC}"; }
warn() { echo -e "${YELLOW}⚠  $*${NC}"; }
die()  { echo -e "${RED}✗  $*${NC}" >&2; exit 1; }

# ── 0. Sanity checks ──────────────────────────────────────────────────────────
[[ "$(uname)" == "Darwin" ]] || die "This script is for macOS only."
command -v python3 >/dev/null 2>&1 || die "python3 not found. Install it via brew or python.org."

# ── 1. Python virtual environment ─────────────────────────────────────────────
step "Creating Python virtual environment..."
if [[ ! -d "$VENV_DIR" ]]; then
    python3 -m venv "$VENV_DIR"
else
    warn "venv already exists — skipping creation."
fi

step "Installing Python dependencies..."
"$VENV_PYTHON" -m pip install --quiet --upgrade pip
"$VENV_PYTHON" -m pip install --quiet -r "$WORK_DIR/requirements.txt"

step "Installing Playwright browsers (Chromium)..."
"$VENV_PYTHON" -m playwright install chromium

# ── 2. .env credentials ───────────────────────────────────────────────────────
ENV_FILE="$WORK_DIR/.env"
if [[ -f "$ENV_FILE" ]]; then
    warn ".env already exists — skipping credential prompts."
else
    step "Setting up credentials..."
    echo ""
    echo "You need a Telegram bot token and your Telegram user ID."
    echo "Get a token:    message @BotFather on Telegram → /newbot"
    echo "Get your ID:    message @userinfobot on Telegram"
    echo ""

    read -rp "Telegram bot token: " BOT_TOKEN
    read -rp "Telegram chat ID  : " CHAT_ID

    [[ -z "$BOT_TOKEN" ]] && die "Bot token cannot be empty."
    [[ -z "$CHAT_ID"   ]] && die "Chat ID cannot be empty."

    cat > "$ENV_FILE" <<EOF
TELEGRAM_BOT_TOKEN=${BOT_TOKEN}
TELEGRAM_CHAT_ID=${CHAT_ID}
EOF
    chmod 600 "$ENV_FILE"
    echo -e "${GREEN}✓  .env written and locked (chmod 600).${NC}"
fi

# ── 3. Log directory ──────────────────────────────────────────────────────────
mkdir -p "$LOG_DIR"

# ── 4. Install launchd plists ─────────────────────────────────────────────────
step "Installing launchd services..."
mkdir -p "$LAUNCH_AGENTS"

_install_plist() {
    local src="$1" dst="$2" label="$3"

    # Unload if already loaded (ignore errors if not loaded)
    launchctl unload "$dst" 2>/dev/null || true

    # Substitute placeholders
    sed \
        -e "s|__VENV_PYTHON__|${VENV_PYTHON}|g" \
        -e "s|__SCRIPT_PATH__|${SCRIPT_PATH}|g" \
        -e "s|__WORK_DIR__|${WORK_DIR}|g" \
        "$src" > "$dst"

    launchctl load "$dst"
    echo -e "${GREEN}✓  ${label} loaded.${NC}"
}

_install_plist "$BOT_PLIST_SRC" "$BOT_PLIST_DST" "com.inbar.bot (Telegram bot daemon)"
_install_plist "$MON_PLIST_SRC" "$MON_PLIST_DST" "com.inbar.monitor (grade checker every 30 min)"

# ── 5. First-run login ────────────────────────────────────────────────────────
echo ""
echo "═══════════════════════════════════════════════════════════════"
echo "  FIRST-TIME LOGIN REQUIRED"
echo "═══════════════════════════════════════════════════════════════"
echo "  A browser window will open. Log in to Inbar, then close it."
echo "  This only needs to happen once; the session is saved."
echo "═══════════════════════════════════════════════════════════════"
echo ""
read -rp "Press ENTER to open the browser and log in now... "

# Temporarily stop the monitor daemon so it doesn't race during login
launchctl unload "$MON_PLIST_DST" 2>/dev/null || true

"$VENV_PYTHON" "$SCRIPT_PATH"

# Reload the monitor now that the session exists
launchctl load "$MON_PLIST_DST"

# ── 6. Done ───────────────────────────────────────────────────────────────────
echo ""
echo "═══════════════════════════════════════════════════════════════"
echo -e "  ${GREEN}Setup complete!${NC}"
echo ""
echo "  Services running:"
echo "    • Bot daemon  → runs forever, restarts on crash"
echo "    • Monitor     → checks for new grades every 30 minutes"
echo ""
echo "  Logs:"
echo "    tail -f $LOG_DIR/bot.log"
echo "    tail -f $LOG_DIR/monitor.log"
echo ""
echo "  To stop everything:"
echo "    launchctl unload $BOT_PLIST_DST"
echo "    launchctl unload $MON_PLIST_DST"
echo ""
echo "  To uninstall completely:"
echo "    launchctl unload $BOT_PLIST_DST"
echo "    launchctl unload $MON_PLIST_DST"
echo "    rm $BOT_PLIST_DST $MON_PLIST_DST"
echo "═══════════════════════════════════════════════════════════════"
