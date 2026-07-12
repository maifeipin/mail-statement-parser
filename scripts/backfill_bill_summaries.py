#!/usr/bin/env python3
"""回填账单的 email_summaries 记录

由于早期 `mail_sync.py` 的双通道分流 A (账单正则识别) 在处理完账单后，
没有将其信息写入 `email_summaries` 表，导致这部分邮件（如招商银行等信用卡账单）
无法被同步到 Meilisearch 并展示在前端搜索中。

本脚本会扫描 `statements` 表，将所有解析成功的账单补充写入 `email_summaries` 中。
"""

import sys
import os
import sqlite3
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from mail_client import DB_PATH
from statement_db import init_db, upsert_email_summary
from statement_models import EmailSummaryRecord

def main():
    print("🚀 开始扫描并回填缺失的账单 summary 记录...")
    if not os.path.exists(DB_PATH):
        print(f"❌ 数据库 {DB_PATH} 不存在。")
        return

    init_db(DB_PATH)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()

    # 查出所有已解析的账单，并看是否已经有 summaries 记录
    try:
        cur.execute("""
            SELECT s.uid, s.subject, s.sender, s.email_date, s.bank_code, 
                   (SELECT account_name FROM email_bodies WHERE uid = s.uid LIMIT 1) as account_name
            FROM statements s
        """)
        statements = cur.fetchall()
    except Exception as e:
        print(f"❌ 查询 statements 失败: {e}")
        conn.close()
        return

    added = 0
    for stmt in statements:
        uid = stmt['uid']
        # 若 statements 没有 account_name 字段，尝试从 email_bodies 表中获取，如果还没有则降级为 default
        acct = stmt['account_name'] if stmt['account_name'] else 'default'
        
        # 检查是否已存在
        cur.execute("SELECT id FROM email_summaries WHERE uid = ? LIMIT 1", (uid,))
        if cur.fetchone():
            continue
            
        # 补充写入 summary
        rec = EmailSummaryRecord(
            account_name=acct,
            uid=uid,
            sender=stmt['sender'],
            subject=stmt['subject'],
            email_date=stmt['email_date'],
            category='BILL',
            importance='high',
            summary=f"💳 信用卡账单已解析 ({stmt['bank_code']})",
            actions_json="[]",
            status="processed",
            retry_count=0,
            processed_at=datetime.now(timezone.utc).isoformat()
        )
        
        try:
            upsert_email_summary(DB_PATH, rec)
            added += 1
            print(f"✅ 补充记录 UID={uid} | {stmt['subject'][:30]}")
        except Exception as e:
            print(f"❌ 写入失败 UID={uid}: {e}")

    conn.close()
    print(f"\n🎉 扫描完成！共计回填了 {added} 条账单记录。")
    print("下一次 Meilisearch 增量同步时，这些账单将会被正确地推送到搜索引擎。")

if __name__ == '__main__':
    main()
