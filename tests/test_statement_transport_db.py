#!/usr/bin/env python3
"""
tests/test_statement_transport_db.py
单元测试：运输身份映射持久化、事务、审计与冲突守卫 (E1 §4, §10.3, §10.4, 二轮 Gap 1)
"""

import os
import sys
import tempfile
import unittest
import email.message

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import statement_db as sd
import statement_identity as si
from statement_models import StatementRecord


class TestStatementTransportDB(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.db = os.path.join(self.tmp, 'test_transport.db')
        sd.init_db(self.db)

        # 构造可信合成 MIME 邮件并提取证据
        self.msg = email.message.EmailMessage()
        self.msg['Message-ID'] = '<bill.202610@cmb.cn>'
        self.msg['Subject'] = '招商银行信用卡电子账单'
        self.msg['From'] = 'service@cmb.cn'
        self.msg['Date'] = 'Sat, 10 Oct 2026 10:00:00 +0800'
        self.msg.set_content('Body of October Bill')
        self.raw_bytes = self.msg.as_bytes()

        self.s1 = StatementRecord(
            account_name='user@163.com',
            uid='UID_POP_1001',
            message_id='<bill.202610@cmb.cn>',
            bank_code='CMB',
            rule_id='CMB_TEMPLATE_V1',
            subject='招商银行信用卡电子账单',
            sender='service@cmb.cn',
            email_date='2026-10-10',
            statement_month='2026-10',
            statement_date='2026-10-10',
            due_date='2026-10-28',
            total_due='1234.56',
            minimum_due='123.45',
            raw_fields_json='{}'
        )
        self.s1_id, _ = sd.save_statement_with_transactions(self.db, self.s1, [])

        self.ev1_archive = os.path.join(self.tmp, 'ev1.eml')
        with open(self.ev1_archive, 'wb') as fp:
            fp.write(self.raw_bytes)

        self.ev1 = si.build_evidence_from_email_message(
            self.msg, raw_bytes=self.raw_bytes, account_name='user@163.com',
            bank_code='CMB', transport_uid='UID_POP_1001'
        )
        self.ev1['archive_path'] = self.ev1_archive
        self.ev1['raw_bytes'] = self.raw_bytes

        sd.save_statement_evidence_index(
            self.db, statement_id=self.s1_id, account_name='user@163.com',
            bank_code='CMB', message_id='<bill.202610@cmb.cn>',
            raw_content_hash=self.ev1['raw_content_hash'],
            evidence_digest=self.ev1['complete_content_digest'],
            coverage_status=self.ev1['coverage_status'],
            archive_path=self.ev1_archive
        )

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _make_candidate_evidence(self, uid='UID_POP_2002', body='Body of October Bill', headers=None):
        cand_msg = email.message.EmailMessage()
        cand_msg['Message-ID'] = '<bill.202610@cmb.cn>'
        cand_msg['Subject'] = '招商银行信用卡电子账单'
        cand_msg['From'] = 'service@cmb.cn'
        cand_msg['Date'] = 'Sat, 10 Oct 2026 10:00:00 +0800'
        if headers:
            for k, v in headers.items():
                if k in cand_msg:
                    del cand_msg[k]
                cand_msg[k] = v
        cand_msg.set_content(body)
        cand_bytes = cand_msg.as_bytes()
        ev = si.build_evidence_from_email_message(
            cand_msg, raw_bytes=cand_bytes, account_name='user@163.com',
            bank_code='CMB', transport_uid=uid
        )
        cand_archive = os.path.join(self.tmp, f'{uid}.eml')
        with open(cand_archive, 'wb') as fp:
            fp.write(cand_bytes)
        ev['archive_path'] = cand_archive
        ev['raw_bytes'] = cand_bytes
        return ev

    def test_associate_and_idempotency_with_verified_evidence(self):
        """真实合成 MIME 证据成功建立 digest_equal 关联并在再次调用时幂等返回"""
        cand_ev = self._make_candidate_evidence(uid='UID_POP_2002')
        res = sd.associate_transport_identity(
            self.db,
            account_name='user@163.com',
            transport_kind='pop3_uidl',
            transport_uid='UID_POP_2002',
            bank_code='CMB',
            statement_id=self.s1_id,
            message_id='<bill.202610@cmb.cn>',
            evidence_digest=cand_ev['complete_content_digest'],
            raw_content_hash=cand_ev['raw_content_hash'],
            method='digest_equal',
            actor='auto_sync',
            reason='digest_equal',
            source_evidence=cand_ev,
            target_evidence=self.ev1
        )
        self.assertEqual(res['status'], 'associated')
        self.assertFalse(res['idempotent'])

        # 再次调用：参数相同，必须幂等返回
        res_idem = sd.associate_transport_identity(
            self.db,
            account_name='user@163.com',
            transport_kind='pop3_uidl',
            transport_uid='UID_POP_2002',
            bank_code='CMB',
            statement_id=self.s1_id,
            message_id='<bill.202610@cmb.cn>',
            evidence_digest=cand_ev['complete_content_digest'],
            raw_content_hash=cand_ev['raw_content_hash'],
            method='digest_equal',
            actor='auto_sync',
            reason='digest_equal',
            source_evidence=cand_ev,
            target_evidence=self.ev1
        )
        self.assertEqual(res_idem['status'], 'already_associated')
        self.assertTrue(res_idem['idempotent'])

    def test_fake_digest_without_evidence_rejected(self):
        """反例 (Gap 1 P0)：不提供证据只传伪造摘要，底层强制拒绝"""
        with self.assertRaises(ValueError) as ctx:
            sd.associate_transport_identity(
                self.db,
                account_name='user@163.com',
                transport_kind='pop3_uidl',
                transport_uid='UID_FAKE',
                bank_code='CMB',
                statement_id=self.s1_id,
                message_id='<bill.202610@cmb.cn>',
                evidence_digest='FAKE_DIGEST_WITHOUT_EVIDENCE',
                method='digest_equal',
                actor='auto_sync',
                reason='fake'
            )
        self.assertIn('source_evidence', str(ctx.exception))

    def test_missing_target_evidence_rejected(self):
        """反例 (Gap 1 P0)：目标账单没有可信证据索引时拒绝 digest_equal 关联"""
        s_unindexed = StatementRecord(
            account_name='user@163.com', uid='UID_NO_EV', message_id='<bill.noev@cmb.cn>',
            bank_code='CMB', rule_id='CMB_TEMPLATE_V1', subject='招商银行信用卡电子账单', sender='service@cmb.cn',
            email_date='2026-10-10', statement_month='2026-10', statement_date='2026-10-10',
            due_date='2026-10-28', total_due='10.00', minimum_due='1.00', raw_fields_json='{}'
        )
        unindexed_id, _ = sd.save_statement_with_transactions(self.db, s_unindexed, [])

        cand_ev = self._make_candidate_evidence(uid='UID_CAND')
        with self.assertRaises(ValueError) as ctx:
            sd.associate_transport_identity(
                self.db,
                account_name='user@163.com',
                transport_kind='pop3_uidl',
                transport_uid='UID_CAND',
                bank_code='CMB',
                statement_id=unindexed_id,
                message_id='<bill.noev@cmb.cn>',
                evidence_digest=cand_ev['complete_content_digest'],
                raw_content_hash=cand_ev['raw_content_hash'],
                method='digest_equal',
                actor='auto_sync',
                reason='test',
                source_evidence=cand_ev
            )
        self.assertIn('target_evidence', str(ctx.exception))

    def test_incomplete_coverage_rejected(self):
        """反例 (Gap 1 P0)：证据未达到 complete 覆盖状态时拒绝关联"""
        cand_ev = self._make_candidate_evidence(uid='UID_INCOMPLETE')
        cand_ev['coverage_status'] = 'missing_raw_bytes'
        with self.assertRaises(ValueError) as ctx:
            sd.associate_transport_identity(
                self.db, account_name='user@163.com', transport_kind='pop3_uidl',
                transport_uid='UID_INCOMPLETE', bank_code='CMB', statement_id=self.s1_id,
                message_id='<bill.202610@cmb.cn>', evidence_digest=cand_ev['complete_content_digest'],
                raw_content_hash=cand_ev['raw_content_hash'], method='digest_equal',
                actor='auto_sync', reason='test', source_evidence=cand_ev, target_evidence=self.ev1
            )
        self.assertIn('coverage_status', str(ctx.exception))

    def test_digest_mismatch_rejected(self):
        """反例 (Gap 1 P0)：全内容摘要不一致时拒绝建立映射"""
        cand_ev = self._make_candidate_evidence(uid='UID_DIFF', body='Different Body Content')
        with self.assertRaises(ValueError) as ctx:
            sd.associate_transport_identity(
                self.db, account_name='user@163.com', transport_kind='pop3_uidl',
                transport_uid='UID_DIFF', bank_code='CMB', statement_id=self.s1_id,
                message_id='<bill.202610@cmb.cn>', evidence_digest=cand_ev['complete_content_digest'],
                raw_content_hash=cand_ev['raw_content_hash'], method='digest_equal',
                actor='auto_sync', reason='test', source_evidence=cand_ev, target_evidence=self.ev1
            )
        self.assertIn('摘要不匹配', str(ctx.exception))

    def test_transport_key_bound_to_different_statement_fails(self):
        """运输键已绑定到 statement_id A，再次绑定到 statement_id B 必须报错拒绝"""
        s2 = StatementRecord(
            account_name='user@163.com', uid='UID_POP_9999', message_id='<other.bill@cmb.cn>',
            bank_code='CMB', rule_id='CMB_TEMPLATE_V1', subject='招商银行信用卡电子账单',
            sender='service@cmb.cn', email_date='2026-10-10', statement_month='2026-10',
            statement_date='2026-10-10', due_date='2026-10-28', total_due='500.00',
            minimum_due='50.00', raw_fields_json='{}'
        )
        s2_id, _ = sd.save_statement_with_transactions(self.db, s2, [])

        cand_ev = self._make_candidate_evidence(uid='UID_SHARED')
        sd.associate_transport_identity(
            self.db, account_name='user@163.com', transport_kind='pop3_uidl',
            transport_uid='UID_SHARED', bank_code='CMB', statement_id=self.s1_id,
            message_id='<bill.202610@cmb.cn>', evidence_digest=cand_ev['complete_content_digest'],
            raw_content_hash=cand_ev['raw_content_hash'], method='digest_equal',
            actor='test', reason='test', source_evidence=cand_ev, target_evidence=self.ev1
        )

        with self.assertRaises(sd.MessageIdentityConflict):
            sd.associate_transport_identity(
                self.db, account_name='user@163.com', transport_kind='pop3_uidl',
                transport_uid='UID_SHARED', bank_code='CMB', statement_id=s2_id,
                message_id='<other.bill@cmb.cn>', evidence_digest=cand_ev['complete_content_digest'],
                raw_content_hash=cand_ev['raw_content_hash'], method='digest_equal',
                actor='test', reason='test', source_evidence=cand_ev, target_evidence=self.ev1
            )

    def test_same_transport_key_different_content_hash_fails(self):
        """反例 (§10.4)：运输键相同，但收到不同内容 (raw_content_hash 改变) 必须报冲突"""
        cand_ev = self._make_candidate_evidence(uid='UID_TEST_HASH')
        sd.associate_transport_identity(
            self.db, account_name='user@163.com', transport_kind='pop3_uidl',
            transport_uid='UID_TEST_HASH', bank_code='CMB', statement_id=self.s1_id,
            message_id='<bill.202610@cmb.cn>', evidence_digest=cand_ev['complete_content_digest'],
            raw_content_hash=cand_ev['raw_content_hash'], method='digest_equal',
            actor='test', reason='test', source_evidence=cand_ev, target_evidence=self.ev1
        )

        cand_ev_drift = self._make_candidate_evidence(uid='UID_TEST_HASH', body='Drifted content')
        with self.assertRaises(sd.MessageIdentityConflict) as ctx:
            sd.associate_transport_identity(
                self.db, account_name='user@163.com', transport_kind='pop3_uidl',
                transport_uid='UID_TEST_HASH', bank_code='CMB', statement_id=self.s1_id,
                message_id='<bill.202610@cmb.cn>', evidence_digest=cand_ev['complete_content_digest'],
                raw_content_hash=cand_ev_drift['raw_content_hash'], method='manual_verified',
                actor='test', reason='test', plan_id='p1', plan_hash='h1',
                anchor_snapshot={'total_due': '1234.56', 'is_paid': 0},
                source_evidence=cand_ev_drift, target_evidence=self.ev1
            )
        self.assertIn('raw content hash', str(ctx.exception))

    def test_find_canonical_and_uid_exists(self):
        """测试 find_canonical_by_transport 以及 uid_exists 对别名的感知"""
        self.assertTrue(sd.uid_exists(self.db, 'UID_POP_1001', 'user@163.com'))
        self.assertFalse(sd.uid_exists(self.db, 'UID_ALIAS_3003', 'user@163.com'))

        cand_ev = self._make_candidate_evidence(uid='UID_ALIAS_3003')
        sd.associate_transport_identity(
            self.db, account_name='user@163.com', transport_kind='pop3_uidl',
            transport_uid='UID_ALIAS_3003', bank_code='CMB', statement_id=self.s1_id,
            message_id='<bill.202610@cmb.cn>', evidence_digest=cand_ev['complete_content_digest'],
            raw_content_hash=cand_ev['raw_content_hash'], method='digest_equal',
            actor='auto_sync', reason='content digest equal', source_evidence=cand_ev, target_evidence=self.ev1
        )

        self.assertTrue(sd.uid_exists(self.db, 'UID_ALIAS_3003', 'user@163.com'))

        canonical = sd.find_canonical_by_transport(self.db, 'user@163.com', 'UID_ALIAS_3003', bank_code='CMB')
        self.assertIsNotNone(canonical)
        self.assertEqual(canonical['id'], self.s1_id)
        self.assertEqual(canonical['uid'], 'UID_POP_1001')

    def test_find_message_id_conflicts_excludes_associated_alias(self):
        """测试 find_message_id_conflicts 排除已关联别名"""
        conflicts = sd.find_message_id_conflicts(
            self.db, account_name='user@163.com', message_id='<bill.202610@cmb.cn>',
            bank_code='CMB', uid='UID_ANOTHER'
        )
        self.assertEqual(len(conflicts), 1)
        self.assertEqual(conflicts[0]['id'], self.s1_id)

        cand_ev = self._make_candidate_evidence(uid='UID_ANOTHER')
        sd.associate_transport_identity(
            self.db, account_name='user@163.com', transport_kind='pop3_uidl',
            transport_uid='UID_ANOTHER', bank_code='CMB', statement_id=self.s1_id,
            message_id='<bill.202610@cmb.cn>', evidence_digest=cand_ev['complete_content_digest'],
            raw_content_hash=cand_ev['raw_content_hash'], method='digest_equal',
            actor='auto_sync', reason='content digest equal', source_evidence=cand_ev, target_evidence=self.ev1
        )

        conflicts_after = sd.find_message_id_conflicts(
            self.db, account_name='user@163.com', message_id='<bill.202610@cmb.cn>',
            bank_code='CMB', uid='UID_ANOTHER'
        )
        self.assertEqual(len(conflicts_after), 0)



    def test_fake_complete_evidence_dict_rejected(self):
        """反例 (T1 P0)：伪造 complete 证据字典无原文无索引，底层必须坚决拒绝并无任何审计/映射副作用"""
        fake_dict = {
            'coverage_status': 'complete',
            'normalization_version': 'v1',
            'complete_content_digest': 'FAKE_DIGEST_WITHOUT_EVIDENCE',
            'raw_content_hash': 'FAKE_RAW_HASH_WITHOUT_EVIDENCE'
        }
        with self.assertRaises(ValueError):
            sd.associate_transport_identity(
                self.db, account_name='user@163.com', transport_kind='pop3_uidl',
                transport_uid='UID_FAKE_ATTACK', bank_code='CMB', statement_id=self.s1_id,
                message_id='<bill.202610@cmb.cn>', evidence_digest='FAKE_DIGEST_WITHOUT_EVIDENCE',
                raw_content_hash='FAKE_RAW_HASH_WITHOUT_EVIDENCE', method='digest_equal',
                actor='attacker', reason='fake dict', source_evidence=fake_dict, target_evidence=fake_dict
            )

        # 验证零映射与零审计副作用
        import sqlite3
        conn = sqlite3.connect(self.db)
        cur = conn.cursor()
        cur.execute("SELECT COUNT(*) FROM statement_transport_identities WHERE transport_uid = 'UID_FAKE_ATTACK'")
        self.assertEqual(cur.fetchone()[0], 0)
        cur.execute("SELECT COUNT(*) FROM statement_identity_audit WHERE transport_uid = 'UID_FAKE_ATTACK'")
        self.assertEqual(cur.fetchone()[0], 0)
        conn.close()

    def test_source_evidence_without_raw_bytes_or_archive_rejected(self):
        """反例 (T1 P0)：源证据缺失可信原始字节与归档引用，拒绝未经验证的字典"""
        cand_ev = self._make_candidate_evidence(uid='UID_NO_BYTES')
        cand_ev['raw_bytes'] = None  # 抹去原始字节且无文件路径
        cand_ev['archive_path'] = None
        with self.assertRaises(ValueError) as ctx:
            sd.associate_transport_identity(
                self.db, account_name='user@163.com', transport_kind='pop3_uidl',
                transport_uid='UID_NO_BYTES', bank_code='CMB', statement_id=self.s1_id,
                message_id='<bill.202610@cmb.cn>', evidence_digest=cand_ev['complete_content_digest'],
                raw_content_hash=cand_ev['raw_content_hash'], method='digest_equal',
                actor='auto_sync', reason='test', source_evidence=cand_ev, target_evidence=self.ev1
            )
        self.assertIn('原始字节或可信归档引用', str(ctx.exception))

    def test_evidence_index_immutable_baseline_conflict_rejected(self):
        """反例 (T4 P1)：不可变基线保护：相同 statement_id 试图以冲突的摘要覆盖 complete 证据时必须拒绝"""
        with self.assertRaises(ValueError) as ctx:
            sd.save_statement_evidence_index(
                self.db, statement_id=self.s1_id, account_name='user@163.com',
                bank_code='CMB', message_id='<bill.202610@cmb.cn>',
                raw_content_hash='conflicting_raw_hash_999',
                evidence_digest='conflicting_digest_999',
                coverage_status='complete', archive_path=None
            )
        self.assertIn('Cannot overwrite immutable evidence index', str(ctx.exception))

    def test_evidence_index_protects_complete_from_incomplete_downgrade(self):
        """正例 (T4 P1)：已有 complete 证据基线，后续不完整覆盖 (如失败重解析) 不会降级覆盖基线"""
        sd.save_statement_evidence_index(
            self.db, statement_id=self.s1_id, account_name='user@163.com',
            bank_code='CMB', message_id='<bill.202610@cmb.cn>',
            raw_content_hash='some_hash', evidence_digest='some_digest',
            coverage_status='missing_raw_bytes', archive_path=None
        )
        ev = sd.get_statement_evidence(self.db, self.s1_id)
        self.assertEqual(ev['coverage_status'], 'complete')
        self.assertEqual(ev['evidence_digest'], self.ev1['complete_content_digest'])


    def test_save_email_records_warning_if_evidence_indexing_fails(self):
        """反例 (T4 P1)：首次保存若证据索引/归档发生异常，不可静默吞掉，必须记录可观察告警与待办状态"""
        from unittest.mock import patch
        import statement_parser as sp
        from parser_paths import resolve_parser_paths

        ctx = resolve_parser_paths(
            data_root=self.tmp,
            db_path=self.db,
            download_dir=self.tmp,
            report_dir=os.path.join(self.tmp, 'reports')
        )

        msg_fresh = email.message.EmailMessage()
        msg_fresh['Message-ID'] = '<new.bill.diskfull@cmb.cn>'
        msg_fresh['Subject'] = '招商银行信用卡电子账单'
        msg_fresh['From'] = 'service@cmb.cn'
        msg_fresh['Date'] = 'Sat, 10 Oct 2026 10:00:00 +0800'
        msg_fresh.set_content('Body for fresh bill')

        with patch('statement_parser.save_statement_evidence_index', side_effect=RuntimeError('Disk Full Simulator')):
            res = sp.validate_and_save_email_message(
                msg_fresh,
                uid='UID_DISK_FULL_SIM',
                account_name='user@163.com',
                raw_bytes=msg_fresh.as_bytes(),
                context=ctx
            )
            self.assertEqual(res['evidence_status'], 'failed')
            self.assertGreaterEqual(res['warnings'], 1)
            import sqlite3
            conn = sqlite3.connect(self.db)
            cur = conn.cursor()
            cur.execute("SELECT code, severity FROM validation_issues WHERE run_id = ?", (res['run_id'],))
            issues = cur.fetchall()
            self.assertTrue(any(c == 'EVIDENCE_INDEX_FAILED' and s == 'warning' for c, s in issues))
            conn.close()


    def test_s1_tampering_source_headers_rejected(self):
        """反例 (S1 P0)：调用者篡改发件人头部但保留真实原始字节，底层锁内重建必须发现矛盾并拒绝"""
        cand_ev = self._make_candidate_evidence(uid='UID_TAMPER_FROM')
        # 篡改字典里的发件人
        cand_ev['headers']['from'] = 'attacker@evil.com'
        with self.assertRaises(ValueError) as ctx:
            sd.associate_transport_identity(
                self.db, account_name='user@163.com', transport_kind='pop3_uidl',
                transport_uid='UID_TAMPER_FROM', bank_code='CMB', statement_id=self.s1_id,
                message_id='<bill.202610@cmb.cn>', evidence_digest=cand_ev['complete_content_digest'],
                raw_content_hash=cand_ev['raw_content_hash'], method='digest_equal',
                actor='auto_sync', reason='tamper test', source_evidence=cand_ev, target_evidence=self.ev1
            )
        self.assertIn('From 头与原始字节重建头部不符', str(ctx.exception))

    def test_s1_tampering_source_tree_nodes_rejected(self):
        """反例 (S1 P0)：调用者伪造 MIME 树结构但保留原始字节，底层锁内重建必须发现矛盾并拒绝"""
        cand_ev = self._make_candidate_evidence(uid='UID_TAMPER_TREE')
        # 伪造插入一个恶意附件节点
        cand_ev['tree_nodes'].append({'content_type': 'application/x-executable', 'filename': 'malware.exe'})
        with self.assertRaises(ValueError) as ctx:
            sd.associate_transport_identity(
                self.db, account_name='user@163.com', transport_kind='pop3_uidl',
                transport_uid='UID_TAMPER_TREE', bank_code='CMB', statement_id=self.s1_id,
                message_id='<bill.202610@cmb.cn>', evidence_digest=cand_ev['complete_content_digest'],
                raw_content_hash=cand_ev['raw_content_hash'], method='digest_equal',
                actor='auto_sync', reason='tamper tree test', source_evidence=cand_ev, target_evidence=self.ev1
            )
        self.assertIn('MIME 树结构与原始字节重建树不符', str(ctx.exception))

    def test_s1_missing_target_archive_file_rejected(self):
        """反例 (S1 P0)：目标索引指向的原始归档文件被删除或不存在，必须拒绝自动关联"""
        # 创建一个无实际磁盘文件的目标
        s_ghost = StatementRecord(
            account_name='user@163.com', uid='UID_NO_FILE', message_id='<bill.nofile@cmb.cn>',
            bank_code='CMB', rule_id='CMB_TEMPLATE_V1', subject='招商银行信用卡电子账单',
            sender='service@cmb.cn', email_date='2026-10-10', statement_month='2026-10',
            statement_date='2026-10-10', due_date='2026-10-28', total_due='100.00',
            minimum_due='10.00', raw_fields_json='{}'
        )
        s_ghost_id, _ = sd.save_statement_with_transactions(self.db, s_ghost, [])
        sd.save_statement_evidence_index(
            self.db, statement_id=s_ghost_id, account_name='user@163.com',
            bank_code='CMB', message_id='<bill.nofile@cmb.cn>',
            raw_content_hash='some_hash_123', evidence_digest='some_dig_123',
            coverage_status='complete', archive_path=os.path.join(self.tmp, 'nonexistent_file.eml')
        )
        cand_ev = self._make_candidate_evidence(uid='UID_CAND_GHOST')
        with self.assertRaises(ValueError) as ctx:
            sd.associate_transport_identity(
                self.db, account_name='user@163.com', transport_kind='pop3_uidl',
                transport_uid='UID_CAND_GHOST', bank_code='CMB', statement_id=s_ghost_id,
                message_id='<bill.nofile@cmb.cn>', evidence_digest='some_dig_123',
                raw_content_hash=cand_ev['raw_content_hash'], method='digest_equal',
                actor='auto_sync', reason='missing target archive', source_evidence=cand_ev
            )
        self.assertIn('目标证据必须具备可重读原始归档文件', str(ctx.exception))

    def test_s1_tampered_target_archive_file_rejected(self):
        """反例 (S1 P0)：目标原始归档文件被篡改 (SHA-256 不符)，锁内校验必须拒绝"""
        tampered_archive = os.path.join(self.tmp, 'tampered_target.eml')
        with open(tampered_archive, 'wb') as fp:
            fp.write(b'Corrupted Target File Content')
        s_tampered = StatementRecord(
            account_name='user@163.com', uid='UID_TAMP_FILE', message_id='<bill.tampfile@cmb.cn>',
            bank_code='CMB', rule_id='CMB_TEMPLATE_V1', subject='招商银行信用卡电子账单',
            sender='service@cmb.cn', email_date='2026-10-10', statement_month='2026-10',
            statement_date='2026-10-10', due_date='2026-10-28', total_due='100.00',
            minimum_due='10.00', raw_fields_json='{}'
        )
        s_tamp_id, _ = sd.save_statement_with_transactions(self.db, s_tampered, [])
        sd.save_statement_evidence_index(
            self.db, statement_id=s_tamp_id, account_name='user@163.com',
            bank_code='CMB', message_id='<bill.tampfile@cmb.cn>',
            raw_content_hash='expected_hash_different_from_corrupted', evidence_digest='some_dig_123',
            coverage_status='complete', archive_path=tampered_archive
        )
        cand_ev = self._make_candidate_evidence(uid='UID_CAND_TAMP')
        with self.assertRaises(ValueError) as ctx:
            sd.associate_transport_identity(
                self.db, account_name='user@163.com', transport_kind='pop3_uidl',
                transport_uid='UID_CAND_TAMP', bank_code='CMB', statement_id=s_tamp_id,
                message_id='<bill.tampfile@cmb.cn>', evidence_digest='some_dig_123',
                raw_content_hash=cand_ev['raw_content_hash'], method='digest_equal',
                actor='auto_sync', reason='tampered target archive', source_evidence=cand_ev
            )
        self.assertIn('目标证据归档哈希被篡改', str(ctx.exception))

    def test_s2_received_header_differs_raw_hash_but_content_equal_associates(self):
        """正例 (S2 P1)：新增 Received 运输头导致 raw_content_hash 改变，但全内容摘要相同，自动关联必须成功"""
        cand_ev = self._make_candidate_evidence(
            uid='UID_POP_WITH_RECEIVED',
            headers={'Received': 'by relay.example.invalid (Postfix) with ESMTPS id 12345'}
        )
        # 确认 raw_content_hash 确实不同
        self.assertNotEqual(cand_ev['raw_content_hash'], self.ev1['raw_content_hash'])
        # 确认 complete_content_digest 保持一致 (因为 Received 是被排除的运输头)
        self.assertEqual(cand_ev['complete_content_digest'], self.ev1['complete_content_digest'])

        res = sd.associate_transport_identity(
            self.db,
            account_name='user@163.com',
            transport_kind='pop3_uidl',
            transport_uid='UID_POP_WITH_RECEIVED',
            bank_code='CMB',
            statement_id=self.s1_id,
            message_id='<bill.202610@cmb.cn>',
            evidence_digest=cand_ev['complete_content_digest'],
            raw_content_hash=cand_ev['raw_content_hash'],
            method='digest_equal',
            actor='auto_sync',
            reason='relay header received diff',
            source_evidence=cand_ev,
            target_evidence=self.ev1
        )
        self.assertEqual(res['status'], 'associated')
        self.assertFalse(res['idempotent'])

        # 验证数据库记录保持财务守恒与正确映射
        import sqlite3
        conn = sqlite3.connect(self.db)
        cur = conn.cursor()
        cur.execute("SELECT total_due, is_paid FROM statements WHERE id = ?", (self.s1_id,))
        st_row = cur.fetchone()
        self.assertEqual(st_row[0], '1234.56')
        self.assertEqual(st_row[1], 0)
        conn.close()

    def test_s2_idempotent_reassociate_with_received_header_variation(self):
        """正例 (S2 P1)：已关联运输键在遇到仅有 Received 头部差异的内容时，仍正常幂等返回而不报冲突"""
        cand_ev = self._make_candidate_evidence(uid='UID_POP_IDEM_RELAY')
        # 首次关联
        sd.associate_transport_identity(
            self.db, account_name='user@163.com', transport_kind='pop3_uidl',
            transport_uid='UID_POP_IDEM_RELAY', bank_code='CMB', statement_id=self.s1_id,
            message_id='<bill.202610@cmb.cn>', evidence_digest=cand_ev['complete_content_digest'],
            raw_content_hash=cand_ev['raw_content_hash'], method='digest_equal',
            actor='auto_sync', reason='initial', source_evidence=cand_ev, target_evidence=self.ev1
        )

        # 再次关联：携带 Received 头的同等内容变体 (raw_content_hash 不同，digest 相同)
        cand_ev_variant = self._make_candidate_evidence(
            uid='UID_POP_IDEM_RELAY',
            headers={'Received': 'by relay2.example.invalid (Postfix) with ESMTPS'}
        )
        self.assertNotEqual(cand_ev_variant['raw_content_hash'], cand_ev['raw_content_hash'])
        self.assertEqual(cand_ev_variant['complete_content_digest'], cand_ev['complete_content_digest'])

        res_idem = sd.associate_transport_identity(
            self.db, account_name='user@163.com', transport_kind='pop3_uidl',
            transport_uid='UID_POP_IDEM_RELAY', bank_code='CMB', statement_id=self.s1_id,
            message_id='<bill.202610@cmb.cn>', evidence_digest=cand_ev_variant['complete_content_digest'],
            raw_content_hash=cand_ev_variant['raw_content_hash'], method='digest_equal',
            actor='auto_sync', reason='idempotent variant',
            source_evidence=cand_ev_variant, target_evidence=self.ev1
        )
        self.assertEqual(res_idem['status'], 'already_associated')
        self.assertTrue(res_idem['idempotent'])


    def test_u1_manual_verified_rejects_fake_plan_or_hash(self):
        """反例 (U1 P0)：提供任意伪造 plan_id 与 plan_hash 字符串，未经过受控 preview 报告，底层坚决拒绝"""
        cand_ev = self._make_candidate_evidence(uid='UID_U1_FAKE')
        with self.assertRaises(ValueError) as ctx:
            sd.associate_transport_identity(
                self.db,
                account_name='user@163.com',
                transport_kind='pop3_uidl',
                transport_uid='UID_U1_FAKE',
                bank_code='CMB',
                statement_id=self.s1_id,
                message_id='<bill.202610@cmb.cn>',
                method='manual_verified',
                actor='reviewer_bob',
                reason='trying fake plan',
                plan_id='fake-plan-id-12345',
                plan_hash='fake-hash-67890',
                anchor_snapshot={'total_due': '1234.56', 'is_paid': 0},
                source_evidence=cand_ev,
                target_evidence=self.ev1
            )
        self.assertIn('未找到计划文件或计划未验证', str(ctx.exception))

    def test_u1_manual_verified_rejects_mismatched_subject(self):
        """反例 (U1 P0)：人工确认放宽完整性，但绝不放宽主题等已知硬矛盾"""
        # 源端真实原始字节即包含不同主题
        cand_ev = self._make_candidate_evidence(
            uid='UID_U1_DIFF_SUBJ',
            headers={'Subject': 'DIFFERENT SUBJECT FROM TARGET'}
        )
        with self.assertRaises(ValueError) as ctx:
            sd.associate_transport_identity(
                self.db,
                account_name='user@163.com',
                transport_kind='pop3_uidl',
                transport_uid='UID_U1_DIFF_SUBJ',
                bank_code='CMB',
                statement_id=self.s1_id,
                message_id='<bill.202610@cmb.cn>',
                method='manual_verified',
                actor='reviewer_bob',
                reason='testing hard contradiction',
                plan_id='fake_p',
                plan_hash='fake_h',
                anchor_snapshot={'total_due': '1234.56', 'is_paid': 0},
                source_evidence=cand_ev
            )
        self.assertIn('主题不匹配', str(ctx.exception))

    def test_u1_manual_verified_rejects_target_modified_after_preview(self):
        """反例 (U1 P0)：preview 计划生成之后，若目标账单在数据库中被修改，confirm 必须安全拒绝"""
        import json
        cand_ev = self._make_candidate_evidence(uid='UID_U1_POST_MOD')
        plan = si.generate_identity_plan(
            cand_ev, self.ev1,
            target_statement_id=self.s1_id,
            anchor_snapshot={'total_due': '1234.56', 'is_paid': 0, 'message_id': '<bill.202610@cmb.cn>'}
        )
        plan_path = os.path.join(self.tmp, f"plan_{plan['plan_id']}.json")
        with open(plan_path, 'w', encoding='utf-8') as fp:
            json.dump(plan, fp)

        # 篡改数据库中的目标账单金额
        import sqlite3
        conn = sqlite3.connect(self.db)
        conn.execute("UPDATE statements SET total_due = '9999.99' WHERE id = ?", (self.s1_id,))
        conn.commit()
        conn.close()

        with self.assertRaises(ValueError) as ctx:
            sd.associate_transport_identity(
                self.db,
                account_name='user@163.com',
                transport_kind='pop3_uidl',
                transport_uid='UID_U1_POST_MOD',
                bank_code='CMB',
                statement_id=self.s1_id,
                message_id='<bill.202610@cmb.cn>',
                method='manual_verified',
                actor='reviewer_bob',
                reason='confirm after modified',
                plan_id=plan['plan_id'],
                plan_hash=plan['plan_hash'],
                plan_path=plan_path,
                anchor_snapshot=plan['anchor_snapshot'],
                source_evidence=cand_ev
            )
        self.assertTrue('被修改' in str(ctx.exception) or 'drifted' in str(ctx.exception))

    def test_u2_existing_key_unverified_without_evidence(self):
        """正例 (U2 P0)：已有运输键重投但未提供任何证据或正文，返回 repeated_key_unverified，不冒充经验证关联"""
        cand_ev = self._make_candidate_evidence(uid='UID_U2_KEY')
        sd.associate_transport_identity(
            self.db, account_name='user@163.com', transport_kind='pop3_uidl',
            transport_uid='UID_U2_KEY', bank_code='CMB', statement_id=self.s1_id,
            message_id='<bill.202610@cmb.cn>', evidence_digest=cand_ev['complete_content_digest'],
            raw_content_hash=cand_ev['raw_content_hash'], method='digest_equal',
            actor='auto_sync', reason='initial', source_evidence=cand_ev, target_evidence=self.ev1
        )

        import sqlite3
        conn = sqlite3.connect(self.db)
        cur = conn.cursor()
        cur.execute("SELECT COUNT(*) FROM statement_identity_audit WHERE transport_uid = 'UID_U2_KEY'")
        audit_count_before = cur.fetchone()[0]
        conn.close()

        # 完全不带证据重投
        res = sd.associate_transport_identity(
            self.db, account_name='user@163.com', transport_kind='pop3_uidl',
            transport_uid='UID_U2_KEY', bank_code='CMB', statement_id=self.s1_id,
            message_id='<bill.202610@cmb.cn>', method='digest_equal',
            actor='auto_sync', reason='empty retry'
        )
        self.assertEqual(res['status'], 'repeated_key_unverified')
        self.assertTrue(res['idempotent'])

        # 验证零审计副作用
        conn = sqlite3.connect(self.db)
        cur = conn.cursor()
        cur.execute("SELECT COUNT(*) FROM statement_identity_audit WHERE transport_uid = 'UID_U2_KEY'")
        audit_count_after = cur.fetchone()[0]
        conn.close()
        self.assertEqual(audit_count_before, audit_count_after)

    def test_u2_existing_key_rejects_real_body_change_even_with_fake_digest(self):
        """反例 (U2 P0)：已有运输键收到不同正文内容，即便伪造旧摘要，重算证据后必须报 MessageIdentityConflict 冲突"""
        cand_ev = self._make_candidate_evidence(uid='UID_U2_DRIFT')
        sd.associate_transport_identity(
            self.db, account_name='user@163.com', transport_kind='pop3_uidl',
            transport_uid='UID_U2_DRIFT', bank_code='CMB', statement_id=self.s1_id,
            message_id='<bill.202610@cmb.cn>', evidence_digest=cand_ev['complete_content_digest'],
            raw_content_hash=cand_ev['raw_content_hash'], method='digest_equal',
            actor='auto_sync', reason='initial', source_evidence=cand_ev, target_evidence=self.ev1
        )

        cand_ev_drift = self._make_candidate_evidence(uid='UID_U2_DRIFT', body='Completely Different Body')
        with self.assertRaises(sd.MessageIdentityConflict):
            sd.associate_transport_identity(
                self.db, account_name='user@163.com', transport_kind='pop3_uidl',
                transport_uid='UID_U2_DRIFT', bank_code='CMB', statement_id=self.s1_id,
                message_id='<bill.202610@cmb.cn>',
                evidence_digest=cand_ev['complete_content_digest'], # 伪造旧摘要
                method='digest_equal', actor='auto_sync', reason='drift',
                source_evidence=cand_ev_drift, target_evidence=self.ev1
            )

    def test_u3_parser_canonical_accepts_received_header_and_rejects_body_change(self):
        """正反例 (U3 P1)：真实 Parser 入口已有关联通过统一事务服务核验，放行 Received 头变化，拒绝真实正文变化"""
        import statement_parser as sp
        from parser_paths import resolve_parser_paths

        ctx = resolve_parser_paths(
            data_root=self.tmp, db_path=self.db, download_dir=self.tmp, report_dir=os.path.join(self.tmp, 'reports')
        )

        # 建立初始已关联运输键 UID_U3_CANONICAL
        cand_init = self._make_candidate_evidence(uid='UID_U3_CANONICAL')
        sd.associate_transport_identity(
            self.db, account_name='user@163.com', transport_kind='pop3_uidl',
            transport_uid='UID_U3_CANONICAL', bank_code='CMB', statement_id=self.s1_id,
            message_id='<bill.202610@cmb.cn>', evidence_digest=cand_init['complete_content_digest'],
            raw_content_hash=cand_init['raw_content_hash'], method='digest_equal',
            actor='auto_sync', reason='initial', source_evidence=cand_init, target_evidence=self.ev1
        )

        # 同运输键重投：仅增加 Received 运输头 (规范化后内容等价)
        msg_variant = email.message.EmailMessage()
        msg_variant['Message-ID'] = '<bill.202610@cmb.cn>'
        msg_variant['Subject'] = '招商银行信用卡电子账单'
        msg_variant['From'] = 'service@cmb.cn'
        msg_variant['Date'] = 'Sat, 10 Oct 2026 10:00:00 +0800'
        msg_variant['Received'] = 'by relay.postfix.local with ESMTPS'
        msg_variant.set_content('Body of October Bill')
        raw_variant = msg_variant.as_bytes()

        res2 = sp.validate_and_save_email_message(
            msg_variant, uid='UID_U3_CANONICAL', account_name='user@163.com', raw_bytes=raw_variant, context=ctx
        )
        self.assertEqual(res2['status'], 'associated_existing')
        self.assertTrue(res2['idempotent'])

        # 同运输键重投：改变正文内容 -> 必须报冲突
        msg_diff = email.message.EmailMessage()
        msg_diff['Message-ID'] = '<bill.202610@cmb.cn>'
        msg_diff['Subject'] = '招商银行信用卡电子账单'
        msg_diff['From'] = 'service@cmb.cn'
        msg_diff['Date'] = 'Sat, 10 Oct 2026 10:00:00 +0800'
        msg_diff.set_content('Different tampered content body')
        raw_diff = msg_diff.as_bytes()

        res3 = sp.validate_and_save_email_message(
            msg_diff, uid='UID_U3_CANONICAL', account_name='user@163.com', raw_bytes=raw_diff, context=ctx
        )
        self.assertEqual(res3['status'], 'identity_conflict')

    def test_manual_incomplete_evidence_replay_requires_identical_bytes(self):
        import json
        msg = email.message_from_bytes(self.raw_bytes)
        msg.replace_header('Content-Type', 'multipart/mixed; boundary="missing"')
        raw = msg.as_bytes()
        ev = si.build_evidence_from_raw_eml(raw, account_name='user@163.com',
                                           bank_code='CMB', transport_uid='manual-partial')
        self.assertFalse(ev['complete_mime'])
        anchor = {'total_due': '1234.56', 'is_paid': 0}
        plan = si.generate_identity_plan(ev, self.ev1, target_statement_id=self.s1_id,
                                         anchor_snapshot=anchor)
        path = os.path.join(self.tmp, 'manual-partial-plan.json')
        with open(path, 'w') as f:
            json.dump(plan, f)
        kwargs = dict(account_name='user@163.com', transport_kind='pop3_uidl',
                      transport_uid='manual-partial', bank_code='CMB',
                      statement_id=self.s1_id, message_id='<bill.202610@cmb.cn>')
        sd.associate_transport_identity(self.db, **kwargs, source_evidence=ev,
                                       source_raw_bytes=raw, method='manual_verified',
                                       actor='fixture_reviewer', reason='fixture manual approval',
                                       plan_id=plan['plan_id'], plan_hash=plan['plan_hash'],
                                       plan_path=path, anchor_snapshot=anchor)
        result = sd.associate_transport_identity(self.db, **kwargs, source_raw_bytes=raw)
        self.assertEqual(result['status'], 'already_associated')
        with self.assertRaises(sd.MessageIdentityConflict):
            sd.associate_transport_identity(self.db, **kwargs, source_raw_bytes=raw + b'changed')
        import sqlite3
        with sqlite3.connect(self.db) as c:
            self.assertEqual(c.execute('SELECT COUNT(*) FROM statement_identity_audit').fetchone()[0], 1)

    def test_existing_claim_without_raw_content_is_rejected(self):
        ev = self._make_candidate_evidence(uid='closeout-claim')
        kwargs = dict(account_name='user@163.com', transport_kind='pop3_uidl',
                      transport_uid='closeout-claim', bank_code='CMB',
                      statement_id=self.s1_id, message_id='<bill.202610@cmb.cn>',
                      evidence_digest=ev['complete_content_digest'])
        sd.associate_transport_identity(self.db, source_evidence=ev, **kwargs)
        ev.pop('raw_bytes', None)
        ev.pop('archive_path', None)
        ev.pop('eml_path', None)
        with self.assertRaisesRegex(ValueError, '可重读原文'):
            sd.associate_transport_identity(self.db, source_evidence=ev, **kwargs)
        import sqlite3
        conn = sqlite3.connect(self.db)
        try:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM statement_identity_audit WHERE transport_uid='closeout-claim'").fetchone()[0], 1)
        finally:
            conn.close()

if __name__ == '__main__':
    unittest.main()
