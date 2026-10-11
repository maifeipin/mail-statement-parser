#!/usr/bin/env python3
"""
tests/test_clean_duplicates_identity.py
单元测试：历史重复账单清洗中的运输身份迁移与安全防线 (E1 §7, §10.5, 二轮 Gap 4)
"""

import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import statement_db as sd
from statement_models import StatementRecord


class TestCleanDuplicatesIdentity(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.db = os.path.join(self.tmp, 'test_clean.db')
        sd.init_db(self.db)
        self.clean_script = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            'scripts', 'clean_duplicate_statements.py'
        )

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _setup_main_and_dup(self, main_ev=True, dup_ev=True, dup_digest='digest_123', dup_ver='v1', dup_raw_hash='raw_123'):
        s_main = StatementRecord(
            account_name='user@163.com', uid='UID_MAIN', message_id='<dup.msg@cmb.cn>',
            bank_code='CMB', rule_id='CMB_TEMPLATE_V1', subject='招商银行信用卡电子账单',
            sender='svc@cmb.cn', email_date='2026-10-10', statement_month='2026-10',
            statement_date='2026-10-10', due_date='2026-10-28', total_due='100.00',
            minimum_due='10.00', raw_fields_json='{}'
        )
        main_id, _ = sd.save_statement_with_transactions(self.db, s_main, [])

        s_dup = StatementRecord(
            account_name='user@163.com', uid='UID_DUP', message_id='<dup.msg@cmb.cn>',
            bank_code='CMB', rule_id='CMB_TEMPLATE_V1', subject='招商银行信用卡电子账单',
            sender='svc@cmb.cn', email_date='2026-10-10', statement_month='2026-10',
            statement_date='2026-10-10', due_date='2026-10-28', total_due='100.00',
            minimum_due='10.00', raw_fields_json='{}'
        )
        conn = sqlite3.connect(self.db)
        cur = conn.cursor()
        cur.execute("""
            INSERT INTO statements (
                account_name, uid, message_id, bank_code, rule_id, subject, sender,
                email_date, statement_month, statement_date, due_date, total_due,
                minimum_due, raw_fields_json, parse_status, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'ok', '2026-10-10', '2026-10-10')
        """, (s_dup.account_name, s_dup.uid, s_dup.message_id, s_dup.bank_code,
              s_dup.rule_id, s_dup.subject, s_dup.sender, s_dup.email_date,
              s_dup.statement_month, s_dup.statement_date, s_dup.due_date,
              s_dup.total_due, s_dup.minimum_due, s_dup.raw_fields_json))
        dup_id = cur.lastrowid

        # 历史别名映射
        cur.execute("""
            INSERT INTO statement_transport_identities (
                account_name, transport_kind, transport_uid, bank_code,
                statement_id, message_id, evidence_digest, raw_content_hash,
                evidence_version, method, created_at, updated_at
            ) VALUES (?, 'pop3_uidl', ?, 'CMB', ?, ?, ?, 'raw_123', ?, 'digest_equal', '2026-10-10', '2026-10-10')
        """, (s_dup.account_name, 'UID_ALIAS_FOR_DUP', dup_id, s_dup.message_id, dup_digest, dup_ver))
        conn.commit()
        conn.close()

        if main_ev:
            sd.save_statement_evidence_index(
                self.db, statement_id=main_id, account_name='user@163.com',
                bank_code='CMB', message_id='<dup.msg@cmb.cn>',
                raw_content_hash='raw_123', evidence_digest='digest_123',
                coverage_status='complete', archive_path=None
            )
        if dup_ev:
            sd.save_statement_evidence_index(
                self.db, statement_id=dup_id, account_name='user@163.com',
                bank_code='CMB', message_id='<dup.msg@cmb.cn>',
                raw_content_hash=dup_raw_hash, evidence_digest=dup_digest,
                coverage_status='complete', archive_path=None,
                evidence_version=dup_ver
            )
        return main_id, dup_id

    def test_duplicate_merge_migrates_identities_conserved(self):
        """正例：双方均有可信等价证据时，主记录合并副本，运输身份原子迁移至主记录，总数守恒"""
        main_id, dup_id = self._setup_main_and_dup(main_ev=True, dup_ev=True)

        res = subprocess.run(
            [sys.executable, self.clean_script, '--db', self.db, '--apply'],
            capture_output=True, text=True
        )
        self.assertEqual(res.returncode, 0, "Cleanup script failed")
        self.assertIn('清洗完成，事务已提交', res.stdout)

        # 验证映射迁移至 main_id
        conn = sqlite3.connect(self.db)
        cur = conn.cursor()
        cur.execute("SELECT statement_id FROM statement_transport_identities WHERE transport_uid = 'UID_ALIAS_FOR_DUP'")
        row = cur.fetchone()
        self.assertIsNotNone(row)
        self.assertEqual(row[0], main_id)

        # 验证审计记录包含 migrate 动作
        cur.execute("SELECT action, old_statement_id, new_statement_id FROM statement_identity_audit WHERE transport_uid = 'UID_ALIAS_FOR_DUP' AND action = 'migrate'")
        audit_row = cur.fetchone()
        self.assertIsNotNone(audit_row)
        self.assertEqual(audit_row[1], dup_id)
        self.assertEqual(audit_row[2], main_id)

        # 核心核验 (T2 P0)：验证清洗完成后外键完整性检查完全为空，无悬挂外键
        cur.execute("PRAGMA foreign_key_check")
        fk_violations = cur.fetchall()
        self.assertEqual(fk_violations, [], f"存在悬挂外键: {fk_violations}")
        conn.close()

    def test_primary_missing_evidence_aborts_merge(self):
        """反例 (Gap 4 P0)：主记录缺失可信证据索引时，拒绝将无矛盾当证据，安全终止合并并回滚"""
        self._setup_main_and_dup(main_ev=False, dup_ev=True)
        res = subprocess.run(
            [sys.executable, self.clean_script, '--db', self.db, '--apply'],
            capture_output=True, text=True
        )
        self.assertNotEqual(res.returncode, 0)
        self.assertIn('主记录 id=1 缺失可信内容证据', res.stderr + res.stdout)

    def test_duplicate_missing_evidence_aborts_merge(self):
        """反例 (Gap 4 P0)：副本缺失可信证据索引时，缺少正向等价证明，安全终止合并并回滚"""
        self._setup_main_and_dup(main_ev=True, dup_ev=False)
        # 清除 duplicate 的 transport_identities 中的 evidence_digest
        conn = sqlite3.connect(self.db)
        conn.execute("UPDATE statement_transport_identities SET evidence_digest = NULL")
        conn.commit()
        conn.close()

        res = subprocess.run(
            [sys.executable, self.clean_script, '--db', self.db, '--apply'],
            capture_output=True, text=True
        )
        self.assertNotEqual(res.returncode, 0)
        self.assertIn('缺失可信内容证据', res.stderr + res.stdout)

    def test_evidence_digest_mismatch_aborts_merge(self):
        """反例 (Gap 4 P0)：双方全内容摘要不一致时，坚决拒绝合并重挂"""
        self._setup_main_and_dup(main_ev=True, dup_ev=True, dup_digest='different_digest_456')
        res = subprocess.run(
            [sys.executable, self.clean_script, '--db', self.db, '--apply'],
            capture_output=True, text=True
        )
        self.assertNotEqual(res.returncode, 0)
        self.assertIn('全内容摘要不一致', res.stderr + res.stdout)

    def test_evidence_version_mismatch_aborts_merge(self):
        """反例 (Gap 4 P0)：证据版本不一致 (如 v999 != v1) 时拒绝合并"""
        self._setup_main_and_dup(main_ev=True, dup_ev=True, dup_ver='v999')
        res = subprocess.run(
            [sys.executable, self.clean_script, '--db', self.db, '--apply'],
            capture_output=True, text=True
        )
        self.assertNotEqual(res.returncode, 0)
        self.assertIn('证据版本不匹配', res.stderr + res.stdout)

    def test_ghost_bill_with_active_identity_aborts_deletion(self):
        """反例 (§10.5)：幽灵账单若有名下活动身份映射，必须安全拒绝删除并回滚"""
        conn = sqlite3.connect(self.db)
        cur = conn.cursor()
        cur.execute("""
            INSERT INTO statements (
                account_name, uid, message_id, bank_code, rule_id, subject, sender,
                email_date, statement_month, statement_date, due_date, total_due,
                minimum_due, raw_fields_json, parse_status, created_at, updated_at
            ) VALUES ('user@163.com', 'UID_GHOST', '<ghost@cmb.cn>', 'CMB', 'CMB_TEMPLATE_V1',
                      '招商银行每日资讯', 'svc@cmb.cn', '2026-10-10', '2026-10', '2026-10-10',
                      '2026-10-28', '0.00', '0.00', '{}', 'ok', '2026-10-10', '2026-10-10')
        """)
        ghost_id = cur.lastrowid
        cur.execute("""
            INSERT INTO statement_transport_identities (
                account_name, transport_kind, transport_uid, bank_code,
                statement_id, message_id, evidence_digest, raw_content_hash,
                evidence_version, method, created_at, updated_at
            ) VALUES ('user@163.com', 'pop3_uidl', 'UID_GHOST_ALIAS', 'CMB',
                      ?, '<ghost@cmb.cn>', 'digest_g', 'raw_g', 'v1', 'digest_equal',
                      '2026-10-10', '2026-10-10')
        """, (ghost_id,))
        conn.commit()
        conn.close()

        res = subprocess.run(
            [sys.executable, self.clean_script, '--db', self.db, '--apply'],
            capture_output=True, text=True
        )
        self.assertNotEqual(res.returncode, 0)
        self.assertTrue('存在活动身份映射' in res.stdout or '存在活动身份映射' in res.stderr)



    def test_ghost_bill_with_active_evidence_index_aborts_deletion(self):
        """反例 (T2 P0)：幽灵账单若拥有可信证据索引，禁止静默删除，必须中止并回滚"""
        conn = sqlite3.connect(self.db)
        cur = conn.cursor()
        cur.execute("""
            INSERT INTO statements (
                account_name, uid, message_id, bank_code, rule_id, subject, sender,
                email_date, statement_month, statement_date, due_date, total_due,
                minimum_due, raw_fields_json, parse_status, created_at, updated_at
            ) VALUES ('user@163.com', 'UID_GHOST_EV', '<ghost.ev@cmb.cn>', 'CMB', 'CMB_TEMPLATE_V1',
                      '招商银行每日资讯', 'svc@cmb.cn', '2026-10-10', '2026-10', '2026-10-10',
                      '2026-10-28', '0.00', '0.00', '{}', 'ok', '2026-10-10', '2026-10-10')
        """)
        ghost_id = cur.lastrowid
        conn.commit()
        conn.close()

        sd.save_statement_evidence_index(
            self.db, statement_id=ghost_id, account_name='user@163.com',
            bank_code='CMB', message_id='<ghost.ev@cmb.cn>',
            raw_content_hash='raw_g_ev', evidence_digest='digest_g_ev',
            coverage_status='complete', archive_path=None
        )

        res = subprocess.run(
            [sys.executable, self.clean_script, '--db', self.db, '--apply'],
            capture_output=True, text=True
        )
        self.assertNotEqual(res.returncode, 0)
        self.assertTrue('存在活动证据索引' in res.stdout or '存在活动证据索引' in res.stderr)


    def test_s2_duplicate_merge_with_different_raw_hash_succeeds(self):
        """正例 (S2 P1)：主记录与副本因可忽略的 Received 运输头导致 raw_content_hash 不同，但全内容摘要相同时，合并清洗成功"""
        main_id, dup_id = self._setup_main_and_dup(
            main_ev=True, dup_ev=True, dup_digest='digest_123', dup_ver='v1',
            dup_raw_hash='different_raw_hash_caused_by_received_header'
        )

        res = subprocess.run(
            [sys.executable, self.clean_script, '--db', self.db, '--apply'],
            capture_output=True, text=True
        )
        self.assertEqual(res.returncode, 0, f"Cleanup script failed: {res.stderr}")
        self.assertIn('清洗完成，事务已提交', res.stdout)

        conn = sqlite3.connect(self.db)
        cur = conn.cursor()
        cur.execute("SELECT statement_id FROM statement_transport_identities WHERE transport_uid = 'UID_ALIAS_FOR_DUP'")
        row = cur.fetchone()
        self.assertIsNotNone(row)
        self.assertEqual(row[0], main_id)

        # 验证财务状态完全保留
        cur.execute("SELECT total_due, minimum_due, is_paid FROM statements WHERE id = ?", (main_id,))
        m_row = cur.fetchone()
        self.assertEqual(m_row[0], '100.00')
        self.assertEqual(m_row[1], '10.00')

        # 核心核验外键完全守恒无悬挂
        cur.execute("PRAGMA foreign_key_check")
        self.assertEqual(cur.fetchall(), [])
        conn.close()

if __name__ == '__main__':
    unittest.main()
