#!/usr/bin/env python3
"""阶段 B 质量协议回归（重构契约 §2 / 任务清单回归矩阵）。

覆盖：表块去重与真实重数、字段来源状态三态区分、金额合法边界、币种未知、
结构指纹噪声免疫、事件幂等/ack/复发、幽灵不产生待办、疑似账单检测、
覆盖缺口 vs 合法零交易、statement_recorded 事件、is_paid 不回退。
全部使用隔离临时库，不触碰项目根 statements.db。
"""

import json
import os
import sys
import sqlite3
import tempfile
import unittest
from decimal import Decimal
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import statement_parser
import statement_db as sd
from statement_models import (StatementRecord, StatementTransactionRecord,
                               QualityEventRecord, Diagnostic)
import mail_quality as mq


class TempDbMixin:
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.db = os.path.join(self.tmp, 't.db')
        sd.init_db(self.db)
        patcher = mock.patch.object(statement_parser, 'DB_PATH', self.db)
        patcher.start()
        self.addCleanup(patcher.stop)

    def _mk_statement(self, **over):
        base = dict(uid='U1', message_id='<m1@x>', bank_code='CMB', rule_id='CMB_TEMPLATE_V1',
                    subject='招商银行信用卡电子账单', sender='ccsvc@message.cmbchina.com',
                    email_date='Sat, 10 Oct 2026 10:00:00 +0800', account_name='acc',
                    statement_month='2026-10', statement_date='2026-10-10', due_date='2026-10-28',
                    total_due='100.00', minimum_due='10.00')
        base.update(over)
        return StatementRecord(**base)


class TestTableBlockDedup(TempDbMixin, unittest.TestCase):
    """回归矩阵：重复表格按结构证据处理；相同内容两笔真实交易保留重数。"""

    # CMB 窄表 6 列（与真实解析器的列数要求一致）
    TABLE = ("| 10/01 | 10/02 | 星巴克 | 38.00 | CNY | CN |\n"
             "| 10/01 | 10/02 | 京东 | -99.00 | CNY | CN |\n")

    def test_duplicate_table_block_parsed_once(self):
        md = self.TABLE + "\n\n" + self.TABLE  # 同一封邮件重复渲染的表格
        rows = statement_parser.parse_cmb_transactions_from_markdown('U1', '2026-10', md)
        self.assertEqual(len(rows), 2)

    def test_identical_real_transactions_keep_multiplicity(self):
        table = ("| 10/01 | 10/02 | 星巴克 | 38.00 | CNY | CN |\n"
                 "| 10/01 | 10/02 | 星巴克 | 38.00 | CNY | CN |\n")  # 同表内两笔完全相同：真实两笔
        rows = statement_parser.parse_cmb_transactions_from_markdown('U1', '2026-10', table)
        self.assertEqual(len(rows), 2)

    def test_citic_text_rows_keep_multiplicity(self):
        body = ("20261001202601024234星巴克CNY38.00CNY38.00\n"
                "20261001202601024234星巴克CNY38.00CNY38.00\n")
        rows = statement_parser.parse_citic_transactions_from_body('U1', '2026-10', body)
        self.assertEqual(len(rows), 2)


class TestFieldSources(TempDbMixin, unittest.TestCase):
    """回归矩阵：extracted / source_missing / extraction_failed 三种原因准确区分。"""

    def test_diagnose_missing_field_matrix(self):
        d1 = mq.diagnose_missing_field('due_date', 'source_missing', '原文未提供')
        self.assertEqual((d1.code, d1.severity, d1.affected_purposes),
                         ('SOURCE_FIELD_MISSING', 'warning', ['repayment_reminder']))
        d2 = mq.diagnose_missing_field('total_due', 'extraction_failed', '提取失败')
        self.assertEqual((d2.code, d2.severity, d2.affected_purposes),
                         ('EXTRACTION_FAILED', 'error', ['financial_summary', 'repayment_reminder']))
        # unknown 保留 unknown 并发出待核查诊断；inferred/extracted 都不诊断
        d3 = mq.diagnose_missing_field('due_date', 'unknown', 'x')
        self.assertEqual((d3.code, d3.severity, d3.affected_purposes),
                         ('FIELD_SOURCE_UNKNOWN', 'warning', ['repayment_reminder']))
        self.assertIsNone(mq.diagnose_missing_field('due_date', 'inferred', 'x'))
        self.assertIsNone(mq.diagnose_missing_field('due_date', 'extracted', 'x'))

    def test_extract_meta_marks_inferred_for_fallback(self):
        rule = {
            'rule_id': 'CMB_TEMPLATE_V1', 'template_version': 'CMB_TEMPLATE_V1.20261011',
            'extract_rules': {'statement_fields': {
                'statement_month': {'pattern': r'(\d{6})', 'group': 1},
                'due_date': {'pattern': r'不存在的标签[:：](\d+)', 'group': 1},
            }},
            'normalize_rules': {'date_formats': ['%Y-%m-%d'], 'amount_strip_tokens': []},
        }
        fields, meta = statement_parser.extract_statement_by_rule(
            rule, 'subj', '202610 正文无还款日标签', return_meta=True)
        self.assertEqual(meta['statement_month']['source'], 'extracted')
        self.assertEqual(meta['due_date']['source'], 'unknown')
        self.assertEqual(meta['due_date']['value'], None)


