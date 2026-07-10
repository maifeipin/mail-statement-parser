#!/usr/bin/env python3
"""SQLite persistence for statement parsing and validation."""

import sqlite3
from datetime import datetime, timezone
from typing import Iterable, Optional

from statement_models import StatementRecord, ValidationIssue, StatementTransactionRecord, EmailSummaryRecord


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def init_db(db_path: str) -> None:
    conn = sqlite3.connect(db_path)
    try:
        cur = conn.cursor()
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS statements (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                uid TEXT NOT NULL,
                message_id TEXT,
                bank_code TEXT NOT NULL,
                rule_id TEXT NOT NULL,
                subject TEXT,
                sender TEXT,
                email_date TEXT,
                statement_month TEXT,
                statement_date TEXT,
                due_date TEXT,
                total_due TEXT,
                minimum_due TEXT,
                raw_fields_json TEXT,
                is_paid INTEGER NOT NULL DEFAULT 0,
                paid_at TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                UNIQUE(uid, bank_code)
            )
            """
        )
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS validation_runs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                uid TEXT NOT NULL,
                bank_code TEXT NOT NULL,
                rule_id TEXT NOT NULL,
                passed INTEGER NOT NULL,
                error_count INTEGER NOT NULL,
                warning_count INTEGER NOT NULL,
                report_path TEXT,
                run_at TEXT NOT NULL
            )
            """
        )
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS validation_issues (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                run_id INTEGER NOT NULL,
                severity TEXT NOT NULL,
                code TEXT NOT NULL,
                field TEXT,
                rule_id TEXT,
                left_expr TEXT,
                right_expr TEXT,
                op TEXT,
                created_at TEXT NOT NULL,
                FOREIGN KEY(run_id) REFERENCES validation_runs(id)
            )
            """
        )
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS statement_transactions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                uid TEXT NOT NULL,
                bank_code TEXT NOT NULL,
                txn_date TEXT,
                post_date TEXT,
                description TEXT,
                amount TEXT,
                currency TEXT,
                txn_location_code TEXT,
                original_amount TEXT,
                direction TEXT,
                raw_row_json TEXT,
                created_at TEXT NOT NULL
            )
            """
        )
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS email_summaries (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                account_name TEXT NOT NULL,
                uid TEXT NOT NULL,
                sender TEXT,
                subject TEXT,
                email_date TEXT,
                category TEXT,
                importance TEXT,
                summary TEXT,
                actions_json TEXT,
                deadline TEXT,
                deadline_raw TEXT,
                status TEXT DEFAULT 'pending',
                retry_count INTEGER DEFAULT 0,
                processed_at TEXT,
                created_at TEXT NOT NULL,
                UNIQUE(account_name, uid)
            )
            """
        )
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS noise_rules (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                pattern_type TEXT NOT NULL, -- 'sender_domain', 'sender_email'
                pattern_value TEXT NOT NULL,
                created_at TEXT NOT NULL,
                UNIQUE(pattern_type, pattern_value)
            )
            """
        )
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS email_bodies (
                id INTEGER PRIMARY KEY,
                account_name TEXT NOT NULL,
                uid TEXT NOT NULL,
                raw_html TEXT,
                plain_text TEXT,
                markdown_tables TEXT,
                content_len INTEGER,
                fetched_at TEXT NOT NULL,
                UNIQUE(account_name, uid)
            )
            """
        )

        # 兼容历史库：为已存在表补齐新增字段
        cur.execute("PRAGMA table_info(statement_transactions)")
        existing_cols = {row[1] for row in cur.fetchall()}
        if 'txn_location_code' not in existing_cols:
            cur.execute("ALTER TABLE statement_transactions ADD COLUMN txn_location_code TEXT")
        if 'original_amount' not in existing_cols:
            cur.execute("ALTER TABLE statement_transactions ADD COLUMN original_amount TEXT")

        # 修复 1: 历史账单状态回填 (幂等)
        cur.execute(
            """
            UPDATE statements
            SET is_paid = 1,
                paid_at = COALESCE(paid_at, datetime('now')),
                updated_at = datetime('now')
            WHERE due_date IS NOT NULL
              AND date(due_date) < date('now')
              AND is_paid = 0
            """
        )

        conn.commit()
    finally:
        conn.close()


def upsert_statement(db_path: str, s: StatementRecord) -> None:
    conn = sqlite3.connect(db_path)
    try:
        now = _utc_now_iso()
        cur = conn.cursor()
        cur.execute(
            """
            INSERT INTO statements (
                uid, message_id, bank_code, rule_id, subject, sender, email_date,
                statement_month, statement_date, due_date, total_due, minimum_due,
                raw_fields_json, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(uid, bank_code) DO UPDATE SET
                message_id = excluded.message_id,
                rule_id = excluded.rule_id,
                subject = excluded.subject,
                sender = excluded.sender,
                email_date = excluded.email_date,
                statement_month = excluded.statement_month,
                statement_date = excluded.statement_date,
                due_date = excluded.due_date,
                total_due = excluded.total_due,
                minimum_due = excluded.minimum_due,
                raw_fields_json = excluded.raw_fields_json,
                updated_at = excluded.updated_at
            """,
            (
                s.uid,
                s.message_id,
                s.bank_code,
                s.rule_id,
                s.subject,
                s.sender,
                s.email_date,
                s.statement_month,
                s.statement_date,
                s.due_date,
                s.total_due,
                s.minimum_due,
                s.raw_fields_json,
                now,
                now,
            ),
        )
        conn.commit()
    finally:
        conn.close()


def save_validation_run(
    db_path: str,
    uid: str,
    bank_code: str,
    rule_id: str,
    passed: bool,
    report_path: str,
    errors: Iterable[ValidationIssue],
    warnings: Iterable[ValidationIssue],
) -> int:
    err_list = list(errors)
    warn_list = list(warnings)
    conn = sqlite3.connect(db_path)
    try:
        cur = conn.cursor()
        cur.execute(
            """
            INSERT INTO validation_runs (
                uid, bank_code, rule_id, passed, error_count, warning_count, report_path, run_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                uid,
                bank_code,
                rule_id,
                1 if passed else 0,
                len(err_list),
                len(warn_list),
                report_path,
                _utc_now_iso(),
            ),
        )
        run_id = cur.lastrowid

        for item in err_list:
            cur.execute(
                """
                INSERT INTO validation_issues (
                    run_id, severity, code, field, rule_id, left_expr, right_expr, op, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    run_id,
                    "error",
                    item.code,
                    item.field,
                    item.rule_id,
                    item.left,
                    item.right,
                    item.op,
                    _utc_now_iso(),
                ),
            )

        for item in warn_list:
            cur.execute(
                """
                INSERT INTO validation_issues (
                    run_id, severity, code, field, rule_id, left_expr, right_expr, op, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    run_id,
                    "warning",
                    item.code,
                    item.field,
                    item.rule_id,
                    item.left,
                    item.right,
                    item.op,
                    _utc_now_iso(),
                ),
            )

        conn.commit()
        return int(run_id)
    finally:
        conn.close()


