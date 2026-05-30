#!/usr/bin/env python3
"""
飞书机器人 - VPS 全能助手 Bot (feishu_bot.py)
通过飞书长连接 (WebSocket) 接收手机端指令，执行账单操作/Shell命令，返回富文本消息卡片。

功能:
  - 账单类指令: /report, /due, /reconcile, /recent, /txns, /fetch, /parse
  - 通用指令:   /sh, /sys, /cron
  - 定时推送:   配合 feishu_push.py 实现 crontab → 飞书卡片

依赖: pip3 install lark-oapi
启动: python3 feishu_bot.py
"""

import json
import os
import re
import subprocess
import sys
import signal
import traceback
from datetime import datetime

import lark_oapi as lark
from lark_oapi.api.im.v1 import *

# ============================================================
#  配置加载
# ============================================================
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(SCRIPT_DIR, "feishu_config.json")
PUSH_TARGET_PATH = os.path.join(SCRIPT_DIR, "feishu_push_target.json")


def load_config():
    """加载飞书配置文件"""
    if not os.path.exists(CONFIG_PATH):
        print(f"❌ 配置文件不存在: {CONFIG_PATH}")
        print(f"   请复制 feishu_config.example.json 为 feishu_config.json 并填入凭证")
        sys.exit(1)
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


config = load_config()
APP_ID = config["app_id"]
APP_SECRET = config["app_secret"]
PROJECT_DIR = config.get("project_dir", SCRIPT_DIR)
PYTHON_CMD = config.get("python_cmd", "python3")
ALLOWED_USERS = config.get("allowed_users", [])

# ============================================================
#  指令注册表 (账单类 — 调用 mail_client.py)
# ============================================================
COMMANDS = {
    "/report": {
        "title": "📊 财务汇总报表",
        "desc": "生成各银行月度财务汇总",
        "usage": "/report [月数]",
        "example": "/report 3",
        "color": "blue",
        "build_args": lambda p: ["report", p[1] if len(p) > 1 else "3"],
    },
    "/due": {
        "title": "⏰ 还款临期提醒",
        "desc": "检查即将到期的信用卡还款",
        "usage": "/due [月数] [天数]",
        "example": "/due 3 7",
        "color": "orange",
        "build_args": lambda p: [
            "due_soon_bills",
            p[1] if len(p) > 1 else "3",
            p[2] if len(p) > 2 else "7",
        ],
    },
    "/reconcile": {
        "title": "🧾 对账差异报表",
        "desc": "应还款总额 vs 交易明细累加校验",
        "usage": "/reconcile [月数] [容差]",
        "example": "/reconcile 3 1.0",
        "color": "green",
        "build_args": lambda p: [
            "reconcile",
            p[1] if len(p) > 1 else "3",
            p[2] if len(p) > 2 else "1.0",
        ],
    },
    "/recent": {
        "title": "📄 近期账单记录",
        "desc": "查看已入库的账单概览",
        "usage": "/recent [月数]",
        "example": "/recent 3",
        "color": "wathet",
        "build_args": lambda p: ["recent", p[1] if len(p) > 1 else "3"],
    },
    "/txns": {
        "title": "🔍 大额交易查询",
        "desc": "筛选超过指定金额的交易",
        "usage": "/txns <金额> [月数]",
        "example": "/txns 500 3",
        "color": "purple",
        "build_args": lambda p: (
            ["txns_over"] + p[1:] if len(p) > 1 else ["txns_over", "500"]
        ),
    },
    "/fetch": {
        "title": "📥 拉取银行账单邮件",
        "desc": "从邮箱下载最近 N 个月的账单",
        "usage": "/fetch [月数]",
        "example": "/fetch 3",
        "color": "turquoise",
        "build_args": lambda p: [
            "download_bank_bills",
            p[1] if len(p) > 1 else "3",
        ],
    },
    "/parse": {
        "title": "📝 批量解析并入库",
        "desc": "解析已下载的邮件并写入 SQLite",
        "usage": "/parse [月数]",
        "example": "/parse 3",
        "color": "turquoise",
        "build_args": lambda p: [
            "validate_bank_bills",
            p[1] if len(p) > 1 else "3",
        ],
    },
}

# ============================================================
#  飞书 API Client (发送回复用)
# ============================================================
api_client = (
    lark.Client.builder()
    .app_id(APP_ID)
    .app_secret(APP_SECRET)
    .log_level(lark.LogLevel.INFO)
    .build()
)