class TestAmountBoundaries(unittest.TestCase):
    """回归矩阵：0、负数按业务处理；空串/NaN/Infinity 不得作为合法金额。"""

    def test_decimal_amount_rejects_invalid(self):
        from billing_readmodel import decimal_amount
        self.assertEqual(decimal_amount('0'), Decimal('0'))
        self.assertEqual(decimal_amount('-1101.70'), Decimal('-1101.70'))
        self.assertIsNone(decimal_amount('NaN'))
        self.assertIsNone(decimal_amount('Infinity'))
        self.assertIsNone(decimal_amount('-Infinity'))
        self.assertIsNone(decimal_amount(''))
        self.assertIsNone(decimal_amount(None))


class TestStructureFingerprint(unittest.TestCase):
    """回归矩阵：广告/样式/金额变化不产生新指纹；未知结构变化可比较基线。"""

    def _md(self, amount):
        return ("| 交易日 | 记账日 | 交易描述 | 人民币金额 |\n"
                f"| --- | --- | --- | --- |\n"
                f"| 10/01 | 10/02 | 商户 | {amount} |\n")

    def test_amount_change_keeps_fingerprint(self):
        blocks1 = mq.split_markdown_table_blocks(self._md('38.00'))
        blocks2 = mq.split_markdown_table_blocks(self._md('999.00'))
        self.assertEqual(mq.structure_fingerprint(blocks1), mq.structure_fingerprint(blocks2))

    def test_ad_style_change_keeps_fingerprint(self):
        md1 = self._md('38.00') + '\n广告：限时优惠\n'
        md2 = '促销横幅\n' + self._md('38.00')
        self.assertEqual(mq.structure_fingerprint(mq.split_markdown_table_blocks(md1)),
                         mq.structure_fingerprint(mq.split_markdown_table_blocks(md2)))

    def test_column_change_alters_fingerprint(self):
        md2 = ("| 交易日 | 记账日 | 交易描述 | 人民币金额 | 手续费 |\n"
               "| --- | --- | --- | --- | --- |\n"
               "| 10/01 | 10/02 | 商户 | 38.00 | 0.00 |\n")
        fp1 = mq.structure_fingerprint(mq.split_markdown_table_blocks(self._md('38.00')))
        fp2 = mq.structure_fingerprint(mq.split_markdown_table_blocks(md2))
        self.assertNotEqual(fp1, fp2)

    def test_format_changed_only_with_baseline(self):
        fp = mq.structure_fingerprint(mq.split_markdown_table_blocks(self._md('38.00')))
        self.assertEqual(mq.diagnose_format(fp, {'quality': {}}), [])  # 无基线：静默
        other_fp = {'table_headers': [['别的', '列']], 'column_counts': [2], 'table_block_count': 1}
        diags = mq.diagnose_format(other_fp, {'quality': {
            'accepted_structure_fingerprints': [fp]}}, None)
        self.assertEqual(len(diags), 1)
        self.assertEqual(diags[0].code, 'FORMAT_CHANGED')


class TestSuspiciousStatement(unittest.TestCase):
    """回归矩阵：未知账单模板 → 保留证据 + 质量 Todo；已知非账单不制造待办。"""

    def test_suspicious_detected(self):
        rules = [{'bank_code': 'CMB'}]
        d = mq.detect_suspicious_statement('ccsvc@message.cmbchina.com', '您10月账单已出',
                                            '|a|b|c|d|e|f|\n应还金额 ¥1,234.56', rules)
        self.assertIsNotNone(d)
        self.assertEqual(d.code, 'UNKNOWN_STATEMENT_TEMPLATE')

    def test_plain_bank_marketing_not_suspicious(self):
        rules = [{'bank_code': 'CMB'}]
        self.assertIsNone(mq.detect_suspicious_statement(
            'ccsvc@message.cmbchina.com', '双11刷卡金活动', '满减优惠', rules))


