import contextlib
import hashlib
import importlib.util
import io
import signal
import sqlite3
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import mail_client as client
import mail_connect as connect
import statement_db as db
from statement_models import StatementRecord, StatementTransactionRecord


class BillingSyncTests(unittest.TestCase):
    def test_numeric_uid_must_be_in_uidl_map(self):
        mail = MagicMock()
        mail.uidl.return_value = (b'+OK', [b'9 123'], 0)
        mail.retr.return_value = (b'+OK', [b'body'], 0)
        connect.pop3_retr_by_uid(mail, '123')
        mail.retr.assert_called_once_with(9)
        mail.retr.reset_mock()
        with self.assertRaises(ValueError):
            connect.pop3_retr_by_uid(mail, '7')
        mail.retr.assert_not_called()

    def test_missing_uidl_never_persists_sequence(self):
        mail = MagicMock()
        mail.list.return_value = (b'+OK', [b'1 10'], 0)
        mail.uidl.return_value = (b'+OK', [], 0)
        with patch.object(client, 'connect_pop3', return_value=mail):
            with self.assertRaises(ValueError):
                client._collect_recent_bill_uids(1, [], {'CMB'}, {'provider': '163'})
        mail.retr.assert_not_called()
        mail.close.assert_called_once()

    def test_pop3_scans_and_processes_once_in_same_session(self):
        mail = MagicMock()
        mail.list.return_value = (b'+OK', [b'1 10'], 0)
        mail.uidl.return_value = (b'+OK', [b'1 stable-id'], 0)
        mail.retr.return_value = (b'+OK', [b'Subject: monthly bill', b'From: bank', b'', b'body'], 0)
        rule = {'bank_code': 'CMB', 'match_rules': {'subject_patterns': ['monthly bill']}}
        callback = MagicMock()
        with patch.object(client, 'connect_pop3', return_value=mail) as factory:
            candidates, stats = client._collect_recent_bill_uids(1, [rule], {'CMB'},
                                                               {'provider': '163'}, callback)
        self.assertEqual(candidates[0][0], 'stable-id')
        self.assertEqual(stats['skipped_error'], 0)
        factory.assert_called_once()
        mail.retr.assert_called_once_with(1)
        self.assertEqual(callback.call_args.args[0], 'stable-id')
        self.assertEqual(callback.call_args.args[1]['Subject'], 'monthly bill')

    def run_sync(self, collector, budget=1):
        with tempfile.TemporaryDirectory() as temp, contextlib.redirect_stdout(io.StringIO()), \
             patch.object(client, 'DB_PATH', str(Path(temp) / 'statements.db')), \
             patch.object(client, 'DOWNLOAD_DIR', str(Path(temp) / 'downloads')), \
             patch.object(client, 'load_rule_files', return_value=[{'bank_code': 'CMB'}]), \
             patch.object(client, 'load_accounts', return_value=[{'account': 'a'}, {'account': 'b'}]), \
             patch.object(client, '_collect_recent_bill_uids', side_effect=collector):
            return client.sync_recent_bank_emails(account_budget_s=budget)

    def test_scan_deadline_interrupts_and_continues_next_account(self):
        original_handler = signal.getsignal(signal.SIGALRM)
        calls = []
        def scan(*args, **kwargs):
            calls.append(args[3]['account'])
            if calls[-1] == 'a':
                time.sleep(5)
            return [], {'skipped_error': 0}
        started = time.monotonic()
        result = self.run_sync(scan, budget=.03)
        self.assertLess(time.monotonic() - started, 1)
        self.assertEqual(calls, ['a', 'b'])
        self.assertEqual([a['status'] for a in result['accounts']], ['timeout', 'ok'])
        self.assertEqual(signal.getitimer(signal.ITIMER_REAL), (0.0, 0.0))
        self.assertEqual(signal.getsignal(signal.SIGALRM), original_handler)

    def test_swallowed_graph_scan_error_is_partial_not_empty_success(self):
        result = self.run_sync(lambda *a, **k: ([], {'skipped_error': 1}))
        self.assertEqual([a['status'] for a in result['accounts']], ['partial', 'partial'])
        self.assertEqual(result['totals']['errors'], 2)

    def test_deadline_escapes_hidden_retry_handlers(self):
        calls = []
        def stalled():
            calls.append(1)
            time.sleep(5)
        with self.assertRaises(connect.AccountBudgetExceeded):
            with connect.account_deadline(.03):
                connect.retry_with_backoff(stalled)
        self.assertEqual(len(calls), 1)

    def test_atomic_replacement_preserves_identical_row_multiplicity_and_clears_empty(self):
        with tempfile.TemporaryDirectory() as temp:
            path = str(Path(temp) / 'statements.db')
            db.init_db(path)
            statement = StatementRecord(uid='stable', message_id='<a>', bank_code='CMB',
                rule_id='cmb', subject='bill', sender='bank', email_date='', account_name='a',
                statement_month='2026-10', due_date='2020-01-01', total_due='0', minimum_due='0')
            txn = StatementTransactionRecord(uid='stable', bank_code='CMB', account_name='a',
                                              description='same merchant', amount='10')
            sid, count = db.save_statement_with_transactions(path, statement, [txn, txn])
            sid2, count2 = db.save_statement_with_transactions(path, statement, [txn, txn])
            self.assertEqual((sid, count), (sid2, count2))
            self.assertEqual(count2, 2)
            db.init_db(path)
            with contextlib.closing(sqlite3.connect(path)) as c:
                self.assertEqual(c.execute('SELECT is_paid FROM statements').fetchone()[0], 0)
            db.save_statement_with_transactions(path, statement, [])
            with contextlib.closing(sqlite3.connect(path)) as c:
                self.assertEqual(c.execute('SELECT COUNT(*) FROM statement_transactions').fetchone()[0], 0)

    def test_message_identity_conflict_does_not_insert_or_overwrite(self):
        with tempfile.TemporaryDirectory() as temp:
            path = str(Path(temp) / 'statements.db')
            db.init_db(path)
            original = StatementRecord(uid='legacy', message_id='<same>', bank_code='HX',
                rule_id='hx', subject='bill', sender='bank', email_date='', account_name='a', total_due='10')
            sid, _ = db.save_statement_with_transactions(path, original, [])
            replay = StatementRecord(uid='stable-uidl', message_id='<same>', bank_code='HX',
                rule_id='hx', subject='bill', sender='bank', email_date='', account_name='a', total_due='999')
            with self.assertRaises(db.MessageIdentityConflict):
                db.save_statement_with_transactions(path, replay, [])
            with contextlib.closing(sqlite3.connect(path)) as c:
                self.assertEqual(c.execute('SELECT id,total_due FROM statements').fetchall(), [(sid, '10')])

    def test_cleanup_can_run_again_with_existing_mapping_history(self):
        spec = importlib.util.spec_from_file_location('cleanup_repeat', ROOT / 'scripts/clean_duplicate_statements.py')
        cleanup = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cleanup)
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / 'statements.db'
            db.init_db(str(path))
            def seed(uid):
                with contextlib.closing(sqlite3.connect(path)) as c:
                    c.execute("INSERT INTO statements(account_name,uid,message_id,bank_code,rule_id,subject,created_at,updated_at) VALUES('a',?,'<same>','HX','hx','bill','now','now')", (uid,))
                    c.commit()
            seed('one'); seed('two')
            for step in range(3):
                if step == 2:
                    seed('three')
                with patch.object(sys, 'argv', ['cleanup', '--db', str(path), '--apply']), \
                     contextlib.redirect_stdout(io.StringIO()):
                    cleanup.main()
            with contextlib.closing(sqlite3.connect(path)) as c:
                self.assertEqual(c.execute('SELECT COUNT(*) FROM statements').fetchone()[0], 1)
                self.assertEqual(c.execute('SELECT COUNT(*) FROM cleanup_id_map').fetchone()[0], 2)

    def test_cleanup_dry_run_is_byte_identical(self):
        spec = importlib.util.spec_from_file_location('cleanup', ROOT / 'scripts/clean_duplicate_statements.py')
        cleanup = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cleanup)
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / 'statements.db'
            db.init_db(str(path))
            before = hashlib.sha256(path.read_bytes()).hexdigest()
            with patch.object(sys, 'argv', ['cleanup', '--db', str(path)]), \
                 contextlib.redirect_stdout(io.StringIO()):
                cleanup.main()
            self.assertEqual(hashlib.sha256(path.read_bytes()).hexdigest(), before)


if __name__ == '__main__':
    unittest.main()
