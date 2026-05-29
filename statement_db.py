#!/usr/bin/env python3
"""SQLite persistence for statement parsing and validation."""

import sqlite3
from datetime import datetime, timezone
from typing import Iterable

from statement_models import StatementRecord, ValidationIssue, StatementTransactionRecord


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

        # 兼容历史库：为已存在表补齐新增字段
        cur.execute("PRAGMA table_info(statement_transactions)")
        existing_cols = {row[1] for row in cur.fetchall()}
        if 'txn_location_code' not in existing_cols:
            cur.execute("ALTER TABLE statement_transactions ADD COLUMN txn_location_code TEXT")
        if 'original_amount' not in existing_cols:
            cur.execute("ALTER TABLE statement_transactions ADD COLUMN original_amount TEXT")

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
