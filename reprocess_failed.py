#!/usr/bin/env python3
"""补跑脚本薄封装：调用 mail_client.reprocess_failed_emails。

核心逻辑已固化入 mail_client.reprocess_failed_emails()。
保留旧文件名以兼容已有 cron 任务 / shell 别名。

用法: cd /home/liteagent/mail-statement-parser && python3 reprocess_failed.py
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import mail_client

if __name__ == "__main__":
    mail_client.reprocess_failed_emails()