# ============================================================
#  推送目标记忆 (自动保存最近交互用户的 chat_id)
# ============================================================
def save_push_target(open_id, chat_id):
    """保存推送目标，供 feishu_push.py 定时推送使用"""
    try:
        target = {"open_id": open_id, "chat_id": chat_id, "updated": datetime.now().isoformat()}
        with open(PUSH_TARGET_PATH, "w", encoding="utf-8") as f:
            json.dump(target, f, ensure_ascii=False, indent=2)
    except Exception as e:
        print(f"  ⚠️ Failed to save push target: {e}")


def load_push_target():
    """加载推送目标"""
    if not os.path.exists(PUSH_TARGET_PATH):
        return None
    try:
        with open(PUSH_TARGET_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


# ============================================================
#  消息卡片构建
# ============================================================
MAX_CARD_CONTENT_LEN = 2800  # 飞书卡片内容限制，避免超长


def build_card(title, content, color="blue", footer=None):
    """构建飞书交互式消息卡片 JSON"""
    elements = []

    if content:
        # 截断超长内容
        if len(content) > MAX_CARD_CONTENT_LEN:
            content = content[:MAX_CARD_CONTENT_LEN] + "\n\n... ✂️ 内容过长已截断"

        elements.append(
            {"tag": "div", "text": {"tag": "lark_md", "content": content}}
        )

    # 分隔线
    elements.append({"tag": "hr"})

    # 底部时间戳
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    note_text = footer or f"🤖 VPS 助手 Bot · {ts}"
    elements.append(
        {"tag": "note", "elements": [{"tag": "plain_text", "content": note_text}]}
    )

    card = {
        "config": {"wide_screen_mode": True},
        "header": {
            "title": {"tag": "plain_text", "content": title},
            "template": color,
        },
        "elements": elements,
    }
    return json.dumps(card, ensure_ascii=False)


def build_help_card():
    """构建 /help 指令帮助卡片"""
    lines = []

    lines.append("**📋 账单指令：**\n")
    for cmd, info in COMMANDS.items():
        lines.append(f"**`{cmd}`** — {info['desc']}")
        lines.append(f"　　用法: `{info['usage']}`　示例: `{info['example']}`\n")

    lines.append("---")
    lines.append("**🖥️ 通用指令：**\n")
    lines.append("**`/sh`** — 执行 Shell 命令")
    lines.append("　　用法: `/sh <命令>`　示例: `/sh df -h`\n")
    lines.append("**`/run`** — 执行 VPS 上的脚本")
    lines.append("　　用法: `/run <脚本路径>`　示例: `/run /root/down/check_cert_expiry.sh`\n")
    lines.append("**`/sys`** — 查看系统状态 (CPU/内存/磁盘/负载)")
    lines.append("　　用法: `/sys`\n")
    lines.append("**`/cron`** — 查看定时任务")
    lines.append("　　用法: `/cron`\n")
    lines.append("**`/status`** — Bot 运行状态")
    lines.append("　　用法: `/status`\n")

    lines.append("---")
    lines.append("💡 指令不区分大小写，`/` 前缀可省略")
    lines.append("💡 群聊中请 @机器人 后输入指令")

    return build_card("📖 VPS 助手 - 指令帮助", "\n".join(lines), color="indigo")


# ============================================================
#  命令执行引擎
# ============================================================
def execute_cli(cli_args):
    """调用 mail_client.py 并捕获完整输出"""
    cmd = [PYTHON_CMD, os.path.join(PROJECT_DIR, "mail_client.py")] + cli_args

    env = os.environ.copy()
    env["PYTHONIOENCODING"] = "utf-8"
    env["TERM"] = "dumb"

    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            cwd=PROJECT_DIR,
            timeout=120,
            env=env,
        )
        output = result.stdout.strip()
        if result.returncode != 0 and result.stderr.strip():
            err = result.stderr.strip()
            output = f"{output}\n\n⚠️ **stderr:**\n{err}" if output else f"⚠️ {err}"
            return output, False
        return output if output else "✅ 命令执行完成（无输出）", True
    except subprocess.TimeoutExpired:
        return "⏳ 命令执行超时（超过 120 秒），请稍后重试", False
    except FileNotFoundError:
        return f"❌ 找不到 Python: `{PYTHON_CMD}`\n请检查 feishu_config.json 中 python_cmd 配置", False
    except Exception as e:
        return f"❌ 执行异常: {e}", False


