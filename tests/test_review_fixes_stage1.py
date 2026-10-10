#!/usr/bin/env python3
"""第一阶段复核专项测试（R1, R2, R7, R8 及 A1, A2, A3, A4）。

严格覆盖复核报告与用户验收要求：
- R1：同 UID 重解析失败，验证原金额、交易、币种余额和还款状态全部不变；
- R2：只读命令不能触发迁移；已有 UNKNOWN/USD 余额时，重复初始化不得新增 CNY；
- A1：质量库不存在、缺表、正常空队列三种状态严格区分，前两者报错且不改库；
- A2：CLI 参数严格校验：缺值、非正整数、未知选项报错，拒绝静默缩水；
- A3：未知收支方向 (None/unknown/非法) 与方向冲突交易单独列示，不误计为支出；
- A4：汇率折算比例按月份与币种对归属，杜绝首笔样本冒充平均，多币种对禁止跨币种混算。
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

        sd.mark_statement_paid_by_id(self.db, sid)

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

        conn = sqlite3.connect(self.db)
        conn.row_factory = sqlite3.Row
        stmt_row = conn.execute("SELECT * FROM statements WHERE id = ?", (sid,)).fetchone()
        self.assertEqual(stmt_row['parse_status'], 'ok', '有效账单 parse_status 不得被篡改为 failed')
        self.assertEqual(stmt_row['total_due'], '666.00', '有效账单 total_due 不得被置空或覆盖')
        self.assertEqual(stmt_row['due_date'], '2026-10-20', '还款日不得被覆盖')
        self.assertEqual(stmt_row['is_paid'], 1, '还款状态 is_paid 不得回退')

        txn_rows = conn.execute("SELECT * FROM statement_transactions WHERE statement_id = ?", (sid,)).fetchall()
        self.assertEqual(len(txn_rows), 1, '旧交易明细绝对不得被清空')
        self.assertEqual(txn_rows[0]['amount'], '666.00')

        bal_rows = conn.execute("SELECT * FROM statement_balances WHERE statement_id = ?", (sid,)).fetchall()
        self.assertEqual(len(bal_rows), 1, '旧币种余额行绝对不得被清空')
        self.assertEqual(bal_rows[0]['currency'], 'CNY')

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

        conn = sqlite3.connect(self.db)
        curs = conn.execute("SELECT currency FROM statement_balances WHERE statement_id = ?", (sid,)).fetchall()
        self.assertEqual([r[0] for r in curs], ['UNKNOWN'])
        conn.close()

        sd.init_db(self.db)
        sd.init_db(self.db)

        conn = sqlite3.connect(self.db)
        curs_after = conn.execute("SELECT currency, total_due FROM statement_balances WHERE statement_id = ?", (sid,)).fetchall()
        conn.close()
        self.assertEqual(len(curs_after), 1, '已有 UNKNOWN/USD 余额的账单在重复 init_db 时不得新增 CNY')
        self.assertEqual(curs_after[0][0], 'UNKNOWN')

    def test_readonly_commands_zero_write(self):
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

        os.chmod(self.db, 0o444)
        try:
            snap = snapshot(self.db, months=3)
            self.assertEqual(snap['statement_count'], 1)

            tr = trends(self.db, months=3)
            self.assertEqual(len(tr['merchants']), 1)

            events = sd.get_unacked_quality_events(self.db, limit=10)
            self.assertEqual(len(events), 1)
        finally:
            os.chmod(self.db, 0o644)


class TestA1QualityEventsSchemaAndPathErrors(unittest.TestCase):
    """A1：质量库不存在、缺表、正常空队列严格区分。"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.orig_db_path = mail_client.DB_PATH

    def tearDown(self):
        mail_client.DB_PATH = self.orig_db_path

    def test_nonexistent_database_returns_error_and_does_not_create_file(self):
        nonexistent_db = os.path.join(self.tmp, 'nonexistent_statement.db')
        self.assertFalse(os.path.exists(nonexistent_db))
        mail_client.DB_PATH = nonexistent_db

        ret = mail_client.cli_quality_events(limit=10)
        self.assertEqual(ret['status'], 'error', '数据库不存在时必须返回 error')
        self.assertTrue(len(ret['errors']) > 0)
        self.assertIn('数据库文件不存在', ret['errors'][0])
        self.assertFalse(os.path.exists(nonexistent_db), '只读命令绝对不得偷偷创建新文件')

    def test_missing_table_returns_error_and_does_not_migrate(self):
        # 创建一个空数据库，没有任何表
        empty_db = os.path.join(self.tmp, 'empty.db')
        conn = sqlite3.connect(empty_db)
        conn.execute("CREATE TABLE dummy (id INTEGER PRIMARY KEY)")
        conn.commit()
        conn.close()

        mail_client.DB_PATH = empty_db
        ret = mail_client.cli_quality_events(limit=10)
        self.assertEqual(ret['status'], 'error', '缺表时必须返回 error')
        self.assertTrue(len(ret['errors']) > 0)
        self.assertIn('no such table: quality_events', ret['errors'][0])

        # 验证没有被偷偷执行 DDL
        conn = sqlite3.connect(empty_db)
        tables = [r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()]
        conn.close()
        self.assertNotIn('quality_events', tables, '只读命令绝对不得私自跑 DDL 补表')

    def test_normal_empty_queue_returns_ok_zero_events(self):
        # 初始化正常库
        normal_db = os.path.join(self.tmp, 'normal.db')
        sd.init_db(normal_db)
        mail_client.DB_PATH = normal_db

        ret = mail_client.cli_quality_events(limit=10)
        self.assertEqual(ret['status'], 'ok', '已初始化且无事件时正常返回 ok')
        self.assertEqual(ret['errors'], [])
        self.assertEqual(ret['data']['count'], 0)
        self.assertEqual(ret['data']['events'], [])