class TestQualityEventLifecycle(TempDbMixin, unittest.TestCase):
    """回归矩阵：同一异常重复/并发/重启重投 → 一条 Todo；影响计数按稳定身份去重。"""

    def _event(self, case_key='k1', event_type='quality_detected', uid='U1'):
        return QualityEventRecord(
            case_key=case_key, event_type=event_type, account_name='acc',
            bank_code='CMB', template_version='CMB_TEMPLATE_V1.20261011',
            diagnostics=[Diagnostic(code='CURRENCY_UNKNOWN', field='original_currency',
                                    reason='r', affected_purposes=['fx_analysis'])],
            evidence_refs=[{'kind': 'statement', 'uid': uid}])

    def test_repeated_scans_update_not_insert(self):
        for _ in range(3):
            sd.upsert_quality_event(self.db, self._event())
        events = sd.get_unacked_quality_events(self.db)
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]['occurrence_count'], 1)
        self.assertEqual(events[0]['revision'], 1)
        self.assertEqual(events[0]['event_type'], 'quality_detected')

    def test_ack_idempotent_and_relapse_reopens(self):
        eid = sd.upsert_quality_event(self.db, self._event())
        first = sd.ack_quality_event(self.db, eid, 'T1', revision=1)
        again = sd.ack_quality_event(self.db, eid, 'T1', revision=1)
        self.assertTrue(first['acked'])
        self.assertEqual(again['reason'], 'already_acked')
        self.assertEqual(len(sd.get_unacked_quality_events(self.db)), 0)
        # Delivery ack is not resolution, and re-scanning old evidence is silent.
        sd.upsert_quality_event(self.db, self._event())
        self.assertEqual(sd.get_unacked_quality_events(self.db), [])
        sd.upsert_quality_event(self.db, self._event(event_type='quality_resolved'))
        sd.upsert_quality_event(self.db, self._event(uid='U2'))
        reopened = sd.get_unacked_quality_events(self.db)
        self.assertEqual(len(reopened), 1)
        self.assertEqual(reopened[0]['event_id'], eid)

    def test_case_key_isolated_by_account(self):
        k1 = mq.make_case_key('acc1', 'CMB', 'v1', 'CURRENCY_UNKNOWN', 'original_currency')
        k2 = mq.make_case_key('acc2', 'CMB', 'v1', 'CURRENCY_UNKNOWN', 'original_currency')
        self.assertNotEqual(k1, k2)  # 不同账户不共享 case（证据不互相泄露）

    def test_evidence_refs_deduped_across_repeated_scans(self):
        """R3 契约：重复扫描同一封邮件证据不重复累加，不同邮件证据合并去重。"""
        ev1 = QualityEventRecord(
            case_key='k_dedup', event_type='quality_detected', account_name='acc',
            bank_code='CMB', template_version='v1',
            diagnostics=[Diagnostic(code='ERR', field='f', reason='r')],
            evidence_refs=[{'kind': 'statement', 'account': 'acc', 'uid': '101', 'message_id': '<m1@b>'}]
        )
        sd.upsert_quality_event(self.db, ev1)
        # 重复扫描完全相同的邮件
        sd.upsert_quality_event(self.db, ev1)
        # 扫描第二封不同邮件
        ev2 = QualityEventRecord(
            case_key='k_dedup', event_type='quality_detected', account_name='acc',
            bank_code='CMB', template_version='v1',
            diagnostics=[Diagnostic(code='ERR', field='f', reason='r')],
            evidence_refs=[{'kind': 'statement', 'account': 'acc', 'uid': '102', 'message_id': '<m2@b>'}]
        )
        sd.upsert_quality_event(self.db, ev2)

        events = sd.get_unacked_quality_events(self.db)
        self.assertEqual(len(events), 1)
        e = events[0]
        # occurrence_count counts unique affected emails, not scans
        self.assertEqual(e['occurrence_count'], 2)
        # 证据引用按 (account, uid, message_id) 严格去重，仅 2 项
        import json
        refs = json.loads(e['evidence_refs_json'])
        self.assertEqual(len(refs), 2)
        uids = {r['uid'] for r in refs}
        self.assertEqual(uids, {'101', '102'})

    def test_monotonic_revision_and_stale_ack_rejection(self):
        """R3 契约：单调递增 revision，旧版本确认拒绝，防止吞掉新事件。"""
        eid = sd.upsert_quality_event(self.db, self._event('k_rev'))
        events = sd.get_unacked_quality_events(self.db)
        self.assertEqual(events[0]['revision'], 1)
        self.assertEqual(events[0]['cycle_id'], 1)

        # 生产者更新事件（发生未解决问题更新）
        sd.upsert_quality_event(self.db, self._event('k_rev', uid='U2'))
        events = sd.get_unacked_quality_events(self.db)
        self.assertEqual(events[0]['revision'], 2)

        # 消费者试图用旧 revision=1 确认 -> 必须拒绝
        stale_ack = sd.ack_quality_event(self.db, eid, 'T_STALE', revision=1)
        self.assertFalse(stale_ack['acked'])
        self.assertEqual(stale_ack['reason'], 'stale_revision')
        self.assertEqual(stale_ack['current_revision'], 2)

        # 事件仍然未被确认，继续对消费者可见
        self.assertEqual(len(sd.get_unacked_quality_events(self.db)), 1)

        # 用匹配的最新 revision=2 确认 -> 成功
        good_ack = sd.ack_quality_event(self.db, eid, 'T_GOOD', revision=2)
        self.assertTrue(good_ack['acked'])
        self.assertEqual(len(sd.get_unacked_quality_events(self.db)), 0)

    def test_relapse_increments_cycle_id_and_reopens(self):
        """Only explicit resolution followed by new evidence opens a new cycle."""
        eid = sd.upsert_quality_event(self.db, self._event('k_cycle'))
        sd.ack_quality_event(self.db, eid, 'T1', revision=1)
        self.assertEqual(len(sd.get_unacked_quality_events(self.db)), 0)

        sd.upsert_quality_event(self.db, self._event('k_cycle', event_type='quality_resolved'))
        # New email after resolution, rather than the same old observation.
        sd.upsert_quality_event(self.db, self._event('k_cycle', uid='U2'))
        events = sd.get_unacked_quality_events(self.db)
        self.assertEqual(len(events), 1)
        reopened = events[0]
        self.assertEqual(reopened['event_id'], eid)
        self.assertEqual(reopened['cycle_id'], 2)
        self.assertEqual(reopened['revision'], 3)
        self.assertIsNone(reopened['acked_at'])

    def test_quality_resolved_lifecycle(self):
        """R3 契约：显式解决事件将类型置为 quality_resolved，清除诊断，revision 递增。"""
        eid = sd.upsert_quality_event(self.db, self._event('k_res'))
        res_event = QualityEventRecord(
            case_key='k_res', event_type='quality_resolved', account_name='acc',
            bank_code='CMB', template_version='v1', diagnostics=[], evidence_refs=[]
        )
        sd.upsert_quality_event(self.db, res_event)
        events = sd.get_unacked_quality_events(self.db)
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]['event_type'], 'quality_resolved')
        self.assertEqual(events[0]['revision'], 2)

    def test_uid_drift_and_report_path_do_not_count_as_new_email(self):
        event = self._event('drift')
        event.evidence_refs = [{'account':'acc','uid':'1','message_id':'<m@b>','report_path':'one'}]
        eid = sd.upsert_quality_event(self.db,event)
        sd.ack_quality_event(self.db,eid,'T',revision=1)
        event.evidence_refs[0].update(uid='999',report_path='two')
        sd.upsert_quality_event(self.db,event)
        self.assertEqual(sd.get_unacked_quality_events(self.db),[])
        conn=sqlite3.connect(self.db)
        self.assertEqual(conn.execute('SELECT revision,cycle_id,occurrence_count FROM quality_events').fetchone(),(1,1,1))
        conn.close()

    def test_exact_ack_rejects_missing_future_and_wrong_owner(self):
        eid=sd.upsert_quality_event(self.db,self._event())
        self.assertEqual(sd.ack_quality_event(self.db,eid,'T')['reason'],'revision_required')
        self.assertFalse(sd.ack_quality_event(self.db,eid,'T',revision=2)['acked'])
        self.assertTrue(sd.ack_quality_event(self.db,eid,'T',revision=1)['acked'])
        self.assertFalse(sd.ack_quality_event(self.db,eid,'T',revision=2)['acked'])
        self.assertFalse(sd.ack_quality_event(self.db,eid,'other',revision=1)['acked'])

    def test_new_evidence_after_ack_stays_same_unresolved_cycle(self):
        eid=sd.upsert_quality_event(self.db,self._event())
        sd.ack_quality_event(self.db,eid,'T',revision=1)
        sd.upsert_quality_event(self.db,self._event(uid='U2'))
        event=sd.get_unacked_quality_events(self.db)[0]
        self.assertEqual((event['revision'],event['cycle_id'],event['occurrence_count']),(2,1,2))
        self.assertFalse(sd.ack_quality_event(self.db,eid,'T',revision=1)['acked'])

    def test_old_observation_and_resolution_cannot_reopen_or_close_new_cycle(self):
        sd.upsert_quality_event(self.db,self._event())
        resolved=self._event(event_type='quality_resolved')
        sd.upsert_quality_event(self.db,resolved)
        sd.upsert_quality_event(self.db,self._event())
        self.assertEqual(sd.get_unacked_quality_events(self.db)[0]['event_type'],'quality_resolved')
        sd.upsert_quality_event(self.db,self._event(uid='U2'))
        with self.assertRaises(ValueError):
            sd.upsert_quality_event(self.db,resolved)
        event=sd.get_unacked_quality_events(self.db)[0]
        self.assertEqual((event['event_type'],event['cycle_id'],event['revision']),('quality_detected',2,3))

    def test_statement_updates_after_ack_keep_type_and_do_not_starve_quality(self):
        statement=self._event('statement',event_type='statement_recorded')
        eid=sd.upsert_quality_event(self.db,statement)
        sd.ack_quality_event(self.db,eid,'B',revision=1)
        statement.evidence_refs[0]['due_date']='2026-12-01'
        sd.upsert_quality_event(self.db,statement)
        sd.upsert_quality_event(self.db,self._event('quality'))
        all_events=sd.get_unacked_quality_events(self.db)
        self.assertIn('statement_recorded',{e['event_type'] for e in all_events})
        only_quality=sd.get_unacked_quality_events(self.db,limit=1,event_types=('quality_detected',))
        self.assertEqual(only_quality[0]['case_key'],'quality')

    def test_concurrent_same_observation_is_one_revision(self):
        from concurrent.futures import ThreadPoolExecutor
        with ThreadPoolExecutor(max_workers=6) as executor:
            list(executor.map(lambda _: sd.upsert_quality_event(self.db,self._event()), range(18)))
        event=sd.get_unacked_quality_events(self.db)[0]
        self.assertEqual((event['revision'],event['cycle_id'],event['occurrence_count']),(1,1,1))

    def test_changed_content_same_message_can_recur_but_not_inflate_email_count(self):
        event=self._event('content')
        event.evidence_refs[0]['content_hash']='old'
        sd.upsert_quality_event(self.db,event)
        sd.upsert_quality_event(self.db,self._event('content',event_type='quality_resolved'))
        event.evidence_refs[0]['content_hash']='new'
        sd.upsert_quality_event(self.db,event)
        row=sd.get_unacked_quality_events(self.db)[0]
        self.assertEqual((row['revision'],row['cycle_id'],row['occurrence_count']),(3,2,1))

    def test_concurrent_schema_migrations_are_atomic_and_idempotent(self):
        from concurrent.futures import ThreadPoolExecutor
        with ThreadPoolExecutor(max_workers=6) as executor:
            list(executor.map(lambda _: sd.init_db(self.db),range(12)))
        sd.upsert_quality_event(self.db,self._event())
        self.assertEqual(sd.get_unacked_quality_events(self.db)[0]['revision'],1)

    def test_statement_field_change_back_to_prior_value_is_deliverable(self):
        event=self._event('statement-back',event_type='statement_recorded')
        event.evidence_refs[0]['due_date']='2026-10-28'
        eid=sd.upsert_quality_event(self.db,event)
        sd.ack_quality_event(self.db,eid,'B',revision=1)
        event.evidence_refs[0]['due_date']='2026-10-29'
        sd.upsert_quality_event(self.db,event)
        sd.ack_quality_event(self.db,eid,'B',revision=2)
        event.evidence_refs[0]['due_date']='2026-10-28'
        sd.upsert_quality_event(self.db,event)
        row=sd.get_unacked_quality_events(self.db)[0]
        self.assertEqual((row['event_type'],row['revision'],row['cycle_id']),('statement_recorded',3,1))