def execute_shell(cmd_str, timeout=30):
    """执行任意 Shell 命令并捕获输出

    Args:
        cmd_str:  Shell 命令字符串
        timeout:  超时秒数 (默认 30s，防止误操作)

    Returns:
        (output_text, is_success) 元组
    """
    # 安全黑名单 — 拒绝危险命令
    DANGEROUS = ["rm -rf /", "mkfs", "dd if=", "> /dev/sd", "shutdown", "reboot", "init 0", "init 6"]
    cmd_lower = cmd_str.lower().strip()
    for d in DANGEROUS:
        if d in cmd_lower:
            return f"🚫 安全拦截: 拒绝执行危险命令 `{d}`", False

    try:
        result = subprocess.run(
            cmd_str,
            shell=True,
            capture_output=True,
            text=True,
            timeout=timeout,
            env={**os.environ, "LANG": "en_US.UTF-8"},
        )
        output = result.stdout.strip()
        if result.stderr.strip():
            stderr = result.stderr.strip()
            if output:
                output += f"\n\n⚠️ **stderr:**\n{stderr}"
            else:
                output = stderr
        if not output:
            output = f"✅ 命令执行完成 (exit code: {result.returncode})"
        return output, result.returncode == 0
    except subprocess.TimeoutExpired:
        return f"⏳ 命令执行超时 (>{timeout}s)，已终止", False
    except Exception as e:
        return f"❌ 执行异常: {e}", False


def get_system_info():
    """采集 VPS 系统状态信息"""
    sections = []

    # 主机名 & 运行时间
    uptime, _ = execute_shell("uptime -p", 5)
    hostname, _ = execute_shell("hostname", 5)
    sections.append(f"**🖥️ 主机:** {hostname}")
    sections.append(f"**⏱️ 运行:** {uptime}")

    # 负载
    load, _ = execute_shell("cat /proc/loadavg | awk '{print $1, $2, $3}'", 5)
    sections.append(f"**📈 负载:** {load}")

    # CPU
    cpu, _ = execute_shell(
        "top -bn1 | grep 'Cpu(s)' | awk '{printf \"用户 %.1f%% | 系统 %.1f%% | 空闲 %.1f%%\", $2, $4, $8}'",
        5,
    )
    sections.append(f"**🔧 CPU:** {cpu}")

    # 内存
    mem, _ = execute_shell(
        "free -h | awk 'NR==2{printf \"已用 %s / 共 %s (%.1f%%)\", $3, $2, $3/$2*100}'",
        5,
    )
    sections.append(f"**💾 内存:** {mem}")

    # 磁盘
    disk, _ = execute_shell(
        "df -h / | awk 'NR==2{printf \"已用 %s / 共 %s (%s)\", $3, $2, $5}'",
        5,
    )
    sections.append(f"**💿 磁盘:** {disk}")

    # 网络 (VPS 流量)
    net, _ = execute_shell(
        "vnstat --oneline 2>/dev/null | awk -F';' '{printf \"今日: ↑%s ↓%s | 本月: ↑%s ↓%s\", $4, $5, $9, $10}' || echo '(vnstat 未安装)'",
        5,
    )
    sections.append(f"**🌐 流量:** {net}")

    return "\n".join(sections)


# ============================================================
#  消息发送
# ============================================================
def reply_with_card(message_id, card_json):
    """回复消息卡片 (自动以回复形式挂载在原消息下)"""
    try:
        request = (
            ReplyMessageRequest.builder()
            .message_id(message_id)
            .request_body(
                ReplyMessageRequestBody.builder()
                .msg_type("interactive")
                .content(card_json)
                .build()
            )
            .build()
        )
        response = api_client.im.v1.message.reply(request)
        if not response.success():
            print(f"  ⚠️ Reply failed: code={response.code}, msg={response.msg}")
        else:
            print(f"  ✅ Reply sent successfully")
    except Exception as e:
        print(f"  ❌ Reply exception: {e}")


