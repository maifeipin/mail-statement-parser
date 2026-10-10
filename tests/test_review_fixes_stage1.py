#!/usr/bin/env python3
"""第一阶段复核专项测试（R1, R2, R7, R8）。

严格覆盖复核报告与用户验收要求：
- R1：同 UID 重解析失败，验证原金额、交易、币种余额和还款状态全部不变；
- R2：只读命令不能触发迁移；已有 UNKNOWN/USD 余额时，重复初始化不得新增 CNY；
- R7：CLI 参数解析（--months 6 生效）与 mark_paid JSON envelope 契约；
- R8：趋势分析真正净额（同月同商户同时支出 100 与退款 -30 合并净额为 70）。
"""

import io
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from decimal import Decimal

# 确保导入当前项目模块
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import statement_db as sd
from statement_models import (StatementRecord, StatementTransactionRecord,
                              StatementBalanceRecord, QualityEventRecord, Diagnostic)
from billing_readmodel import snapshot, trends
import mail_client


class TestR1SameUidReparseFailure(unittest.TestCase):
    """R1：同 UID 重解析失败保护。"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.db = os.path.join(self.tmp, 'test_r1.db')
        sd.init_db(self.db)

    def test_same_uid_reparse_failure_keeps_all_valid_financial_data(self):
        # 1. 存入初始合法账单（total_due=666.00, parse_status='ok', 包含交易明细与余额，且标记已还款）
        s_valid = StatementRecord(
            account_name='testbox', uid='UID-888', message_id='<msg-888@test>', bank_code='CMB',
            rule_id='CMB_TEMPLATE_V1', subject='招商银行信用卡对账单', sender='cmb@cmbchina.com',
            email_date='2026-10-01', statement_month='2026-10', statement_date='2026-10-01',
            due_date='2026-10-20', total_due='666.00', minimum_due='66.60',
            raw_fields_json='{"total_due": "666.00"}', parse_status='ok',
        )
        txns = [
            StatementTransactionRecord(
                account_name='testbox', uid='UID-888', bank_code='CMB', txn_date='2026-09-25',
                post_date='2026-09-26', description='苹果专卖店', amount='666.00', currency='CNY',
                currency_source='rule_declared', direction='debit',
            )
        ]
        balances = [
            StatementBalanceRecord(
                currency='CNY', total_due='666.00', minimum_due='66.60',
                currency_source='rule_declared', evidence={'note': 'original verified'},
            )
        ]
        sid, txn_count = sd.save_statement_with_transactions(self.db, s_valid, txns, balances=balances)
        self.assertEqual(txn_count, 1)

        # 标记已还款
        sd.mark_statement_paid_by_id(self.db, sid)

        # 2. 模拟同 UID 重新解析失败（例如模板提取失败或规则校验未通过）
        # 此时传入相同 (account_name, uid, bank_code)，但 parse_status='failed'，空交易，空余额，total_due=None
        s_failed = StatementRecord(
            account_name='testbox', uid='UID-888', message_id='<msg-888@test>', bank_code='CMB',
            rule_id='CMB_TEMPLATE_V1', subject='招商银行信用卡对账单', sender='cmb@cmbchina.com',
            email_date='2026-10-01', statement_month='2026-10', statement_date='2026-10-01',
            due_date=None, total_due=None, minimum_due=None,
            raw_fields_json='{}', parse_status='failed',
        )
        diag = Diagnostic(
            code='EXTRACTION_FAILED', field='total_due', severity='error',
            reason='模拟重解析失败', affected_purposes=['financial_summary'],
        )
        q_event = QualityEventRecord(
            case_key='parse_error|testbox|CMB|UID-888',
            event_type='quality_detected',
            account_name='testbox', bank_code='CMB',
            template_version='CMB_TEMPLATE_V1',
            diagnostics=[diag],
        )

        res_sid, res_txns = sd.save_statement_with_transactions(
            self.db, s_failed, [], balances=[], quality_events=[q_event]
        )
        self.assertEqual(res_sid, sid)

        # 3. 严格核验：原账单金额、状态、交易、多币种余额和还款状态全部不变！
        conn = sqlite3.connect(self.db)
        conn.row_factory = sqlite3.Row
        stmt_row = conn.execute("SELECT * FROM statements WHERE id = ?", (sid,)).fetchone()
        self.assertEqual(stmt_row['parse_status'], 'ok', '有效账单 parse_status 不得被篡改为 failed')
        self.assertEqual(stmt_row['total_due'], '666.00', '有效账单 total_due 不得被置空或覆盖')
        self.assertEqual(stmt_row['due_date'], '2026-10-20', '还款日不得被覆盖')
        self.assertEqual(stmt_row['is_paid'], 1, '还款状态 is_paid 不得回退')

        # 交易明细不得被清空
        txn_rows = conn.execute("SELECT * FROM statement_transactions WHERE statement_id = ?", (sid,)).fetchall()
        self.assertEqual(len(txn_rows), 1, '旧交易明细绝对不得被清空')
        self.assertEqual(txn_rows[0]['amount'], '666.00')
        self.assertEqual(txn_rows[0]['description'], '苹果专卖店')

        # 币种余额不得被清空
        bal_rows = conn.execute("SELECT * FROM statement_balances WHERE statement_id = ?", (sid,)).fetchall()
        self.assertEqual(len(bal_rows), 1, '旧币种余额行绝对不得被清空')
        self.assertEqual(bal_rows[0]['currency'], 'CNY')
        self.assertEqual(bal_rows[0]['total_due'], '666.00')

        # 质量事件正常被记录以供审计
        ev_row = conn.execute("SELECT * FROM quality_events WHERE case_key = ?", ('parse_error|testbox|CMB|UID-888',)).fetchone()
        self.assertIsNotNone(ev_row, '失败重解析的质量事件诊断应当保留')
        conn.close()


class TestR2IdempotentMigrationAndReadOnlyZeroWrite(unittest.TestCase):
    """R2：重复初始化不得为已有 UNKNOWN/USD 新增 CNY；只读命令零写入。"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.db = os.path.join(self.tmp, 'test_r2.db')
        sd.init_db(self.db)

    def test_existing_unknown_or_usd_balance_never_backfilled_cny(self):
        # 1. 存入一个只有 UNKNOWN 余额的账单
        s = StatementRecord(
            account_name='testbox', uid='UID-UNKNOWN', message_id='<msg-unk@test>', bank_code='HX',
            rule_id='HX_TEMPLATE_V1', subject='华夏银行信用卡电子账单', sender='hx@hxb.com',
            email_date='2026-10-01', statement_month='2026-10', total_due='100.00', parse_status='ok',
        )
        bal_unk = StatementBalanceRecord(
            currency='UNKNOWN', total_due='100.00', minimum_due='10.00',
            currency_source=None, evidence={'note': 'unverified currency'},
        )
        sid, _ = sd.save_statement_with_transactions(self.db, s, [], balances=[bal_unk])

        # 验证初始状态只有 UNKNOWN 余额
        conn = sqlite3.connect(self.db)
        curs = conn.execute("SELECT currency FROM statement_balances WHERE statement_id = ?", (sid,)).fetchall()
        self.assertEqual([r[0] for r in curs], ['UNKNOWN'])
        conn.close()

        # 2. 多次调用 init_db(self.db) 模拟服务重启或多进程初始化
        sd.init_db(self.db)
        sd.init_db(self.db)

        # 3. 严格核验：依然只有 1 行 UNKNOWN 余额，绝对不能凭空新增 CNY 余额产生双倍债务！
        conn = sqlite3.connect(self.db)
        curs_after = conn.execute("SELECT currency, total_due FROM statement_balances WHERE statement_id = ?", (sid,)).fetchall()
        conn.close()
        self.assertEqual(len(curs_after), 1, '已有 UNKNOWN/USD 余额的账单在重复 init_db 时不得新增 CNY')
        self.assertEqual(curs_after[0][0], 'UNKNOWN')

    def test_readonly_commands_zero_write(self):
        # 插入一些测试数据
        s = StatementRecord(
            account_name='testbox', uid='UID-RO', message_id='<msg-ro@test>', bank_code='CMB',
            rule_id='CMB_TEMPLATE_V1', subject='招商银行账单', sender='cmb@cmb.com',
            email_date='2026-10-01', statement_month='2026-10', total_due='200.00', parse_status='ok',
        )
        sid, _ = sd.save_statement_with_transactions(self.db, s, [
            StatementTransactionRecord(
                account_name='testbox', uid='UID-RO', bank_code='CMB', txn_date='2026-10-02',
                description='测试消费', amount='200.00', currency='CNY', direction='debit',
            )
        ])

        # 将数据库文件权限设为只读 (0444)
        os.chmod(self.db, 0o444)

        try:
            # 在只读模式下调用 snapshot, trends 以及只读事件获取
            # 如果里面调用了 init_db 或写连接，会抛出 sqlite3.OperationalError: attempt to write a readonly database
            snap = snapshot(self.db, months=3)
            self.assertEqual(snap['statement_count'], 1)

            tr = trends(self.db, months=3)
            self.assertEqual(len(tr['merchants']), 1)

            events = sd.get_unacked_quality_events(self.db, limit=10)
            self.assertEqual(len(events), 1)
            self.assertEqual(events[0]['event_type'], 'statement_recorded')
        finally:
            # 恢复权限以便临时目录清理
            os.chmod(self.db, 0o644)