class TestCoverageGap(TempDbMixin, unittest.TestCase):
    """回归矩阵：合法零交易账单不告警；声明有交易但零解析报覆盖缺口。"""

    def test_legal_zero_txn_no_gap(self):
        self.assertEqual(mq.diagnose_coverage({'valid_row_count': 0, 'candidate_row_count': 0,
                                                'rejected_row_count': 0, 'table_block_count': 0}), [])

    def test_coverage_gap_when_rows_rejected(self):
        diags = mq.diagnose_coverage({'declared_txn_count': 10, 'valid_row_count': 0,
                                      'candidate_row_count': 10, 'rejected_row_count': 10,
                                      'table_block_count': 1})
        self.assertEqual([d.code for d in diags], ['TRANSACTION_COVERAGE_GAP'])

    def test_partial_rejection_warns(self):
        diags = mq.diagnose_coverage({'valid_row_count': 8, 'candidate_row_count': 10,
                                      'rejected_row_count': 2, 'table_block_count': 1})
        self.assertEqual([d.code for d in diags], ['TRANSACTION_ROW_INVALID'])


class TestCurrencyUnknown(TempDbMixin, unittest.TestCase):
    """回归矩阵：121.93 CNY / 2860.00 / JP 无原币币种 → 未知，不能猜 JPY。"""

    def test_currency_unknown_diagnostic(self):
        diags = mq.diagnose_currency({'original_amount_count': 1, 'original_currency_known': 0})
        self.assertEqual(len(diags), 1)
        self.assertEqual(diags[0].code, 'CURRENCY_UNKNOWN')
        self.assertEqual(diags[0].affected_purposes, ['fx_analysis'])
        self.assertEqual(mq.diagnose_currency({'original_amount_count': 0}), [])
        self.assertEqual(mq.diagnose_currency({'original_amount_count': 1,
                                              'original_currency_known': 1}), [])


