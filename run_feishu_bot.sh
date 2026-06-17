#!/bin/bash
# =============================================
#  Feishu Bot Launcher (VPS)
#  Usage: ./run_feishu_bot.sh
#  Background: nohup ./run_feishu_bot.sh &
# =============================================

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SCRIPT_DIR" || exit 1

# Install dependency if missing
python3 -c "import lark_oapi" 2>/dev/null || {
    echo "[*] Installing lark-oapi..."
    pip3 install lark-oapi
}

echo "[*] Starting Feishu Bot..."
exec python3 "$SCRIPT_DIR/feishu_bot.py"
