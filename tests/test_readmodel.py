#!/usr/bin/env python3
"""阶段 B 读模型回归（重构契约 §4/§6.2 / 任务清单回归矩阵）。

固定回归样本：GOOGLE*CHATGPT，交易日 2026-08-13、记账日 2026-08-15、
入账 121.93 CNY、原币金额 2860.00、地区 JP。当前证据没有原币币种：
必须显示未知、不得猜 JPY；fixture 补上 JPY 证据时比例 ≈ 0.042633 CNY/JPY。
测试用隔离 fixture 定位，不依赖生产自增 ID。
"""

import os
import sys
import tempfile
import unittest
from decimal import Decimal

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import statement_db as sd
from statement_models import StatementRecord, StatementTransactionRecord
from billing_readmodel import snapshot, trends, decimal_amount, merchant_of


def _mk_db(self, with_fx_evidence=False):
    self.tmp = tempfile.mkdtemp()
    self.db = os.path.join(self.tmp, 't.db')
    sd.init_db(self.db)
    base = dict(uid='U887', message_id='<g@x>', bank_code='CMB', rule_id='CMB_TEMPLATE_V1',
                subject='招商银行信用卡电子账单', sender='s', email_date='2026-08-15',
                account_name='163box', statement_month='2026-08', statement_date='2026-08-15',
                due_date='2026-09-02', total_due='500.00', minimum_due='50.00')
    sd.save_statement_with_transactions(self.db, StatementRecord(**base), [
        StatementTransactionRecord(
            uid='U887', bank_code='CMB', account_name='163box',
            txn_date='2026-08-13', post_date='2026-08-15',
            description='GOOGLE*CHATGPT', amount='121.93', currency='CNY',
            txn_location_code='JP', original_amount='2860.00',
            original_currency='JPY' if with_fx_evidence else None,
            direction='debit'),
        StatementTransactionRecord(
            uid='U887', bank_code='CMB', account_name='163box',
            txn_date='2026-08-20', post_date='2026-08-21',
            description='某商店退款', amount='-30.00', currency='CNY', direction='credit'),
        StatementTransactionRecord(
            uid='U887', bank_code='CMB', account_name='163box',
            txn_date='2026-08-25', post_date='2026-08-26',
            description='境外美元消费', amount='70.00', currency='USD',
            original_amount='10.00', original_currency='USD', direction='debit'),
    ])


class TestSnapshot(unittest.TestCase):
    def setUp(self):
        _mk_db(self)

    def test_totals_by_currency_direction(self):
        snap = snapshot(self.db, months=6)
        totals = {(t['currency'], t['direction']): t for t in snap['transaction_totals']}
        self.assertEqual(totals[('CNY', 'debit')]['amount'], '121.93')
        self.assertEqual(totals[('CNY', 'credit')]['amount'], '-30.00')   # 退款独立列示
        self.assertEqual(totals[('USD', 'debit')]['amount'], '70.00')
        self.assertEqual(snap['transaction_count'], 3)

    def test_unknown_original_currency_counted_not_guessed(self):
        snap = snapshot(self.db, months=6)
        # 仅 GOOGLE*CHATGPT 一笔有原币金额但无币种证据（USD 那笔有证据）
        self.assertEqual(snap['unknown_currency_original_amounts'], 1)
        tr = trends(self.db, months=6, merchant='GOOGLE*CHATGPT')
        m = tr['merchants'][0]
        self.assertIsNone(m['avg_fx_rate_sample'])      # 不能猜 JPY → 不计算折算比例
        self.assertEqual(m['months'][0]['currencies'][0]['debit_amount'], '121.93')

    def test_fx_rate_computed_only_with_evidence(self):
        _mk_db(self, with_fx_evidence=True)
        tr = trends(self.db, months=6, merchant='GOOGLE*CHATGPT')
        m = tr['merchants'][0]
        self.assertEqual(Decimal(m['avg_fx_rate_sample']), Decimal('0.042633'))
        self.assertIn('实际折算比例', m['fx_rate_note'])

    def test_balances_present(self):
        snap = snapshot(self.db, months=6)
        st = snap['statements'][0]
        self.assertEqual(st['balances'][0]['currency'], 'CNY')
        self.assertEqual(st['balances'][0]['total_due'], '500.00')

    def test_credit_net_not_double_negated(self):
        # amount 自带符号（credit 为负），净额 = 直接求和：121.93 - 30 + 70(CNY 之外不混)
        tr = trends(self.db, months=6, merchant='某商店退款')
        cur = tr['merchants'][0]['months'][0]['currencies'][0]
        self.assertEqual(cur['credit_amount'], '-30.00')
        self.assertEqual(cur['net'], '-30.00')

    def test_merchant_normalization(self):
        self.assertEqual(merchant_of(' google*chatgpt '), 'GOOGLE*CHATGPT')
        self.assertEqual(merchant_of('星巴克（上海）'), merchant_of('星巴克(上海)'))


class TestCoverageGapInReadmodel(unittest.TestCase):
    def test_missing_month_marked_not_zeroed(self):
        tmp = tempfile.mkdtemp()
        db = os.path.join(tmp, 't.db')
        sd.init_db(db)
        base = dict(uid='U1', message_id='<a@x>', bank_code='CMB', rule_id='r',
                    subject='s', sender='s', email_date='2026-07-10', account_name='163box',
                    statement_month='2026-07', total_due='100.00')
        sd.save_statement_with_transactions(db, StatementRecord(**base), [])
        # 只有 2026-07 一期：从首期到上个完整月无跳月（单期不产生缺口）
        snap = snapshot(db, months=12)
        self.assertEqual(snap['coverage_gaps'], [])
        # 加 2026-09 一期（跳过 2026-08）→ 2026-08 为缺口，且缺月不补零
        base2 = dict(base, uid='U2', message_id='<b@x>', statement_month='2026-09', email_date='2026-09-10')
        sd.save_statement_with_transactions(db, StatementRecord(**base2), [])
        snap2 = snapshot(db, months=12)
        self.assertIn({'account': '163box', 'bank': 'CMB', 'missing_month': '2026-08'},
                      snap2['coverage_gaps'])
        months_seen = {s['month'] for s in snap2['statements']}
        self.assertNotIn('2026-08', months_seen)


class TestDecimalAmount(unittest.TestCase):
    def test_rejects(self):
        self.assertIsNone(decimal_amount('NaN'))
        self.assertIsNone(decimal_amount('Infinity'))
        self.assertIsNone(decimal_amount('abc'))
        self.assertEqual(decimal_amount('1,234.56'), Decimal('1234.56'))


if __name__ == '__main__':
    unittest.main()
