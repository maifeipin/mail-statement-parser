"""Synthetic E2 CLI contract tests; only explicitly owned temporary stores."""
import contextlib
import io
import sqlite3
import tempfile
import json
import sys
import unittest
from pathlib import Path
from unittest.mock import patch
import mail_client as client
import statement_db as db
import billing_readmodel as model


class TestE2MailContracts(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = str(Path(self.temp.name)/'statements.db')
        db.init_db(self.path)
        self.patch = patch.object(client,'DB_PATH',self.path)
        self.patch.start(); self.addCleanup(self.patch.stop)
        with sqlite3.connect(self.path) as conn:
            conn.execute("INSERT INTO email_summaries(account_name,uid,subject,status,created_at) VALUES('fixture','1','fixture','pending','2030-01-01')")

    def call(self,fn,*args,**kwargs):
        with contextlib.redirect_stdout(io.StringIO()):
            return fn(*args,**kwargs)

    def test_feedback_normal_path_commits_in_one_transaction(self):
        result=self.call(client.cli_mail_noise_rule_add,'sender_email','fixture@example.invalid',1)
        self.assertEqual(result['status'],'ok')
        with sqlite3.connect(self.path) as conn:
            self.assertEqual(conn.execute('SELECT status FROM email_summaries').fetchone()[0],'noise')
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM noise_rules').fetchone()[0],1)
        # The previous implementation called a second writer under this transaction.
        again=self.call(client.cli_mail_noise_rule_add,'sender_email','fixture@example.invalid',1)
        self.assertEqual(again['status'],'ok')

    def test_feedback_failure_rolls_back_summary_and_rule(self):
        with sqlite3.connect(self.path) as conn:
            conn.execute("CREATE TRIGGER reject_rule BEFORE INSERT ON noise_rules BEGIN SELECT RAISE(ABORT,'fixture'); END")
        result=self.call(client.cli_mail_noise_rule_add,'sender_email','fixture@example.invalid',1)
        self.assertEqual(result['status'],'error')
        with sqlite3.connect(self.path) as conn:
            self.assertEqual(conn.execute('SELECT status FROM email_summaries').fetchone()[0],'pending')
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM noise_rules').fetchone()[0],0)

    def test_missing_feedback_summary_has_no_rule_side_effect(self):
        result=self.call(client.cli_mail_noise_rule_add,'sender_email','fixture@example.invalid',999)
        self.assertEqual(result['status'],'error')
        with sqlite3.connect(self.path) as conn:
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM noise_rules').fetchone()[0],0)

    def test_both_read_commands_do_not_create_missing_store(self):
        missing=str(Path(self.temp.name)/'missing.db')
        with patch.object(client,'DB_PATH',missing):
            for fn,args in ((client.cli_mail_summary_get,(1,)),(client.cli_mail_noise_rules_list,())):
                self.assertEqual(self.call(fn,*args)['status'],'error')
                self.assertFalse(Path(missing).exists())

    def test_header_queries_do_not_create_missing_store(self):
        missing=str(Path(self.temp.name)/'missing.db')
        with patch.object(client,'DB_PATH',missing):
            for fn in (client.cli_mail_headers,client.cli_mail_missed):
                self.assertEqual(self.call(fn)['status'],'error')
                self.assertFalse(Path(missing).exists())

    def test_search_preview_and_pagination_preserve_account_filter(self):
        with sqlite3.connect(self.path) as conn:
            for account,uid in [('fixture','1'),('fixture','2'),('other','1')]:
                conn.execute("INSERT OR IGNORE INTO email_summaries(account_name,uid,subject,status,created_at) VALUES(?,?,'fixture','pending','2030-01-01')",(account,uid))
                conn.execute("INSERT INTO email_bodies(account_name,uid,plain_text,fetched_at) VALUES(?,?,?,'2030-01-01')",(account,uid,'中'*20000))
        first=self.call(client.cli_mail_search,'中',1,'fixture',0)['data']
        second=self.call(client.cli_mail_search,'中',1,'fixture',1)['data']
        self.assertTrue(first['has_more']);self.assertFalse(second['has_more'])
        self.assertNotEqual(first['results'][0]['uid'],second['results'][0]['uid'])
        for page in (first,second):
            row=page['results'][0]
            self.assertEqual(row['account_name'],'fixture')
            self.assertLessEqual(len(row['plain_text'].encode()),8192)
            self.assertTrue(row['is_truncated'])

    def test_original_utf8_limit_does_not_expand_small_requests(self):
        with sqlite3.connect(self.path) as conn:
            conn.execute("INSERT INTO email_bodies(account_name,uid,plain_text,fetched_at) VALUES('fixture','1',?,'2030-01-01')",('中'*60000,))
        for budget in (1,50000,1000000):
            result=self.call(client.cli_mail_view_original,'fixture','1',budget)['data']
            self.assertLessEqual(len(result['content'].encode()),min(budget,50000))
            self.assertTrue(result['is_truncated'])

    def test_bad_as_of_is_error_not_current_date_fallback(self):
        self.assertEqual(self.call(client.cli_billing_snapshot,as_of='2030-01-01garbage')['status'],'error')

    def test_snapshot_uses_literal_custom_path_with_uri_characters(self):
        unusual=str(Path(self.temp.name)/'custom?#.db')
        with sqlite3.connect(self.path) as source, sqlite3.connect(unusual) as target:
            source.backup(target)
        result=model.snapshot(unusual,today='2030-01-01')
        self.assertEqual(result['database_statement_count'],0)
        self.assertFalse((Path(self.temp.name)/'custom').exists())

    def test_single_snapshot_scope_uses_n_calendar_months_plus_latest(self):
        with sqlite3.connect(self.path) as conn:
            for uid,bank,month in [('1','CMB','2030-09'),('2','CMB','2030-10'),('3','HX','2029-01')]:
                conn.execute("INSERT INTO statements(account_name,uid,message_id,bank_code,rule_id,subject,sender,email_date,statement_month,created_at,updated_at) VALUES('fixture',?,? ,?,'fixture','fixture','fixture','',?,'2030-01-01','2030-01-01')",(uid,'<fixture-'+uid+'>',bank,month))
        current=model.snapshot(self.path,months=1,today='2030-10-10')
        two=model.snapshot(self.path,months=2,today='2030-10-10')
        self.assertEqual(current['scope_start_month'],'2030-10')
        self.assertEqual(two['scope_start_month'],'2030-09')
        self.assertEqual({r['statement_month'] for r in current['statements']},{'2030-10','2029-01'})
        self.assertEqual({r['statement_month'] for r in two['statements']},{'2030-09','2030-10','2029-01'})

    def test_retired_command_aliases_are_rejected(self):
        for command in ('show_headers','show_missed','exec3m','validate3m','sync_bank_bills','interactive','reprocess'):
            with self.subTest(command=command),patch.object(sys,'argv',['mail_client.py',command]):
                output=io.StringIO()
                with contextlib.redirect_stdout(output),self.assertRaises(SystemExit) as exc:
                    client.main()
                self.assertNotEqual(exc.exception.code,0)
                self.assertEqual(json.loads(output.getvalue())['status'],'error')

    def test_header_command_accepts_only_envelope_contract(self):
        with patch.object(sys,'argv',['mail_client.py','mail_headers','--limit','2']):
            output=io.StringIO()
            with contextlib.redirect_stdout(output):
                client.main()
            self.assertEqual(json.loads(output.getvalue())['command'],'mail_headers')
        with patch.object(sys,'argv',['mail_client.py','mail_headers','--envelope']):
            output=io.StringIO()
            with contextlib.redirect_stdout(output),self.assertRaises(SystemExit):
                client.main()
            self.assertEqual(json.loads(output.getvalue())['status'],'error')

    def test_unscoped_month_is_reported_without_crashing_snapshot(self):
        with sqlite3.connect(self.path) as conn:
            conn.execute("INSERT INTO statements(account_name,uid,message_id,bank_code,rule_id,subject,sender,email_date,statement_month,created_at,updated_at) VALUES('fixture','1','<fixture>','CMB','fixture','fixture','fixture','','','2030-01-01','2030-01-01')")
        result=model.snapshot(self.path,today='2030-01-01')
        self.assertEqual(result['unscoped_statement_count'],1)
        self.assertEqual(result['statement_count'],0)
        self.assertTrue(any('账期缺失' in row for row in result['anomalies']))