def replace_transactions(db_path: str, uid: str, bank_code: str, txns: Iterable[StatementTransactionRecord]) -> int:
    items = list(txns)
    conn = sqlite3.connect(db_path)
    try:
        cur = conn.cursor()
        cur.execute(
            """
            DELETE FROM statement_transactions
            WHERE uid = ? AND bank_code = ?
            """,
            (uid, bank_code),
        )

        for t in items:
            cur.execute(
                """
                INSERT INTO statement_transactions (
                    uid, bank_code, txn_date, post_date, description, amount,
                    currency, txn_location_code, original_amount, direction,
                    raw_row_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    t.uid,
                    t.bank_code,
                    t.txn_date,
                    t.post_date,
                    t.description,
                    t.amount,
                    t.currency,
                    t.txn_location_code,
                    t.original_amount,
                    t.direction,
                    t.raw_row_json,
                    _utc_now_iso(),
                ),
            )

        conn.commit()
        return len(items)
    finally:
        conn.close()


def uid_exists(db_path: str, uid: str) -> bool:
    """检查 uid 是否已存在于 statements 表（任意 bank_code）。"""
    conn = sqlite3.connect(db_path)
    try:
        cur = conn.cursor()
        cur.execute("SELECT 1 FROM statements WHERE uid = ? LIMIT 1", (uid,))
        return cur.fetchone() is not None
    finally:
        conn.close()


def get_recent_statements(db_path: str, months: int = 3) -> list[sqlite3.Row]:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        cur = conn.cursor()
        months = max(1, int(months))
        cur.execute(
            """
            SELECT
                s.uid,
                s.bank_code,
                s.rule_id,
                s.subject,
                s.statement_month,
                s.statement_date,
                s.due_date,
                s.total_due,
                s.minimum_due,
                COALESCE(tx.txn_count, 0) AS txn_count,
                ROUND(COALESCE(tx.txn_sum, 0), 2) AS txn_sum,
                s.updated_at
            FROM statements s
            LEFT JOIN (
                SELECT uid, bank_code, COUNT(*) AS txn_count, SUM(CAST(COALESCE(amount, '0') AS REAL)) AS txn_sum
                FROM statement_transactions
                GROUP BY uid, bank_code
            ) tx ON tx.uid = s.uid AND tx.bank_code = s.bank_code
            WHERE date(substr(updated_at, 1, 10)) >= date('now', ?)
            ORDER BY date(substr(updated_at, 1, 10)) DESC, id DESC
            """,
            (f'-{months} months',),
        )
        return list(cur.fetchall())
    finally:
        conn.close()


def get_summary_by_bank_month(db_path: str, months: int = 3) -> list[sqlite3.Row]:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        cur = conn.cursor()
        months = max(1, int(months))
        cur.execute(
            """
            WITH base AS (
                SELECT
                    s.uid,
                    s.bank_code,
                    COALESCE(s.statement_month, substr(s.statement_date, 1, 7), 'unknown') AS ym,
                    CAST(COALESCE(s.total_due, '0') AS REAL) AS total_due,
                    CAST(COALESCE(s.minimum_due, '0') AS REAL) AS minimum_due
                FROM statements s
                WHERE date(substr(s.updated_at, 1, 10)) >= date('now', ?)
            ),
            tx AS (
                SELECT uid, bank_code, SUM(CAST(COALESCE(amount, '0') AS REAL)) AS txn_sum
                FROM statement_transactions
                GROUP BY uid, bank_code
            ),
            fx_code AS (
                SELECT
                    b.bank_code,
                    b.ym,
                    UPPER(t.txn_location_code) AS fx_code,
                    SUM(CAST(COALESCE(t.original_amount, '0') AS REAL)) AS fx_amount
                FROM base b
                JOIN statement_transactions t ON t.uid = b.uid AND t.bank_code = b.bank_code
                WHERE t.txn_location_code IS NOT NULL
                  AND t.txn_location_code <> ''
                  AND UPPER(t.txn_location_code) <> 'CN'
                GROUP BY b.bank_code, b.ym, UPPER(t.txn_location_code)
            ),
            fx_agg AS (
                SELECT
                    bank_code,
                    ym,
                    GROUP_CONCAT(fx_code) AS foreign_codes,
                    ROUND(SUM(fx_amount), 2) AS sum_foreign_amount,
                    GROUP_CONCAT(fx_code || ':' || ROUND(fx_amount, 2)) AS foreign_amount_breakdown
                FROM fx_code
                GROUP BY bank_code, ym
            )
            SELECT
                b.bank_code,
                b.ym,
                COUNT(*) AS statement_count,
                ROUND(SUM(b.total_due), 2) AS sum_total_due,
                ROUND(SUM(b.minimum_due), 2) AS sum_minimum_due,
                ROUND(SUM(COALESCE(tx.txn_sum, 0)), 2) AS sum_txn_amount,
                ROUND(SUM(b.total_due - COALESCE(tx.txn_sum, 0)), 2) AS sum_reconcile_diff,
                COALESCE(fx_agg.foreign_codes, '-') AS foreign_codes,
                COALESCE(fx_agg.sum_foreign_amount, 0) AS sum_foreign_amount,
                COALESCE(fx_agg.foreign_amount_breakdown, '-') AS foreign_amount_breakdown
            FROM base b
            LEFT JOIN tx ON tx.uid = b.uid AND tx.bank_code = b.bank_code
            LEFT JOIN fx_agg ON fx_agg.bank_code = b.bank_code AND fx_agg.ym = b.ym
            GROUP BY b.bank_code, b.ym
            ORDER BY b.ym DESC, b.bank_code ASC
            """,
            (f'-{months} months',),
        )
        return list(cur.fetchall())
    finally:
        conn.close()


def get_reconciliation_rows(db_path: str, months: int = 3) -> list[sqlite3.Row]:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        cur = conn.cursor()
        months = max(1, int(months))
        cur.execute(
            """
            SELECT
                s.uid,
                s.bank_code,
                s.statement_month,
                s.total_due,
                ROUND(COALESCE(tx.txn_sum, 0), 2) AS txn_sum,
                ROUND(CAST(COALESCE(s.total_due, '0') AS REAL) - COALESCE(tx.txn_sum, 0), 2) AS reconcile_diff
            FROM statements s
            LEFT JOIN (
                SELECT uid, bank_code, SUM(CAST(COALESCE(amount, '0') AS REAL)) AS txn_sum
                FROM statement_transactions
                GROUP BY uid, bank_code
            ) tx ON tx.uid = s.uid AND tx.bank_code = s.bank_code
            WHERE date(substr(s.updated_at, 1, 10)) >= date('now', ?)
            ORDER BY s.bank_code ASC, s.uid ASC
            """,
            (f'-{months} months',),
        )
        return list(cur.fetchall())
    finally:
        conn.close()


def get_transactions_above_amount(db_path: str, min_amount: float, months: int = None) -> list:
    """查询金额大于等于阈值的交易明细（按绝对值过滤）。"""
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        cur = conn.cursor()
        threshold = float(min_amount)

        if months is not None:
            months = max(1, int(months))
            cur.execute(
                """
                SELECT
                    t.uid,
                    t.bank_code,
                    s.statement_month,
                    t.txn_date,
                    t.post_date,
                    t.description,
                    ROUND(CAST(COALESCE(t.amount, '0') AS REAL), 2) AS amount,
                    t.currency,
                    t.txn_location_code,
                    ROUND(CAST(COALESCE(t.original_amount, '0') AS REAL), 2) AS original_amount
                FROM statement_transactions t
                JOIN statements s ON s.uid = t.uid AND s.bank_code = t.bank_code
                WHERE date(substr(s.updated_at, 1, 10)) >= date('now', ?)
                  AND ABS(CAST(COALESCE(t.amount, '0') AS REAL)) >= ?
                ORDER BY ABS(CAST(COALESCE(t.amount, '0') AS REAL)) DESC,
                         date(COALESCE(t.post_date, t.txn_date)) DESC,
                         t.uid DESC
                """,
                (f'-{months} months', threshold),
            )
        else:
            cur.execute(
                """
                SELECT
                    t.uid,
                    t.bank_code,
                    s.statement_month,
                    t.txn_date,
                    t.post_date,
                    t.description,
                    ROUND(CAST(COALESCE(t.amount, '0') AS REAL), 2) AS amount,
                    t.currency,
                    t.txn_location_code,
                    ROUND(CAST(COALESCE(t.original_amount, '0') AS REAL), 2) AS original_amount
                FROM statement_transactions t
                JOIN statements s ON s.uid = t.uid AND s.bank_code = t.bank_code
                WHERE ABS(CAST(COALESCE(t.amount, '0') AS REAL)) >= ?
                ORDER BY ABS(CAST(COALESCE(t.amount, '0') AS REAL)) DESC,
                         date(COALESCE(t.post_date, t.txn_date)) DESC,
                         t.uid DESC
                """,
                (threshold,),
            )
        return list(cur.fetchall())
    finally:
        conn.close()

def mark_statement_paid(db_path: str, bank_code: str, statement_month: str = None) -> int:
    conn = sqlite3.connect(db_path)
    try:
        cur = conn.cursor()
        now_str = _utc_now_iso()
        if statement_month:
            cur.execute(
                "UPDATE statements SET is_paid=1, paid_at=?, updated_at=? WHERE bank_code=? AND statement_month=? AND is_paid=0",
                (now_str, now_str, bank_code, statement_month)
            )
        else:
            cur.execute(
                """
                UPDATE statements SET is_paid=1, paid_at=?, updated_at=? WHERE id = (
                    SELECT id FROM statements
                    WHERE bank_code=? AND is_paid=0 AND CAST(COALESCE(total_due, '0') AS REAL) > 0
                    ORDER BY date(due_date) DESC, date(statement_date) DESC, id DESC
                    LIMIT 1
                )
                """,
                (now_str, now_str, bank_code)
            )
        rowcount = cur.rowcount
        conn.commit()
        return rowcount
    finally:
        conn.close()

def get_unpaid_statements(db_path: str, months: int = None) -> list[sqlite3.Row]:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        cur = conn.cursor()
        
        query = """
            WITH latest AS (
              SELECT uid, bank_code, subject, statement_month, statement_date, due_date, total_due, minimum_due, email_date, updated_at,
                     ROW_NUMBER() OVER (
                       PARTITION BY bank_code, statement_month, due_date, total_due
                       ORDER BY email_date DESC, id DESC
                     ) AS rn
              FROM statements
              WHERE is_paid = 0
                AND CAST(COALESCE(total_due, '0') AS REAL) > 0
            )
            SELECT uid, bank_code, subject, statement_month, statement_date, due_date, total_due, minimum_due
            FROM latest
            WHERE rn = 1
        """
        
        params = []
        if months is not None:
            query += " AND date(substr(updated_at, 1, 10)) >= date('now', ?)"
            params.append(f'-{months} months')
            
        query += " ORDER BY due_date ASC, bank_code ASC"
        
        cur.execute(query, params)
        return list(cur.fetchall())
    finally:
        conn.close()


def upsert_email_body(db_path: str, account_name: str, uid: str,
                      raw_html: str = None, plain_text: str = None,
                      markdown_tables: str = None) -> int:
    """同步 upsert 正文（与 upsert_email_summary 同级调用）。"""
    conn = sqlite3.connect(db_path)
    try:
        content_len = (len(plain_text or "") + len(raw_html or ""))
        now = _utc_now_iso()
        cur = conn.cursor()
        cur.execute(
            """
            INSERT INTO email_bodies (account_name, uid, raw_html, plain_text,
                                       markdown_tables, content_len, fetched_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(account_name, uid) DO UPDATE SET
                raw_html = excluded.raw_html,
                plain_text = excluded.plain_text,
                markdown_tables = excluded.markdown_tables,
                content_len = excluded.content_len,
                fetched_at = excluded.fetched_at
            """,
            (account_name, uid, raw_html, plain_text, markdown_tables, content_len, now)
        )
        conn.commit()
        return cur.lastrowid
    finally:
        conn.close()


def upsert_email_summary(db_path: str, record: EmailSummaryRecord) -> int:
    conn = sqlite3.connect(db_path)
    try:
        cur = conn.cursor()
        now = _utc_now_iso()
        cur.execute(
            """
            INSERT INTO email_summaries (
                account_name, uid, sender, subject, email_date, category,
                importance, summary, actions_json, deadline, deadline_raw,
                status, retry_count, processed_at, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(account_name, uid) DO UPDATE SET
                sender = excluded.sender,
                subject = excluded.subject,
                email_date = excluded.email_date,
                category = excluded.category,
                importance = excluded.importance,
                summary = excluded.summary,
                actions_json = excluded.actions_json,
                deadline = excluded.deadline,
                deadline_raw = excluded.deadline_raw,
                status = excluded.status,
                retry_count = excluded.retry_count,
                processed_at = excluded.processed_at
            """,
            (
                record.account_name,
                record.uid,
                record.sender,
                record.subject,
                record.email_date,
                record.category,
                record.importance,
                record.summary,
                record.actions_json,
                record.deadline,
                record.deadline_raw,
                record.status,
                record.retry_count,
                record.processed_at,
                now
            )
        )
        if cur.lastrowid:
            row_id = cur.lastrowid
        else:
            cur.execute(
                "SELECT id FROM email_summaries WHERE account_name = ? AND uid = ? LIMIT 1",
                (record.account_name, record.uid)
            )
            row_id = cur.fetchone()[0]
        conn.commit()
        return row_id
    finally:
        conn.close()


def get_email_summary_status(db_path: str, account_name: str, uid: str) -> tuple[Optional[str], int]:
    """获取邮件的当前状态和已重试次数。返回 (status, retry_count)。如果不存在则返回 (None, 0)。"""
    conn = sqlite3.connect(db_path)
    try:
        cur = conn.cursor()
        cur.execute(
            "SELECT status, retry_count FROM email_summaries WHERE account_name = ? AND uid = ? LIMIT 1",
            (account_name, uid)
        )
        row = cur.fetchone()
        if row:
            return row[0], row[1] or 0
        return None, 0
    finally:
        conn.close()


def add_noise_rule(db_path: str, pattern_type: str, pattern_value: str) -> None:
    """插入一条降噪过滤规则（具有唯一约束，幂等）"""
    conn = sqlite3.connect(db_path)
    try:
        cur = conn.cursor()
        now = _utc_now_iso()
        cur.execute(
            """
            INSERT OR IGNORE INTO noise_rules (pattern_type, pattern_value, created_at)
            VALUES (?, ?, ?)
            """,
            (pattern_type, pattern_value, now)
        )
        conn.commit()
    finally:
        conn.close()


def delete_noise_rule(db_path: str, pattern_type: str, pattern_value: str) -> bool:
    """删除一条指定的降噪过滤规则"""
    conn = sqlite3.connect(db_path)
    try:
        cur = conn.cursor()
        cur.execute(
            "DELETE FROM noise_rules WHERE pattern_type = ? AND pattern_value = ?",
            (pattern_type, pattern_value)
        )
        conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()


def load_noise_rules(db_path: str) -> list[tuple[str, str]]:
    """加载所有自定义的降噪规则，返回 list[(pattern_type, pattern_value)]"""
    conn = sqlite3.connect(db_path)
    try:
        cur = conn.cursor()
        cur.execute("SELECT pattern_type, pattern_value FROM noise_rules")
        return cur.fetchall()
    finally:
        conn.close()


def update_summary_status(db_path: str, summary_id: int, status: str) -> bool:
    """依据数据库自增主键 ID，更新邮件摘要的状态 (status)"""
    conn = sqlite3.connect(db_path)
    try:
        cur = conn.cursor()
        now = _utc_now_iso()
        cur.execute(
            "UPDATE email_summaries SET status = ?, processed_at = ? WHERE id = ?",
            (status, now, summary_id)
        )
        conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()


def get_email_summary_by_id(db_path: str, summary_id: int) -> Optional[sqlite3.Row]:
    """依据数据库自增主键 ID，获取单条邮件摘要记录"""
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        cur = conn.cursor()
        cur.execute("SELECT * FROM email_summaries WHERE id = ?", (summary_id,))
        return cur.fetchone()
    finally:
        conn.close()


def get_recent_email_headers(db_path: str, limit: int = 15) -> list[sqlite3.Row]:
    """获取最近拉取的邮件摘要记录用于标题列表展示"""
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        cur = conn.cursor()
        cur.execute(
            """
            SELECT id, account_name, uid, sender, subject, email_date, category, importance, status, processed_at
            FROM email_summaries 
            ORDER BY email_date DESC, id DESC 
            LIMIT ?
            """,
            (limit,)
        )
        return list(cur.fetchall())
    finally:
        conn.close()


def get_potential_missed_emails(db_path: str, limit: int = 15) -> list[sqlite3.Row]:
    """获取可能错过的非高优邮件记录 (最近7天内，importance != 'high', category != 'Spam')"""
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        cur = conn.cursor()
        import datetime
        cutoff_date = (datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=7)).isoformat()
        cur.execute(
            """
            SELECT id, account_name, uid, sender, subject, email_date, category, importance, summary, status
            FROM email_summaries 
            WHERE importance != 'high' AND category != 'Spam' AND status IN ('processed', 'skipped') AND email_date >= ?
            ORDER BY email_date DESC, id DESC 
            LIMIT ?
            """,
            (cutoff_date, limit)
        )
        return list(cur.fetchall())
    finally:
        conn.close()
