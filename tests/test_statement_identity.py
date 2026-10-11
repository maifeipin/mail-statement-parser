#!/usr/bin/env python3
"""
tests/test_statement_identity.py
单元测试：内容证据契约与反例测试矩阵 (E1 §3, §10)
"""

import email
import email.message
import email.policy
import hashlib
import json
import unittest

import os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import statement_identity as si


class TestStatementIdentityContract(unittest.TestCase):

    def test_message_policy_cannot_change_raw_evidence_digest(self):
        raw = (b'Message-ID: <policy@example.invalid>\r\n'
               b'From: "Bank"<billing@example.invalid>\r\n'
               b'Subject: Monthly statement\r\n'
               b'Date: Sat, 10 Oct 2026 10:00:00 +0800\r\n'
               b'Content-Type: text/plain; charset="utf-8"\r\n\r\nbody\r\n')
        canonical = si.build_evidence_from_raw_eml(raw, 'fixture', 'CMB')
        for policy in (email.policy.compat32, email.policy.default):
            msg = email.message_from_bytes(raw, policy=policy)
            evidence = si.build_evidence_from_email_message(msg, raw_bytes=raw,
                                                            account_name='fixture', bank_code='CMB')
            self.assertEqual(evidence['complete_content_digest'], canonical['complete_content_digest'])
            self.assertEqual(evidence['headers'], canonical['headers'])

    def test_sender_metadata_spacing_preserves_identity_boundaries(self):
        self.assertTrue(si.sender_metadata_equal('Bank <billing@example.invalid>',
                                                'Bank<billing@example.invalid>'))
        for candidate in ('Other <billing@example.invalid>', 'Bank <other@example.invalid>',
                          'Bank <billing@example.invalid>, Other <other@example.invalid>',
                          'Bank', ''):
            with self.subTest(candidate=candidate):
                self.assertFalse(si.sender_metadata_equal('Bank <billing@example.invalid>', candidate))

    def test_excluded_headers_lock(self):
        """排除头白名单锁定测试：确保传输跳板头被排除在内容签名之外，且普通作者头不被漏算"""
        locked_headers = {
            'received', 'return-path', 'delivered-to', 'received-spf',
            'authentication-results', 'dkim-signature', 'arc-seal',
            'arc-message-signature', 'arc-authentication-results',
            'x-received', 'x-google-smtp-source',
            'x-forefront-antispam-report', 'x-microsoft-antispam',
            'x-ms-exchange-organization-authas',
            'x-ms-exchange-organization-authsource',
            'x-ms-exchange-organization-network-message-id',
            'x-ms-exchange-organization-scl',
            'x-ms-traffictypediagnostic',
            'x-ms-office365-filtering-correlation-id',
            'status', 'x-status', 'x-keywords', 'x-uid'
        }
        for h in locked_headers:
            self.assertIn(h, si.EXCLUDED_HEADERS, f"Excluded header missing from lock: {h}")

    def test_message_id_case_preserved(self):
        """Message-ID 严格保留原始大小写与字符，仅剥除外围空格"""
        msg = email.message.EmailMessage()
        msg['Message-ID'] = '  <AbCdEf.1234@Bank.Com>  '
        hdr = si.extract_header_evidence(msg)
        self.assertEqual(hdr['message_id'], '<AbCdEf.1234@Bank.Com>')

    def test_text_parts_crlf_normalization(self):
        """文本正文 (text/plain, text/html) 规范化行尾：CRLF 与 LF 规范化为相同摘要"""
        msg1 = email.message.EmailMessage()
        msg1['Message-ID'] = '<msg1@bank.cn>'
        msg1['Subject'] = 'Test Bill'
        msg1['From'] = 'service@bank.cn'
        msg1['Date'] = 'Mon, 10 Oct 2026 10:00:00 +0800'
        msg1.set_content('Line 1\r\nLine 2\r\nLine 3\r\n')

        msg2 = email.message.EmailMessage()
        msg2['Message-ID'] = '<msg1@bank.cn>'
        msg2['Subject'] = 'Test Bill'
        msg2['From'] = 'service@bank.cn'
        msg2['Date'] = 'Mon, 10 Oct 2026 10:00:00 +0800'
        msg2.set_content('Line 1\nLine 2\nLine 3\n')

        b1, b2 = msg1.as_bytes(), msg2.as_bytes()
        ev1 = si.build_evidence_from_email_message(msg1, raw_bytes=b1, account_name='user@163.com', bank_code='CMB')
        ev2 = si.build_evidence_from_email_message(msg2, raw_bytes=b2, account_name='user@163.com', bank_code='CMB')

        self.assertEqual(ev1['complete_content_digest'], ev2['complete_content_digest'])
        self.assertEqual(ev1['comparable_body_digest'], ev2['comparable_body_digest'])

    def test_binary_attachment_byte_by_byte_no_crlf_normalization(self):
        """反例 1 (§10.1, §10.7)：二进制附件逐字节摘要，CRLF 与 LF 差异绝不能被当作相等"""
        bin_data_crlf = b"PDF-1.4\r\nstream\r\nendstream"
        bin_data_lf = b"PDF-1.4\nstream\nendstream"

        msg1 = email.message.EmailMessage()
        msg1['Message-ID'] = '<msg@bank.cn>'
        msg1['Subject'] = 'Bill'
        msg1['From'] = 'svc@bank.cn'
        msg1['Date'] = 'Mon, 10 Oct 2026 10:00:00 +0800'
        msg1.set_content('Body')
        msg1.add_attachment(bin_data_crlf, maintype='application', subtype='pdf', filename='bill.pdf')

        msg2 = email.message.EmailMessage()
        msg2['Message-ID'] = '<msg@bank.cn>'
        msg2['Subject'] = 'Bill'
        msg2['From'] = 'svc@bank.cn'
        msg2['Date'] = 'Mon, 10 Oct 2026 10:00:00 +0800'
        msg2.set_content('Body')
        msg2.add_attachment(bin_data_lf, maintype='application', subtype='pdf', filename='bill.pdf')

        b1, b2 = msg1.as_bytes(), msg2.as_bytes()
        ev1 = si.build_evidence_from_email_message(msg1, raw_bytes=b1, account_name='acct', bank_code='CMB')
        ev2 = si.build_evidence_from_email_message(msg2, raw_bytes=b2, account_name='acct', bank_code='CMB')

        # 附件 SHA-256 必须不同
        self.assertNotEqual(ev1['parts'][1]['part_sha256'], ev2['parts'][2 if len(ev2['parts']) > 2 else 1]['part_sha256'])
        self.assertNotEqual(ev1['complete_content_digest'], ev2['complete_content_digest'])

        status, reasons, _ = si.compare_evidence(ev1, ev2)
        self.assertEqual(status, 'identity_conflict')

    def test_mime_parts_order_and_duplication_preserved(self):
        """反例 2 (§10.1, §10.7)：MIME part 顺序或重复数量不同，摘要必须不同"""
        # msg1 两个相同名字但顺序为 A, B 的附件
        msg1 = email.message.EmailMessage()
        msg1['Message-ID'] = '<msg@bank.cn>'
        msg1['Subject'] = 'Bill'
        msg1['From'] = 'svc@bank.cn'
        msg1['Date'] = 'Mon, 10 Oct 2026 10:00:00 +0800'
        msg1.set_content('Body')
        msg1.add_attachment(b"Data_AAA", maintype='application', subtype='octet-stream', filename='f1.dat')
        msg1.add_attachment(b"Data_BBB", maintype='application', subtype='octet-stream', filename='f2.dat')

        # msg2 顺序相反为 B, A
        msg2 = email.message.EmailMessage()
        msg2['Message-ID'] = '<msg@bank.cn>'
        msg2['Subject'] = 'Bill'
        msg2['From'] = 'svc@bank.cn'
        msg2['Date'] = 'Mon, 10 Oct 2026 10:00:00 +0800'
        msg2.set_content('Body')
        msg2.add_attachment(b"Data_BBB", maintype='application', subtype='octet-stream', filename='f2.dat')
        msg2.add_attachment(b"Data_AAA", maintype='application', subtype='octet-stream', filename='f1.dat')

        b1, b2 = msg1.as_bytes(), msg2.as_bytes()
        ev1 = si.build_evidence_from_email_message(msg1, raw_bytes=b1, account_name='acct', bank_code='CMB')
        ev2 = si.build_evidence_from_email_message(msg2, raw_bytes=b2, account_name='acct', bank_code='CMB')

        self.assertNotEqual(ev1['complete_content_digest'], ev2['complete_content_digest'])
        status, _, _ = si.compare_evidence(ev1, ev2)
        self.assertEqual(status, 'identity_conflict')

    def test_charset_decode_failure_rejects_complete_evidence(self):
        """反例 3 (§10.1, §10.7)：字符集解码失败不使用 errors=ignore/replace，标记 decode_failed 且 complete_digest 为 None"""
        # 构造无效 utf-8 序列
        bad_bytes = b"\xff\xfe\xfd\x00\x80\x81"
        payload = (
            b'Content-Type: text/plain; charset="utf-8"\r\n'
            b'Content-Transfer-Encoding: 8bit\r\n'
            b'Message-ID: <bad@bank.cn>\r\n'
            b'Subject: Bad Charset\r\n'
            b'From: svc@bank.cn\r\n'
            b'Date: Mon, 10 Oct 2026 10:00:00 +0800\r\n\r\n'
        ) + bad_bytes
        msg = email.message_from_bytes(payload)
        ev = si.build_evidence_from_email_message(msg, raw_bytes=payload, account_name='acct', bank_code='CMB')

        self.assertEqual(ev['coverage_status'], 'decode_failed')
        self.assertFalse(ev['complete_mime'])
        self.assertIsNone(ev['complete_content_digest'])

    def test_graph_synthesized_message_is_coverage_unknown(self):
        """反例 4 (§10.1, §10.5, §10.7)：Graph 合成邮件必须标记 coverage_unknown，不能宣布为完整 MIME"""
        msg = email.message.EmailMessage()
        msg['Message-ID'] = '<graph-msg@outlook.com>'
        msg['Subject'] = 'Graph Statement'
        msg['From'] = 'svc@bank.cn'
        msg['Date'] = 'Mon, 10 Oct 2026 10:00:00 +0800'
        msg.set_content('Synthesized plain text body')

        ev = si.build_evidence_from_email_message(
            msg, account_name='acct', bank_code='CMB', is_synthesized=True
        )
        self.assertEqual(ev['coverage_status'], 'coverage_unknown')
        self.assertFalse(ev['complete_mime'])
        self.assertIsNone(ev['complete_content_digest'])

        # 尝试与真实 EML 比对 -> 必须返回 insufficient_evidence
        real_msg = email.message.EmailMessage()
        real_msg['Message-ID'] = '<graph-msg@outlook.com>'
        real_msg['Subject'] = 'Graph Statement'
        real_msg['From'] = 'svc@bank.cn'
        real_msg['Date'] = 'Mon, 10 Oct 2026 10:00:00 +0800'
        real_msg.set_content('Synthesized plain text body')
        ev_real = si.build_evidence_from_email_message(real_msg, account_name='acct', bank_code='CMB')

        status, reasons, _ = si.compare_evidence(ev, ev_real)
        self.assertEqual(status, 'insufficient_evidence')

    def test_same_message_id_different_body_reports_conflict(self):
        """反例 5：同 Message-ID 但不同正文 -> 确凿冲突 identity_conflict"""
        msg1 = email.message.EmailMessage()
        msg1['Message-ID'] = '<dup@bank.cn>'
        msg1['Subject'] = 'Bill'
        msg1['From'] = 'svc@bank.cn'
        msg1['Date'] = 'Mon, 10 Oct 2026 10:00:00 +0800'
        msg1.set_content('Body Version 1: Total Due 100.00')

        msg2 = email.message.EmailMessage()
        msg2['Message-ID'] = '<dup@bank.cn>'
        msg2['Subject'] = 'Bill'
        msg2['From'] = 'svc@bank.cn'
        msg2['Date'] = 'Mon, 10 Oct 2026 10:00:00 +0800'
        msg2.set_content('Body Version 2: Total Due 200.00')

        b1, b2 = msg1.as_bytes(), msg2.as_bytes()
        ev1 = si.build_evidence_from_email_message(msg1, raw_bytes=b1, account_name='acct', bank_code='CMB')
        ev2 = si.build_evidence_from_email_message(msg2, raw_bytes=b2, account_name='acct', bank_code='CMB')

        status, reasons, _ = si.compare_evidence(ev1, ev2)
        self.assertEqual(status, 'identity_conflict')
        self.assertTrue(any('全内容摘要不同' in r for r in reasons))

    def test_placeholder_account_rejected(self):
        """反例 6：占位账户 (__orphan__, default) 拒绝自动关联，返回 insufficient_evidence"""
        msg = email.message.EmailMessage()
        msg['Message-ID'] = '<msg@bank.cn>'
        msg['Subject'] = 'Bill'
        msg['From'] = 'svc@bank.cn'
        msg['Date'] = 'Mon, 10 Oct 2026 10:00:00 +0800'
        msg.set_content('Body')

        ev_orphan = si.build_evidence_from_email_message(msg, account_name='__orphan__', bank_code='CMB')
        ev_real = si.build_evidence_from_email_message(msg, account_name='real_user', bank_code='CMB')

        status, reasons, _ = si.compare_evidence(ev_orphan, ev_real)
        self.assertEqual(status, 'insufficient_evidence')
        self.assertTrue(any('占位账户' in r for r in reasons))

    def test_full_digest_equal_success_plan(self):
        """正例：完全相同内容与元数据 -> associated_existing 并成功生成不可变 plan"""
        msg1 = email.message.EmailMessage()
        msg1['Message-ID'] = '<exact@bank.cn>'
        msg1['Subject'] = 'Bill October'
        msg1['From'] = 'svc@bank.cn'
        msg1['Date'] = 'Mon, 10 Oct 2026 10:00:00 +0800'
        msg1.set_content('Exact Body Content')
        msg1.add_attachment(b'PDF_RAW_PAYLOAD', maintype='application', subtype='pdf', filename='bill.pdf')

        # 模拟中间中继加了 Received 和 Microsoft 传输诊断头
        msg2 = email.message.EmailMessage()
        msg2['Received'] = 'from mail.example.com by relay.com; Mon, 10 Oct 2026'
        msg2['X-MS-Exchange-Organization-SCL'] = '1'
        msg2['Message-ID'] = '<exact@bank.cn>'
        msg2['Subject'] = 'Bill October'
        msg2['From'] = 'svc@bank.cn'
        msg2['Date'] = 'Mon, 10 Oct 2026 10:00:00 +0800'
        msg2.set_content('Exact Body Content\r\n')
        msg2.add_attachment(b'PDF_RAW_PAYLOAD', maintype='application', subtype='pdf', filename='bill.pdf')

        b1, b2 = msg1.as_bytes(), msg2.as_bytes()
        ev1 = si.build_evidence_from_email_message(msg1, raw_bytes=b1, account_name='my_acct', bank_code='CMB', transport_uid='uid_pop_1')
        ev2 = si.build_evidence_from_email_message(msg2, raw_bytes=b2, account_name='my_acct', bank_code='CMB', transport_uid='uid_pop_2')

        status, reasons, details = si.compare_evidence(ev1, ev2)
        self.assertEqual(status, 'associated_existing')
        self.assertEqual(details['method'], 'digest_equal')

        plan = si.generate_identity_plan(
            ev2, ev1, target_statement_id=123,
            anchor_snapshot={'statement_id': 123, 'total_due': '100.00', 'is_paid': 1}
        )
        self.assertEqual(plan['status'], 'associated_existing')
        self.assertIn('plan_hash', plan)
        self.assertIn('plan_id', plan)
        self.assertEqual(plan['target_statement_id'], 123)


