"""Billing scope must never open unrelated accounts or mislabel associations."""
import contextlib
import io
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch
import mail_client as client


class BillingScopeTests(unittest.TestCase):
    accounts = [
        {'account': 'billing@example.invalid', 'provider': '163'},
        {'account': 'other@example.invalid', 'provider': 'gmail'},
        {'account': 'third@example.invalid', 'provider': 'outlook'},
    ]

    def test_scoped_scan_only_visits_163_and_explicit_override_visits_gmail(self):
        for scope, expected in (({'provider': '163'}, 'billing@example.invalid'),
                                ({'account': 'other@example.invalid'}, 'other@example.invalid')):
            with tempfile.TemporaryDirectory() as temp:
                context = client.resolve_parser_paths(data_root=temp)
                with patch.object(client, 'load_accounts', return_value=self.accounts), \
                     patch.object(client, 'load_rule_files', return_value=[{'bank_code': 'CMB'}]), \
                     patch.object(client, '_collect_recent_bill_uids', return_value=([], {'skipped_error': 0})) as scan, \
                     contextlib.redirect_stdout(io.StringIO()):
                    result = client.sync_recent_bank_emails(context=context, **scope)
                self.assertEqual(scan.call_count, 1)
                self.assertEqual(scan.call_args.args[3]['account'], expected)
                self.assertEqual([a['account'] for a in result['accounts']], [expected])

    def test_missing_or_conflicting_scope_fails_before_schema_and_connection(self):
        with patch.object(client, 'load_accounts', return_value=self.accounts), \
             patch.object(client, 'init_db') as init, \
             patch.object(client, '_collect_recent_bill_uids') as scan:
            for scope in ({'account': 'missing'}, {'provider': 'missing'},
                          {'account': 'billing@example.invalid', 'provider': '163'}):
                with self.assertRaises(ValueError):
                    client.sync_recent_bank_emails(**scope)
            init.assert_not_called()
            scan.assert_not_called()

    def test_cli_scope_parsing_and_rejection(self):
        with patch.object(client, 'sync_recent_bank_emails') as sync, contextlib.redirect_stdout(io.StringIO()):
            with patch.object(sys, 'argv', ['mail_client.py', 'sync3m', '--months', '2', '--provider', '163']):
                client.main()
            self.assertEqual(sync.call_args.args, (2,))
            self.assertEqual(sync.call_args.kwargs, {'account': None, 'provider': '163'})
            sync.reset_mock()
            for args in (['2', '--account', ''], ['2', '--provider', ''], ['2', '--unknown', '163'],
                         ['2', '--account', 'x', '--provider', '163']):
                with patch.object(sys, 'argv', ['mail_client.py', 'sync3m'] + args):
                    with self.assertRaises(SystemExit):
                        client.main()
            sync.assert_not_called()

    def test_associated_existing_is_successful_not_a_parse_error(self):
        with tempfile.TemporaryDirectory() as temp:
            context = client.resolve_parser_paths(data_root=temp)
            def scan(*args, **kwargs):
                kwargs['on_candidate']('stable-uid', MagicMock())
                return [], {'skipped_error': 0}
            with patch.object(client, 'load_accounts', return_value=self.accounts), \
                 patch.object(client, 'load_rule_files', return_value=[{'bank_code': 'CMB'}]), \
                 patch.object(client, '_downloaded_file_exists', return_value=True), \
                 patch.object(client, 'uid_exists', return_value=False), \
                 patch.object(client, 'validate_and_save_email_message', return_value={'status': 'associated_existing'}), \
                 patch.object(client, '_collect_recent_bill_uids', side_effect=scan), \
                 contextlib.redirect_stdout(io.StringIO()):
                result = client.sync_recent_bank_emails(context=context, provider='163')
            self.assertEqual(result['accounts'][0]['status'], 'ok', result['accounts'])
            self.assertEqual(result['totals']['errors'], 0)
            self.assertEqual(result['totals']['associated_existing'], 1)

    def test_ordinary_fetch_summaries_zero_candidate_counters_initialized(self):
        """普通 fetch_summaries 路径: 全部命中去重时,三类计数器不得未初始化(生产 UnboundLocalError 回归)。"""
        import mail_sync
        with tempfile.TemporaryDirectory() as temp:
            context = client.resolve_parser_paths(data_root=temp)
            fake_mail = MagicMock()
            fake_mail.list.return_value = (b'+OK', [b'1'])
            with patch.object(mail_sync, 'init_db'), \
                 patch.object(mail_sync, 'load_rule_files', return_value=[]), \
                 patch('mail_client.load_accounts', return_value=[{'account': 'a@b.c', 'provider': '163'}]), \
                 patch.object(mail_sync, 'is_graph_api', return_value=False), \
                 patch.object(mail_sync, 'connect_pop3', return_value=fake_mail), \
                 patch.object(mail_sync, 'pop3_uidl_map', return_value={1: 'uid-xyz'}), \
                 patch.object(mail_sync, 'uid_exists', return_value=True), \
                 patch.object(mail_sync, 'get_email_summary_status', return_value=('processed', 0)), \
                 contextlib.redirect_stdout(io.StringIO()):
                result = mail_sync.fetch_recent_emails_and_summarize(1, context=context)
            totals = result['totals']
            self.assertEqual(totals['skipped_dup'], 1)
            for key in ('associated_existing', 'identity_conflict', 'insufficient_evidence'):
                self.assertEqual(totals[key], 0, totals)
            self.assertEqual(result['accounts'][0]['status'], 'ok', result['accounts'])
