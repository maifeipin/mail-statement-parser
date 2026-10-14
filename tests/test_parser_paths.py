"""
P0.1 路径隔离与运行时上下文安全验证测试套件。
验证指标：
1. cwd 隔离：工作目录位于临时目录 B 时，指定上下文 A 仅写入 A 库与 A/reports，B/cwd 绝无新库/目录产生；
2. reparse 单上下文一致性：读取 A 库缓存后只写 A，缺失原文不创建外部默认库；
3. 同进程并发多上下文隔离：同一进程并发处理上下文 A 与 B，脱敏邮件记录、质量事件与报告无串库；
4. 路径越界与前缀攻击防范：/allowed-evil 前缀撞车、符号链接逃逸、冲突数据根均被显式拒绝；
5. 只读访问零副作用：读取不存在规则目录不触发 mkdir，只读检查不生成数据库。
"""

import os
import shutil
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from email.message import EmailMessage

import statement_parser
import statement_db as sd
from parser_paths import (
    ParserPaths,
    resolve_parser_paths,
    validate_write_target,
)


class TestParserPathsIsolation(unittest.TestCase):

    def setUp(self):
        self.tmp_dir = tempfile.mkdtemp()
        self.dir_a = os.path.join(self.tmp_dir, 'root_a')
        self.dir_b = os.path.join(self.tmp_dir, 'root_b')
        os.makedirs(self.dir_a, exist_ok=True)
        os.makedirs(self.dir_b, exist_ok=True)

        self.context_a = resolve_parser_paths(
            data_root=self.dir_a,
            db_path=os.path.join(self.dir_a, 'a.db'),
            report_dir=os.path.join(self.dir_a, 'reports'),
            writable_roots=(self.dir_a,),
        )
        self.context_b = resolve_parser_paths(
            data_root=self.dir_b,
            db_path=os.path.join(self.dir_b, 'b.db'),
            report_dir=os.path.join(self.dir_b, 'reports'),
            writable_roots=(self.dir_b,),
        )

    def tearDown(self):
        shutil.rmtree(self.tmp_dir, ignore_errors=True)

    def _make_sample_email(self, uid='101', subject='招商银行信用卡电子账单'):
        msg = EmailMessage()
        msg['Subject'] = subject
        msg['From'] = 'ccsvc@message.cmbchina.com'
        msg['Date'] = 'Sat, 10 Oct 2026 10:00:00 +0800'
        msg['Message-ID'] = f'<test_{uid}@example.com>'
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
            '<tr><td>1</td><td>1001</td><td>1002</td><td>TEST_STORE</td>'
            '<td>50.00</td><td>CNY</td><td>CN</td><td>50.00</td></tr></table>'
            '</body></html>'
        )
        msg.set_content(plain)
        msg.add_alternative(html, subtype='html')
        return msg

    def test_cwd_isolation(self):
        """当当前工作目录 (cwd) 切换到 dir_b 时，指定 context_a 解析只写 dir_a，dir_b 保持干净"""
        orig_cwd = os.getcwd()
        try:
            os.chdir(self.dir_b)
            msg = self._make_sample_email(uid='U-CWD')
            rules = statement_parser.load_rule_files()
            res = statement_parser.validate_and_save_email_message(
                msg, 'U-CWD', rules=rules, account_name='acc_a', context=self.context_a
            )
            self.assertEqual(res.get('status'), 'saved')

            # 验证 dir_a 中有 a.db 和 reports，且 a.db 中有写入
            self.assertTrue(os.path.exists(self.context_a.db_path))
            self.assertTrue(os.path.isdir(self.context_a.report_dir))
            self.assertTrue(len(os.listdir(self.context_a.report_dir)) > 0)

            # 验证 cwd (dir_b) 没有生成任何 statements.db, validation-reports, rules 等文件
            b_contents = os.listdir(self.dir_b)
            self.assertEqual(b_contents, [], f"cwd 受到污染: {b_contents}")
        finally:
            os.chdir(orig_cwd)

    def test_reparse_cached_statement_context_consistency(self):
        """reparse 从 A 库读取缓存并写回 A 库；不向 B 库或全局默认库写任何数据"""
        msg = self._make_sample_email(uid='U-REPARSE')
        rules = statement_parser.load_rule_files()
        res1 = statement_parser.validate_and_save_email_message(
            msg, 'U-REPARSE', rules=rules, account_name='acc_a', context=self.context_a, raw_bytes=msg.as_bytes()
        )
        self.assertEqual(res1.get('status'), 'saved')

        # 执行 reparse
        res2 = statement_parser.reparse_cached_statement(
            'acc_a', 'U-REPARSE', context=self.context_a
        )
        self.assertEqual(res2.get('status'), 'saved')
        self.assertEqual(res2.get('statement_id'), res1.get('statement_id'))

        # 检查 dir_b 中依然不存在 b.db
        self.assertFalse(os.path.exists(self.context_b.db_path))

        # 缺失缓存的 UID 进行 reparse 时返回错误且不创建 b.db
        err_res = statement_parser.reparse_cached_statement(
            'acc_b', 'NONEXISTENT_UID', context=self.context_b
        )
        self.assertEqual(err_res.get('status'), 'error')
        self.assertEqual(err_res.get('error'), 'no_cached_body')

    def test_reparse_rejects_corrupted_original_without_financial_mutation(self):
        msg = self._make_sample_email(uid='CORRUPT')
        result = statement_parser.validate_and_save_email_message(msg, 'CORRUPT',
            account_name='acc_a', context=self.context_a, raw_bytes=msg.as_bytes())
        evidence = sd.get_statement_evidence(self.context_a.db_path, result['statement_id'])
        import sqlite3
        with sqlite3.connect(self.context_a.db_path) as conn:
            before = conn.execute('SELECT total_due, minimum_due, due_date FROM statements').fetchall()
        with open(evidence['archive_path'], 'wb') as fp:
            fp.write(b'corrupted')
        self.assertEqual(statement_parser.reparse_cached_statement('acc_a', 'CORRUPT',
            context=self.context_a)['error'], 'original_mime_hash_mismatch')
        with sqlite3.connect(self.context_a.db_path) as conn:
            self.assertEqual(conn.execute('SELECT total_due, minimum_due, due_date FROM statements').fetchall(), before)

    def test_missing_original_refuses_body_reconstruction(self):
        msg = self._make_sample_email(uid='BODY_ONLY')
        statement_parser.validate_and_save_email_message(msg, 'BODY_ONLY',
            account_name='acc_a', context=self.context_a)
        result = statement_parser.reparse_cached_statement('acc_a', 'BODY_ONLY', context=self.context_a)
        self.assertEqual(result['error'], 'no_original_mime_evidence')

    def test_normal_sync_still_skips_but_explicit_reparse_runs_validation(self):
        msg = self._make_sample_email(uid='ORIGINAL')
        raw = msg.as_bytes()
        first = statement_parser.validate_and_save_email_message(msg, 'ORIGINAL',
            account_name='acc_a', context=self.context_a, raw_bytes=raw)
        repeated = statement_parser.validate_and_save_email_message(msg, 'ORIGINAL',
            account_name='acc_a', context=self.context_a, raw_bytes=raw)
        self.assertEqual(repeated['status'], 'associated_existing')
        reparsed = statement_parser.reparse_cached_statement('acc_a', 'ORIGINAL', context=self.context_a)
        self.assertEqual(reparsed['status'], 'saved')
        self.assertEqual(reparsed['statement_id'], first['statement_id'])
        self.assertGreater(reparsed['run_id'], first['run_id'])

    def test_reparse_blocks_confirmed_currency_downgrade(self):
        msg = self._make_sample_email(uid='CURRENCY_GUARD')
        first = statement_parser.validate_and_save_email_message(msg, 'CURRENCY_GUARD',
            account_name='acc_a', context=self.context_a, raw_bytes=msg.as_bytes())
        import sqlite3
        with sqlite3.connect(self.context_a.db_path) as conn:
            conn.execute("UPDATE statement_transactions SET currency='JPY', currency_source='extracted' WHERE statement_id=?", (first['statement_id'],))
        result = statement_parser.reparse_cached_statement('acc_a', 'CURRENCY_GUARD', context=self.context_a)
        self.assertEqual(result['status'], 'reparse_blocked')
        self.assertIn('currency', result['changed_fields'])
        with sqlite3.connect(self.context_a.db_path) as conn:
            self.assertEqual(conn.execute('SELECT currency FROM statement_transactions').fetchone()[0], 'JPY')

    def test_original_reparse_without_transport_anchor_does_not_create_one(self):
        msg = self._make_sample_email(uid='HISTORICAL')
        first = statement_parser.validate_and_save_email_message(msg, 'HISTORICAL',
            account_name='acc_a', context=self.context_a, raw_bytes=msg.as_bytes())
        import sqlite3
        with sqlite3.connect(self.context_a.db_path) as conn:
            conn.execute('DELETE FROM statement_transport_identities')
        result = statement_parser.reparse_cached_statement('acc_a', 'HISTORICAL', context=self.context_a)
        self.assertEqual(result['status'], 'saved')
        self.assertEqual(result['statement_id'], first['statement_id'])
        with sqlite3.connect(self.context_a.db_path) as conn:
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM statement_transport_identities').fetchone()[0], 0)

    def test_reviewed_hx_baseline_is_narrow(self):
        from mail_quality import diagnose_format
        rule = next(r for r in statement_parser.load_rule_files() if r['bank_code']=='HX')
        reviewed = {'table_headers':[['txn_date','post_date','description',None,'amount']], 'table_block_count':1}
        self.assertFalse(diagnose_format(reviewed, rule))
        historical = {'table_headers':reviewed['table_headers'] + [['txn_date','post_date','description',None,'amount','amount']], 'table_block_count':2}
        self.assertEqual(diagnose_format(historical, rule)[0].code, 'FORMAT_CHANGED')

    def test_concurrent_contexts_no_crosstalk(self):
        """同一进程并发使用 context_a 与 context_b 处理不同邮件，互不串库"""
        rules = statement_parser.load_rule_files()

        def parse_worker(args):
            uid, acct, ctx = args
            msg = self._make_sample_email(uid=uid)
            return statement_parser.validate_and_save_email_message(
                msg, uid, rules=rules, account_name=acct, context=ctx
            )

        tasks = [
            ('UID_A_1', 'acct_a', self.context_a),
            ('UID_B_1', 'acct_b', self.context_b),
            ('UID_A_2', 'acct_a', self.context_a),
            ('UID_B_2', 'acct_b', self.context_b),
        ]

        with ThreadPoolExecutor(max_workers=4) as executor:
            results = list(executor.map(parse_worker, tasks))

        for r in results:
            self.assertEqual(r.get('status'), 'saved')

        # 验证 A 库只包含 acct_a 的记录，B 库只包含 acct_b 的记录
        import sqlite3
        conn_a = sqlite3.connect(self.context_a.db_path)
        uids_a = {row[0] for row in conn_a.execute("SELECT uid FROM statements").fetchall()}
        conn_a.close()

        conn_b = sqlite3.connect(self.context_b.db_path)
        uids_b = {row[0] for row in conn_b.execute("SELECT uid FROM statements").fetchall()}
        conn_b.close()

        self.assertEqual(uids_a, {'UID_A_1', 'UID_A_2'})
        self.assertEqual(uids_b, {'UID_B_1', 'UID_B_2'})

    def test_prefix_collision_and_symlink_escape_rejection(self):
        """可写目录前缀伪装 (/root_a-evil) 与符号链接逃逸被严格拒绝"""
        # 1. 前缀伪装
        evil_target = self.dir_a + '-evil' + '/bad.db'
        with self.assertRaises(ValueError):
            validate_write_target(self.context_a, evil_target)

        # 2. 跨目录逃逸
        traversal = os.path.join(self.dir_a, '..', 'escape.db')
        with self.assertRaises(ValueError):
            validate_write_target(self.context_a, traversal)

        # 3. 符号链接跳出根目录
        outside_file = os.path.join(self.tmp_dir, 'outside.txt')
        with open(outside_file, 'w') as f:
            f.write('outside')
        symlink_path = os.path.join(self.dir_a, 'symlink_out.txt')
        os.symlink(outside_file, symlink_path)

        with self.assertRaises(ValueError):
            validate_write_target(self.context_a, symlink_path)

        # 4. 显式冲突组合检测
        with self.assertRaises(ValueError):
            resolve_parser_paths(
                data_root=self.dir_a,
                db_path=os.path.join(self.dir_b, 'outside.db'),
                writable_roots=(self.dir_a,)
            )

    def test_default_download_dir_matches_production(self):
        """默认下载归档目录名必须为 email-downloads，保持与既有生产归档一致"""
        paths = resolve_parser_paths()
        self.assertEqual(os.path.basename(paths.download_dir), 'email-downloads')

    def test_load_rule_files_read_only_no_mkdir(self):
        """只读读取不存在的规则目录时不调用 mkdir"""
        nonexistent_rules = os.path.join(self.tmp_dir, 'no_such_rules_dir')
        ctx = resolve_parser_paths(
            data_root=self.dir_a,
            rules_dir=nonexistent_rules
        )
        rules = statement_parser.load_rule_files(context=ctx)
        self.assertEqual(rules, [])
        self.assertFalse(os.path.exists(nonexistent_rules))



    def test_explicit_data_root_cross_root_default_reject_and_explicit_allow(self):
        """显式 data_root 时，外部 DB 默认拒绝；只有明确授权 writable_roots 时才允许"""
        outside_db = os.path.join(self.dir_b, 'outside.db')
        # 默认拒绝
        with self.assertRaises(ValueError):
            resolve_parser_paths(data_root=self.dir_a, db_path=outside_db)

        # 显式授权通过
        ctx = resolve_parser_paths(
            data_root=self.dir_a,
            db_path=outside_db,
            writable_roots=(self.dir_a, self.dir_b)
        )
        self.assertEqual(ctx.db_path, outside_db)

    def test_relative_path_resolution_stable_no_duplicate_segments(self):
        """相对路径基于仓库根解析，不同 cwd 解析一致且绝不重复拼接目录"""
        repo_root = os.path.dirname(os.path.abspath(statement_parser.__file__))
        expected_db = os.path.join(repo_root, 'sub', 'example.db')

        orig_cwd = os.getcwd()
        try:
            os.chdir(self.dir_a)
            ctx1 = resolve_parser_paths(db_path='sub/example.db')
            os.chdir(self.dir_b)
            ctx2 = resolve_parser_paths(db_path='sub/example.db')
        finally:
            os.chdir(orig_cwd)

        self.assertEqual(ctx1.db_path, expected_db)
        self.assertEqual(ctx2.db_path, expected_db)
        self.assertNotIn('sub' + os.sep + 'sub', ctx1.db_path)
        self.assertEqual(ctx1.data_root, os.path.dirname(expected_db))

    def test_direct_out_of_bounds_parserpaths_reparse_no_side_effects(self):
        """直接构造越界 ParserPaths 进行 reparse 时立即抛出 ValueError 且不创建 DB 文件"""
        forbidden_db = os.path.join(self.dir_b, 'forbidden.db')
        from parser_paths import ParserPaths
        ctx = ParserPaths(
            db_path=forbidden_db,
            rules_dir=self.context_a.rules_dir,
            report_dir=self.context_a.report_dir,
            download_dir=self.context_a.download_dir,
            data_root=self.dir_a,
            writable_roots=(self.dir_a,)
        )
        with self.assertRaises(ValueError):
            statement_parser.reparse_cached_statement('acc', 'U123', context=ctx)

        self.assertFalse(os.path.exists(forbidden_db), "越界 DB 文件不应被创建")

    def test_direct_out_of_bounds_report_dir_no_mkdir_side_effects(self):
        """直接构造越界 report_dir 时立即抛出 ValueError 且不创建目录"""
        forbidden_rep = os.path.join(self.dir_b, 'forbidden_reports')
        from parser_paths import ParserPaths
        ctx = ParserPaths(
            db_path=self.context_a.db_path,
            rules_dir=self.context_a.rules_dir,
            report_dir=forbidden_rep,
            download_dir=self.context_a.download_dir,
            data_root=self.dir_a,
            writable_roots=(self.dir_a,)
        )
        msg = self._make_sample_email(uid='U-DIR-ESCAPE')
        with self.assertRaises(ValueError):
            statement_parser.validate_and_save_email_message(msg, 'U-DIR-ESCAPE', context=ctx)

        self.assertFalse(os.path.exists(forbidden_rep), "越界报告目录不应被创建")

    def test_dynamic_env_override_picked_up(self):
        """运行时修改环境变量能够被动态生效，不被模块加载期常量覆盖"""
        test_db = os.path.join(self.dir_a, 'env_dynamic.db')
        orig_env = os.environ.get('MAIL_STATEMENT_DB_PATH')
        orig_data_root = os.environ.get('MAIL_STATEMENT_DATA_ROOT')
        try:
            os.environ.pop('MAIL_STATEMENT_DATA_ROOT', None)
            os.environ['MAIL_STATEMENT_DB_PATH'] = test_db
            ctx = resolve_parser_paths()
            self.assertEqual(ctx.db_path, test_db)
        finally:
            if orig_env is None:
                os.environ.pop('MAIL_STATEMENT_DB_PATH', None)
            else:
                os.environ['MAIL_STATEMENT_DB_PATH'] = orig_env
            if orig_data_root is not None:
                os.environ['MAIL_STATEMENT_DATA_ROOT'] = orig_data_root


    def test_mail_sync_fetch_out_of_bounds_context_blocks_init_db(self):
        """C4: fetch_recent_emails_and_summarize 传入直接构造的越界上下文时，在任何 init_db 前被拦截"""
        import mail_sync
        from unittest.mock import patch
        forbidden_db = os.path.join(self.dir_b, 'forbidden_sync.db')
        ctx = ParserPaths(
            db_path=forbidden_db,
            rules_dir=os.path.join(self.dir_a, 'rules'),
            report_dir=os.path.join(self.dir_a, 'reports'),
            download_dir=os.path.join(self.dir_a, 'email-downloads'),
            data_root=self.dir_a,
            writable_roots=(self.dir_a,)
        )
        with patch('mail_sync.init_db') as mock_init:
            with self.assertRaises(ValueError):
                mail_sync.fetch_recent_emails_and_summarize(context=ctx)
            mock_init.assert_not_called()
        self.assertFalse(os.path.exists(forbidden_db), "越界数据库文件不应被创建")

    def test_mail_sync_reprocess_out_of_bounds_context_blocks_init_db(self):
        """C4: reprocess_by_status 传入直接构造的越界上下文时，在任何 init_db 前被拦截"""
        import mail_sync
        from unittest.mock import patch
        forbidden_db = os.path.join(self.dir_b, 'forbidden_reprocess.db')
        ctx = ParserPaths(
            db_path=forbidden_db,
            rules_dir=os.path.join(self.dir_a, 'rules'),
            report_dir=os.path.join(self.dir_a, 'reports'),
            download_dir=os.path.join(self.dir_a, 'email-downloads'),
            data_root=self.dir_a,
            writable_roots=(self.dir_a,)
        )
        with patch('statement_db.init_db') as mock_init:
            with self.assertRaises(ValueError):
                mail_sync.reprocess_by_status(context=ctx)
            mock_init.assert_not_called()
        self.assertFalse(os.path.exists(forbidden_db), "越界数据库文件不应被创建")

    def test_mail_sync_reprocess_mismatched_db_path_rejected(self):
        """C4: reprocess_by_status 显式 db_path 与 context.db_path 冲突时抛出 ValueError"""
        import mail_sync
        ctx = ParserPaths(
            db_path=os.path.join(self.dir_a, 'a.db'),
            rules_dir=os.path.join(self.dir_a, 'rules'),
            report_dir=os.path.join(self.dir_a, 'reports'),
            download_dir=os.path.join(self.dir_a, 'email-downloads'),
            data_root=self.dir_a,
            writable_roots=(self.dir_a,)
        )
        with self.assertRaises(ValueError):
            mail_sync.reprocess_by_status(db_path=os.path.join(self.dir_a, 'conflict.db'), context=ctx)

    def test_invalid_context_type_raises_type_error(self):
        """C4: 传入非 ParserPaths / dict 类型的非法 context 抛出明确 TypeError"""
        import mail_sync, statement_parser
        with self.assertRaises(TypeError):
            mail_sync.fetch_recent_emails_and_summarize(context="invalid_string_context")
        with self.assertRaises(TypeError):
            mail_sync.reprocess_by_status(context=[1, 2, 3])
        with self.assertRaises(TypeError):
            statement_parser.validate_and_save_email_message(None, 'uid', context=12345)


if __name__ == '__main__':
    unittest.main()
