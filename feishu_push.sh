#!/bin/bash
# =============================================
#  Feishu Push Wrapper (VPS)
#  Usage: ./feishu_push.sh "标题" "内容" [颜色]
#  Or pipe: echo "内容" | ./feishu_push.sh "标题"
# =============================================

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SCRIPT_DIR" || exit 1

TITLE="$1"
CONTENT="$2"
COLOR="${3:-blue}"

if [ -z "$TITLE" ]; then
    echo "用法: $0 \"标题\" [内容] [颜色]"
    exit 1
fi

if [ -z "$CONTENT" ]; then
    # Read from stdin
    CONTENT=$(cat)
fi

exec python3 "$SCRIPT_DIR/feishu_bot.py" --push "$TITLE" "$CONTENT" "$COLOR"