class TestA2CLIStrictArgumentValidation(unittest.TestCase):
    """A2：CLI 参数严格校验：缺值、非正整数、未知选项报错，拒绝静默缩水。"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.db = os.path.join(self.tmp, 'test_a2.db')
        sd.init_db(self.db)
        self.orig_db_path = mail_client.DB_PATH
        mail_client.DB_PATH = self.db

    def tearDown(self):
        mail_client.DB_PATH = self.orig_db_path

    def _run_cmd(self, cmd_args):
        old_argv = sys.argv
        buf = io.StringIO()
        try:
            sys.argv = ['mail_client.py'] + cmd_args
            with redirect_stdout(buf):
                try:
                    mail_client.main()
                    exit_code = 0
                except SystemExit as e:
                    exit_code = e.code
            output = buf.getvalue().strip()
            envelope = json.loads(output) if output else {}
            return exit_code, envelope
        finally:
            sys.argv = old_argv

    def test_missing_flag_value_errors(self):
        # billing_snapshot --months (缺值)
        code, env = self._run_cmd(['billing_snapshot', '--months'])
        self.assertNotEqual(code, 0)
        self.assertEqual(env.get('status'), 'error')
        self.assertIn("缺少参数值", env.get('errors', [''])[0])

        # billing_trends --months (缺值)
        code, env = self._run_cmd(['billing_trends', '--months'])
        self.assertNotEqual(code, 0)
        self.assertEqual(env.get('status'), 'error')
        self.assertIn("缺少参数值", env.get('errors', [''])[0])

    def test_invalid_non_integer_value_errors(self):
        code, env = self._run_cmd(['billing_snapshot', '--months', 'bad'])
        self.assertNotEqual(code, 0)
        self.assertEqual(env.get('status'), 'error')
        self.assertIn("必须为整数", env.get('errors', [''])[0])

    def test_non_positive_value_errors(self):
        code, env = self._run_cmd(['billing_snapshot', '--months', '0'])
        self.assertNotEqual(code, 0)
        self.assertEqual(env.get('status'), 'error')
        self.assertIn("必须 >= 1", env.get('errors', [''])[0])

        code, env = self._run_cmd(['billing_snapshot', '--months', '-2'])
        self.assertNotEqual(code, 0)
        self.assertEqual(env.get('status'), 'error')

    def test_unknown_option_errors(self):
        code, env = self._run_cmd(['billing_snapshot', '--unknown-flag', '123'])
        self.assertNotEqual(code, 0)
        self.assertEqual(env.get('status'), 'error')
        self.assertIn("未知选项", env.get('errors', [''])[0])

    def test_conflicting_duplicate_and_excess_arguments_rejected(self):
        cases = [
            ['billing_snapshot', '--months', '6', '3'],
            ['billing_snapshot', '6', '20', 'unexpected'],
            ['billing_snapshot', '--months', '6', '--months', '3'],
            ['billing_trends', '6', 'unexpected'],
            ['quality_events', '100', 'unexpected'],
            ['quality_ack', 'event', 'todo', 'unexpected'],
            ['mark_paid', '--statement-id'],
            ['mark_paid', '--statement-id', '0'],
            ['mark_paid', '--statement-id', 'bad'],
            ['mark_paid', '--statement-id', '1', 'unexpected'],
        ]
        for args in cases:
            with self.subTest(args=args):
                code, env = self._run_cmd(args)
                self.assertEqual(code, 1)
                self.assertEqual(env['status'], 'error')
                self.assertEqual(env['command'], args[0])

    def test_mark_paid_id_envelope_and_idempotency(self):
        s = StatementRecord(uid='paid-fixture', message_id='<paid@test>', bank_code='CMB',
                            rule_id='r', subject='fixture', sender='fixture', email_date='2026-10-01',
                            account_name='fixture', statement_month='2026-10', total_due='100')
        sid, _ = sd.save_statement_with_transactions(self.db, s, [])
        code, env = self._run_cmd(['mark_paid', '--statement-id', str(sid)])
        self.assertEqual(code, 0)
        self.assertEqual(env['schema_version'], 1)
        self.assertEqual(env['data'], {'statement_id': sid, 'updated': True})
        code, env = self._run_cmd(['mark_paid', '--statement-id', str(sid)])
        self.assertEqual(code, 0)
        self.assertFalse(env['data']['updated'])

    def test_valid_positional_and_flag_paths(self):
        # 正常 flag
        code, env = self._run_cmd(['billing_snapshot', '--months', '6'])
        self.assertEqual(code, 0)
        self.assertEqual(env.get('status'), 'ok')
        self.assertEqual(env['data']['months'], 6)

        # 正常旧位置参数
        code, env = self._run_cmd(['billing_snapshot', '6'])
        self.assertEqual(code, 0)
        self.assertEqual(env.get('status'), 'ok')
        self.assertEqual(env['data']['months'], 6)


class TestA3DirectionHandlingAndConflicts(unittest.TestCase):
    """A3：未知收支方向 (None/unknown/非法) 与方向冲突交易单独列示，不误计为支出。"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.db = os.path.join(self.tmp, 'test_a3.db')
        sd.init_db(self.db)

    def test_unknown_direction_not_counted_as_debit(self):
        s = StatementRecord(
            account_name='box', uid='U-A3', message_id='<a3@test>', bank_code='CMB',
            rule_id='r', subject='s', sender='s', email_date='2026-10-01',
            statement_month='2026-10', total_due='100.00', parse_status='ok',
        )
        txns = [
            # 1. 正常支出 100
            StatementTransactionRecord(
                account_name='box', uid='U-A3', bank_code='CMB', txn_date='2026-10-01',
                description='M1', amount='100.00', currency='CNY', direction='debit',
            ),
            # 2. 未知方向 (None) 50
            StatementTransactionRecord(
                account_name='box', uid='U-A3', bank_code='CMB', txn_date='2026-10-02',
                description='M1', amount='50.00', currency='CNY', direction=None,
            ),
            # 3. 未知方向 ('unknown') 30
            StatementTransactionRecord(
                account_name='box', uid='U-A3', bank_code='CMB', txn_date='2026-10-03',
                description='M1', amount='30.00', currency='CNY', direction='unknown',
            ),
            # 4. 方向与符号冲突 (标为 debit 但金额为 -20)
            StatementTransactionRecord(
                account_name='box', uid='U-A3', bank_code='CMB', txn_date='2026-10-04',
                description='M1', amount='-20.00', currency='CNY', direction='debit',
            ),
            # 5. 正常退款 (credit) -30
            StatementTransactionRecord(
                account_name='box', uid='U-A3', bank_code='CMB', txn_date='2026-10-05',
                description='M1', amount='-30.00', currency='CNY', direction='credit',
            ),
        ]
        sd.save_statement_with_transactions(self.db, s, txns)

        tr = trends(self.db, months=3, merchant='M1')
        cny = tr['merchants'][0]['months'][0]['currencies'][0]

        # 核心断言：
        # 1. debit 只能有正常支出 1 笔 100.00，绝对不能把未知方向或冲突项计入！
        self.assertEqual(cny['debit_count'], 1)
        self.assertEqual(cny['debit_amount'], '100.00')

        # 2. credit 为 1 笔 -30.00
        self.assertEqual(cny['credit_count'], 1)
        self.assertEqual(cny['credit_amount'], '-30.00')

        # 3. 确定性净额只能是 100 + (-30) = 70.00！
        self.assertEqual(cny['net'], '70.00')

        # 4. 未知方向单独列出：2 笔，共 80.00
        self.assertEqual(cny['unknown_direction_count'], 2)
        self.assertEqual(cny['unknown_direction_amount'], '80.00')

        # 5. 冲突方向单独列出：1 笔，共 -20.00
        self.assertEqual(cny['direction_conflict_count'], 1)
        self.assertEqual(cny['direction_conflict_amount'], '-20.00')