def send_card_to_chat(chat_id, card_json):
    """主动向指定会话发送卡片 (用于定时推送)"""
    try:
        request = (
            CreateMessageRequest.builder()
            .receive_id_type("chat_id")
            .request_body(
                CreateMessageRequestBody.builder()
                .receive_id(chat_id)
                .msg_type("interactive")
                .content(card_json)
                .build()
            )
            .build()
        )
        response = api_client.im.v1.message.create(request)
        if not response.success():
            print(f"  ⚠️ Send failed: code={response.code}, msg={response.msg}")
        else:
            print(f"  ✅ Card sent to chat {chat_id}")
    except Exception as e:
        print(f"  ❌ Send exception: {e}")


# ============================================================
#  指令处理核心
# ============================================================
def extract_text(message):
    """从消息中提取纯文本，去除 @mention 占位符"""
    try:
        content = json.loads(message.content)
        text = content.get("text", "").strip()
    except (json.JSONDecodeError, AttributeError):
        return ""

    text = re.sub(r"@_user_\d+\s*", "", text).strip()
    text = re.sub(r"@_all\s*", "", text).strip()
    return text


def handle_command(message_id, text):
    """解析并执行用户指令"""
    parts = text.split()
    if not parts:
        return

    cmd = parts[0].lower()

    # 兼容无 / 前缀
    if not cmd.startswith("/"):
        cmd = "/" + cmd

    # --- /help ---
    if cmd == "/help":
        reply_with_card(message_id, build_help_card())
        return

    # --- /status ---
    if cmd == "/status":
        ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        card = build_card(
            "🟢 Bot 运行状态",
            f"**状态:** 在线运行中\n**服务器时间:** {ts}\n**项目目录:** `{PROJECT_DIR}`",
            color="green",
        )
        reply_with_card(message_id, card)
        return

    # --- /sh <command> ---
    if cmd == "/sh":
        if len(parts) < 2:
            reply_with_card(message_id, build_card(
                "⚠️ 缺少参数", "用法: `/sh <命令>`\n示例: `/sh df -h`", "grey"
            ))
            return
        shell_cmd = " ".join(parts[1:])
        print(f"  🔧 Shell: {shell_cmd}")
        output, success = execute_shell(shell_cmd)
        card = build_card(
            f"🖥️ Shell: {shell_cmd[:40]}",
            output,
            color="blue" if success else "red",
        )
        reply_with_card(message_id, card)
        return

    # --- /run <script_path> [args...] ---
    if cmd == "/run":
        if len(parts) < 2:
            reply_with_card(message_id, build_card(
                "⚠️ 缺少参数", "用法: `/run <脚本路径> [参数...]`\n示例: `/run /root/down/check_cert_expiry.sh`", "grey"
            ))
            return
        script_cmd = " ".join(parts[1:])
        # 自动加 bash 前缀 (如果是 .sh 脚本)
        if script_cmd.strip().endswith(".sh") or script_cmd.strip().split()[0].endswith(".sh"):
            script_cmd = f"bash {script_cmd}"
        print(f"  🔧 Run script: {script_cmd}")
        output, success = execute_shell(script_cmd, timeout=60)
        script_name = os.path.basename(parts[1])
        card = build_card(
            f"📜 脚本: {script_name}",
            output,
            color="turquoise" if success else "red",
        )
        reply_with_card(message_id, card)
        return

    # --- /sys ---
    if cmd == "/sys":
        print("  🔧 Collecting system info...")
        info = get_system_info()
        card = build_card("🖥️ VPS 系统状态", info, color="violet")
        reply_with_card(message_id, card)
        return

    # --- /cron ---
    if cmd == "/cron":
        print("  🔧 Listing crontab...")
        output, success = execute_shell("crontab -l 2>/dev/null || echo '(无定时任务)'", 5)
        card = build_card("⏰ 定时任务列表", output, color="indigo")
        reply_with_card(message_id, card)
        return

    # --- 已注册的账单指令 ---
    if cmd in COMMANDS:
        cmd_info = COMMANDS[cmd]
        cli_args = cmd_info["build_args"](parts)

        log_line = f"mail_client.py {' '.join(cli_args)}"
        print(f"  🔧 Executing: {log_line}")

        output, success = execute_cli(cli_args)
        color = cmd_info["color"] if success else "red"
        card = build_card(
            title=cmd_info["title"],
            content=output,
            color=color,
        )
        reply_with_card(message_id, card)
        return

    # --- 未知指令 ---
    card = build_card(
        "❓ 未识别的指令",
        f"收到: `{text}`\n\n发送 **/help** 查看所有可用指令",
        color="grey",
    )
    reply_with_card(message_id, card)