if __name__ == '__main__':
    unittest.main()


class TestStatementIdentityR2AndR5Hardening(unittest.TestCase):

    def test_multipart_subtype_change_differs(self):
        """反例 (R2)：外层相同，内层 multipart/mixed 改为 multipart/alternative，叶内容相同，摘要必须不同"""
        # msg1 内层为 multipart/mixed
        raw1 = (
            b'Content-Type: multipart/mixed; boundary="B1"\r\n'
            b'Message-ID: <tree.test@bank.cn>\r\n'
            b'Subject: Bill Tree\r\n'
            b'From: svc@bank.cn\r\n'
            b'Date: Sat, 10 Oct 2026 10:00:00 +0800\r\n\r\n'
            b'--B1\r\n'
            b'Content-Type: text/plain; charset="utf-8"\r\n\r\n'
            b'Leaf Content\r\n'
            b'--B1--\r\n'
        )
        # msg2 内层为 multipart/alternative
        raw2 = (
            b'Content-Type: multipart/alternative; boundary="B1"\r\n'
            b'Message-ID: <tree.test@bank.cn>\r\n'
            b'Subject: Bill Tree\r\n'
            b'From: svc@bank.cn\r\n'
            b'Date: Sat, 10 Oct 2026 10:00:00 +0800\r\n\r\n'
            b'--B1\r\n'
            b'Content-Type: text/plain; charset="utf-8"\r\n\r\n'
            b'Leaf Content\r\n'
            b'--B1--\r\n'
        )
        ev1 = si.build_evidence_from_raw_eml(raw1, 'user@163.com', 'CMB')
        ev2 = si.build_evidence_from_raw_eml(raw2, 'user@163.com', 'CMB')

        self.assertNotEqual(ev1['complete_content_digest'], ev2['complete_content_digest'])
        status, reasons, _ = si.compare_evidence(ev1, ev2)
        self.assertEqual(status, 'identity_conflict')

    def test_different_normalization_version_rejected(self):
        """反例 (R2)：证据版本改为 v999，compare_evidence 必须拒绝自动关联"""
        raw = (
            b'Content-Type: text/plain\r\n'
            b'Message-ID: <ver.test@bank.cn>\r\n'
            b'Subject: Bill\r\n'
            b'From: svc@bank.cn\r\n'
            b'Date: Sat, 10 Oct 2026 10:00:00 +0800\r\n\r\n'
            b'Hello\r\n'
        )
        ev1 = si.build_evidence_from_raw_eml(raw, 'user@163.com', 'CMB')
        ev2 = dict(ev1)
        ev2['normalization_version'] = 'v999'

        status, reasons, _ = si.compare_evidence(ev1, ev2)
        self.assertEqual(status, 'insufficient_evidence')
        self.assertTrue(any('版本不匹配' in r for r in reasons))

    def test_no_raw_bytes_marked_incomplete(self):
        """反例 (R2)：无 raw_bytes 仅 Message 对象，必须标 complete_mime=False"""
        msg = email.message.EmailMessage()
        msg['Message-ID'] = '<nobytes@bank.cn>'
        msg['Subject'] = 'Bill'
        msg['From'] = 'svc@bank.cn'
        msg['Date'] = 'Sat, 10 Oct 2026 10:00:00 +0800'
        msg.set_content('Body')

        ev = si.build_evidence_from_email_message(msg, raw_bytes=None, account_name='acct', bank_code='CMB')
        self.assertFalse(ev['complete_mime'])
        self.assertEqual(ev['coverage_status'], 'missing_raw_bytes')
        self.assertIsNone(ev['complete_content_digest'])

    def test_truncated_multipart_boundary_defect(self):
        """反例 (R2)：原始 multipart 截断缺失结束 boundary，必须标 truncated/defect"""
        truncated_raw = (
            b'Content-Type: multipart/mixed; boundary="B_TRUNC"\r\n'
            b'Message-ID: <trunc@bank.cn>\r\n'
            b'Subject: Bill\r\n'
            b'From: svc@bank.cn\r\n'
            b'Date: Sat, 10 Oct 2026 10:00:00 +0800\r\n\r\n'
            b'--B_TRUNC\r\n'
            b'Content-Type: text/plain\r\n\r\n'
            b'Truncated payload without end boundary'
        )
        ev = si.build_evidence_from_raw_eml(truncated_raw, 'user@163.com', 'CMB')
        self.assertFalse(ev['complete_mime'])
        self.assertIn(ev['coverage_status'], ('truncated', 'mime_defect'))

    def test_hard_contradiction_not_masked_by_incomplete_evidence(self):
        """反例 (R2)：Subject 硬矛盾不能因一侧缺少正文被遮蔽为 insufficient_evidence，必须报 identity_conflict"""
        raw_src = (
            b'Content-Type: text/plain\r\n'
            b'Message-ID: <same.msgid@bank.cn>\r\n'
            b'Subject: Bill Subject A\r\n'
            b'From: svc@bank.cn\r\n'
            b'Date: Sat, 10 Oct 2026 10:00:00 +0800\r\n\r\n'
            b'Body A\r\n'
        )
        ev_src = si.build_evidence_from_raw_eml(raw_src, 'user@163.com', 'CMB')

        # 构造一个缺少完整正文/coverage_unknown 但 Subject 明确不同的 target
        ev_tgt = {
            'normalization_version': si.NORMALIZATION_VERSION,
            'account_name': 'user@163.com',
            'bank_code': 'CMB',
            'transport_kind': 'pop3_uidl',
            'transport_uid': 'UID_TGT',
            'headers': {
                'message_id': '<same.msgid@bank.cn>',
                'subject': 'Bill Subject B (CONTRADICTION)',
                'from': 'svc@bank.cn',
                'date': 'Sat, 10 Oct 2026 10:00:00 +0800',
            },
            'message_id': '<same.msgid@bank.cn>',
            'coverage_status': 'coverage_unknown',
            'complete_mime': False,
            'complete_content_digest': None,
        }

        status, reasons, _ = si.compare_evidence(ev_src, ev_tgt)
        # 必须是 identity_conflict！不能因为 ev_tgt 缺完整证据就被判定为 insufficient_evidence！
        self.assertEqual(status, 'identity_conflict')
        self.assertTrue(any('关键头硬矛盾' in r for r in reasons))

    def test_archive_eml_validates_actual_hash_and_permission(self):
        """反例 (R5)：archive_evidence_eml 严格校验实际字节哈希与权限"""
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            raw_bytes = b"Actual EML Bytes"
            correct_hash = hashlib.sha256(raw_bytes).hexdigest()

            # 传入伪造 hash 必须抛异常
            with self.assertRaises(ValueError):
                si.archive_evidence_eml(td, raw_bytes, raw_hash="fake_hash_123")

            # 正常归档
            arch_path = si.archive_evidence_eml(td, raw_bytes, raw_hash=correct_hash)
            self.assertTrue(os.path.exists(arch_path))
            mode = oct(os.stat(arch_path).st_mode & 0o777)
            self.assertEqual(mode, '0o600')
