#!/usr/bin/env python3
"""SQLite persistence for statement parsing and validation."""

import os
import hashlib
import json
import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Iterable, List, Optional, Tuple

from statement_models import (StatementRecord, ValidationIssue, StatementTransactionRecord,
                              EmailSummaryRecord, QualityEventRecord, StatementBalanceRecord)


class MessageIdentityConflict(ValueError):
    """Same account/bank/Message-ID under a different transport identity."""


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _migrate_account_name(cur) -> None:
    """账单存储层加 account_name 列，防跨账号 UID 碰撞覆盖。幂等。
    statements 改 UNIQUE 须重建表；statement_transactions/validation_runs 无 UNIQUE，ALTER+UPDATE 回填。
    单账户 uid 可还原归属；多账号同号(碰撞幸存行) -> '__orphan__'。"""
    # --- statements: 重建表（UNIQUE(uid,bank_code) -> UNIQUE(account_name,uid,bank_code)）---
    stmt_cols = {r[1] for r in cur.execute("PRAGMA table_info(statements)").fetchall()}
    if 'account_name' not in stmt_cols:
        cur.execute("""
            CREATE TABLE statements_new (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                account_name TEXT NOT NULL DEFAULT '__orphan__',
                uid TEXT NOT NULL, message_id TEXT, bank_code TEXT NOT NULL, rule_id TEXT NOT NULL,
                subject TEXT, sender TEXT, email_date TEXT,
                statement_month TEXT, statement_date TEXT, due_date TEXT,
                total_due TEXT, minimum_due TEXT, raw_fields_json TEXT,
                is_paid INTEGER NOT NULL DEFAULT 0, paid_at TEXT,
                created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
                UNIQUE(account_name, uid, bank_code)
            )
        """)
        cur.execute("""
            INSERT INTO statements_new (
                account_name, uid, message_id, bank_code, rule_id, subject, sender, email_date,
                statement_month, statement_date, due_date, total_due, minimum_due, raw_fields_json,
                is_paid, paid_at, created_at, updated_at)
            SELECT
                COALESCE(es.account_name, '__orphan__'),
                s.uid, s.message_id, s.bank_code, s.rule_id, s.subject, s.sender, s.email_date,
                s.statement_month, s.statement_date, s.due_date, s.total_due, s.minimum_due, s.raw_fields_json,
                s.is_paid, s.paid_at, s.created_at, s.updated_at
            FROM statements s
            LEFT JOIN (
                SELECT uid, MIN(account_name) AS account_name
                FROM email_summaries GROUP BY uid HAVING COUNT(DISTINCT account_name)=1
            ) es ON es.uid = s.uid
        """)
        cur.execute("DROP TABLE statements")
        cur.execute("ALTER TABLE statements_new RENAME TO statements")

    # --- statement_transactions: ALTER + UPDATE 回填 ---
    txn_cols = {r[1] for r in cur.execute("PRAGMA table_info(statement_transactions)").fetchall()}
    if 'account_name' not in txn_cols:
        cur.execute("ALTER TABLE statement_transactions ADD COLUMN account_name TEXT NOT NULL DEFAULT '__orphan__'")
        cur.execute("""
            UPDATE statement_transactions SET account_name = COALESCE(
                (SELECT es.account_name FROM (
                    SELECT uid, MIN(account_name) AS account_name FROM email_summaries
                    GROUP BY uid HAVING COUNT(DISTINCT account_name)=1
                ) es WHERE es.uid = statement_transactions.uid),
                '__orphan__')
        """)

    # --- validation_runs: ALTER + UPDATE 回填（无数据丢失风险，仅补归属）---
    vr_cols = {r[1] for r in cur.execute("PRAGMA table_info(validation_runs)").fetchall()}
    if 'account_name' not in vr_cols:
        cur.execute("ALTER TABLE validation_runs ADD COLUMN account_name TEXT NOT NULL DEFAULT '__orphan__'")
        cur.execute("""
            UPDATE validation_runs SET account_name = COALESCE(
                (SELECT es.account_name FROM (
                    SELECT uid, MIN(account_name) AS account_name FROM email_summaries
                    GROUP BY uid HAVING COUNT(DISTINCT account_name)=1
                ) es WHERE es.uid = validation_runs.uid),
                '__orphan__')
        """)


