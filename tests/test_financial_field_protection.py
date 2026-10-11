"""Synthetic regressions for classification, DOM evidence and safe financial saves."""
import contextlib
import copy
import email
import io
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
import statement_db as db
import statement_parser as parser
from statement_models import StatementRecord, StatementTransactionRecord, StatementBalanceRecord
from parser_paths import resolve_parser_paths

ROOT = Path(__file__).resolve().parents[1]

class TestFinancialFieldProtection(unittest.TestCase):
    def test_nested_layout_tables_preserve_physical_row_multiplicity(self):
        from mail_parse import parse_html_tables, tables_to_markdown
        from statement_evidence import parse_semantic_transactions
        detail = '<table><tr><th>交易日</th><th>交易描述</th><th>人民币金额</th></tr>'
        detail += '<tr><td>2030-10-01</td><td>fixture</td><td>10</td></tr>' * 2
        detail += '</table>'
        nested = '<table><tr><td>' * 3 + detail + '</td></tr></table>' * 3
        md = tables_to_markdown(parse_html_tables(nested))
        rows, stats, _ = parse_semantic_transactions('fixture', '2030-10', md,
            {'bank_code': 'CMB'}, lambda text, month: text or None)
        self.assertEqual(len(rows), 2)
        self.assertEqual(stats['candidate_row_count'], 2)
        self.assertEqual([r.currency for r in rows], ['CNY', 'CNY'])

    def test_cmb_html_id_is_explicit_source_evidence(self):
        rule = json.loads((ROOT / 'rules/CMB_TEMPLATE_V1.json').read_text())
        fields, meta = parser.extract_statement_by_rule(rule, '', '', True,
            html_content='<div id="paymentDueDate"><span>2030/10/25</span></div>')
        self.assertEqual(fields['due_date'], '2030-10-25')
        self.assertEqual(meta['due_date']['source'], 'extracted')
        self.assertEqual(meta['due_date']['evidence']['element_id'], 'paymentDueDate')

    def test_invalid_duplicate_and_unclosed_html_are_not_trusted(self):
        rule = json.loads((ROOT / 'rules/CMB_TEMPLATE_V1.json').read_text())
        for html in ('<div id="paymentDueDate">2030/02/31</div>',
                     '<div id="paymentDueDate">2030/10/25</div><div id="paymentDueDate">2030/10/26</div>',
                     '<div id="paymentDueDate">2030/10/25'):
            with self.subTest(html=html):
                fields, meta = parser.extract_statement_by_rule(rule, '', '', True, html_content=html)
                self.assertIsNone(fields['due_date'])
                self.assertEqual(meta['due_date']['source'], 'extraction_failed')
        fields = parser.extract_statement_by_rule(rule, '', '', html_content='<div>2030/10/25</div>')
        self.assertIsNone(fields['due_date'])

    def test_cmbc_notice_is_not_a_statement(self):
        rule = json.loads((ROOT / 'rules/CMBC_TEMPLATE_V1.json').read_text())
        sender = 'master@creditcard.cmbc.com.cn'
        matched, _ = parser.identify_rule('民生信用卡重要信息变更提示', sender, '合约修订公告', [rule])
        self.assertIsNone(matched)
        for title in ('民生信用卡2030年10月电子对账单', '民生信用卡2030年10月电子对帐单'):
            self.assertEqual(parser.identify_rule(title, sender, '', [rule])[0]['bank_code'], 'CMBC')

    def test_nominally_valid_reparse_cannot_clear_financial_fields(self):
        with tempfile.TemporaryDirectory() as temp:
            p = temp + '/statements.db'; db.init_db(p)
            s = StatementRecord(account_name='fixture', uid='legacy', message_id='<fixture>', bank_code='CMB',
                rule_id='fixture', subject='bill', sender='bank', email_date='',
                statement_month='2030-01', statement_date='2030-01-10', due_date='2030-01-25',
                total_due='0', minimum_due='0')
            txn = StatementTransactionRecord(uid='legacy', bank_code='CMB', description='fixture', amount='-1')
            db.save_statement_with_transactions(p, s, [txn, txn],
                balances=[StatementBalanceRecord(currency='CNY', total_due='0')])
            with sqlite3.connect(p) as c:
                c.execute("UPDATE statements SET is_paid=1, paid_at='fixture-proof'")
                baseline = {t:c.execute('SELECT * FROM '+t).fetchall() for t in
                            ('statements','statement_transactions','statement_balances')}
            for field in ('due_date','total_due','minimum_due','statement_month','statement_date'):
                incoming = copy.deepcopy(s); setattr(incoming, field, None)
                with self.subTest(field=field), self.assertRaises(db.FinancialFieldRegression):
                    db.save_statement_with_transactions(p, incoming, [])
                with sqlite3.connect(p) as c:
                    self.assertEqual(baseline, {t:c.execute('SELECT * FROM '+t).fetchall() for t in baseline})
            s.statement_month='2030-10';s.statement_date='2030-10-10';s.due_date='2030-10-25';s.total_due='-1'
            db.save_statement_with_transactions(p, s, [txn, txn])
            with sqlite3.connect(p) as c:
                self.assertEqual(c.execute('SELECT statement_month,total_due,is_paid,paid_at FROM statements').fetchone(),
                                 ('2030-10','-1',1,'fixture-proof'))

    def test_parser_reports_blocked_reparse_and_persists_quality_case(self):
        with tempfile.TemporaryDirectory() as temp:
            p=temp+'/statements.db';db.init_db(p)
            s=StatementRecord(account_name='fixture',uid='legacy',message_id='<probe@example.invalid>',bank_code='CMB',
                rule_id='fixture',subject='bill 2030-10',sender='bank',email_date='',
                statement_month='2030-10',due_date='2030-10-25',total_due='100',minimum_due='0')
            sid,_=db.save_statement_with_transactions(p,s,[])
            rule={'bank_code':'CMB','rule_id':'fixture','match_rules':{'subject_patterns':['bill']},
                  'extract_rules':{'statement_fields':{
                    'statement_month':{'source':'subject','pattern':r'(2030-10)'},
                    'total_due':{'pattern':r'Total: (100)'},
                    'due_date':{'pattern':r'Due: (2030-10-25)'}}},
                  'validate_rules':{'required_statement_fields':['statement_month','total_due']}}
            msg=email.message.EmailMessage();msg['Subject']=s.subject;msg['From']='bank';msg['Message-ID']=s.message_id
            msg.set_content('Total: 100')
            with contextlib.redirect_stdout(io.StringIO()):
                result=parser.validate_and_save_email_message(msg,'legacy',rules=[rule],account_name='fixture',
                    context=resolve_parser_paths(data_root=temp,db_path=p),raw_bytes=msg.as_bytes())
            self.assertEqual(result['status'],'reparse_blocked')
            self.assertEqual(result['statement_id'],sid)
            with sqlite3.connect(p) as c:
                self.assertEqual(c.execute('SELECT due_date,minimum_due FROM statements').fetchone(),('2030-10-25','0'))
                self.assertEqual(c.execute("SELECT COUNT(*) FROM quality_events WHERE case_key LIKE 'reparse_guard|%'").fetchone()[0],1)

if __name__ == '__main__':
    unittest.main()
