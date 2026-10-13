"""R6 deterministic evidence regressions; no external mail access."""
import json
import unittest
from pathlib import Path
from statement_evidence import currency_evidence, field_meta, number, parse_semantic_transactions, purpose_availability, confirmed_currency
from statement_parser import _safe_decimal, _infer_date_from_any
from statement_models import Diagnostic
from mail_quality import fingerprint_matches_baseline

class TestR6Evidence(unittest.TestCase):
    def test_currency_requires_a_complete_unambiguous_token(self):
        self.assertEqual(currency_evidence('CNY 121.93'),'CNY')
        for value in ('USDfoo','USD/CNY','JP','¥','XYZ'):
            self.assertIsNone(currency_evidence(value))

    def test_finite_zero_and_negative_amounts(self):
        for parse in (number,_safe_decimal):
            self.assertEqual(parse('0'),0)
            self.assertEqual(parse('-1'),-1)
            for text in ('NaN','Infinity','-Infinity','bad'):
                self.assertIsNone(parse(text))

    def test_field_source_three_states_keep_evidence(self):
        cfg={'source_probe':{'label_pattern':'Due Date','region_pattern':'Statement'}}
        args=('due_date',None,None,None)
        self.assertEqual(field_meta(*args,cfg,'Statement: none')['source'],'source_missing')
        self.assertEqual(field_meta(*args,cfg,'Statement Due Date: bad')['source'],'extraction_failed')
        self.assertEqual(field_meta(*args,cfg,'unidentified region')['source'],'unknown')
        bad=field_meta('due_date',None,'bad','Due Date: bad',cfg,'Statement')
        self.assertEqual(bad['source'],'extraction_failed')
        self.assertEqual(bad['evidence']['raw_value'],'bad')

    def test_semantic_columns_preserve_real_duplicate_rows(self):
        md='| 交易描述 | 人民币金额 | 交易日期 | 原币金额 | 原币币种 |\n| SHOP | 10 | 1001 | 2 | USD |\n| SHOP | 10 | 1001 | 2 | USD |'
        rows,stats,diags=parse_semantic_transactions('u','2026-10',md,{'bank_code':'CMB'},_infer_date_from_any)
        self.assertEqual(len(rows),2)
        self.assertEqual(rows[0].currency,'CNY')
        self.assertEqual(rows[0].original_currency,'USD')
        self.assertEqual(stats['candidate_row_count'],2)
        self.assertFalse(diags)

    def test_missing_date_is_a_rejected_detail_region(self):
        md='| 交易描述 | 人民币金额 |\n| SHOP | 10 |'
        rows,stats,diags=parse_semantic_transactions('u','2026-10',md,{'bank_code':'CMB'},_infer_date_from_any)
        self.assertEqual(rows,[])
        self.assertEqual(stats['rejected_row_count'],1)
        self.assertEqual(diags[0].code,'TRANSACTION_ROW_INVALID')

    def test_gate_never_downgrades_blocked_on_later_warning(self):
        diags=[Diagnostic(code='TRANSACTION_COVERAGE_GAP',affected_purposes=['transaction_trends']),
               Diagnostic(code='FORMAT_CHANGED',affected_purposes=['transaction_trends'])]
        self.assertEqual(purpose_availability(diags)['transaction_trends']['status'],'blocked')

    _HX_SYMBOL_RULE={'bank_code':'HX','quality':{'settlement_cell_symbol':{'￥':'CNY','¥':'CNY'}}}

    def test_settlement_cell_symbol_confirms_cny_only_for_opted_in_templates(self):
        md='| 交易日期 | 交易描述 | 交易金额 |\n| 1001 | SHOP | ￥24.80 |'
        rows,_,_=parse_semantic_transactions('u','2026-10',md,self._HX_SYMBOL_RULE,_infer_date_from_any)
        self.assertEqual(rows[0].currency,'CNY')
        self.assertEqual(rows[0].currency_source,'cell_symbol')
        self.assertTrue(confirmed_currency(rows[0].currency,rows[0].currency_source))
        # 窄规则: 模板未显式声明符号映射时不得启用
        plain,_,_=parse_semantic_transactions('u','2026-10',md,{'bank_code':'HX'},_infer_date_from_any)
        self.assertEqual(plain[0].currency,'UNKNOWN')
        self.assertIsNone(plain[0].currency_source)

    def test_explicit_jpy_is_never_overridden_by_yen_symbol(self):
        md='| 交易日期 | 交易描述 | 交易金额 | 币种 |\n| 1001 | SHOP | ￥100 | JPY |'
        rows,_,_=parse_semantic_transactions('u','2026-10',md,self._HX_SYMBOL_RULE,_infer_date_from_any)
        self.assertEqual(rows[0].currency,'JPY')
        self.assertEqual(rows[0].currency_source,'extracted')

    def test_conflicting_explicit_currencies_never_fall_through_to_symbol(self):
        md='| 交易日期 | 交易描述 | 人民币金额 | 币种 |\n| 1001 | SHOP | ￥100 | JPY |'
        rows,_,_=parse_semantic_transactions('u','2026-10',md,self._HX_SYMBOL_RULE,_infer_date_from_any)
        self.assertEqual(rows[0].currency,'UNKNOWN')
        self.assertIsNone(rows[0].currency_source)

    def test_original_currency_never_derives_from_location_or_symbol(self):
        md='| 交易日期 | 交易描述 | 交易金额 | 地区 | 原币金额 |\n| 1001 | GOOGLE | ¥ 125.55 | JP | 2,860.00 |'
        rows,_,_=parse_semantic_transactions('u','2026-10',md,self._HX_SYMBOL_RULE,_infer_date_from_any)
        self.assertEqual(rows[0].currency,'CNY')
        self.assertEqual(rows[0].currency_source,'cell_symbol')
        self.assertIsNone(rows[0].original_currency)
        self.assertIsNone(rows[0].original_currency_source)
        self.assertEqual(rows[0].txn_location_code,'JP')

    def test_non_currency_code_cell_allows_symbol_only_for_opted_in_templates(self):
        # CMB 八列契约: "币种"语义列实际是商户代码(如 5510), 无币种证据;
        # 声明符号映射的模板用结算单元格符号兜底, 未声明模板维持 UNKNOWN。
        md='| 交易日期 | 记账日 | 交易说明 | 人民币金额 | 币种 | 地区 | 原币金额 |\n| 0912 | 0915 | GOOGLE*CHATGPT | ¥ 125.55 | 5510 | JP | 2,860.00 |'
        rows,_,_=parse_semantic_transactions('u','2026-10',md,self._HX_SYMBOL_RULE,_infer_date_from_any)
        self.assertEqual(rows[0].currency,'CNY')
        self.assertEqual(rows[0].currency_source,'cell_symbol')
        plain,_,_=parse_semantic_transactions('u','2026-10',md,{'bank_code':'CMB'},_infer_date_from_any)
        self.assertEqual(plain[0].currency,'UNKNOWN')

    def test_reviewed_header_reordering_and_ignored_columns_are_safe(self):
        base=[{'table_headers':[['ignored','txn_date','description','amount']]}]
        self.assertTrue(fingerprint_matches_baseline({'table_headers':[['amount','description','txn_date']]},base))
        self.assertFalse(fingerprint_matches_baseline({'table_headers':[['description','amount']]},base))

    def test_fixture_provenance_points_to_existing_file(self):
        root=Path(__file__).parents[1]
        rule=json.loads((root/'rules/CMB_TEMPLATE_V1.json').read_text())
        self.assertTrue((root/rule['quality']['baseline_provenance']['fixture']).is_file())
        self.assertFalse(rule['quality']['currency_declaration'])

    def test_no_unlabeled_date_is_guessed_as_due_date(self):
        from statement_parser import _infer_cmb_due_date
        self.assertIsNone(_infer_cmb_due_date('2026/09/11 - 2026/10/10','2026-10-10'))
        self.assertEqual(_infer_cmb_due_date('到期还款日：2026/10/28','2026-10-10'),'2026-10-28')

    def test_empty_detail_table_does_not_imply_missing_transactions(self):
        from mail_quality import diagnose_coverage
        self.assertEqual(diagnose_coverage({'table_block_count':1,'candidate_row_count':0,'valid_row_count':0}),[])