class TestA4FxRatesDisaggregationAndAverages(unittest.TestCase):
    """A4：汇率折算比例按月份与币种对归属，杜绝首笔样本冒充平均，多币种对禁止跨币种混算。"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.db = os.path.join(self.tmp, 'test_a4.db')
        sd.init_db(self.db)

    def test_single_pair_multiple_rates_computes_real_average(self):
        # 单一币种对 (USD/CNY) 存在两笔不同汇率样本：
        # 笔 1: 100 CNY / 14 USD ≈ 7.142857
        # 笔 2: 72 CNY / 10 USD = 7.200000
        # 真实算术平均: (100/14 + 72/10) / 2，最终舍入为 7.171429 (不是首笔样本 7.142857)
        s = StatementRecord(
            account_name='box', uid='U-A4-1', message_id='<a4-1@test>', bank_code='CMB',
            rule_id='r', subject='s', sender='s', email_date='2026-10-01',
            statement_month='2026-10', total_due='172.00', parse_status='ok',
        )
        txns = [
            StatementTransactionRecord(
                account_name='box', uid='U-A4-1', bank_code='CMB', txn_date='2026-10-01',
                description='SAAS_TOOL', amount='100.00', currency='CNY', direction='debit',
                original_amount='14.00', original_currency='USD',
            ),
            StatementTransactionRecord(
                account_name='box', uid='U-A4-1', bank_code='CMB', txn_date='2026-10-02',
                description='SAAS_TOOL', amount='72.00', currency='CNY', direction='debit',
                original_amount='10.00', original_currency='USD',
            ),
        ]
        sd.save_statement_with_transactions(self.db, s, txns)

        tr = trends(self.db, months=3, merchant='SAAS_TOOL')
        m = tr['merchants'][0]

        # 核心断言：
        # 1. 商户顶层真实算术平均值必须是 7.171429，绝对不能只是首笔样本 7.142857！
        self.assertEqual(Decimal(m['avg_fx_rate_sample']), Decimal('7.171429'))
        monthly = m['months'][0]['currencies'][0]['fx_pairs']['USD/CNY']
        self.assertEqual(Decimal(monthly['simple_avg_rate']), Decimal('7.171429'))
        self.assertEqual(m['avg_fx_rate_algorithm'], 'simple_arithmetic_mean')

        # 2. 币种对汇总信息准确
        fx_summary = m['fx_pairs_summary']['USD/CNY']
        self.assertEqual(fx_summary['count'], 2)
        self.assertEqual(Decimal(fx_summary['simple_avg_rate']), Decimal('7.171429'))
        # 加权平均: (100 + 72) / (14 + 10) = 172 / 24 ≈ 7.166667
        self.assertEqual(Decimal(fx_summary['weighted_avg_rate']), Decimal('7.166667'))

    def test_multi_pair_merchant_disaggregated_and_suppresses_global_average(self):
        # 多币种对商户 (既有 USD/CNY 汇率 ≈ 7.14，又有 JPY/CNY 汇率 ≈ 0.046)
        # 绝对不能跨币种混合计算平均值！
        s1 = StatementRecord(
            account_name='box', uid='U-A4-2', message_id='<a4-2@test>', bank_code='CMB',
            rule_id='r', subject='s', sender='s', email_date='2026-09-01',
            statement_month='2026-09', total_due='100.00', parse_status='ok',
        )
        s2 = StatementRecord(
            account_name='box', uid='U-A4-3', message_id='<a4-3@test>', bank_code='CMB',
            rule_id='r', subject='s', sender='s', email_date='2026-10-01',
            statement_month='2026-10', total_due='46.00', parse_status='ok',
        )
        sd.save_statement_with_transactions(self.db, s1, [
            StatementTransactionRecord(
                account_name='box', uid='U-A4-2', bank_code='CMB', txn_date='2026-09-10',
                description='GLOBAL_STORE', amount='100.00', currency='CNY', direction='debit',
                original_amount='14.00', original_currency='USD',
            )
        ])
        sd.save_statement_with_transactions(self.db, s2, [
            StatementTransactionRecord(
                account_name='box', uid='U-A4-3', bank_code='CMB', txn_date='2026-10-15',
                description='GLOBAL_STORE', amount='46.00', currency='CNY', direction='debit',
                original_amount='1000.00', original_currency='JPY',
            )
        ])

        tr = trends(self.db, months=3, merchant='GLOBAL_STORE')
        m = tr['merchants'][0]

        # 核心断言：
        # 1. 存在多个不同币种对时，全局平均必须置为 None，防止荒谬的跨币种混合！
        self.assertIsNone(m['avg_fx_rate_sample'], '多币种对商户不得输出荒谬的全局混合平均值')
        self.assertEqual(m['avg_fx_rate_algorithm'], 'disaggregated_by_pair')

        # 2. 各币种对独立在 summary 中可见
        self.assertIn('USD/CNY', m['fx_pairs_summary'])
        self.assertIn('JPY/CNY', m['fx_pairs_summary'])

        # 3. 月度样本独立，9 月只有 USD/CNY，10 月只有 JPY/CNY，绝对不串组！
        months_dict = {entry['month']: entry for entry in m['months']}
        sep_entry = months_dict['2026-09']['currencies'][0]
        oct_entry = months_dict['2026-10']['currencies'][0]

        self.assertIn('USD/CNY', sep_entry['fx_pairs'])
        self.assertNotIn('JPY/CNY', sep_entry['fx_pairs'])

        self.assertIn('JPY/CNY', oct_entry['fx_pairs'])
        self.assertNotIn('USD/CNY', oct_entry['fx_pairs'])


if __name__ == '__main__':
    unittest.main()