def init_db(db_path: str) -> None:
    parent = os.path.dirname(os.path.abspath(db_path))
    if parent:
        os.makedirs(parent, exist_ok=True)
    conn = sqlite3.connect(db_path)
    try:
        conn.execute('BEGIN IMMEDIATE')
        cur = conn.cursor()
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS statements (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                account_name TEXT NOT NULL DEFAULT '__orphan__',
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
                parse_status TEXT NOT NULL DEFAULT 'ok',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                UNIQUE(account_name, uid, bank_code)
            )
            """
        )
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS validation_runs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                account_name TEXT NOT NULL DEFAULT '__orphan__',
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
                account_name TEXT NOT NULL DEFAULT '__orphan__',
                uid TEXT NOT NULL,
                bank_code TEXT NOT NULL,
                statement_id INTEGER,
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

        cur.execute("PRAGMA table_info(email_summaries)")
        summar_cols = {row[1] for row in cur.fetchall()}
        if 'pushed' not in summar_cols:
            cur.execute("ALTER TABLE email_summaries ADD COLUMN pushed INTEGER NOT NULL DEFAULT 0")

        # account_name 迁移：账单存储层加账号维度，防跨账号 UID 碰撞覆盖（幂等）
        _migrate_account_name(cur)

        # statement_id / parse_status 迁移（必须在 _migrate_account_name 之后：
        # 旧库重建 statements 表才会带 account_name，回填 JOIN 才不报错；幂等）
        cur.execute("PRAGMA table_info(statement_transactions)")
        txn_cols2 = {row[1] for row in cur.fetchall()}
        if 'statement_id' not in txn_cols2:
            # 交易显式关联稳定 statement_id（原子级联替换的锚点）
            cur.execute("ALTER TABLE statement_transactions ADD COLUMN statement_id INTEGER")
            cur.execute("""
                UPDATE statement_transactions SET statement_id = (
                    SELECT s.id FROM statements s
                    WHERE s.account_name = statement_transactions.account_name
                      AND s.uid = statement_transactions.uid
                      AND s.bank_code = statement_transactions.bank_code
                    LIMIT 1)
                WHERE statement_id IS NULL
            """)
        cur.execute("CREATE INDEX IF NOT EXISTS idx_stmt_txn_sid ON statement_transactions(statement_id)")

        cur.execute("PRAGMA table_info(statements)")
        stmt_cols2 = {row[1] for row in cur.fetchall()}
        # 三态分流：'ok' 合法账单 / 'failed' 命中模板但校验未过（保留证据，排除出财务汇总）
        if 'parse_status' not in stmt_cols2:
            cur.execute("ALTER TABLE statements ADD COLUMN parse_status TEXT NOT NULL DEFAULT 'ok'")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_statements_msgid ON statements(account_name, bank_code, message_id)")

        # ---------- 币种证据列（重构契约 §1.1）：历史 CNY 标记为 legacy_assumed，不作已核实证据
        cur.execute("PRAGMA table_info(statement_transactions)")
        txn_cols3 = {row[1] for row in cur.fetchall()}
        if 'original_currency' not in txn_cols3:
            cur.execute("ALTER TABLE statement_transactions ADD COLUMN original_currency TEXT")
        if 'currency_source' not in txn_cols3:
            cur.execute("ALTER TABLE statement_transactions ADD COLUMN currency_source TEXT")
            cur.execute("UPDATE statement_transactions SET currency_source = 'legacy_assumed' WHERE currency = 'CNY'")
        if 'original_currency_source' not in txn_cols3:
            cur.execute("ALTER TABLE statement_transactions ADD COLUMN original_currency_source TEXT")

        # validation_runs 解析器版本（解析尝试证据链）
        cur.execute("PRAGMA table_info(validation_runs)")
        vr_cols2 = {row[1] for row in cur.fetchall()}
        if 'parser_version' not in vr_cols2:
            cur.execute("ALTER TABLE validation_runs ADD COLUMN parser_version TEXT")

        # ---------- 多币种应还余额（重构契约 §1.2）；statements.total_due 保留为兼容字段
        cur.execute("""
            CREATE TABLE IF NOT EXISTS statement_balances (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                statement_id INTEGER NOT NULL,
                currency TEXT NOT NULL,
                total_due TEXT,
                minimum_due TEXT,
                currency_source TEXT,
                evidence_json TEXT NOT NULL DEFAULT '{}',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                UNIQUE(statement_id, currency)
            )
        """)
        # 历史账单回填一行 legacy CNY（幂等：INSERT OR IGNORE 不覆盖已存在行）
        # 注：evidence_json 用字符串常量，不依赖 SQLite json1 编译选项
        cur.execute("""
            INSERT OR IGNORE INTO statement_balances
                (statement_id, currency, total_due, minimum_due, currency_source, evidence_json, created_at, updated_at)
            SELECT s.id, 'CNY', s.total_due, s.minimum_due, 'legacy_assumed',
                   '{"note": "legacy backfill, not verified evidence"}',
                   s.created_at, s.updated_at
            FROM statements s
            WHERE s.parse_status = 'ok' AND s.total_due IS NOT NULL AND s.total_due <> ''
              AND NOT EXISTS (SELECT 1 FROM statement_balances b WHERE b.statement_id = s.id)
        """)

        # ---------- 质量事件持久化（重构契约 §1.3）：at-least-once + 消费方幂等
        cur.execute("""
            CREATE TABLE IF NOT EXISTS quality_events (
                event_id TEXT PRIMARY KEY,
                case_key TEXT NOT NULL,
                event_type TEXT NOT NULL,
                account_name TEXT,
                bank_code TEXT,
                template_version TEXT,
                diagnostics_json TEXT NOT NULL,
                evidence_refs_json TEXT NOT NULL DEFAULT '[]',
                occurrence_count INTEGER NOT NULL DEFAULT 1,
                revision INTEGER NOT NULL DEFAULT 1,
                cycle_id INTEGER NOT NULL DEFAULT 1,
                first_seen_at TEXT NOT NULL,
                last_seen_at TEXT NOT NULL,
                acked_at TEXT,
                acked_todo_id TEXT
            )
        """)
        cur.execute("CREATE INDEX IF NOT EXISTS idx_quality_events_case ON quality_events(case_key)")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_quality_events_unacked ON quality_events(acked_at) WHERE acked_at IS NULL")
        cur.execute("PRAGMA table_info(quality_events)")
        qe_cols = {row[1] for row in cur.fetchall()}
        if 'revision' not in qe_cols:
            cur.execute("ALTER TABLE quality_events ADD COLUMN revision INTEGER NOT NULL DEFAULT 1")
        if 'cycle_id' not in qe_cols:
            cur.execute("ALTER TABLE quality_events ADD COLUMN cycle_id INTEGER NOT NULL DEFAULT 1")
        if "observation_keys_json" not in qe_cols:
            cur.execute("ALTER TABLE quality_events ADD COLUMN observation_keys_json TEXT NOT NULL DEFAULT '[]'")

        cur.execute("""CREATE TABLE IF NOT EXISTS quality_event_sources (
            event_id TEXT NOT NULL, cycle_id INTEGER NOT NULL, identity_key TEXT NOT NULL,
            content_hash TEXT, state TEXT NOT NULL DEFAULT 'unresolved',
            checked_parser_version TEXT, checked_at TEXT,
            PRIMARY KEY(event_id, cycle_id, identity_key))""")

        for event_id, cycle_id, refs_json in cur.execute(
                "SELECT event_id,cycle_id,evidence_refs_json FROM quality_events WHERE event_type IN ('quality_detected','quality_updated')").fetchall():
            for ref in json.loads(refs_json or '[]'):
                identity=json.dumps(_evidence_identity(ref),ensure_ascii=False)
                cur.execute("INSERT OR IGNORE INTO quality_event_sources(event_id,cycle_id,identity_key,content_hash,state) VALUES(?,?,?,?,'unresolved')",
                            (event_id,cycle_id,identity,ref.get('content_hash')))

        # ---------- 跨进程互斥租约（重构契约 §1.4）：覆盖并发 cron / 手动命令 / 重试
        cur.execute("""
            CREATE TABLE IF NOT EXISTS sync_leases (
                lease_key TEXT PRIMARY KEY,
                owner TEXT NOT NULL,
                expires_at TEXT NOT NULL,
                acquired_at TEXT NOT NULL
            )
        """)

        # ---------- 运输身份映射与审计（E1 契约 §4, §10） ----------
        cur.execute("""
            CREATE TABLE IF NOT EXISTS statement_transport_identities (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                account_name TEXT NOT NULL,
                transport_kind TEXT NOT NULL,
                transport_uid TEXT NOT NULL,
                bank_code TEXT NOT NULL,
                statement_id INTEGER NOT NULL,
                message_id TEXT NOT NULL,
                evidence_digest TEXT,
                evidence_version TEXT NOT NULL DEFAULT 'v1',
                raw_content_hash TEXT,
                method TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                FOREIGN KEY(statement_id) REFERENCES statements(id),
                UNIQUE(account_name, transport_kind, transport_uid, bank_code)
            )
        """)
        cur.execute("CREATE INDEX IF NOT EXISTS idx_stmt_trans_sid ON statement_transport_identities(statement_id)")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_stmt_trans_lookup ON statement_transport_identities(account_name, transport_uid)")

        cur.execute("""
            CREATE TABLE IF NOT EXISTS statement_identity_audit (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                batch_id TEXT NOT NULL,
                plan_id TEXT,
                plan_hash TEXT,
                action TEXT NOT NULL,
                account_name TEXT NOT NULL,
                transport_kind TEXT NOT NULL,
                transport_uid TEXT NOT NULL,
                bank_code TEXT NOT NULL,
                statement_id INTEGER NOT NULL,
                old_statement_id INTEGER,
                new_statement_id INTEGER,
                message_id TEXT NOT NULL,
                normalization_version TEXT NOT NULL,
                source_raw_hash TEXT,
                source_digest TEXT,
                target_raw_hash TEXT,
                target_digest TEXT,
                coverage_status TEXT,
                missing_reason TEXT,
                method TEXT NOT NULL,
                actor TEXT NOT NULL,
                reason TEXT NOT NULL,
                created_at TEXT NOT NULL
            )
        """)
        cur.execute("CREATE INDEX IF NOT EXISTS idx_stmt_audit_sid ON statement_identity_audit(statement_id)")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_stmt_audit_uid ON statement_identity_audit(account_name, transport_uid)")

        cur.execute("""
            CREATE TABLE IF NOT EXISTS statement_evidence_index (
                statement_id INTEGER PRIMARY KEY,
                account_name TEXT NOT NULL,
                bank_code TEXT NOT NULL,
                message_id TEXT NOT NULL,
                raw_content_hash TEXT NOT NULL,
                evidence_digest TEXT NOT NULL,
                evidence_version TEXT NOT NULL DEFAULT 'v1',
                coverage_status TEXT NOT NULL,
                archive_path TEXT,
                created_at TEXT NOT NULL,
                FOREIGN KEY(statement_id) REFERENCES statements(id)
            )
        """)
        cur.execute("CREATE INDEX IF NOT EXISTS idx_stmt_ev_msg ON statement_evidence_index(account_name, bank_code, message_id)")


        # 历史账单还款状态不再由 init_db 推断：到期日已过不构成还款证据，
        # is_paid 仅通过显式标记（mark_paid / 清洗继承）变更。

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
                account_name, uid, message_id, bank_code, rule_id, subject, sender, email_date,
                statement_month, statement_date, due_date, total_due, minimum_due,
                raw_fields_json, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(account_name, uid, bank_code) DO UPDATE SET
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
                s.account_name,
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


def derive_event_id(case_key: str) -> str:
    """event_id 由 case_key 确定性派生：同一 case 重复扫描不产生新行（重构契约 §1.3）。"""
    return hashlib.sha256(('quality://' + case_key).encode('utf-8')).hexdigest()[:32]


def _evidence_identity(ref):
    """Message-ID survives UID drift; scope identity by mailbox."""
    account = ref.get('account') or ref.get('account_name') or ''
    if ref.get('message_id'):
        return (account, 'message_id', str(ref['message_id']).strip())
    if ref.get('uid') is not None:
        return (account, 'uid', str(ref['uid']))
    if ref.get('statement_id') is not None:
        return (account, 'statement_id', str(ref['statement_id']))
    return (account, 'evidence', json.dumps(ref, sort_keys=True, ensure_ascii=False))


def _dedupe_evidence_refs(existing_refs, new_refs):
    combined = {_evidence_identity(r): r for r in existing_refs or []}
    combined.update({_evidence_identity(r): r for r in new_refs or []})
    return list(combined.values())


def _observation_key(diagnostics, refs, template_version):
    # Report paths and transport UIDs are delivery details, not new observations.
    semantic_diags = [{k: v for k, v in d.items() if k != 'evidence_ref'} for d in diagnostics]
    semantic_refs = []
    for ref in refs:
        semantic_refs.append({'identity': _evidence_identity(ref), **{
            k: ref[k] for k in ('content_hash', 'parser_version', 'due_date', 'total_due') if k in ref}})
    payload = [template_version, sorted(semantic_diags, key=lambda d: json.dumps(d, sort_keys=True)),
               sorted(semantic_refs, key=lambda r: json.dumps(r, sort_keys=True))]
    return hashlib.sha256(json.dumps(payload, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def _record_quality_sources_locked(cur, event_id, cycle_id, refs):
    for ref in refs:
        identity = json.dumps(_evidence_identity(ref), ensure_ascii=False)
        cur.execute("""INSERT INTO quality_event_sources(event_id,cycle_id,identity_key,content_hash,state)
            VALUES(?,?,?,?,'unresolved') ON CONFLICT(event_id,cycle_id,identity_key)
            DO UPDATE SET content_hash=excluded.content_hash,state='unresolved',
            checked_parser_version=NULL,checked_at=NULL""",
            (event_id,cycle_id,identity,ref.get('content_hash')))


def _upsert_quality_event_locked(cur, event: 'QualityEventRecord') -> str:
    """Delivery acknowledgment does not resolve a problem. Only new evidence advances it."""
    if event.event_type not in ('quality_detected', 'quality_updated', 'quality_resolved', 'statement_recorded'):
        raise ValueError('unsupported quality event type')
    now = _utc_now_iso()
    event_id = derive_event_id(event.case_key)
    row = cur.execute("""SELECT event_type, diagnostics_json, evidence_refs_json,
        revision, cycle_id, observation_keys_json, template_version
        FROM quality_events WHERE event_id=?""", (event_id,)).fetchone()
    diagnostics = [d.to_dict() for d in event.diagnostics]
    refs = _dedupe_evidence_refs([], event.evidence_refs)
    if event.event_type == 'quality_resolved':
        if row is None:
            raise ValueError('cannot resolve an unknown quality case')
        old_type, _, _, rev, cycle, _, _ = row
        if old_type == 'statement_recorded':
            raise ValueError('statement event is not a quality case')
        if old_type == 'quality_resolved' and event.cycle_id == cycle and event.revision in (rev, rev - 1):
            return event_id
        if event.revision != rev or event.cycle_id != cycle:
            raise ValueError('stale quality resolution: revision/cycle mismatch')
        if old_type != 'quality_resolved':
            cur.execute("""UPDATE quality_events SET event_type='quality_resolved',
                diagnostics_json='[]', revision=revision+1, last_seen_at=?,
                acked_at=NULL, acked_todo_id=NULL WHERE event_id=?""", (now, event_id))
        return event_id
    observation = _observation_key(diagnostics, refs, event.template_version)
    if row is None:
        cur.execute("""INSERT INTO quality_events(event_id,case_key,event_type,account_name,
            bank_code,template_version,diagnostics_json,evidence_refs_json,occurrence_count,
            revision,cycle_id,observation_keys_json,first_seen_at,last_seen_at)
            VALUES(?,?,?,?,?,?,?,?,?,1,1,?,?,?)""",
            (event_id,event.case_key,event.event_type,event.account_name,event.bank_code,
             event.template_version,json.dumps(diagnostics,ensure_ascii=False),
             json.dumps(refs,ensure_ascii=False),len(refs) or 1,
             json.dumps([observation]),now,now))
        if event.event_type != 'statement_recorded':
            _record_quality_sources_locked(cur, event_id, 1, refs)
        return event_id
    old_type, old_diag, old_refs_json, rev, cycle, keys_json, old_template = row
    if (old_type == 'statement_recorded') != (event.event_type == 'statement_recorded'):
        raise ValueError('cannot change statement event into quality case')
    old_refs = json.loads(old_refs_json or '[]')
    keys = set(json.loads(keys_json or '[]'))
    if not keys:
        # Seed legacy events lazily within this write transaction.
        keys.add(_observation_key(json.loads(old_diag or '[]'), old_refs, old_template))
    repeated = observation in keys
    if event.event_type == 'statement_recorded':
        # A financial field can change back to a prior value: notify that change too.
        repeated = observation == _observation_key(json.loads(old_diag or '[]'), old_refs, old_template)
    if repeated:
        cur.execute('UPDATE quality_events SET observation_keys_json=? WHERE event_id=?',
                    (json.dumps(sorted(keys)), event_id))
        return event_id
    keys.add(observation)
    merged = _dedupe_evidence_refs(old_refs, refs)
    new_cycle = cycle + 1 if old_type == 'quality_resolved' else cycle
    cur.execute("""UPDATE quality_events SET event_type=?,account_name=COALESCE(?,account_name),
        bank_code=COALESCE(?,bank_code),template_version=COALESCE(?,template_version),
        diagnostics_json=?,evidence_refs_json=?,occurrence_count=?,revision=?,cycle_id=?,
        observation_keys_json=?,last_seen_at=?,acked_at=NULL,acked_todo_id=NULL WHERE event_id=?""",
        (event.event_type,event.account_name,event.bank_code,event.template_version,
         json.dumps(diagnostics,ensure_ascii=False),json.dumps(merged,ensure_ascii=False),
         len(merged) or 1,rev+1,new_cycle,json.dumps(sorted(keys)),now,event_id))
    if event.event_type != 'statement_recorded':
        _record_quality_sources_locked(cur, event_id, new_cycle, refs)
    return event_id

def upsert_quality_event(db_path: str, event: 'QualityEventRecord') -> str:
    """独立连接版本（供工具/测试使用）；主路径在 save_statement_with_transactions 事务内调用。"""
    conn = sqlite3.connect(db_path)
    try:
        conn.execute('BEGIN IMMEDIATE')
        event_id = _upsert_quality_event_locked(conn.cursor(), event)
        conn.commit()
        return event_id
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def get_unacked_quality_events(db_path: str, limit: int = 100, event_types=None) -> list:
    """未确认质量事件（只读），按最近发生排序。
    若数据库文件不存在或缺少 quality_events 表，显式向上抛出异常，
    由外层转为结构化错误回执，严禁静默吞掉异常伪装为空队列。"""
    if not os.path.exists(db_path):
        raise FileNotFoundError(f"数据库文件不存在：{db_path}")
    conn = sqlite3.connect(f'file:{db_path}?mode=ro', uri=True)
    conn.row_factory = sqlite3.Row
    try:
        where = 'acked_at IS NULL'
        params = []
        if event_types is not None:
            if not event_types:
                return []
            where += ' AND event_type IN (' + ','.join('?' for _ in event_types) + ')'
            params.extend(event_types)
        rows = conn.execute('SELECT * FROM quality_events WHERE ' + where +
                            ' ORDER BY last_seen_at, event_id LIMIT ?', params + [limit]).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def ack_quality_event(db_path: str, event_id: str, todo_id: str, revision: Optional[int] = None) -> dict:
    """Exact version CAS; delivery ack does not advance the occurrence cycle."""
    result = {'acked': False, 'event_id': event_id, 'todo_id': todo_id, 'revision': revision}
    if not isinstance(revision, int) or isinstance(revision, bool) or revision < 1:
        return {**result, 'reason': 'revision_required'}
    if not event_id or not todo_id:
        return {**result, 'reason': 'identity_required'}
    conn = sqlite3.connect(db_path)
    try:
        conn.execute('BEGIN IMMEDIATE')
        row = conn.execute('SELECT acked_todo_id,acked_at,revision FROM quality_events WHERE event_id=?',
                           (event_id,)).fetchone()
        if row is None:
            return {**result, 'reason': 'not_found'}
        owner, acked_at, current = row
        if revision != current:
            return {**result, 'current_revision': current,
                    'reason': 'stale_revision' if revision < current else 'revision_mismatch'}
        if acked_at is not None:
            return {**result, 'acked': owner == todo_id,
                    'reason': 'already_acked' if owner == todo_id else 'owner_mismatch'}
        cur = conn.execute("""UPDATE quality_events SET acked_at=?,acked_todo_id=?
            WHERE event_id=? AND revision=? AND acked_at IS NULL""",
            (_utc_now_iso(),todo_id,event_id,revision))
        conn.commit()
        return {**result, 'acked': cur.rowcount == 1, 'reason': 'acked'}
    finally:
        conn.close()


def acquire_lease(db_path: str, lease_key: str, owner: str, ttl_seconds: int = 600) -> bool:
    """跨进程互斥租约：获取成功 True；被占用/过期前占用者持有中 False。

    过期租约由下一次获取自动清理（恢复语义：持有者崩溃后 TTL 到期自然让位）。"""
    now_dt = datetime.now(timezone.utc)
    expires = (now_dt + timedelta(seconds=ttl_seconds)).isoformat()
    conn = sqlite3.connect(db_path, isolation_level=None)
    try:
        conn.execute('PRAGMA busy_timeout = 3000')
        conn.execute('BEGIN IMMEDIATE')
        conn.execute("DELETE FROM sync_leases WHERE lease_key = ? AND expires_at < ?",
                     (lease_key, now_dt.isoformat()))
        try:
            conn.execute("INSERT INTO sync_leases (lease_key, owner, expires_at, acquired_at) VALUES (?, ?, ?, ?)",
                         (lease_key, owner, expires, now_dt.isoformat()))
            conn.execute('COMMIT')
            return True
        except sqlite3.IntegrityError:
            conn.execute('ROLLBACK')
            return False
    finally:
        conn.close()


def release_lease(db_path: str, lease_key: str, owner: str) -> bool:
    """释放租约（仅持有者本人可释放）。"""
    conn = sqlite3.connect(db_path)
    try:
        cur = conn.cursor()
        cur.execute("DELETE FROM sync_leases WHERE lease_key = ? AND owner = ?", (lease_key, owner))
        conn.commit()
        return bool(cur.rowcount)
    finally:
        conn.close()


def get_statement_balances(db_path: str, statement_id: int) -> list:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute("SELECT * FROM statement_balances WHERE statement_id = ? ORDER BY currency",
                            (statement_id,)).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def save_statement_with_transactions(db_path: str, s: StatementRecord,
                                     txns: Iterable[StatementTransactionRecord],
                                     balances: Optional[list] = None,
                                     quality_events: Optional[list] = None,
                                     parser_version: Optional[str] = None,
                                     quality_confirmation: Optional[dict] = None) -> tuple:
    """账单 + 交易 + 多币种余额 + 质量事件单事务原子保存。

    步骤（同一 BEGIN IMMEDIATE 事务内）：
    1. Upsert statements 取稳定 statement_id（is_paid/paid_at 永不回退）；
    2. 清空该账单旧明细（含迁移期未挂 statement_id 的同源明细），即使新明细为空也执行，
       避免重解析残留旧交易；旧余额行同样整份替换；
    3. 批量插入新明细并回挂 statement_id（含币种证据列）；
    4. 首次落库或关键字段（due_date/total_due）实质变化的合法账单 → 自动产出
       statement_recorded 事件（还款提醒闭环的事件源，重构契约 §5.3）；
    5. 传入的质量事件 upsert（与账单数据同事务，不伪跨库单事务）。
    返回 (statement_id, txn_count)。任何一步失败整体回滚。
    """
    items = list(txns)
    now = _utc_now_iso()
    conn = sqlite3.connect(db_path)
    try:
        conn.execute('BEGIN IMMEDIATE')
        cur = conn.cursor()
        # 保存前旧值（判断首次落库与关键字段变化，is_paid 保护不依赖此查询）
        cur.execute(
            "SELECT id, due_date, total_due, is_paid, parse_status FROM statements WHERE account_name=? AND uid=? AND bank_code=?",
            (s.account_name, str(s.uid), s.bank_code),
        )
        existing = cur.fetchone()
        # Check under the write lock, so concurrent transports cannot both
        # insert the same message. Existing finances and paid evidence stay intact.
        if s.message_id:
            cur.execute("SELECT id FROM statements WHERE account_name=? AND bank_code=? AND message_id=? AND uid<>?",
                        (s.account_name, s.bank_code, s.message_id, str(s.uid)))
            conflicts = [row[0] for row in cur.fetchall()]
            if conflicts:
                cur.execute(
                    "SELECT statement_id FROM statement_transport_identities WHERE account_name=? AND transport_uid=? AND bank_code=?",
                    (s.account_name, str(s.uid), s.bank_code)
                )
                mapped_sids = {row[0] for row in cur.fetchall()}
                unmapped = [cid for cid in conflicts if cid not in mapped_sids]
                if unmapped:
                    raise MessageIdentityConflict('Message-ID already belongs to statement IDs ' + str(unmapped))

        incoming_status = s.parse_status or 'ok'
        # R1 核心保护：若已有账单为合法账单 (parse_status='ok')，重解析失败 (parse_status!='ok')
        # 绝不覆盖原账单、原金额、交易明细与多币种余额及还款状态；仅保存新尝试的诊断事件。
        if existing is not None and len(existing) >= 5 and existing[4] == 'ok' and incoming_status != 'ok':
            existing_id = int(existing[0])
            for ev in (quality_events or []):
                _upsert_quality_event_locked(cur, ev)
            cur.execute("SELECT COUNT(*) FROM statement_transactions WHERE statement_id = ?", (existing_id,))
            cnt = int(cur.fetchone()[0])
            conn.commit()
            return existing_id, cnt

        cur.execute(
            """
            INSERT INTO statements (
                account_name, uid, message_id, bank_code, rule_id, subject, sender, email_date,
                statement_month, statement_date, due_date, total_due, minimum_due,
                raw_fields_json, parse_status, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(account_name, uid, bank_code) DO UPDATE SET
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
                parse_status = excluded.parse_status,
                updated_at = excluded.updated_at
            """,
            (
                s.account_name, s.uid, s.message_id, s.bank_code, s.rule_id,
                s.subject, s.sender, s.email_date, s.statement_month, s.statement_date,
                s.due_date, s.total_due, s.minimum_due, s.raw_fields_json,
                s.parse_status or 'ok', now, now,
            ),
        )
        cur.execute(
            "SELECT id FROM statements WHERE account_name = ? AND uid = ? AND bank_code = ? LIMIT 1",
            (s.account_name, s.uid, s.bank_code),
        )
        statement_id = int(cur.fetchone()[0])

        incoming_quality=json.loads(s.raw_fields_json or '{}').get('quality', {})
        incomplete_detail=any(d.get('field')=='transactions' and d.get('code') in
            ('TRANSACTION_COVERAGE_GAP','TRANSACTION_ROW_INVALID')
            for d in incoming_quality.get('diagnostics', []))
        preserve_detail=bool(existing and existing[4]=='ok' and incomplete_detail)
        if preserve_detail:
            incoming_quality['retained_previous_transactions']=True
            raw=json.loads(s.raw_fields_json or '{}')
            raw['quality']=incoming_quality
            cur.execute('UPDATE statements SET raw_fields_json=? WHERE id=?',
                        (json.dumps(raw,ensure_ascii=False),statement_id))
        else:
            cur.execute(
                """
                DELETE FROM statement_transactions
                WHERE statement_id = ?
                   OR (statement_id IS NULL AND account_name = ? AND uid = ? AND bank_code = ?)
                """,
                (statement_id, s.account_name, s.uid, s.bank_code),
            )

            for t in items:
                cur.execute(
                    """
                    INSERT INTO statement_transactions (
                        account_name, uid, bank_code, statement_id, txn_date, post_date, description,
                        amount, currency, currency_source, txn_location_code, original_amount,
                        original_currency, original_currency_source, direction,
                        raw_row_json, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        t.account_name or s.account_name, t.uid, t.bank_code, statement_id,
                        t.txn_date, t.post_date, t.description, t.amount, t.currency,
                        getattr(t, 'currency_source', None),
                        t.txn_location_code, t.original_amount,
                        getattr(t, 'original_currency', None),
                        getattr(t, 'original_currency_source', None),
                        t.direction,
                        t.raw_row_json, _utc_now_iso(),
                    ),
                )

        # 余额整份替换（含历史 legacy 回填行），无证据币种用 'UNKNOWN' 显式展示
        cur.execute("DELETE FROM statement_balances WHERE statement_id = ?", (statement_id,))
        for b in (balances or []):
            cur.execute(
                """INSERT INTO statement_balances (
                    statement_id, currency, total_due, minimum_due, currency_source,
                    evidence_json, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (statement_id, b.currency, b.total_due, b.minimum_due, b.currency_source,
                 json.dumps(b.evidence or {}, ensure_ascii=False), now, now),
            )

        # statement_recorded：首次落库或还款关键字段实质变化（多卡/多币种按 Message-ID 区分）
        key_changed = (existing is None) or (
            existing[1] != s.due_date or existing[2] != s.total_due
        )
        if key_changed and (s.parse_status or 'ok') == 'ok' and s.message_id:
            _upsert_quality_event_locked(cur, QualityEventRecord(
                case_key=f'billing_due|{s.account_name}|{s.bank_code}|{s.message_id}',
                event_type='statement_recorded',
                account_name=s.account_name, bank_code=s.bank_code,
                template_version=s.rule_id,
                diagnostics=[],
                evidence_refs=[{'kind': 'statement', 'statement_id': statement_id,
                                'account': s.account_name, 'uid': str(s.uid),
                                'message_id': s.message_id, 'due_date': s.due_date,
                                'total_due': s.total_due}],
            ))

        for ev in (quality_events or []):
            _upsert_quality_event_locked(cur, ev)
        if quality_confirmation:
            args={k:v for k,v in quality_confirmation.items() if k != 'results'}
            resolution_receipt=_confirm_quality_sources_locked(cur, **args)

        count=cur.execute('SELECT COUNT(*) FROM statement_transactions WHERE statement_id=?', (statement_id,)).fetchone()[0]
        conn.commit()
        if quality_confirmation:
            quality_confirmation['results'].extend(resolution_receipt)
        return statement_id, count
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def find_message_id_conflicts(db_path: str, account_name: str, message_id: str,
                              bank_code: str, uid: str) -> list[sqlite3.Row]:
    """同账户+银行+Message-ID 但 uid 不同的已存账单（疑似重复投递/重复入库）。

    仅检测并交由调用方记录冲突告警，绝不静默覆盖。message_id 为空时直接返回空。
    排除已合法映射到目标账单的别名 UID。
    """
    if not message_id:
        return []
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        cur = conn.cursor()
        cur.execute(
            """
            SELECT id, uid, subject, statement_month, total_due, parse_status, is_paid
            FROM statements
            WHERE account_name = ? AND bank_code = ? AND message_id = ? AND uid <> ?
            ORDER BY id ASC
            """,
            (account_name, bank_code, message_id, str(uid)),
        )
        candidates = list(cur.fetchall())
        if not candidates:
            return []
        try:
            cur.execute(
                """
                SELECT statement_id FROM statement_transport_identities
                WHERE account_name = ? AND transport_uid = ? AND bank_code = ?
                """,
                (account_name, str(uid), bank_code)
            )
            mapped_sids = {row[0] for row in cur.fetchall()}
            if mapped_sids:
                candidates = [c for c in candidates if c['id'] not in mapped_sids]
        except sqlite3.OperationalError:
            pass
        return candidates
    finally:
        conn.close()


def save_validation_run(
    db_path: str,
    account_name: str,
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
                account_name, uid, bank_code, rule_id, passed, error_count, warning_count, report_path, run_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                account_name or 'default',
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


def replace_transactions(db_path: str, account_name: str, uid: str, bank_code: str, txns: Iterable[StatementTransactionRecord]) -> int:
    items = list(txns)
    conn = sqlite3.connect(db_path)
    try:
        cur = conn.cursor()
        cur.execute(
            """
            DELETE FROM statement_transactions
            WHERE account_name = ? AND uid = ? AND bank_code = ?
            """,
            (account_name or 'default', uid, bank_code),
        )

        for t in items:
            cur.execute(
                """
                INSERT INTO statement_transactions (
                    account_name, uid, bank_code, txn_date, post_date, description, amount,
                    currency, txn_location_code, original_amount, direction,
                    raw_row_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    t.account_name or 'default',
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


def uid_exists(db_path: str, uid: str, account_name: str = None) -> bool:
    """检查 uid 是否已存在于 statements 表或已关联别名表。"""
    conn = sqlite3.connect(db_path)
    try:
        cur = conn.cursor()
        if account_name:
            cur.execute(
                "SELECT 1 FROM statements WHERE account_name = ? AND uid = ? LIMIT 1",
                (account_name, str(uid))
            )
            if cur.fetchone() is not None:
                return True
            try:
                cur.execute(
                    "SELECT 1 FROM statement_transport_identities WHERE account_name = ? AND transport_uid = ? LIMIT 1",
                    (account_name, str(uid))
                )
                return cur.fetchone() is not None
            except sqlite3.OperationalError:
                return False
        else:
            cur.execute("SELECT 1 FROM statements WHERE uid = ? LIMIT 1", (str(uid),))
            if cur.fetchone() is not None:
                return True
            try:
                cur.execute("SELECT 1 FROM statement_transport_identities WHERE transport_uid = ? LIMIT 1", (str(uid),))
                return cur.fetchone() is not None
            except sqlite3.OperationalError:
                return False
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
                  AND COALESCE(s.parse_status, 'ok') != 'failed'
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
              AND COALESCE(s.parse_status, 'ok') != 'failed'
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
                  AND COALESCE(s.parse_status, 'ok') != 'failed'
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
                  AND COALESCE(s.parse_status, 'ok') != 'failed'
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
                "UPDATE statements SET is_paid=1, paid_at=?, updated_at=? WHERE bank_code=? AND statement_month=? AND is_paid=0 AND COALESCE(parse_status, 'ok') != 'failed'",
                (now_str, now_str, bank_code, statement_month)
            )
        else:
            cur.execute(
                """
                UPDATE statements SET is_paid=1, paid_at=?, updated_at=? WHERE id = (
                    SELECT id FROM statements
                    WHERE bank_code=? AND is_paid=0 AND CAST(COALESCE(total_due, '0') AS REAL) > 0
                      AND COALESCE(parse_status, 'ok') != 'failed'
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


def mark_statement_paid_by_id(db_path: str, statement_id: int) -> int:
    """精确到逻辑账单的已还款标记（重构契约 §5.3）：按 statement_id 单行更新，
    防止银行+月份批量误标其他卡；已还款不重复更新（幂等返回 0）。"""
    conn = sqlite3.connect(db_path)
    try:
        cur = conn.cursor()
        cur.execute(
            "UPDATE statements SET is_paid=1, paid_at=COALESCE(paid_at, ?), updated_at=? "
            "WHERE id=? AND is_paid=0 AND COALESCE(parse_status, 'ok') != 'failed'",
            (_utc_now_iso(), _utc_now_iso(), int(statement_id)),
        )
        rowcount = cur.rowcount
        conn.commit()
        return rowcount
    finally:
        conn.close()

def get_unpaid_statements(db_path: str, months: int = None) -> list:
    from statement_evidence import stored_purpose, confirmed_currency, number
    conn = sqlite3.connect('file:' + os.path.abspath(db_path) + '?mode=ro', uri=True)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute('BEGIN')
        query="SELECT * FROM statements WHERE is_paid=0 AND COALESCE(parse_status,'ok')!='failed'"
        params=[]
        if months is not None:
            query += " AND date(substr(updated_at,1,10)) >= date('now',?)"
            params.append(f'-{months} months')
        rows=[dict(r) for r in conn.execute(query+' ORDER BY due_date,bank_code,id',params)]
        rows=[r for r in rows if number(r['total_due']) is None or number(r['total_due'])>0]
        for row in rows:
            row['repayment_availability']=stored_purpose(row.get('raw_fields_json'),'repayment_reminder')
            balances=list(conn.execute('SELECT currency,currency_source FROM statement_balances WHERE statement_id=?',(row['id'],)))
            currencies={b['currency'] for b in balances if confirmed_currency(b['currency'],b['currency_source'])}
            row['currency']=next(iter(currencies)) if len(currencies)==1 and len(balances)==1 else 'UNKNOWN'
            if row['currency']=='UNKNOWN' or number(row['total_due']) is None:
                row['repayment_availability']={'status':'blocked','reasons':[{'code':'BALANCE_CURRENCY_UNCONFIRMED'}]}
        return rows
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


def get_email_body(db_path: str, account_name: str, uid: str) -> Optional[str]:
    """从 email_bodies 读取本地缓存的纯文本正文。无则返回 None（供 enrich 离线读取）。"""
    conn = sqlite3.connect(db_path)
    try:
        cur = conn.cursor()
        cur.execute(
            "SELECT plain_text FROM email_bodies WHERE account_name = ? AND uid = ? LIMIT 1",
            (account_name, uid)
        )
        row = cur.fetchone()
        return row[0] if row else None
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


def check_summary_uid_match(db_path: str, account_name: str, uid: str, subject: str) -> bool:
    """校验 uid 是否仍对应同一条邮件（防 POP3 UID 回收误判）。

    返回 True 表示 subject 匹配（同一封邮件），False 表示 UID 已被回收重用。
    """
    conn = sqlite3.connect(db_path)
    try:
        cur = conn.cursor()
        cur.execute(
            "SELECT 1 FROM email_summaries WHERE account_name = ? AND uid = ? AND subject = ? LIMIT 1",
            (account_name, uid, subject)
        )
        return cur.fetchone() is not None
    finally:
        conn.close()


def get_unpushed_high(db_path: str) -> list[sqlite3.Row]:
    """获取 importance=high 且 pushed=0 的邮件（含正文 snippet）。"""
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        return conn.execute(
            "SELECT * FROM email_summaries WHERE importance='high' AND pushed=0 "
            "ORDER BY id ASC"
        ).fetchall()
    finally:
        conn.close()


def mark_pushed(db_path: str, ids: list[int]) -> None:
    """批量标记高优邮件已推送。"""
    if not ids:
        return
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            f"UPDATE email_summaries SET pushed=1 WHERE id IN ({','.join('?'*len(ids))})",
            ids
        )
        conn.commit()
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


def capture_quality_cases(db_path, account_name, bank_code):
    """Capture CAS tokens before parsing. Read-only; unavailable old schemas return no tokens."""
    if not os.path.exists(db_path):
        return []
    conn = sqlite3.connect('file:' + db_path + '?mode=ro', uri=True)
    conn.row_factory = sqlite3.Row
    try:
        return [dict(r) for r in conn.execute("""SELECT event_id,case_key,template_version,revision,cycle_id,
            evidence_refs_json,diagnostics_json FROM quality_events WHERE account_name=? AND bank_code=?
            AND event_type IN ('quality_detected','quality_updated')""", (account_name,bank_code))]
    except sqlite3.OperationalError as exc:
        if 'no such table' in str(exc) or 'no such column' in str(exc):
            return []
        raise
    finally:
        conn.close()


def _confirm_quality_sources_locked(cur, captured_cases, evidence_ref, current_case_keys,
                                    template_version, parser_version, parse_passed,
                                    verified_fields=None):
    """Confirm the same source/content and captured revision under the financial write lock."""
    if not parse_passed or not evidence_ref.get('content_hash') or not parser_version or not verified_fields:
        return []
    identity = json.dumps(_evidence_identity(evidence_ref), ensure_ascii=False)
    results=[]
    for captured in captured_cases:
        if captured['case_key'] in current_case_keys or captured['template_version'] != template_version:
            continue
        # Do not resolve merely because a diagnostic disappeared; require a
        # positively evaluated field and reject any remaining diagnostic on it.
        diagnostics=json.loads(captured.get('diagnostics_json') or '[]')
        if not diagnostics or any(not d.get('field') or d['field'] not in verified_fields for d in diagnostics):
            continue
        row=cur.execute('SELECT revision,cycle_id,event_type FROM quality_events WHERE event_id=?',
                        (captured['event_id'],)).fetchone()
        if not row or row[:2] != (captured['revision'],captured['cycle_id']) or row[2] not in ('quality_detected','quality_updated'):
            results.append({'event_id':captured['event_id'],'status':'stale'})
            continue
        source=cur.execute('SELECT content_hash FROM quality_event_sources WHERE event_id=? AND cycle_id=? AND identity_key=?',
                           (captured['event_id'],captured['cycle_id'],identity)).fetchone()
        if not source or source[0] != evidence_ref['content_hash']:
            continue
        # Every source is a stable identity/content pair, including sources not in this parse.
        cur.execute("""UPDATE quality_event_sources SET state='resolved',checked_parser_version=?,checked_at=?
            WHERE event_id=? AND cycle_id=? AND identity_key=? AND content_hash=?""",
            (parser_version,_utc_now_iso(),captured['event_id'],captured['cycle_id'],identity,evidence_ref['content_hash']))
        all_sources=cur.execute('SELECT state,checked_parser_version,content_hash FROM quality_event_sources WHERE event_id=? AND cycle_id=?',
                               (captured['event_id'],captured['cycle_id'])).fetchall()
        # Untracked legacy refs cannot be ignored merely because one source was rechecked.
        expected={json.dumps(_evidence_identity(r),ensure_ascii=False) for r in json.loads(captured['evidence_refs_json'] or '[]')}
        tracked={r[0] for r in cur.execute('SELECT identity_key FROM quality_event_sources WHERE event_id=? AND cycle_id=?',
                                          (captured['event_id'],captured['cycle_id']))}
        complete=(all_sources and expected == tracked and all(r[0]=='resolved' and r[1]==parser_version and r[2] for r in all_sources))
        if complete:
            _upsert_quality_event_locked(cur,QualityEventRecord(case_key=captured['case_key'],event_type='quality_resolved',
                template_version=template_version,revision=captured['revision'],cycle_id=captured['cycle_id'],
                evidence_refs=[dict(evidence_ref,verified_source_count=len(all_sources))]))
            results.append({'event_id':captured['event_id'],'status':'resolved','verified_source_count':len(all_sources)})
        else:
            results.append({'event_id':captured['event_id'],'status':'source_verified','verified_source_count':sum(r[0]=='resolved' and r[1]==parser_version for r in all_sources)})
    return results


def confirm_quality_sources(db_path, captured_cases, evidence_ref, current_case_keys,
                            template_version, parser_version, parse_passed,
                            verified_fields=None):
    """Standalone verifier for tools; parser calls the locked helper inside atomic save."""
    conn = sqlite3.connect(db_path)
    try:
        conn.execute('BEGIN IMMEDIATE')
        results=_confirm_quality_sources_locked(conn.cursor(), captured_cases, evidence_ref,
            current_case_keys, template_version, parser_version, parse_passed, verified_fields)
        conn.commit()
        return results
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def get_transport_identity(db_path: str, account_name: str, transport_kind: str,
                           transport_uid: str, bank_code: str) -> Optional[dict]:
    conn = sqlite3.connect(f'file:{db_path}?mode=ro', uri=True)
    conn.row_factory = sqlite3.Row
    try:
        cur = conn.cursor()
        cur.execute("""
            SELECT id, account_name, transport_kind, transport_uid, bank_code, statement_id,
                   message_id, evidence_digest, evidence_version, raw_content_hash, method,
                   created_at, updated_at
            FROM statement_transport_identities
            WHERE account_name = ? AND transport_kind = ? AND transport_uid = ? AND bank_code = ?
        """, (account_name, transport_kind, str(transport_uid), bank_code))
        row = cur.fetchone()
        return dict(row) if row else None
    except sqlite3.OperationalError:
        return None
    finally:
        conn.close()


def find_canonical_by_transport(db_path: str, account_name: str, transport_uid: str,
                                bank_code: Optional[str] = None,
                                transport_kind: Optional[str] = None) -> Optional[sqlite3.Row]:
    conn = sqlite3.connect(f'file:{db_path}?mode=ro', uri=True)
    conn.row_factory = sqlite3.Row
    try:
        cur = conn.cursor()
        q = """
            SELECT s.* FROM statements s
            JOIN statement_transport_identities ti ON s.id = ti.statement_id
            WHERE ti.account_name = ? AND ti.transport_uid = ?
        """
        params = [account_name, str(transport_uid)]
        if bank_code:
            q += " AND ti.bank_code = ?"
            params.append(bank_code)
        if transport_kind:
            q += " AND ti.transport_kind = ?"
            params.append(transport_kind)
        cur.execute(q, params)
        rows = cur.fetchall()
        if len(rows) > 1:
            raise MessageIdentityConflict(f'Ambiguous transport identity matches {len(rows)} canonical statements')
        return rows[0] if rows else None
    except sqlite3.OperationalError:
        return None
    finally:
        conn.close()



def save_statement_evidence_index(
    db_path: str,
    statement_id: int,
    account_name: str,
    bank_code: str,
    message_id: str,
    raw_content_hash: str,
    evidence_digest: str,
    coverage_status: str,
    archive_path: Optional[str] = None,
    evidence_version: str = "v1"
) -> None:
    now = _utc_now_iso()
    conn = sqlite3.connect(db_path)
    try:
        conn.execute('PRAGMA foreign_keys = ON')
        conn.execute('BEGIN IMMEDIATE')
        cur = conn.cursor()

        # 验证 statement_id 必须存在于 statements 表
        cur.execute("SELECT id FROM statements WHERE id = ?", (statement_id,))
        if not cur.fetchone():
            raise ValueError(f"Target statement {statement_id} does not exist in statements table")

        cur.execute("""
            SELECT statement_id, account_name, bank_code, message_id,
                   raw_content_hash, evidence_digest, evidence_version,
                   coverage_status, archive_path
            FROM statement_evidence_index WHERE statement_id = ?
        """, (statement_id,))
        existing = cur.fetchone()

        if existing is None:
            cur.execute("""
                INSERT INTO statement_evidence_index (
                    statement_id, account_name, bank_code, message_id,
                    raw_content_hash, evidence_digest, evidence_version,
                    coverage_status, archive_path, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (statement_id, account_name, bank_code, message_id,
                  raw_content_hash, evidence_digest, evidence_version,
                  coverage_status, archive_path, now))
        else:
            ext_cov = existing[7]
            ext_digest = existing[5]
            ext_raw_hash = existing[4]
            ext_archive = existing[8]

            if ext_cov == 'complete':
                # 基线保护 (T4)：已有完整证据基线，禁止被失败重解析或不完整覆盖降级替换
                if coverage_status != 'complete':
                    return
                # 同为 complete 时验证摘要不可变
                if ext_digest != evidence_digest or ext_raw_hash != raw_content_hash:
                    raise ValueError(
                        f"Cannot overwrite immutable evidence index for statement_id={statement_id}: "
                        f"existing digest={ext_digest}, raw_hash={ext_raw_hash} != "
                        f"new digest={evidence_digest}, raw_hash={raw_content_hash}"
                    )
                # 幂等合并归档路径
                if not ext_archive and archive_path:
                    cur.execute(
                        "UPDATE statement_evidence_index SET archive_path = ? WHERE statement_id = ?",
                        (archive_path, statement_id)
                    )
            else:
                # 原记录不完整，新记录达到完整覆盖时允许升级为基线
                if coverage_status == 'complete':
                    cur.execute("""
                        UPDATE statement_evidence_index SET
                            account_name = ?, bank_code = ?, message_id = ?,
                            raw_content_hash = ?, evidence_digest = ?, evidence_version = ?,
                            coverage_status = ?, archive_path = ?
                        WHERE statement_id = ?
                    """, (account_name, bank_code, message_id, raw_content_hash,
                          evidence_digest, evidence_version, coverage_status,
                          archive_path or ext_archive, statement_id))

        conn.commit()
    finally:
        conn.close()


def get_statement_evidence(db_path: str, statement_id: int) -> Optional[Dict[str, Any]]:
    conn = sqlite3.connect(f'file:{db_path}?mode=ro', uri=True)
    conn.row_factory = sqlite3.Row
    try:
        cur = conn.cursor()
        cur.execute("""
            SELECT statement_id, account_name, bank_code, message_id,
                   raw_content_hash, evidence_digest, evidence_version,
                   coverage_status, archive_path, created_at
            FROM statement_evidence_index
            WHERE statement_id = ?
        """, (statement_id,))
        row = cur.fetchone()
        if not row:
            return None
        return dict(row)
    finally:
        conn.close()


def _verify_manual_plan(plan_path: Optional[str], plan_id: Optional[str], plan_hash: Optional[str],
                        statement_id: int, account_name: str, bank_code: str, transport_uid: str,
                        target_row: Any, db_path: str, *, transport_kind: Optional[str] = None,
                        source_ev: Optional[dict] = None) -> dict:
    import statement_identity as si
    from datetime import datetime, timezone

    if not plan_id or not plan_hash:
        raise ValueError("manual_verified 必须提供已批准的 plan_id 与 plan_hash")

    # 定位计划报告
    cand_path = plan_path
    if not cand_path or not os.path.exists(cand_path):
        try:
            from parser_paths import resolve_parser_paths
            ctx = resolve_parser_paths()
            cand_path = os.path.join(ctx.data_root, 'identity_reports', f'plan_{plan_id}.json')
        except Exception:
            cand_path = None

    if not cand_path or not os.path.exists(cand_path):
        raise ValueError(f"manual_verified 必须提供受控预览生成的有效计划报告文件 (未找到计划文件或计划未验证: plan_id={plan_id})")

    with open(cand_path, 'r', encoding='utf-8') as fp:
        plan = json.load(fp)

    computed_hash = si.compute_plan_hash(plan)
    if computed_hash != plan_hash:
        raise ValueError(f"计划哈希不匹配: computed={computed_hash} != provided={plan_hash}")
    if plan.get('plan_hash') != plan_hash:
        raise ValueError(f"计划内存储哈希不匹配: {plan.get('plan_hash')} != {plan_hash}")

    if plan.get('schema_version') != 1:
        raise ValueError(f"不支持的计划 schema_version: {plan.get('schema_version')}")
    if plan.get('normalization_version') != 'v1':
        raise ValueError(f"不支持的计划 normalization_version: {plan.get('normalization_version')}")

    expires_at_str = plan.get('expires_at')
    if not expires_at_str:
        raise ValueError("计划缺少 expires_at 时效时间戳")
    exp_dt = datetime.fromisoformat(expires_at_str)
    if datetime.now(timezone.utc) > exp_dt:
        raise ValueError(f"计划已于 {expires_at_str} 过期，禁止执行确认")

    if plan.get('target_statement_id') != statement_id:
        raise ValueError(f"计划目标账单 ID 不匹配: {plan.get('target_statement_id')} != {statement_id}")
    if plan.get('account_name') != account_name or plan.get('bank_code') != bank_code:
        raise ValueError(f"计划账户/银行不匹配: ({plan.get('account_name')}/{plan.get('bank_code')}) != ({account_name}/{bank_code})")
    if str(plan.get('transport_uid')) != str(transport_uid):
        raise ValueError(f"计划运输 UID 不匹配: {plan.get('transport_uid')} != {transport_uid}")

    if plan.get('status') not in ('associated_existing', 'insufficient_evidence'):
        raise ValueError(f"计划状态 '{plan.get('status')}' 不允许执行人工确认")

    if plan.get('plan_id') != plan_id:
        raise ValueError("计划 ID 与所引用计划不符")
    if transport_kind is not None and plan.get('transport_kind') != transport_kind:
        raise ValueError("计划运输类型不匹配")
    if source_ev is None:
        raise ValueError("人工计划缺少可重建的源端证据")
    promised_source = plan.get('source_evidence_summary') or {}
    for field in ('raw_content_hash', 'complete_content_digest', 'message_id'):
        if promised_source.get(field) != source_ev.get(field):
            raise ValueError(f"源端证据与批准计划不符: {field}")

    # 验证 preview 之后目标账单是否发生被改写或修改 (U1)
    plan_anchor = plan.get('anchor_snapshot') or {}
    if 'total_due' in plan_anchor and str(plan_anchor['total_due']) != str(target_row[5]):
        raise ValueError("目标账单金额在 preview 后被修改，计划作废")
    if 'is_paid' in plan_anchor and int(plan_anchor['is_paid']) != int(target_row[6] or 0):
        raise ValueError("目标账单还款状态在 preview 后被修改，计划作废")
    if 'message_id' in plan_anchor and plan_anchor['message_id'] and plan_anchor['message_id'] != target_row[3]:
        raise ValueError("目标账单 Message-ID 在 preview 后被修改，计划作废")

    return plan


def associate_transport_identity(
    db_path: str,
    account_name: str,
    transport_kind: str,
    transport_uid: str,
    bank_code: str,
    statement_id: int,
    message_id: str,
    evidence_digest: Optional[str] = None,
    evidence_version: str = 'v1',
    raw_content_hash: Optional[str] = None,
    method: str = 'digest_equal',
    actor: str = 'auto_sync',
    reason: str = 'content digest equal',
    plan_id: Optional[str] = None,
    plan_hash: Optional[str] = None,
    source_evidence: Optional[dict] = None,
    target_evidence: Optional[dict] = None,
    anchor_snapshot: Optional[dict] = None,
    batch_id: Optional[str] = None,
    target_ref: Optional[dict] = None,
    source_ref: Optional[dict] = None,
    source_raw_bytes: Optional[bytes] = None,
    plan_path: Optional[str] = None,
) -> dict:
    import statement_identity as si

    if not account_name or account_name in ('__orphan__', 'default'):
        raise ValueError(f'Cannot associate transport identity for placeholder account: {account_name}')
    if not actor or not actor.strip():
        raise ValueError('actor must be provided')
    if not reason or not reason.strip():
        raise ValueError('reason must be provided')
    if not message_id or not message_id.strip():
        raise ValueError('message_id must be provided')
    if method not in ('digest_equal', 'manual_verified'):
        raise ValueError(f'Invalid method: {method}')
    if evidence_version != 'v1':
        raise ValueError(f'Unsupported evidence version: {evidence_version}')

    now = _utc_now_iso()
    batch_id = batch_id or f'batch_{now}_{transport_uid}'
    conn = sqlite3.connect(db_path)
    try:
        conn.execute('PRAGMA foreign_keys = ON')
        conn.execute('BEGIN IMMEDIATE')
        cur = conn.cursor()

        # 1. 锁内校验目标 statement_id
        cur.execute(
            'SELECT id, account_name, bank_code, message_id, statement_month, total_due, is_paid, subject, sender, email_date '
            'FROM statements WHERE id = ?',
            (statement_id,)
        )
        target_row = cur.fetchone()
        if not target_row:
            raise ValueError(f'Target statement id {statement_id} does not exist')
        if target_row[1] != account_name or target_row[2] != bank_code:
            raise ValueError(f'Target statement mismatch: acct/bank ({target_row[1]}/{target_row[2]}) != ({account_name}/{bank_code})')

        # 2. 统一源端提取与构建 (Step A)
        src_bytes = source_raw_bytes
        if src_bytes is None and source_evidence and isinstance(source_evidence, dict):
            src_bytes = source_evidence.get('raw_bytes')
        if src_bytes is None:
            cand_src_path = (source_ref or {}).get('archive_path') or (source_ref or {}).get('eml_path') or (source_evidence or {}).get('archive_path') or (source_evidence or {}).get('eml_path')
            if cand_src_path and os.path.exists(cand_src_path):
                try:
                    with open(cand_src_path, 'rb') as fp:
                        src_bytes = fp.read()
                except Exception:
                    pass

        rebuilt_src_ev = None
        if src_bytes is not None:
            rebuilt_src_ev = si.build_evidence_from_raw_eml(
                src_bytes, account_name=account_name, bank_code=bank_code,
                transport_kind=transport_kind, transport_uid=str(transport_uid)
            )
            # 防篡改校验：如果调用者传入了 source_evidence 字典，必须与从原始字节重建的内容完全吻合 (S1)
            if source_evidence and isinstance(source_evidence, dict):
                if source_evidence.get('complete_content_digest') and source_evidence['complete_content_digest'] != rebuilt_src_ev['complete_content_digest']:
                    raise ValueError(f"源端证据摘要与原始字节重建摘要矛盾: 摘要不匹配 {source_evidence['complete_content_digest']} != {rebuilt_src_ev['complete_content_digest']}")
                if source_evidence.get('raw_content_hash') and source_evidence['raw_content_hash'] != rebuilt_src_ev['raw_content_hash']:
                    raise ValueError("源端 raw_content_hash 与原始字节矛盾")
                src_hdr = source_evidence.get('headers') or {}
                reb_hdr = rebuilt_src_ev.get('headers') or {}
                if src_hdr.get('from') and src_hdr.get('from') != reb_hdr.get('from'):
                    raise ValueError(f"源端证据 From 头与原始字节重建头部不符: {src_hdr.get('from')} != {reb_hdr.get('from')}")
                if src_hdr.get('subject') and src_hdr.get('subject') != reb_hdr.get('subject'):
                    raise ValueError(f"源端证据 Subject 头与原始字节重建头部不符: {src_hdr.get('subject')} != {reb_hdr.get('subject')}")
                if src_hdr.get('message_id') and src_hdr.get('message_id') != reb_hdr.get('message_id'):
                    raise ValueError(f"源端证据 Message-ID 头与原始字节重建头部不符: {src_hdr.get('message_id')} != {reb_hdr.get('message_id')}")
                if source_evidence.get('tree_nodes') and source_evidence['tree_nodes'] != rebuilt_src_ev['tree_nodes']:
                    raise ValueError("源端证据 MIME 树结构与原始字节重建树不符")

        if rebuilt_src_ev is None and source_evidence:
            raise ValueError("源端证据字典缺少原始字节或可信归档引用，必须有可重读原文")

        # 3. 统一已知硬元数据矛盾排查 (Step B, U1: 人工只放宽完整性，不放宽已知硬矛盾)
        src_subject = (rebuilt_src_ev.get('headers', {}).get('subject') if rebuilt_src_ev else None) or (source_evidence or {}).get('headers', {}).get('subject')
        src_from = (rebuilt_src_ev.get('headers', {}).get('from') if rebuilt_src_ev else None) or (source_evidence or {}).get('headers', {}).get('from')
        src_date = (rebuilt_src_ev.get('headers', {}).get('date') if rebuilt_src_ev else None) or (source_evidence or {}).get('headers', {}).get('date')

        # 1) Message-ID
        if target_row[3] and message_id and target_row[3] != message_id:
            raise ValueError(f"硬元数据矛盾：目标账单 Message-ID={target_row[3]} 与源={message_id} 不一致，禁止关联")

        # 2) Subject
        tgt_subject = target_row[7]
        if src_subject and tgt_subject and src_subject != tgt_subject:
            raise ValueError(f"硬元数据矛盾：主题不匹配: src={src_subject!r} != tgt={tgt_subject!r}")

        # 3) From / Sender
        tgt_sender = target_row[8]
        if src_from and tgt_sender and src_from != tgt_sender:
            raise ValueError(f"硬元数据矛盾：发件人不匹配: src={src_from!r} != tgt={tgt_sender!r}")

        # 4) Date / email_date
        tgt_date = target_row[9]
        if src_date and tgt_date:
            sv_ymd = si._parse_date_to_ymd(src_date)
            tv_ymd = si._parse_date_to_ymd(tgt_date)
            if sv_ymd and tv_ymd:
                if sv_ymd != tv_ymd:
                    raise ValueError(f"硬元数据矛盾：日期不匹配: src={src_date!r} ({sv_ymd}) != tgt={tgt_date!r} ({tv_ymd})")
            elif src_date != tgt_date:
                raise ValueError(f"硬元数据矛盾：日期不匹配: src={src_date!r} != tgt={tgt_date!r}")

        # 5) 唯一有效锚点核对 (锁内核对唯一主账单)
        cur.execute('SELECT id FROM statements WHERE account_name = ? AND bank_code = ? AND message_id = ?',
                    (account_name, bank_code, message_id))
        matching_anchors = [r[0] for r in cur.fetchall()]
        if len(matching_anchors) > 1:
            raise MessageIdentityConflict(f'Multiple anchor statements exist for Message-ID: {matching_anchors}')
        if matching_anchors and matching_anchors[0] != statement_id:
            raise MessageIdentityConflict(f'Target statement {statement_id} is not the primary anchor ({matching_anchors[0]})')

        # 6) 锚点快照财务一致性 (防止财务漂移)
        if anchor_snapshot:
            if 'total_due' in anchor_snapshot and str(anchor_snapshot['total_due']) != str(target_row[5]):
                raise ValueError(f'Anchor total_due drifted: current={target_row[5]} != snapshot={anchor_snapshot["total_due"]}')
            if 'is_paid' in anchor_snapshot and int(anchor_snapshot['is_paid']) != int(target_row[6] or 0):
                raise ValueError(f'Anchor is_paid drifted: current={target_row[6]} != snapshot={anchor_snapshot["is_paid"]}')

        # 4. 检查现有运输键映射 (Step C, U2: 已有映射的幂等返回必须在证据验证之后)
        cur.execute("""
            SELECT id, statement_id, message_id, raw_content_hash, evidence_digest
            FROM statement_transport_identities
            WHERE account_name = ? AND transport_kind = ? AND transport_uid = ? AND bank_code = ?
        """, (account_name, transport_kind, str(transport_uid), bank_code))
        existing = cur.fetchone()

        if existing:
            ext_id, ext_sid, ext_msgid, ext_raw_hash, ext_digest = existing
            if ext_sid != statement_id:
                raise MessageIdentityConflict(
                    f'Transport key ({account_name},{transport_kind},{transport_uid},{bank_code}) '
                    f'already bound to statement ID {ext_sid}, cannot bind to {statement_id}'
                )
            if message_id and ext_msgid and message_id != ext_msgid:
                raise MessageIdentityConflict(
                    f'Transport key reused for different Message-ID: {message_id} != {ext_msgid}'
                )

            # U2: 如果完全缺证据重投 (只有运输键查重而无新正文/证据)
            if src_bytes is None and not source_evidence:
                conn.commit()
                return {'status': 'repeated_key_unverified', 'statement_id': ext_sid, 'idempotent': True}

            # 候选经验证仍为同邮件：核验源端与内容等价性
            if rebuilt_src_ev is not None:
                cand_digest = rebuilt_src_ev.get('complete_content_digest')
                if ext_digest and cand_digest != ext_digest:
                    raise MessageIdentityConflict(
                        f'Transport key reused for different raw content hash / digest: {cand_digest} != {ext_digest}'
                    )
                if evidence_digest and ext_digest and evidence_digest != ext_digest:
                    raise MessageIdentityConflict(
                        f'Transport key reused for different raw content hash / digest: {evidence_digest} != {ext_digest}'
                    )
                if ext_raw_hash and rebuilt_src_ev.get('raw_content_hash') != ext_raw_hash and not ext_digest:
                    raise MessageIdentityConflict(
                        f'Transport key reused for different raw content hash: {rebuilt_src_ev.get("raw_content_hash")} != {ext_raw_hash}'
                    )

                if method == 'digest_equal':
                    if rebuilt_src_ev.get('coverage_status') != 'complete':
                        raise ValueError(f"源端字节重建证据未达 complete 覆盖: coverage_status={rebuilt_src_ev.get('coverage_status')}")
                elif method == 'manual_verified':
                    _verify_manual_plan(plan_path, plan_id, plan_hash, statement_id, account_name, bank_code, transport_uid, target_row, db_path, transport_kind=transport_kind, source_ev=rebuilt_src_ev)

            conn.commit()
            return {'status': 'already_associated', 'statement_id': statement_id, 'idempotent': True}

        # 5. 首次关联分支
        if method == 'digest_equal':
            # ==================== 自动关联 (digest_equal) 契约 ====================
            # 目标端必须具备数据库证据索引
            cur.execute("""
                SELECT statement_id, account_name, bank_code, message_id,
                   raw_content_hash, evidence_digest, evidence_version,
                   coverage_status, archive_path
                FROM statement_evidence_index WHERE statement_id = ?
            """, (statement_id,))
            ev_row = cur.fetchone()
            if not ev_row:
                raise ValueError(f"目标账单 id={statement_id} 缺失数据库可信证据索引 (statement_evidence_index / target_evidence)，无法建立关联")

            if ev_row[1] != account_name or ev_row[2] != bank_code:
                raise ValueError(f"目标证据索引元数据不匹配: ({ev_row[1]}/{ev_row[2]}) != ({account_name}/{bank_code})")
            if ev_row[3] != message_id:
                raise ValueError(f"目标证据索引 Message-ID 不匹配: {ev_row[3]} != {message_id}")
            if ev_row[6] != 'v1':
                raise ValueError(f"目标证据版本不受支持: {ev_row[6]}")
            if ev_row[7] != 'complete':
                raise ValueError(f"目标证据未达完整覆盖状态: {ev_row[7]}")

            tgt_archive = ev_row[8] or (target_ref or {}).get('archive_path')
            if not tgt_archive or not os.path.exists(tgt_archive):
                raise ValueError(f"目标证据必须具备可重读原始归档文件: {tgt_archive}")

            try:
                with open(tgt_archive, 'rb') as fp:
                    tgt_bytes = fp.read()
            except Exception as e_arch:
                raise ValueError(f"无法读取目标归档文件: {e_arch}")

            tgt_arch_hash = hashlib.sha256(tgt_bytes).hexdigest()
            if tgt_arch_hash != ev_row[4]:
                raise ValueError(f"目标证据归档哈希被篡改: {tgt_arch_hash} != {ev_row[4]}")

            # 锁内对目标原始字节重建证据
            rebuilt_tgt_ev = si.build_evidence_from_raw_eml(
                tgt_bytes, account_name=account_name, bank_code=bank_code,
                transport_kind=transport_kind, transport_uid=str(statement_id)
            )
            if rebuilt_tgt_ev.get('coverage_status') != 'complete':
                raise ValueError("目标归档字节重建证据未达 complete 覆盖")
            if rebuilt_tgt_ev.get('complete_content_digest') != ev_row[5]:
                raise ValueError(f"目标归档字节重建摘要与索引矛盾: {rebuilt_tgt_ev.get('complete_content_digest')} != {ev_row[5]}")

            # 源端必须获取真实原始字节并重建证据
            if src_bytes is None or rebuilt_src_ev is None:
                raise ValueError("源端证据缺少真实原始字节或可信归档引用，拒绝未经验证的字典 (source_evidence / raw_bytes)")

            if source_evidence and isinstance(source_evidence, dict):
                if source_evidence.get('coverage_status') and source_evidence.get('coverage_status') != 'complete':
                    raise ValueError(f"源端证据未达 complete 覆盖状态: coverage_status={source_evidence.get('coverage_status')}")

            if rebuilt_src_ev.get('coverage_status') != 'complete':
                raise ValueError(f"源端字节重建证据未达 complete 覆盖: coverage_status={rebuilt_src_ev.get('coverage_status')}")

            # 运行 compare_evidence 全套正向契约
            decision, reasons, details = si.compare_evidence(rebuilt_src_ev, rebuilt_tgt_ev)
            if decision != 'associated_existing':
                raise ValueError(f"全内容证据比对未达等价契约: decision={decision}, reasons={reasons}, 摘要不匹配")

            evidence_digest = rebuilt_src_ev['complete_content_digest']
            raw_content_hash = rebuilt_src_ev['raw_content_hash']
            src_ev = rebuilt_src_ev
            tgt_ev = rebuilt_tgt_ev

        elif method == 'manual_verified':
            # ==================== 人工确认 (manual_verified) 契约 (U1, S3) ====================
            if not actor or not actor.strip() or actor == 'auto_sync':
                raise ValueError("manual_verified 必须提供明确的人类审查人 (actor)")
            if not reason or not reason.strip():
                raise ValueError("manual_verified 必须提供明确的审查理由 (reason)")
            if not anchor_snapshot:
                raise ValueError("manual_verified 必须提供 anchor_snapshot 进行锚点核验")

            # 计划完整性与受控 preview 验证 (U1)
            verified_plan = _verify_manual_plan(
                plan_path, plan_id, plan_hash, statement_id, account_name, bank_code, transport_uid, target_row, db_path,
                transport_kind=transport_kind, source_ev=rebuilt_src_ev
            )

            cur.execute("""
                SELECT statement_id, account_name, bank_code, message_id,
                       raw_content_hash, evidence_digest, evidence_version,
                       coverage_status, archive_path
                FROM statement_evidence_index WHERE statement_id = ?
            """, (statement_id,))
            ev_row = cur.fetchone()
            if ev_row:
                tgt_ev = {
                    'complete_content_digest': ev_row[5],
                    'normalization_version': ev_row[6],
                    'raw_content_hash': ev_row[4],
                    'coverage_status': ev_row[7],
                    'archive_path': ev_row[8],
                }
            else:
                tgt_ev = target_evidence or {
                    'complete_content_digest': None,
                    'normalization_version': 'v1',
                    'raw_content_hash': None,
                    'coverage_status': 'coverage_unknown',
                    'archive_path': None,
                }

            src_ev = rebuilt_src_ev or source_evidence or {}
            evidence_digest = src_ev.get('complete_content_digest')
            raw_content_hash = src_ev.get('raw_content_hash')

        # 6. 插入新映射
        cur.execute("""
            INSERT INTO statement_transport_identities (
                account_name, transport_kind, transport_uid, bank_code, statement_id,
                message_id, evidence_digest, evidence_version, raw_content_hash, method,
                created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            account_name, transport_kind, str(transport_uid), bank_code, statement_id,
            message_id, evidence_digest, evidence_version, raw_content_hash, method,
            now, now
        ))

        # 7. 插入审计记录
        cur.execute("""
            INSERT INTO statement_identity_audit (
                batch_id, plan_id, plan_hash, action, account_name, transport_kind,
                transport_uid, bank_code, statement_id, old_statement_id, new_statement_id,
                message_id, normalization_version, source_raw_hash, source_digest,
                target_raw_hash, target_digest, coverage_status, missing_reason,
                method, actor, reason, created_at
            ) VALUES (?, ?, ?, 'associate', ?, ?, ?, ?, ?, NULL, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            batch_id, plan_id, plan_hash, account_name, transport_kind,
            str(transport_uid), bank_code, statement_id, statement_id,
            message_id, evidence_version,
            raw_content_hash or (src_ev or {}).get('raw_content_hash'),
            evidence_digest or (src_ev or {}).get('complete_content_digest'),
            (tgt_ev or {}).get('raw_content_hash'),
            (tgt_ev or {}).get('complete_content_digest'),
            (src_ev or {}).get('coverage_status') or 'coverage_unknown',
            (src_ev or {}).get('missing_reason'),
            method, actor, reason, now
        ))

        conn.commit()
        return {'status': 'associated', 'statement_id': statement_id, 'idempotent': False}
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