class TestR7CLIArgumentsAndEnvelope(unittest.TestCase):
    """R7：CLI 参数解析 (--months 6) 与 mark_paid JSON envelope 契约。"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.db = os.path.join(self.tmp, 'test_r7.db')
        sd.init_db(self.db)
        self.orig_db_path = mail_client.DB_PATH
        mail_client.DB_PATH = self.db

        s = StatementRecord(
            account_name='testbox', uid='UID-R7', message_id='<msg-r7@test>', bank_code='CMB',
            rule_id='CMB_TEMPLATE_V1', subject='招行账单', sender='cmb@cmb.com',
            email_date='2026-10-01', statement_month='2026-05', total_due='500.00', parse_status='ok',
        )
        self.sid, _ = sd.save_statement_with_transactions(self.db, s, [])

    def tearDown(self):
        mail_client.DB_PATH = self.orig_db_path

    def test_cli_billing_snapshot_and_trends_months_flag(self):
        old_argv = sys.argv
        try:
            # snapshot --months 6
            sys.argv = ['mail_client.py', 'billing_snapshot', '--months', '6']
            buf = io.StringIO()
            with redirect_stdout(buf):
                mail_client.main()
            output = buf.getvalue().strip()
            envelope = json.loads(output)
            self.assertEqual(envelope['schema_version'], 1)
            self.assertEqual(envelope['command'], 'billing_snapshot')
            self.assertEqual(envelope['status'], 'ok')
            self.assertEqual(envelope['data']['months'], 6, 'CLI 参数 --months 6 必须被正确解析，不得退回默认 1 个月')

            # trends --months 6
            sys.argv = ['mail_client.py', 'billing_trends', '--months', '6']
            buf2 = io.StringIO()
            with redirect_stdout(buf2):
                mail_client.main()
            output2 = buf2.getvalue().strip()
            envelope2 = json.loads(output2)
            self.assertEqual(envelope2['schema_version'], 1)
            self.assertEqual(envelope2['command'], 'billing_trends')
            self.assertEqual(envelope2['status'], 'ok')
            self.assertEqual(envelope2['data']['months'], 6, 'CLI 参数 --months 6 必须被正确解析，不得退回默认 3 个月')
        finally:
            sys.argv = old_argv

    def test_cli_mark_paid_by_id_envelope(self):
        old_argv = sys.argv
        try:
            sys.argv = ['mail_client.py', 'mark_paid', '--statement-id', str(self.sid)]
            buf = io.StringIO()
            with redirect_stdout(buf):
                mail_client.main()
            output = buf.getvalue().strip()
            envelope = json.loads(output)
            self.assertEqual(envelope['schema_version'], 1)
            self.assertEqual(envelope['command'], 'mark_paid')
            self.assertEqual(envelope['status'], 'ok')
            self.assertEqual(envelope['data']['statement_id'], self.sid)
            self.assertTrue(envelope['data']['updated'])

            # 再次调用（已还款）
            buf2 = io.StringIO()
            with redirect_stdout(buf2):
                mail_client.main()
            envelope2 = json.loads(buf2.getvalue().strip())
            self.assertEqual(envelope2['status'], 'ok')
            self.assertFalse(envelope2['data']['updated'], '已经还款时 updated 为 False')
        finally:
            sys.argv = old_argv


class TestR8TrendsNetAndFxPairs(unittest.TestCase):
    """R8：趋势分析真正净额相消，汇率归属币种对与月份。"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.db = os.path.join(self.tmp, 'test_r8.db')
        sd.init_db(self.db)

    def test_same_merchant_debit_and_credit_netted_to_70(self):
        # 针对复核报告核心复现：同商户同月同币种支出 100，退款 -30 → 净额必须为 70！
        s = StatementRecord(
            account_name='163box', uid='UID-NET', message_id='<msg-net@test>', bank_code='CMB',
            rule_id='CMB_TEMPLATE_V1', subject='账单', sender='cmb@cmb.com',
            email_date='2026-10-01', statement_month='2026-10', total_due='70.00', parse_status='ok',
        )
        txns = [
            StatementTransactionRecord(
                account_name='163box', uid='UID-NET', bank_code='CMB', txn_date='2026-10-05',
                description='OPENAI*SUBSCRIPTION', amount='100.00', currency='CNY',
                direction='debit', original_amount='14.00', original_currency='USD',
            ),
            StatementTransactionRecord(
                account_name='163box', uid='UID-NET', bank_code='CMB', txn_date='2026-10-10',
                description='OPENAI*SUBSCRIPTION', amount='-30.00', currency='CNY',
                direction='credit',
            ),
        ]
        sd.save_statement_with_transactions(self.db, s, txns)

        tr = trends(self.db, months=3, merchant='OPENAI*SUBSCRIPTION')
        self.assertEqual(len(tr['merchants']), 1)
        m = tr['merchants'][0]
        self.assertEqual(m['merchant'], 'OPENAI*SUBSCRIPTION')
        month_entry = m['months'][0]
        self.assertEqual(month_entry['month'], '2026-10')

        # 重点：currencies 必须只有 1 个 CNY 记录，而不是被 direction 拆成 2 个！
        self.assertEqual(len(month_entry['currencies']), 1, '同商户同月同币种只能聚合为 1 个 currency 条目')
        cny_stat = month_entry['currencies'][0]
        self.assertEqual(cny_stat['currency'], 'CNY')
        self.assertEqual(cny_stat['debit_count'], 1)
        self.assertEqual(cny_stat['debit_amount'], '100.00')
        self.assertEqual(cny_stat['credit_count'], 1)
        self.assertEqual(cny_stat['credit_amount'], '-30.00')
        # 核心断言：net 必须是真正净额 70.00！
        self.assertEqual(cny_stat['net'], '70.00')

        # 原币汇率对 USD/CNY
        self.assertIn('USD/CNY', cny_stat['fx_pairs'])
        fx_info = cny_stat['fx_pairs']['USD/CNY']
        # 100 / 14 ≈ 7.142857
        self.assertEqual(Decimal(fx_info['sample_rate']), Decimal('7.142857'))


if __name__ == '__main__':
    unittest.main()