# ============================================================
#  飞书事件回调 (WebSocket 长连接)
# ============================================================
def on_message_receive(data: P2ImMessageReceiveV1):
    """处理 im.message.receive_v1 事件 (接收用户消息)"""
    try:
        message = data.event.message
        sender = data.event.sender

        # 忽略非用户消息 (防止 bot 回复自己造成死循环)
        if sender.sender_type and sender.sender_type != "user":
            return

        # 只处理文本消息
        if message.message_type != "text":
            reply_with_card(
                message.message_id,
                build_card(
                    "⚠️ 不支持的消息类型",
                    "当前仅支持**文本指令**\n\n发送 **/help** 查看可用指令",
                    color="grey",
                ),
            )
            return

        # 提取文本
        text = extract_text(message)
        if not text:
            return

        sender_id = ""
        if sender.sender_id:
            sender_id = sender.sender_id.open_id or "unknown"

        # 自动保存推送目标 (供 feishu_push.py 定时推送使用)
        chat_id = message.chat_id or ""
        if sender_id and chat_id:
            save_push_target(sender_id, chat_id)

        # 可选: 白名单校验
        if ALLOWED_USERS and sender_id not in ALLOWED_USERS:
            reply_with_card(
                message.message_id,
                build_card("🚫 无权限", "您不在授权用户列表中。", "red"),
            )
            return

        chat_type = message.chat_type or "p2p"
        print(f"📩 [{chat_type}] {sender_id}: {text}")

        # 分发指令
        handle_command(message.message_id, text)

    except Exception as e:
        print(f"❌ Error in on_message_receive: {e}")
        traceback.print_exc()
        try:
            reply_with_card(
                data.event.message.message_id,
                build_card("❌ 内部错误", f"处理消息时发生异常:\n`{e}`", "red"),
            )
        except Exception:
            pass


# ============================================================
#  CLI 推送模式入口 (供 feishu_push.py 内部调用)
# ============================================================
def push_message(title, content, color="blue"):
    """主动推送消息卡片到上次交互的用户 (用于 crontab 定时推送)"""
    target = load_push_target()
    if not target or not target.get("chat_id"):
        print("❌ 尚无推送目标，请先在飞书中给 Bot 发一条消息以注册")
        return False

    card = build_card(title, content, color)
    send_card_to_chat(target["chat_id"], card)
    return True


# ============================================================
#  启动入口
# ============================================================
def main():
    # 支持 --push 模式: python3 feishu_bot.py --push "标题" "内容"
    if len(sys.argv) >= 3 and sys.argv[1] == "--push":
        title = sys.argv[2]
        if len(sys.argv) >= 4:
            content = sys.argv[3]
        else:
            # 从 stdin 读取
            content = sys.stdin.read().strip()
        color = sys.argv[4] if len(sys.argv) >= 5 else "blue"
        push_message(title, content, color)
        return

    banner = f"""
{'=' * 52}
  🤖  VPS 助手 · 飞书 Bot  启动中...
{'=' * 52}
  App ID:       {APP_ID[:16]}...
  Project Dir:  {PROJECT_DIR}
  Python Cmd:   {PYTHON_CMD}
  接入模式:      WebSocket 长连接
  用户白名单:    {'已启用 (' + str(len(ALLOWED_USERS)) + ' 人)' if ALLOWED_USERS else '未启用 (所有人可用)'}
{'=' * 52}
"""
    print(banner)

    # 构建事件处理器
    event_handler = (
        lark.EventDispatcherHandler.builder("", "")
        .register_p2_im_message_receive_v1(on_message_receive)
        .build()
    )

    # 构建 WebSocket 长连接客户端
    ws_client = lark.ws.Client(
        app_id=APP_ID,
        app_secret=APP_SECRET,
        event_handler=event_handler,
        log_level=lark.LogLevel.INFO,
    )

    print("🚀 Bot 已上线! 在手机飞书中发送 /help 查看指令")
    print("   按 Ctrl+C 停止\n")

    # 优雅退出
    def _shutdown(sig, frame):
        print("\n👋 Bot 正在关闭...")
        sys.exit(0)

    signal.signal(signal.SIGINT, _shutdown)
    signal.signal(signal.SIGTERM, _shutdown)

    # 阻塞式运行 (SDK 内部维护 WebSocket 心跳与重连)
    ws_client.start()


if __name__ == "__main__":
    main()
