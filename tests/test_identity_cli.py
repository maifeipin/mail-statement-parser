#!/usr/bin/env python3
"""
tests/test_identity_cli.py
单元测试：CLI 人工确认工作台 (identity_preview, identity_confirm, identity_status) (E1 §6, §10.6)
"""

import email.message
import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import mail_client as mc
import statement_db as sd
from parser_paths import resolve_parser_paths
from statement_models import StatementRecord


class TestIdentityCLIWorkbench(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.db = os.path.join(self.tmp, 'test_cli.db')
        self.dl_dir = os.path.join(self.tmp, 'email-downloads')
        self.rep_dir = os.path.join(self.tmp, 'reports')
        os.makedirs(self.dl_dir, exist_ok=True)
        os.makedirs(self.rep_dir, exist_ok=True)

        os.environ['MAIL_STATEMENT_DATA_ROOT'] = self.tmp
        os.environ['MAIL_STATEMENT_DB_PATH'] = self.db
        os.environ['MAIL_STATEMENT_DOWNLOAD_DIR'] = self.dl_dir
        os.environ['MAIL_STATEMENT_REPORT_DIR'] = self.rep_dir

        sd.init_db(self.db)

        # 预置已存目标账单
        self.s1 = StatementRecord(
            account_name='user@163.com',
            uid='UID_TARGET',
            message_id='<msg.october@cmb.cn>',
            bank_code='CMB',
            rule_id='CMB_TEMPLATE_V1',
            subject='招商银行信用卡电子账单',
            sender='service@cmb.cn',
            email_date='2026-10-10',
            statement_month='2026-10',
            statement_date='2026-10-10',
            due_date='2026-10-28',
            total_due='100.00',
            minimum_due='10.00',
            raw_fields_json='{}'
        )
        self.s1_id, _ = sd.save_statement_with_transactions(self.db, self.s1, [])

        # 创建目标邮件 EML
        msg_target = email.message.EmailMessage()
        msg_target['Message-ID'] = '<msg.october@cmb.cn>'
        msg_target['Subject'] = '招商银行信用卡电子账单'
        msg_target['From'] = 'service@cmb.cn'
        msg_target['Date'] = 'Sat, 10 Oct 2026 10:00:00 +0800'
        msg_target.set_content('Body content for October Bill')
        with open(os.path.join(self.dl_dir, 'UID_TARGET.eml'), 'wb') as fp:
            fp.write(msg_target.as_bytes())

    def tearDown(self):
        import shutil
        os.environ.pop('MAIL_STATEMENT_DATA_ROOT', None)
        os.environ.pop('MAIL_STATEMENT_DB_PATH', None)
        os.environ.pop('MAIL_STATEMENT_DOWNLOAD_DIR', None)
        os.environ.pop('MAIL_STATEMENT_REPORT_DIR', None)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_identity_preview_and_confirm_flow(self):
        """测试完整的 preview -> confirm -> status 流程"""
        # 1. 准备 candidate eml (内容完全一致)
        cand_msg = email.message.EmailMessage()
        cand_msg['Message-ID'] = '<msg.october@cmb.cn>'
        cand_msg['Subject'] = '招商银行信用卡电子账单'
        cand_msg['From'] = 'service@cmb.cn'
        cand_msg['Date'] = 'Sat, 10 Oct 2026 10:00:00 +0800'
        cand_msg.set_content('Body content for October Bill')
        cand_eml_path = os.path.join(self.dl_dir, 'UID_CANDIDATE.eml')
        with open(cand_eml_path, 'wb') as fp:
            fp.write(cand_msg.as_bytes())

        # 2. Preview
        prev_env = mc.cli_identity_preview(
            statement_id=self.s1_id,
            account='user@163.com',
            transport_kind='pop3_uidl',
            transport_uid='UID_CANDIDATE',
            bank='CMB',
            eml_path=cand_eml_path
        )
        self.assertEqual(prev_env['status'], 'ok')
        prev_data = prev_env['data']
        self.assertEqual(prev_data['status'], 'associated_existing')
        plan_hash = prev_data['plan_hash']
        plan_path = prev_data['report_path']
        self.assertTrue(os.path.exists(plan_path))

        # 3. Confirm (带合法 actor 与 reason)
        conf_env = mc.cli_identity_confirm(
            plan_path=plan_path,
            plan_hash=plan_hash,
            actor='reviewer_alice',
            reason='Verified exact message-id and body'
        )
        self.assertEqual(conf_env['status'], 'ok')
        self.assertTrue(conf_env['data']['confirmed'])

        # 4. Status 查询
        stat_env = mc.cli_identity_status(statement_id=self.s1_id)
        self.assertEqual(stat_env['status'], 'ok')
        identities = stat_env['data']['identities']
        self.assertEqual(len(identities), 1)
        self.assertEqual(identities[0]['transport_uid'], 'UID_CANDIDATE')

    def test_confirm_rejects_missing_actor_or_reason(self):
        """反例：confirm 拒绝空 actor 或空 reason"""
        env = mc.cli_identity_confirm(
            plan_path='nonexistent.json',
            plan_hash='hash',
            actor='',
            reason='some reason'
        )
        self.assertEqual(env['status'], 'error')
        self.assertTrue(any('actor' in e for e in env['errors']))

        env2 = mc.cli_identity_confirm(
            plan_path='nonexistent.json',
            plan_hash='hash',
            actor='alice',
            reason='   '
        )
        self.assertEqual(env2['status'], 'error')
        self.assertTrue(any('reason' in e for e in env2['errors']))

    def test_confirm_rejects_hash_tampering(self):
        """反例：篡改 plan_hash 拒绝确认"""
        cand_msg = email.message.EmailMessage()
        cand_msg['Message-ID'] = '<msg.october@cmb.cn>'
        cand_msg['Subject'] = '招商银行信用卡电子账单'
        cand_msg['From'] = 'service@cmb.cn'
        cand_msg['Date'] = 'Sat, 10 Oct 2026 10:00:00 +0800'
        cand_msg.set_content('Body content for October Bill')
        cand_eml_path = os.path.join(self.dl_dir, 'UID_TAMP.eml')
        with open(cand_eml_path, 'wb') as fp:
            fp.write(cand_msg.as_bytes())

        prev_env = mc.cli_identity_preview(
            statement_id=self.s1_id,
            account='user@163.com',
            transport_kind='pop3_uidl',
            transport_uid='UID_TAMP',
            bank='CMB',
            eml_path=cand_eml_path
        )
        plan_path = prev_env['data']['report_path']

        # 传入错误的 plan_hash
        conf_env = mc.cli_identity_confirm(
            plan_path=plan_path,
            plan_hash='tampered_hash_value_123',
            actor='reviewer',
            reason='manual check'
        )
        self.assertEqual(conf_env['status'], 'error')
        self.assertTrue(any('mismatch' in e for e in conf_env['errors']))


    def test_confirm_rejects_source_file_swap_after_preview(self):
        """反例 (Gap 2 P0)：preview 后源 EML 文件被替换，confirm 时重读重核必须拒绝"""
        cand_msg = email.message.EmailMessage()
        cand_msg['Message-ID'] = '<msg.october@cmb.cn>'
        cand_msg['Subject'] = '招商银行信用卡电子账单'
        cand_msg['From'] = 'service@cmb.cn'
        cand_msg['Date'] = 'Sat, 10 Oct 2026 10:00:00 +0800'
        cand_msg.set_content('Body content for October Bill')
        cand_eml_path = os.path.join(self.dl_dir, 'UID_SWAP.eml')
        with open(cand_eml_path, 'wb') as fp:
            fp.write(cand_msg.as_bytes())

        prev_env = mc.cli_identity_preview(
            statement_id=self.s1_id,
            account='user@163.com',
            transport_kind='pop3_uidl',
            transport_uid='UID_SWAP',
            bank='CMB',
            eml_path=cand_eml_path
        )
        self.assertEqual(prev_env['status'], 'ok')
        plan_hash = prev_env['data']['plan_hash']
        plan_path = prev_env['data']['report_path']

        # 恶意替换或修改源文件内容 (计划文件中的 hash 保持不变)
        with open(cand_eml_path, 'wb') as fp:
            fp.write(b'COMPLETELY_DIFFERENT_SWAPPED_PAYLOAD')

        # 执行确认：必须拒绝！
        conf_env = mc.cli_identity_confirm(
            plan_path=plan_path,
            plan_hash=plan_hash,
            actor='reviewer',
            reason='manual check'
        )
        self.assertEqual(conf_env['status'], 'error')
        self.assertTrue(any('replaced or modified' in e for e in conf_env['errors']))

    def test_preview_generates_diff_report(self):
        """正例 (Gap 5 P1)：preview 生成可信私有并排差异分析报告"""
        cand_msg = email.message.EmailMessage()
        cand_msg['Message-ID'] = '<msg.october@cmb.cn>'
        cand_msg['Subject'] = '招商银行信用卡电子账单'
        cand_msg['From'] = 'service@cmb.cn'
        cand_msg['Date'] = 'Sat, 10 Oct 2026 10:00:00 +0800'
        cand_msg.set_content('Body content for October Bill')
        cand_eml_path = os.path.join(self.dl_dir, 'UID_DIFF_TEST.eml')
        with open(cand_eml_path, 'wb') as fp:
            fp.write(cand_msg.as_bytes())

        prev_env = mc.cli_identity_preview(
            statement_id=self.s1_id,
            account='user@163.com',
            transport_kind='pop3_uidl',
            transport_uid='UID_DIFF_TEST',
            bank='CMB',
            eml_path=cand_eml_path
        )
        self.assertEqual(prev_env['status'], 'ok')
        diff_path = prev_env['data'].get('diff_path')
        self.assertIsNotNone(diff_path)
        self.assertTrue(os.path.exists(diff_path))

        with open(diff_path, 'r', encoding='utf-8') as fp:
            diff_data = json.load(fp)
        self.assertIn('metadata_diff', diff_data)
        self.assertIn('mime_tree', diff_data)
        self.assertIn('body_and_digest', diff_data)
        self.assertIn('coverage', diff_data)



    def test_confirm_rejects_deleted_target_archive_no_fallback(self):
        """反例 (T3 P0)：若计划中声明了目标归档，confirm 前目标归档被删除，必须坚决拒绝，禁止回退到计划摘要"""
        cand_msg = email.message.EmailMessage()
        cand_msg['Message-ID'] = '<msg.october@cmb.cn>'
        cand_msg['Subject'] = '招商银行信用卡电子账单'
        cand_msg['From'] = 'service@cmb.cn'
        cand_msg['Date'] = 'Sat, 10 Oct 2026 10:00:00 +0800'
        cand_msg.set_content('Body content for October Bill')
        cand_path = os.path.join(self.dl_dir, 'UID_DEL_TEST.eml')
        with open(cand_path, 'wb') as fp:
            fp.write(cand_msg.as_bytes())

        prev_env = mc.cli_identity_preview(
            statement_id=self.s1_id,
            account='user@163.com',
            transport_kind='pop3_uidl',
            transport_uid='UID_DEL_TEST',
            bank='CMB'
        )
        self.assertEqual(prev_env['status'], 'ok')
        plan_path = prev_env['data']['report_path']
        plan_hash = prev_env['data']['plan_hash']

        with open(plan_path, 'r', encoding='utf-8') as fp:
            plan = json.load(fp)

        # 模拟外部篡改/删除目标归档
        target_archive = plan['target_ref']['archive_path']
        self.assertTrue(os.path.exists(target_archive))
        os.remove(target_archive)

        # 执行 confirm：必须拒绝，不能落回 target_evidence_summary
        conf_env = mc.cli_identity_confirm(
            plan_path=plan_path,
            plan_hash=plan_hash,
            actor='reviewer_alice',
            reason='test missing target archive'
        )
        self.assertEqual(conf_env['status'], 'error')
        self.assertTrue(any('Target archive declared in plan is missing or deleted' in err for err in conf_env['errors']))

        # 验证数据库中零映射
        import sqlite3
        conn = sqlite3.connect(self.db)
        cur = conn.cursor()
        cur.execute("SELECT COUNT(*) FROM statement_transport_identities WHERE transport_uid = 'UID_DEL_TEST'")
        self.assertEqual(cur.fetchone()[0], 0)
        conn.close()

    def test_confirm_rejects_tampered_target_archive(self):
        """反例 (T3 P0)：若目标归档在 preview 后被修改，confirm 必须识别并拒绝"""
        cand_msg = email.message.EmailMessage()
        cand_msg['Message-ID'] = '<msg.october@cmb.cn>'
        cand_msg['Subject'] = '招商银行信用卡电子账单'
        cand_msg['From'] = 'service@cmb.cn'
        cand_msg['Date'] = 'Sat, 10 Oct 2026 10:00:00 +0800'
        cand_msg.set_content('Body content for October Bill')
        cand_path = os.path.join(self.dl_dir, 'UID_TAMPER_TEST.eml')
        with open(cand_path, 'wb') as fp:
            fp.write(cand_msg.as_bytes())

        prev_env = mc.cli_identity_preview(
            statement_id=self.s1_id,
            account='user@163.com',
            transport_kind='pop3_uidl',
            transport_uid='UID_TAMPER_TEST',
            bank='CMB'
        )
        self.assertEqual(prev_env['status'], 'ok')
        plan_path = prev_env['data']['report_path']
        plan_hash = prev_env['data']['plan_hash']

        with open(plan_path, 'r', encoding='utf-8') as fp:
            plan = json.load(fp)

        # 篡改目标归档内容
        target_archive = plan['target_ref']['archive_path']
        with open(target_archive, 'wb') as fp:
            fp.write(b'TAMPERED TARGET ARCHIVE CONTENT')

        conf_env = mc.cli_identity_confirm(
            plan_path=plan_path,
            plan_hash=plan_hash,
            actor='reviewer_alice',
            reason='test tampered target archive'
        )
        self.assertEqual(conf_env['status'], 'error')
        self.assertTrue(any('Target EML archive was modified after preview' in err for err in conf_env['errors']))


    def test_s3_historical_cache_manual_confirm_and_idempotency_flow(self):
        """正例 (S3 P1)：真实历史缓存账单（无原文归档、无证据索引）preview 产生 insufficient_evidence，"""
        """人工审核通过后成功建立 manual_verified 映射与审计，重投幂等返回"""
        # 1. 预置一条历史纯数据库账单（无本地 .eml 归档，无 statement_evidence_index）
        s_hist = StatementRecord(
            account_name='user@163.com',
            uid='UID_HISTORICAL_CACHE',
            message_id='<hist.202501@cmb.cn>',
            bank_code='CMB',
            rule_id='CMB_TEMPLATE_V1',
            subject='招商银行信用卡电子账单',
            sender='service@cmb.cn',
            email_date='2025-01-10',
            statement_month='2025-01',
            statement_date='2025-01-10',
            due_date='2025-01-28',
            total_due='555.55',
            minimum_due='55.55',
            raw_fields_json='{}'
        )
        hist_id, _ = sd.save_statement_with_transactions(self.db, s_hist, [])

        # 2. 准备源端候选邮件（有真实 EML）
        cand_msg = email.message.EmailMessage()
        cand_msg['Message-ID'] = '<hist.202501@cmb.cn>'
        cand_msg['Subject'] = '招商银行信用卡电子账单'
        cand_msg['From'] = 'service@cmb.cn'
        cand_msg['Date'] = 'Fri, 10 Jan 2025 10:00:00 +0800'
        cand_msg.set_content('Body of 2025 January historical bill')
        cand_eml_path = os.path.join(self.dl_dir, 'UID_CAND_HIST.eml')
        with open(cand_eml_path, 'wb') as fp:
            fp.write(cand_msg.as_bytes())

        # 3. Preview：必须生成 insufficient_evidence 状态，但明确 can_confirm=True
        prev_env = mc.cli_identity_preview(
            statement_id=hist_id,
            account='user@163.com',
            transport_kind='pop3_uidl',
            transport_uid='UID_CAND_HIST',
            bank='CMB',
            eml_path=cand_eml_path
        )
        self.assertEqual(prev_env['status'], 'ok')
        prev_data = prev_env['data']
        self.assertEqual(prev_data['status'], 'insufficient_evidence')
        self.assertTrue(prev_data['summary']['can_confirm'])
        plan_hash = prev_data['plan_hash']
        plan_path = prev_data['report_path']
        self.assertTrue(os.path.exists(plan_path))

        # 4. Confirm：带明确审查人和理由，人工确认路径必须成功放行
        conf_env = mc.cli_identity_confirm(
            plan_path=plan_path,
            plan_hash=plan_hash,
            actor='ops_reviewer_bob',
            reason='Verified against paper archive for 2025-01'
        )
        self.assertEqual(conf_env['status'], 'ok')
        self.assertTrue(conf_env['data']['confirmed'])
        self.assertEqual(conf_env['data']['method'], 'manual_verified')
        self.assertFalse(conf_env['data']['idempotent'])

        # 5. 验证数据库中映射与审计记录
        import sqlite3
        conn = sqlite3.connect(self.db)
        cur = conn.cursor()
        cur.execute("SELECT method, statement_id FROM statement_transport_identities WHERE transport_uid = 'UID_CAND_HIST'")
        id_row = cur.fetchone()
        self.assertIsNotNone(id_row)
        self.assertEqual(id_row[0], 'manual_verified')
        self.assertEqual(id_row[1], hist_id)

        cur.execute("SELECT method, actor, reason, coverage_status FROM statement_identity_audit WHERE transport_uid = 'UID_CAND_HIST'")
        audit_row = cur.fetchone()
        self.assertIsNotNone(audit_row)
        self.assertEqual(audit_row[0], 'manual_verified')
        self.assertEqual(audit_row[1], 'ops_reviewer_bob')
        self.assertEqual(audit_row[2], 'Verified against paper archive for 2025-01')
        self.assertEqual(audit_row[3], 'complete') # 源端已完整提取
        conn.close()

        # 6. 重投幂等测试：再次调用必须成功返回且 idempotent=True
        conf_env2 = mc.cli_identity_confirm(
            plan_path=plan_path,
            plan_hash=plan_hash,
            actor='ops_reviewer_bob',
            reason='Verified against paper archive for 2025-01'
        )
        self.assertEqual(conf_env2['status'], 'ok')
        self.assertTrue(conf_env2['data']['confirmed'])
        self.assertTrue(conf_env2['data']['idempotent'])

    def test_s3_hard_metadata_contradiction_rejected(self):
        """反例 (S3 P1)：Message-ID 或账号存在硬元数据矛盾时，禁止人工强制关联"""
        # 预置一条带有不同 Message-ID 的账单
        s_diff_msg = StatementRecord(
            account_name='user@163.com',
            uid='UID_DIFF_MSG',
            message_id='<different.id@cmb.cn>',
            bank_code='CMB',
            rule_id='CMB_TEMPLATE_V1',
            subject='招商银行信用卡电子账单',
            sender='service@cmb.cn',
            email_date='2026-10-10',
            statement_month='2026-10',
            statement_date='2026-10-10',
            due_date='2026-10-28',
            total_due='100.00',
            minimum_due='10.00',
            raw_fields_json='{}'
        )
        diff_id, _ = sd.save_statement_with_transactions(self.db, s_diff_msg, [])

        cand_msg = email.message.EmailMessage()
        cand_msg['Message-ID'] = '<other.id@cmb.cn>'
        cand_msg['Subject'] = '招商银行信用卡电子账单'
        cand_msg['From'] = 'service@cmb.cn'
        cand_msg['Date'] = 'Sat, 10 Oct 2026 10:00:00 +0800'
        cand_msg.set_content('Body')
        cand_eml_path = os.path.join(self.dl_dir, 'UID_CAND_HARD.eml')
        with open(cand_eml_path, 'wb') as fp:
            fp.write(cand_msg.as_bytes())

        # preview 产生 identity_conflict
        prev_env = mc.cli_identity_preview(
            statement_id=diff_id,
            account='user@163.com',
            transport_kind='pop3_uidl',
            transport_uid='UID_CAND_HARD',
            bank='CMB',
            eml_path=cand_eml_path
        )
        self.assertEqual(prev_env['status'], 'ok')
        self.assertEqual(prev_env['data']['status'], 'identity_conflict')
        self.assertFalse(prev_env['data']['summary']['can_confirm'])

        # 尝试强行对该 plan 执行 confirm 必须报错拒绝
        plan_path = prev_env['data']['report_path']
        plan_hash = prev_env['data']['plan_hash']
        conf_env = mc.cli_identity_confirm(
            plan_path=plan_path,
            plan_hash=plan_hash,
            actor='ops_reviewer',
            reason='try to force'
        )
        self.assertEqual(conf_env['status'], 'error')
        self.assertTrue(any('not eligible for confirmation' in err for err in conf_env['errors']))

if __name__ == '__main__':
    unittest.main()