class TestStatementRecordedAndPaidProtection(TempDbMixin, unittest.TestCase):
    """回归矩阵：新账单重复同步 → 一条还款任务；已还后再同步状态不回退。"""

    def test_statement_recorded_once_and_revisible_on_change(self):
        s = self._mk_statement()
        sd.save_statement_with_transactions(self.db, s, [])
        evs = sd.get_unacked_quality_events(self.db)
        recorded = [e for e in evs if e['event_type'] == 'statement_recorded']
        self.assertEqual(len(recorded), 1)
        # 同值重复保存：不重新触发（occurrence 不变）
        sd.save_statement_with_transactions(self.db, s, [])
        evs2 = sd.get_unacked_quality_events(self.db)
        rec2 = [e for e in evs2 if e['event_type'] == 'statement_recorded'][0]
        self.assertEqual(rec2['occurrence_count'], 1)
        # due_date 实质变化 → 重新可见
        s2 = self._mk_statement(due_date='2026-11-05')
        sd.save_statement_with_transactions(self.db, s2, [])
        evs3 = sd.get_unacked_quality_events(self.db)
        rec3 = [e for e in evs3 if e['event_type'] == 'statement_recorded'][0]
        self.assertEqual(rec3['occurrence_count'], 1)
        self.assertEqual(rec3['revision'], 2)

    def test_is_paid_not_regressed_on_resave(self):
        s = self._mk_statement()
        sid, _ = sd.save_statement_with_transactions(self.db, s, [])
        sd.mark_statement_paid_by_id(self.db, sid)
        # 重解析同账单（due_date/total_due 不变）→ is_paid 保持
        sd.save_statement_with_transactions(self.db, s, [])
        conn = sqlite3.connect(self.db)
        row = conn.execute('SELECT is_paid FROM statements WHERE id=?', (sid,)).fetchone()
        conn.close()
        self.assertEqual(row[0], 1)

    def test_mark_paid_by_id_precise(self):
        s1 = self._mk_statement(uid='U1', message_id='<a@x>')
        s2 = self._mk_statement(uid='U2', message_id='<b@x>')
        id1, _ = sd.save_statement_with_transactions(self.db, s1, [])
        id2, _ = sd.save_statement_with_transactions(self.db, s2, [])
        n = sd.mark_statement_paid_by_id(self.db, id2)
        self.assertEqual(n, 1)
        conn = sqlite3.connect(self.db)
        paid = dict(conn.execute('SELECT id, is_paid FROM statements').fetchall())
        conn.close()
        self.assertEqual(paid[id1], 0)   # 同银行同月份的另一张卡不被误标
        self.assertEqual(paid[id2], 1)
        self.assertEqual(sd.mark_statement_paid_by_id(self.db, id2), 0)  # 幂等


