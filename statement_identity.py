#!/usr/bin/env python3
"""
statement_identity.py
负责邮件内容证据的提取、树结构规范化、严格比较与不可变计划契约。
纯证据规范化与比较契约，不包含财务解析逻辑或网络调用。
"""

import email
import email.errors
import email.header
import email.utils
import email.policy
import hashlib
import json
import os
import tempfile
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

NORMALIZATION_VERSION = "v1"

# 明确列举的传输层跳板与认证头（排除在邮件内容摘要比较之外）
EXCLUDED_HEADERS = {
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

PLACEHOLDER_ACCOUNTS = {'__orphan__', 'default', ''}


def _decode_header_str(val: Optional[str]) -> str:
    """严格解码 MIME 头字符串，不使用 errors='replace' 制造假相等"""
    if not val:
        return ""
    try:
        parts = email.header.decode_header(val)
        decoded = []
        for content, encoding in parts:
            if isinstance(content, bytes):
                encoding = encoding or 'utf-8'
                # 严格解码，若未知编码尝试常用中文编码
                try:
                    decoded.append(content.decode(encoding))
                except (LookupError, UnicodeDecodeError):
                    for enc in ('utf-8', 'gb18030', 'gbk'):
                        try:
                            decoded.append(content.decode(enc))
                            break
                        except (LookupError, UnicodeDecodeError):
                            continue
                    else:
                        # 字符集解码彻底失败，保留 hex 表示以防伪造假等价
                        decoded.append(f"<raw_hex:{content.hex()}>")
            else:
                decoded.append(str(content))
        return "".join(decoded).strip()
    except Exception:
        return str(val).strip()


def extract_header_evidence(msg: email.message.Message) -> Dict[str, Any]:
    """
    提取关键邮件头证据：
    - Message-ID: 严格仅剥除外围空白，保留原始大小写，禁止空串
    - Subject, From, To, Date: 严格 MIME 解码
    - 保留所有非排除头列表（含重复头，保留出现顺序与重数，禁止字典覆盖）
    """
    raw_msg_id = msg.get('Message-ID', '')
    msg_id = str(raw_msg_id).strip() if raw_msg_id else ""
    
    subject = _decode_header_str(msg.get('Subject', ''))
    from_addr = _decode_header_str(msg.get('From', ''))
    to_addr = _decode_header_str(msg.get('To', ''))
    date_str = _decode_header_str(msg.get('Date', ''))

    # 保留头列表顺序与重数，防止同名头（如多个 X-Custom-Header）被字典覆盖
    ordered_headers = []
    for key, val in msg.items():
        k_lower = key.lower().strip()
        if k_lower in EXCLUDED_HEADERS or k_lower.startswith('x-ms-exchange-organization-'):
            continue
        decoded_val = _decode_header_str(val)
        if k_lower == 'content-type':
            ct_parts = [p.strip() for p in decoded_val.split(';') if p.strip()]
            filtered_parts = [p for p in ct_parts if not p.lower().startswith('boundary=')]
            decoded_val = "; ".join(filtered_parts)
        ordered_headers.append((k_lower, decoded_val))

    return {
        'message_id': msg_id,
        'subject': subject,
        'from': from_addr,
        'to': to_addr,
        'date': date_str,
        'headers_fingerprint': hashlib.sha256(
            json.dumps(ordered_headers, sort_keys=False, ensure_ascii=False).encode('utf-8')
        ).hexdigest(),
        'ordered_headers': ordered_headers,
    }


def _try_decode_text_bytes(payload: bytes, charset: Optional[str]) -> Tuple[Optional[str], bool]:
    """尝试无损解码文本 bytes，若字符集失败绝不使用 errors=ignore/replace，直接返回失败"""
    if charset:
        enc = charset.strip().lower().strip('"' + "'")
        if enc in ('gb2312', 'gbk'):
            enc = 'gb18030'
        try:
            return payload.decode(enc), False
        except (LookupError, UnicodeDecodeError):
            return None, True

    for enc in ('utf-8', 'gb18030'):
        try:
            return payload.decode(enc), False
        except (LookupError, UnicodeDecodeError):
            continue
    return None, True


def _traverse_mime_tree(
    part: email.message.Message,
    tree_path: str = "0"
) -> Tuple[List[Dict[str, Any]], bool, List[Tuple[str, str]]]:
    """
    递归遍历 MIME 树，保留完整层级路径、multipart 节点类型与 subtype、
    Content-Disposition、文件名及叶子节点逐字节/规范化摘要。
    返回: (nodes_manifest, has_decode_error, normalized_body_texts)
    """
    nodes = []
    has_decode_error = False
    body_texts = []

    ctype = (part.get_content_type() or 'application/octet-stream').lower()
    disposition = (part.get('Content-Disposition') or '').strip().lower()
    # 提取 disposition 类型 (inline / attachment)
    disp_type = disposition.split(';')[0].strip() if disposition else ""
    fname = part.get_filename()
    if fname:
        fname = _decode_header_str(fname)
    is_attachment = disp_type == 'attachment' or bool(fname)

    if part.is_multipart():
        # 记录 multipart 容器节点本身及其 subtype 与路径
        nodes.append({
            'path': tree_path,
            'is_container': True,
            'content_type': ctype,
            'disposition': disp_type,
            'filename': fname,
        })
        payload = part.get_payload()
        if isinstance(payload, list):
            for idx, child in enumerate(payload):
                child_path = f"{tree_path}.{idx}"
                child_nodes, child_err, child_bodies = _traverse_mime_tree(child, child_path)
                nodes.extend(child_nodes)
                if child_err:
                    has_decode_error = True
                body_texts.extend(child_bodies)
    else:
        # 叶子节点
        raw_payload = part.get_payload(decode=True) or b''
        decode_err = False
        part_sha256 = None
        effective_size = len(raw_payload)

        if ctype in ('text/plain', 'text/html') and not is_attachment:
            charset = part.get_content_charset()
            text_str, decode_err = _try_decode_text_bytes(raw_payload, charset)
            if decode_err:
                has_decode_error = True
                part_sha256 = hashlib.sha256(raw_payload).hexdigest()
            else:
                norm_text = text_str.replace('\r\n', '\n').replace('\r', '\n')
                norm_bytes = norm_text.encode('utf-8')
                part_sha256 = hashlib.sha256(norm_bytes).hexdigest()
                effective_size = len(norm_bytes)
                body_texts.append((ctype, part_sha256))
        else:
            # 二进制内容/附件：逐字节摘要，绝不做换行转换
            part_sha256 = hashlib.sha256(raw_payload).hexdigest()

        nodes.append({
            'path': tree_path,
            'is_container': False,
            'content_type': ctype,
            'disposition': disp_type,
            'filename': fname,
            'is_attachment': is_attachment,
            'part_sha256': part_sha256,
            'size_bytes': effective_size,
            'decode_error': decode_err,
        })

    return nodes, has_decode_error, body_texts


def build_evidence_from_email_message(
    msg: email.message.Message,
    raw_bytes: Optional[bytes] = None,
    account_name: str = "",
    bank_code: str = "",
    transport_kind: str = "pop3_uidl",
    transport_uid: str = "",
    is_synthesized: bool = False,
    missing_reason: Optional[str] = None
) -> Dict[str, Any]:
    """
    从 email.message.Message 提取版本化内容证据。
    强化要求 (R2, R5)：
    1. 必须有可信原始 bytes，无 raw_bytes 绝不认定 complete_mime；
    2. 检查 MIME defects 与截断异常，存在结构缺陷标 defect/truncated；
    3. 保留整棵 MIME 树路径、multipart 类型与 Content-Disposition；
    4. 文本规范化后的度量 (normalized bytes length) 保持一致，避免 CRLF 导致 size 差异；
    5. Graph 合成正文 (is_synthesized=True) 明确标记 coverage_unknown。
    """
    headers = extract_header_evidence(msg)
    raw_hash = hashlib.sha256(raw_bytes).hexdigest() if raw_bytes else None

    # 检查缺陷与截断
    defects = getattr(msg, 'defects', [])
    has_structure_defect = any(isinstance(d, (
        email.errors.StartBoundaryNotFoundDefect,
        email.errors.CloseBoundaryNotFoundDefect,
        email.errors.MultipartInvariantViolationDefect,
    )) for d in defects)

    tree_nodes, has_decode_error, body_texts = _traverse_mime_tree(msg)

    # 覆盖完整性综合判定
    if is_synthesized:
        coverage_status = 'coverage_unknown'
        complete_mime = False
        eff_missing_reason = missing_reason or 'synthesized_message_missing_full_mime'
    elif not raw_bytes:
        coverage_status = 'missing_raw_bytes'
        complete_mime = False
        eff_missing_reason = missing_reason or 'no_raw_bytes_provided'
    elif has_structure_defect:
        coverage_status = 'truncated'
        complete_mime = False
        eff_missing_reason = 'mime_boundary_or_truncation_defect'
    elif has_decode_error:
        coverage_status = 'decode_failed'
        complete_mime = False
        eff_missing_reason = 'text_part_decode_failed'
    else:
        coverage_status = 'complete'
        complete_mime = True
        eff_missing_reason = None

    # 计算可比较正文摘要
    if body_texts:
        comp_body_str = json.dumps(body_texts, sort_keys=True)
        comparable_body_digest = hashlib.sha256(comp_body_str.encode('utf-8')).hexdigest()
    else:
        comparable_body_digest = None

    # 计算全内容摘要 (complete_content_digest)
    # 严格依赖确定性树结构清单 + 头序列
    if complete_mime and not has_decode_error and not has_structure_defect:
        content_manifest = {
            'v': NORMALIZATION_VERSION,
            'headers_fp': headers['headers_fingerprint'],
            'tree': tree_nodes,
        }
        complete_content_digest = hashlib.sha256(
            json.dumps(content_manifest, sort_keys=True, ensure_ascii=False).encode('utf-8')
        ).hexdigest()
    else:
        complete_content_digest = None

    return {
        'normalization_version': NORMALIZATION_VERSION,
        'account_name': account_name,
        'bank_code': bank_code,
        'transport_kind': transport_kind,
        'transport_uid': str(transport_uid),
        'headers': headers,
        'message_id': headers['message_id'],
        'raw_content_hash': raw_hash,
        'raw_bytes': raw_bytes,
        'parts': [n for n in tree_nodes if not n.get('is_container')],
        'tree_nodes': tree_nodes,
        'parts_count': sum(1 for n in tree_nodes if not n.get('is_container')),
        'attachments_count': sum(1 for n in tree_nodes if n.get('is_attachment')),
        'coverage_status': coverage_status,
        'complete_mime': complete_mime,
        'comparable_body_digest': comparable_body_digest,
        'complete_content_digest': complete_content_digest,
        'missing_reason': eff_missing_reason,
        'created_at': datetime.now(timezone.utc).isoformat()
    }


def build_evidence_from_raw_eml(
    eml_bytes: bytes,
    account_name: str,
    bank_code: str,
    transport_kind: str = "pop3_uidl",
    transport_uid: str = ""
) -> Dict[str, Any]:
    """从原始 RFC822 EML 字节构建完整内容证据"""
    msg = email.message_from_bytes(eml_bytes, policy=email.policy.default)
    return build_evidence_from_email_message(
        msg,
        raw_bytes=eml_bytes,
        account_name=account_name,
        bank_code=bank_code,
        transport_kind=transport_kind,
        transport_uid=transport_uid,
        is_synthesized=False
    )


def _parse_date_to_ymd(d_str: Optional[str]) -> Optional[str]:
    if not d_str:
        return None
    d_str = d_str.strip()
    try:
        dt = email.utils.parsedate_to_datetime(d_str)
        return dt.strftime('%Y-%m-%d')
    except Exception:
        pass
    try:
        dt = datetime.fromisoformat(d_str[:10])
        return dt.strftime('%Y-%m-%d')
    except Exception:
        pass
    return None

def sender_metadata_equal(left: str, right: str) -> bool:
    """Compare parsed sender metadata; leave the v1 content digest unchanged."""
    if left == right:
        return True
    try:
        lhs = email.utils.getaddresses([left])
        rhs = email.utils.getaddresses([right])
    except (TypeError, ValueError):
        return False
    # Only unambiguous single mailbox syntax may ignore presentation spacing.
    if len(lhs) != 1 or len(rhs) != 1:
        return False
    if any(not address or '@' not in address for _, address in lhs + rhs):
        return False
    return lhs == rhs


def compare_evidence(
    source_ev: Dict[str, Any],
    target_ev: Dict[str, Any]
) -> Tuple[str, List[str], Dict[str, Any]]:
    """
    严格比对两端证据。
    安全判定次序 (R2)：
    1. 版本校验；
    2. 账户与银行核对；
    3. Message-ID 校验；
    4. 先行排查已知硬矛盾（Subject, From, Date 等），绝不被缺证据遮蔽为 insufficient；
    5. 证据完整性检查（缺 raw_bytes / 截断 / coverage_unknown 输出 insufficient_evidence）；
    6. 全内容 Digest 严格一致性比对。
    """
    reasons = []
    details = {
        'source_version': source_ev.get('normalization_version'),
        'target_version': target_ev.get('normalization_version'),
        'source_coverage': source_ev.get('coverage_status'),
        'target_coverage': target_ev.get('coverage_status'),
    }

    # 1. 规范化算法版本必须一致且等于当前支持版本
    src_ver = source_ev.get('normalization_version')
    tgt_ver = target_ev.get('normalization_version')
    if src_ver != NORMALIZATION_VERSION or tgt_ver != NORMALIZATION_VERSION:
        reasons.append(f"证据版本不匹配或不支持: ({src_ver} vs {tgt_ver}, 期望 {NORMALIZATION_VERSION})")
        return 'insufficient_evidence', reasons, details

    # 2. 基础账户与银行核对
    src_acct = source_ev.get('account_name')
    tgt_acct = target_ev.get('account_name')
    if not src_acct or not tgt_acct or src_acct in PLACEHOLDER_ACCOUNTS or tgt_acct in PLACEHOLDER_ACCOUNTS:
        reasons.append('占位账户或账户名为空，不能建立有效身份关联')
        return 'insufficient_evidence', reasons, details

    if src_acct != tgt_acct:
        reasons.append(f'跨账户不能自动关联: {src_acct} != {tgt_acct}')
        return 'identity_conflict', reasons, details

    src_bank = source_ev.get('bank_code')
    tgt_bank = target_ev.get('bank_code')
    if not src_bank or not tgt_bank or src_bank != tgt_bank:
        reasons.append(f'银行代码不匹配: {src_bank} != {tgt_bank}')
        return 'identity_conflict', reasons, details

    # 3. Message-ID 硬核对
    src_msgid = source_ev.get('message_id')
    tgt_msgid = target_ev.get('message_id')
    if not src_msgid or not tgt_msgid:
        reasons.append('Message-ID 为空，无法作为有效证据')
        return 'insufficient_evidence', reasons, details

    if src_msgid != tgt_msgid:
        reasons.append(f'Message-ID 不一致: {src_msgid} != {tgt_msgid}')
        return 'identity_conflict', reasons, details

    # 4. 先行排查已知硬矛盾 (Subject, From, Date) —— 绝不因缺正文遮蔽已知矛盾！
    src_hdr = source_ev.get('headers', {})
    tgt_hdr = target_ev.get('headers', {})
    for key in ('subject', 'from', 'date'):
        sv = src_hdr.get(key, '')
        tv = tgt_hdr.get(key, '')
        if sv and tv:
            if key == 'date':
                sv_ymd = _parse_date_to_ymd(sv)
                tv_ymd = _parse_date_to_ymd(tv)
                if sv_ymd and tv_ymd:
                    if sv_ymd != tv_ymd:
                        reasons.append(f'关键头硬矛盾: date (src={sv[:40]!r} != tgt={tv[:40]!r})')
                        return 'identity_conflict', reasons, details
                elif sv != tv:
                    reasons.append(f'关键头硬矛盾: date (src={sv[:40]!r} != tgt={tv[:40]!r})')
                    return 'identity_conflict', reasons, details
            elif not (sender_metadata_equal(sv, tv) if key == 'from' else sv == tv):
                reasons.append(f'关键头硬矛盾: {key} (src={sv[:40]!r} != tgt={tv[:40]!r})')
                return 'identity_conflict', reasons, details

    # 5. 双方证据完整度检查
    src_complete = source_ev.get('complete_mime', False)
    tgt_complete = target_ev.get('complete_mime', False)
    if not src_complete or not tgt_complete:
        missing_s = source_ev.get('missing_reason') or 'source_incomplete'
        missing_t = target_ev.get('missing_reason') or 'target_incomplete'
        reasons.append(f'证据不完整（源: {missing_s}, 目标: {missing_t}），输出 insufficient_evidence')
        return 'insufficient_evidence', reasons, details

    # 关键头必须双方皆存在
    for key in ('subject', 'from', 'date'):
        if not src_hdr.get(key) or not tgt_hdr.get(key):
            reasons.append(f'缺少关键头证据: {key}')
            return 'insufficient_evidence', reasons, details

    # 6. 全内容 Digest 严格一致性比对
    src_dig = source_ev.get('complete_content_digest')
    tgt_dig = target_ev.get('complete_content_digest')

    if not src_dig or not tgt_dig:
        reasons.append('缺失完整内容摘要，无法判定 digest_equal')
        return 'insufficient_evidence', reasons, details

    if src_dig != tgt_dig:
        reasons.append(f'Message-ID 相同但全内容摘要不同 (src={src_dig[:12]} != tgt={tgt_dig[:12]})')
        sb_dig = source_ev.get('comparable_body_digest')
        tb_dig = target_ev.get('comparable_body_digest')
        if sb_dig and tb_dig and sb_dig != tb_dig:
            reasons.append(f'正文部分摘要不一致 (src_body={sb_dig[:12]} != tgt_body={tb_dig[:12]})')
        return 'identity_conflict', reasons, details

    details['method'] = 'digest_equal'
    return 'associated_existing', reasons, details


def compute_plan_hash(plan: Dict[str, Any]) -> str:
    """计算不可变的规范化计划哈希（排除 plan_id 与 plan_hash 外层键）"""
    payload_to_hash = {k: v for k, v in plan.items() if k not in ('plan_id', 'plan_hash')}
    canonical_bytes = json.dumps(payload_to_hash, sort_keys=True, ensure_ascii=False).encode('utf-8')
    return hashlib.sha256(canonical_bytes).hexdigest()


def generate_identity_plan(
    source_ev: Dict[str, Any],
    target_ev: Dict[str, Any],
    target_statement_id: int,
    anchor_snapshot: Dict[str, Any],
    source_ref: Optional[Dict[str, Any]] = None,
    target_ref: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """
    生成不可变、版本化的人工确认计划。
    采用规范的 canonical_plan_hash 机制保证无法篡改字段。
    """
    status, reasons, details = compare_evidence(source_ev, target_ev)
    created_at = datetime.now(timezone.utc).isoformat()
    # 计划有效期 2 小时
    expires_at = datetime.fromtimestamp(
        datetime.now(timezone.utc).timestamp() + 7200, timezone.utc
    ).isoformat()

    plan_payload = {
        'schema_version': 1,
        'normalization_version': NORMALIZATION_VERSION,
        'status': status,
        'reasons': reasons,
        'details': details,
        'account_name': source_ev.get('account_name'),
        'bank_code': source_ev.get('bank_code'),
        'transport_kind': source_ev.get('transport_kind'),
        'transport_uid': str(source_ev.get('transport_uid')),
        'target_statement_id': target_statement_id,
        'anchor_snapshot': anchor_snapshot,
        'source_evidence_summary': {
            'message_id': source_ev.get('message_id'),
            'raw_content_hash': source_ev.get('raw_content_hash'),
            'complete_content_digest': source_ev.get('complete_content_digest'),
            'comparable_body_digest': source_ev.get('comparable_body_digest'),
            'coverage_status': source_ev.get('coverage_status'),
            'parts_count': source_ev.get('parts_count'),
            'attachments_count': source_ev.get('attachments_count'),
            'missing_reason': source_ev.get('missing_reason'),
        },
        'target_evidence_summary': {
            'message_id': target_ev.get('message_id'),
            'raw_content_hash': target_ev.get('raw_content_hash'),
            'complete_content_digest': target_ev.get('complete_content_digest'),
            'comparable_body_digest': target_ev.get('comparable_body_digest'),
            'coverage_status': target_ev.get('coverage_status'),
            'parts_count': target_ev.get('parts_count'),
            'attachments_count': target_ev.get('attachments_count'),
            'missing_reason': target_ev.get('missing_reason'),
        },
        'created_at': created_at,
        'expires_at': expires_at,
        'source_ref': source_ref or {
            'eml_path': source_ev.get('eml_path'),
            'raw_content_hash': source_ev.get('raw_content_hash'),
            'complete_content_digest': source_ev.get('complete_content_digest'),
        },
        'target_ref': target_ref or {
            'statement_id': target_statement_id,
            'archive_path': target_ev.get('archive_path'),
            'raw_content_hash': target_ev.get('raw_content_hash'),
            'complete_content_digest': target_ev.get('complete_content_digest'),
        },
    }

    plan_hash = compute_plan_hash(plan_payload)
    plan_id = hashlib.sha256(f"plan:{created_at}:{source_ev.get('transport_uid')}".encode('utf-8')).hexdigest()[:16]

    plan_payload['plan_id'] = plan_id
    plan_payload['plan_hash'] = plan_hash
    return plan_payload


def get_evidence_archive_path(data_root: str, raw_hash: str) -> str:
    """按 content sha256 计算私有归档路径"""
    return os.path.join(data_root, 'evidence', f"{raw_hash}.eml")


def archive_evidence_eml(data_root: str, raw_bytes: bytes, raw_hash: str, context: Any = None) -> str:
    """
    原子且安全地归档原始 EML 字节到私有数据根的 evidence 目录 (R5)。
    1. 内部独立重算 raw_bytes SHA-256，校验与 raw_hash 严格一致；
    2. 若已有同名归档文件，核验文件内容摘要；若损坏或不一致则报错；
    3. 使用安全临时文件，设置权限 0o600，原子替换写入。
    """
    if not raw_bytes:
        raise ValueError("raw_bytes 不能为空")
    actual_hash = hashlib.sha256(raw_bytes).hexdigest()
    if actual_hash != raw_hash:
        raise ValueError(f"原始字节 SHA-256 不匹配: actual={actual_hash} != declared={raw_hash}")

    target = get_evidence_archive_path(data_root, raw_hash)
    if context and hasattr(context, 'writable_roots'):
        from parser_paths import validate_write_target
        validate_write_target(context, target)

    target_dir = os.path.dirname(target)
    os.makedirs(target_dir, exist_ok=True)

    if os.path.exists(target):
        with open(target, 'rb') as fp:
            existing_bytes = fp.read()
        existing_hash = hashlib.sha256(existing_bytes).hexdigest()
        if existing_hash != raw_hash:
            raise ValueError(f"已存在的归档文件摘要损坏: path={target}, hash={existing_hash} != {raw_hash}")
        return target

    # 使用 NamedTemporaryFile 在目标同目录创建临时文件，设置 0o600 权限
    with tempfile.NamedTemporaryFile(dir=target_dir, prefix="eml_", suffix=".tmp", delete=False) as tf:
        os.chmod(tf.name, 0o600)
        tf.write(raw_bytes)
        tf.flush()
        os.fsync(tf.fileno())
        tmp_name = tf.name

    os.replace(tmp_name, target)
    return target


def save_identity_plan_report(data_root: str, plan: Dict[str, Any], context: Any = None) -> str:
    """保存版本化私有计划报告至私有数据根的 identity_reports 目录"""
    plan_id = plan.get('plan_id', 'unknown')
    target = os.path.join(data_root, 'identity_reports', f"plan_{plan_id}.json")
    if context and hasattr(context, 'writable_roots'):
        from parser_paths import validate_write_target
        validate_write_target(context, target)

    target_dir = os.path.dirname(target)
    os.makedirs(target_dir, exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=target_dir, prefix="plan_", suffix=".tmp", delete=False, mode='w', encoding='utf-8') as tf:
        os.chmod(tf.name, 0o600)
        json.dump(plan, tf, indent=2, ensure_ascii=False)
        tf.flush()
        tmp_name = tf.name

    os.replace(tmp_name, target)
    return target


def save_identity_diff_report(data_root: str, plan_id: str, diff_report: Dict[str, Any], context: Any = None) -> str:
    """保存版本化私有并排差异分析报告至私有数据根的 identity_reports 目录 (R5)"""
    target = os.path.join(data_root, 'identity_reports', f"diff_{plan_id}.json")
    if context and hasattr(context, 'writable_roots'):
        from parser_paths import validate_write_target
        validate_write_target(context, target)

    target_dir = os.path.dirname(target)
    os.makedirs(target_dir, exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=target_dir, prefix="diff_", suffix=".tmp", delete=False, mode='w', encoding='utf-8') as tf:
        os.chmod(tf.name, 0o600)
        json.dump(diff_report, tf, indent=2, ensure_ascii=False)
        tf.flush()
        tmp_name = tf.name

    os.replace(tmp_name, target)
    return target