class TestGhostProducesNoTodo(TempDbMixin, unittest.TestCase):
    """回归矩阵：CMB 每日信用管家 → 非账单，不入库，不制造未知账单待办。"""

    def test_daily_credit_report_gated_and_quiet(self):
        rules = statement_parser.load_rule_files()
        subj, sender = '每日信用管家', 'ccsvc@message.cmbchina.com'
        rule, _ = statement_parser.identify_rule(subj, sender, '正文', rules)
        self.assertIsNone(rule)  # 主题门禁拦截
        # 且不被判为"疑似账单"（无表格/账单关键词）
        self.assertIsNone(mq.detect_suspicious_statement(sender, subj, '正文', rules))


class TestMigrationIdempotent(unittest.TestCase):
    """回归矩阵：schema 二次迁移幂等；旧库迁移保留数据并标记 legacy_assumed。"""

    def _legacy_db(self):
        tmp = tempfile.mkdtemp()
        db = os.path.join(tmp, 'legacy.db')
        conn = sqlite3.connect(db)
        conn.executescript("""
            CREATE TABLE statements (
                id INTEGER PRIMARY KEY AUTOINCREMENT, uid TEXT NOT NULL, message_id TEXT,
                bank_code TEXT NOT NULL, rule_id TEXT NOT NULL, subject TEXT, sender TEXT,
                email_date TEXT, statement_month TEXT, statement_date TEXT, due_date TEXT,
                total_due TEXT, minimum_due TEXT, raw_fields_json TEXT,
                is_paid INTEGER NOT NULL DEFAULT 0, paid_at TEXT,
                created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
            CREATE TABLE statement_transactions (
                id INTEGER PRIMARY KEY AUTOINCREMENT, uid TEXT NOT NULL, bank_code TEXT NOT NULL,
                txn_date TEXT, post_date TEXT, description TEXT, amount TEXT, currency TEXT,
                txn_location_code TEXT, original_amount TEXT, direction TEXT,
                raw_row_json TEXT, created_at TEXT NOT NULL);
            CREATE TABLE validation_runs (
                id INTEGER PRIMARY KEY AUTOINCREMENT, uid TEXT NOT NULL, bank_code TEXT NOT NULL,
                rule_id TEXT NOT NULL, passed INTEGER NOT NULL, error_count INTEGER NOT NULL,
                warning_count INTEGER NOT NULL, report_path TEXT, run_at TEXT NOT NULL);
            CREATE TABLE email_summaries (
                id INTEGER PRIMARY KEY AUTOINCREMENT, account_name TEXT NOT NULL, uid TEXT NOT NULL,
                sender TEXT, subject TEXT, email_date TEXT, status TEXT DEFAULT 'pending',
                retry_count INTEGER DEFAULT 0, created_at TEXT NOT NULL);
        """)
        conn.execute("INSERT INTO statements(uid, bank_code, rule_id, total_due, created_at, updated_at) "
                     "VALUES('U1','CMB','CMB_TEMPLATE_V1','100.00','2026-01-01','2026-01-01')")
        conn.execute("INSERT INTO statement_transactions(uid, bank_code, amount, currency, created_at) "
                     "VALUES('U1','CMB','38.00','CNY','2026-01-01')")
        conn.commit()
        conn.close()
        return db

    def test_legacy_migration_marks_and_preserves(self):
        db = self._legacy_db()
        sd.init_db(db)
        sd.init_db(db)  # 幂等：二跑无异常
        conn = sqlite3.connect(db)
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM statements WHERE account_name='__orphan__'").fetchone()[0], 1)
        self.assertEqual(conn.execute(
            "SELECT currency_source FROM statement_transactions").fetchone()[0], 'legacy_assumed')
        bal = conn.execute("SELECT currency, currency_source, total_due FROM statement_balances").fetchone()
        conn.close()
        self.assertEqual(bal[0], 'CNY')
        self.assertEqual(bal[1], 'legacy_assumed')   # 不把旧 CNY 当已核实证据
        self.assertEqual(bal[2], '100.00')


class TestLease(unittest.TestCase):
    """跨进程租约：互斥、过期让位、仅持有者可释放。"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.db = os.path.join(self.tmp, 't.db')
        sd.init_db(self.db)

    def test_mutex_and_owner(self):
        self.assertTrue(sd.acquire_lease(self.db, 'scan:a', 'o1', 60))
        self.assertFalse(sd.acquire_lease(self.db, 'scan:a', 'o2', 60))
        self.assertFalse(sd.release_lease(self.db, 'scan:a', 'o2'))   # 非持有者不能释放
        self.assertTrue(sd.release_lease(self.db, 'scan:a', 'o1'))
        self.assertTrue(sd.acquire_lease(self.db, 'scan:a', 'o2', 60))

    def test_expired_lease_recovers(self):
        self.assertTrue(sd.acquire_lease(self.db, 'scan:a', 'o1', ttl_seconds=-1))  # 已过期
        self.assertTrue(sd.acquire_lease(self.db, 'scan:a', 'o2', 60))  # 过期自动让位


class TestEndToEndSave(TempDbMixin, unittest.TestCase):
    """端到端：CMB 账单邮件 → 解析入库 + 币种未知诊断 + statement_recorded 事件。"""

    def test_cmb_email_full_flow(self):
        report_dir = os.path.join(self.tmp, 'reports')
        with mock.patch.object(statement_parser, 'VALIDATION_REPORT_DIR', report_dir):
            from email.message import EmailMessage
            msg = EmailMessage()
            msg['Subject'] = '招商银行信用卡电子账单'
            msg['From'] = 'ccsvc@message.cmbchina.com'
            msg['Date'] = 'Sat, 10 Oct 2026 10:00:00 +0800'
            msg['Message-ID'] = '<e2e@x>'
            plain = '2026/09/11 - 2026/10/10\n到期还款日：2026/10/28\n'
            html = (
                '<html><body>'
                '<p>2026/09/11 - 2026/10/10</p>'
                '<p>到期还款日：2026/10/28</p>'
                '<table><tr><td>本期应还金额</td><td></td><td></td><td></td>'
                '<td>¥100.00</td><td></td><td>¥10.00</td></tr>'
                '<tr><td>上期应还金额</td><td></td><td></td><td></td>'
                '<td>¥0.00</td><td></td><td>¥0.00</td></tr></table>'
                '<table><tr><td>序号</td><td>交易日</td><td>记账日</td><td>交易描述</td>'
                '<td>人民币金额</td><td>币种</td><td>地区</td><td>原币金额</td></tr>'
                '<tr><td>1</td><td>1001</td><td>1002</td><td>GOOGLE*CHATGPT</td>'
                '<td>121.93</td><td>CNY</td><td>JP</td><td>2860.00</td></tr></table>'
                '</body></html>'
            )
            msg.set_content(plain)
            msg.add_alternative(html, subtype='html')
            rules = statement_parser.load_rule_files()
            result = statement_parser.validate_and_save_email_message(
                msg, 'U-E2E', rules=rules, account_name='acc')
        self.assertEqual(result.get('status'), 'saved')
        self.assertEqual(result.get('txn_count'), 1)
        codes = {d['code'] for d in result.get('diagnostics') or []}
        self.assertIn('CURRENCY_UNKNOWN', codes)  # 2860 原币无币种证据
        events = sd.get_unacked_quality_events(self.db)
        types = {e['event_type'] for e in events}
        self.assertIn('statement_recorded', types)
        self.assertIn('quality_detected', types)
        # 入账币种证据：CMB 未声明 settlement_currency → currency_source 保持 NULL（未知）
        conn = sqlite3.connect(self.db)
        cur_row = conn.execute('SELECT currency, currency_source, original_amount, original_currency '
                               'FROM statement_transactions').fetchone()
        conn.close()
        self.assertEqual(cur_row[0], 'CNY')
        self.assertEqual(cur_row[1], 'extracted')
        self.assertEqual(cur_row[2], '2860.00')
        self.assertIsNone(cur_row[3])

    def test_reparse_failure_keeps_valid_data(self):
        """回归矩阵：已有有效账单重解析失败 → 原有效账单、交易、还款状态完整不变。"""
        s = self._mk_statement()
        sid, _ = sd.save_statement_with_transactions(
            self.db, s,
            [StatementTransactionRecord(uid='U1', bank_code='CMB', account_name='acc',
                                        txn_date='2026-10-01', description='星巴克',
                                        amount='38.00', direction='debit')])
        sd.mark_statement_paid_by_id(self.db, sid)
        # 同 Message-ID 不同 uid 重放 → 身份冲突拒绝，不覆盖
        s2 = self._mk_statement(uid='U2', total_due='999.00')
        with self.assertRaises(sd.MessageIdentityConflict):
            sd.save_statement_with_transactions(self.db, s2, [])
        conn = sqlite3.connect(self.db)
        row = conn.execute('SELECT total_due, is_paid FROM statements WHERE id=?', (sid,)).fetchone()
        txns = conn.execute('SELECT COUNT(*) FROM statement_transactions WHERE statement_id=?', (sid,)).fetchone()[0]
        conn.close()
        self.assertEqual(row[0], '100.00')
        self.assertEqual(row[1], 1)
        self.assertEqual(txns, 1)


if __name__ == '__main__':
    unittest.main()
