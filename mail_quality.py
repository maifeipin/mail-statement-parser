#!/usr/bin/env python3
"""解析质量核心：结构指纹、字段来源诊断、case_key 与用途隔离（纯逻辑，无 IO/无连接）。

职责边界（重构契约 §2）：本模块不解析账单内容、不写数据库、不做邮件连接；
statement_parser 负责采集原始证据（表块、字段来源状态、行统计），本模块负责
把证据变成 Diagnostic / case_key / 结构指纹。账单规则不进本模块。
"""

import hashlib
import json
import re
from typing import Dict, List, Optional

from statement_models import Diagnostic, DIAGNOSTIC_CODES

# 字段缺失/异常时受影响的业务用途（重构契约 §2.1：用途互相隔离）
FIELD_PURPOSE_MATRIX = {
    'statement_month': ('financial_summary', 'repayment_reminder', 'fx_analysis'),
    'statement_date': ('financial_summary',),
    'due_date': ('repayment_reminder',),
    'total_due': ('financial_summary', 'repayment_reminder'),
    'minimum_due': ('repayment_reminder',),
    'original_currency': ('fx_analysis',),
}

_FULLWIDTH_TRANS = str.maketrans({
    '　': ' ', '（': '(', '）': ')', '，': ',', '：': ':',
})


def normalize_header_cell(cell: str) -> str:
    """表头单元格规范化：去空白、全半角统一、大小写归一。用于表头别名匹配与结构指纹。"""
    s = (cell or '').translate(_FULLWIDTH_TRANS).strip()
    s = re.sub(r'\s+', ' ', s)
    return s.lower()


def normalize_merchant(desc: str) -> str:
    """商户名规范化：去空白、全半角、大小写。保留数字（同一商户的不同门店号不合并）。"""
    s = (desc or '').translate(_FULLWIDTH_TRANS).strip()
    s = re.sub(r'\s+', ' ', s)
    return s.upper()


def split_markdown_table_blocks(markdown_text: str) -> List[List[str]]:
    """把 markdown 切成连续表格行块（每块是一个 list of 原始行）。

    用于区分两类重复：同一邮件渲染出的重复 HTML 表格（块级重复，只保留一份）
    与同一表格内两行内容完全相同的真实交易（行级，必须保留重数）。
    """
    blocks: List[List[str]] = []
    current: List[str] = []
    for line in (markdown_text or '').splitlines():
        if line.strip().startswith('|'):
            current.append(line.rstrip())
        else:
            if current:
                blocks.append(current)
                current = []
    if current:
        blocks.append(current)
    return blocks


def dedupe_table_blocks(blocks: List[List[str]]) -> List[List[str]]:
    """内容完全一致的重复表块只保留首次出现（同一封邮件重复渲染的表格）。

    块内行不做任何去重：同内容多笔真实交易的行级重数由本函数天然保留。
    """
    seen = set()
    out = []
    for block in blocks:
        key = '\n'.join(block)
        if key in seen:
            continue
        seen.add(key)
        out.append(block)
    return out


def _table_header_row(block: List[str]) -> List[str]:
    """取表块的第一行作为表头并规范化（markdown 表由 header + --- + 数据行组成）。"""
    if not block:
        return []
    first = block[0].strip().strip('|')
    return [normalize_header_cell(c) for c in first.split('|')]


def structure_fingerprint(blocks: List[List[str]]) -> Dict:
    """结构指纹：规范化表头、列数、字段语义。不 hash 整份 HTML，
    金额/日期/月份/排版变化不产生新指纹（重构契约 §2.2）。"""
    headers = []
    for block in blocks:
        header = _table_header_row(block)
        if header and header[0] not in ('---', '') and any(c for c in header):
            headers.append(header)
    column_counts = sorted({len(block[0].strip().strip('|').split('|')) for block in blocks if block})
    return {
        'table_headers': headers,          # list[list[str]]，规范化表头
        'column_counts': column_counts,   # list[int]，出现的列数集合
        'table_block_count': len(blocks),
    }


def fingerprint_matches_baseline(fingerprint: Dict, accepted: List[Dict]) -> bool:
    """指纹是否命中人工确认的基线（quality.accepted_structure_fingerprints）。

    已确认的列重排/别名表头应存为基线；未配置基线时不因指纹不同而告警
    （避免初始上线制造噪声待办）。"""
    if not accepted:
        return True  # 无基线＝未启用格式告警，静默通过
    def canonical(headers):
        return sorted(tuple(sorted(str(c) for c in row if c != 'ignored')) for row in headers)
    headers = canonical(fingerprint.get('table_headers', []))
    for base in accepted:
        if canonical(base.get('table_headers', [])) == headers:
            return True
    return False


def resolve_header_alias(header_cell: str, aliases: Dict[str, List[str]]) -> Optional[str]:
    """把表头别名解析到标准语义名（如「交易日期」→「交易日」）。"""
    if not aliases:
        return None
    norm = normalize_header_cell(header_cell)
    for canonical, variants in aliases.items():
        if norm == normalize_header_cell(canonical):
            return canonical
        for v in variants or []:
            if norm == normalize_header_cell(v):
                return canonical
    return None


def make_case_key(account_name: str, bank_code: str, template_version: str,
                  diag_code: str, field: Optional[str], extra: str = '') -> str:
    """确定性 case_key：账户范围内隔离（不同账户不共享 case），不含金额/动态日期/正文。

    同一账户同一银行同一问题特征 → 同一 key，重复扫描更新 occurrence 不新增行。
    """
    raw = '|'.join([
        account_name or '__orphan__', bank_code or '', template_version or '',
        diag_code, field or '', extra or '',
    ])
    return hashlib.sha256(raw.encode('utf-8')).hexdigest()[:32]


SUSPECT_AMOUNT_RE = re.compile(r'[¥￥]?\s*-?\d{1,3}(?:,\d{3})*(?:\.\d{2})')
BANK_DOMAIN_HINTS = {
    'cmbchina.com': 'CMB', 'spdb.com.cn': 'SPDB', 'icbc.com.cn': 'ICBC',
    'citicbank.com': 'CITIC', 'citic.com': 'CITIC', 'mbc.com.cn': 'CMBC',
    'hxb.com.cn': 'HX', 'ecitic.com': 'CITIC',
}


def detect_suspicious_statement(sender: str, subject: str, body_text: str,
                                 rules: List[dict]) -> Optional[Diagnostic]:
    """未命中模板但疑似账单：发件人域命中已知银行 + 正文有金额与表格特征。

    已知非账单（如「每日信用管家」）在 identify_rule 门禁处已排除，不会走到这里。
    返回 UNKNOWN_STATEMENT_TEMPLATE 诊断；不像账单返回 None（不制造噪声）。"""
    sender_l = (sender or '').lower()
    bank_hint = None
    for domain, bank in BANK_DOMAIN_HINTS.items():
        if domain in sender_l:
            bank_hint = bank
            break
    if not bank_hint:
        return None
    matched_banks = {r.get('bank_code') for r in rules if r.get('bank_code')}
    has_amount = bool(SUSPECT_AMOUNT_RE.search(body_text or ''))
    has_table = '|' in (body_text or '') and body_text.count('|') >= 6
    # 主题含银行账单关键词才进一步判定，避免银行营销邮件大量误报
    subject_like_bill = any(k in (subject or '')
                            for k in ('账单', '对账单', '还款', '应还', 'Statement'))
    if has_amount and has_table and subject_like_bill and bank_hint in matched_banks:
        return Diagnostic(
            code='UNKNOWN_STATEMENT_TEMPLATE', severity='warning',
            reason=f'发件人域指向 {bank_hint} 且正文含金额与表格特征，但未命中任何已确认模板',
            affected_purposes=['financial_summary', 'repayment_reminder'],
        )
    return None


def diagnose_missing_field(field: str, source: str, reason: str,
                           evidence_ref: Optional[dict] = None) -> Optional[Diagnostic]:
    """按字段来源状态生成缺失/失败诊断。

    - source_missing → SOURCE_FIELD_MISSING（原文确认未提供）
    - extraction_failed → EXTRACTION_FAILED（原文有但没提取出来）
    - inferred → 不算缺失，但不生成诊断（inferred 是合法推算，展示层标注即可）
    - extracted/unknown → 不诊断（unknown 不能伪装成已确认缺失）
    """
    purposes = list(FIELD_PURPOSE_MATRIX.get(field, ()))
    if source == 'source_missing':
        return Diagnostic(code='SOURCE_FIELD_MISSING', field=field, severity='warning',
                          reason=reason, affected_purposes=purposes, evidence_ref=evidence_ref)
    if source == 'extraction_failed':
        return Diagnostic(code='EXTRACTION_FAILED', field=field, severity='error',
                          reason=reason, affected_purposes=purposes, evidence_ref=evidence_ref)
    if source == 'unknown':
        return Diagnostic(code='FIELD_SOURCE_UNKNOWN', field=field, severity='warning',
                          reason=reason, affected_purposes=purposes, evidence_ref=evidence_ref)
    return None


def diagnose_currency(txn_stats: Dict, evidence_ref: Optional[dict] = None) -> List[Diagnostic]:
    """币种诊断：存在原币金额但无原币币种证据 → CURRENCY_UNKNOWN（只阻 fx_analysis）。"""
    diags = []
    if txn_stats.get('original_amount_count', 0) > 0 and txn_stats.get('original_currency_known', 0) < txn_stats.get('original_amount_count', 0):
        diags.append(Diagnostic(
            code='CURRENCY_UNKNOWN', field='original_currency', severity='warning',
            reason=f"{txn_stats['original_amount_count']-txn_stats.get('original_currency_known',0)} 笔交易有原币金额但无币种证据，"
                   '不得默认 CNY 或按地区猜测；外汇趋势分析受影响',
            affected_purposes=['fx_analysis'], evidence_ref=evidence_ref,
        ))
    if txn_stats.get('settlement_currency_unknown', 0):
        diags.append(Diagnostic(code='CURRENCY_UNKNOWN', field='currency', reason=f"{txn_stats['settlement_currency_unknown']} 笔入账币种无可靠证据", affected_purposes=['financial_summary','transaction_trends','fx_analysis'], evidence_ref=evidence_ref))
    return diags


def diagnose_coverage(txn_stats: Dict, evidence_ref: Optional[dict] = None) -> List[Diagnostic]:
    """覆盖缺口：账单声明有交易或存在明细表，但有效行为 0。合法零交易账单不告警。"""
    diags = []
    if txn_stats.get('declared_txn_count') or txn_stats.get('candidate_row_count') or txn_stats.get('rejected_row_count'):
        if not txn_stats.get('valid_row_count'):
            diags.append(Diagnostic(
                code='TRANSACTION_COVERAGE_GAP', field='transactions', severity='error',
                reason=f"存在交易声明/明细表但有效行数为 0"
                       f"（候选 {txn_stats.get('candidate_row_count', 0)} 行，"
                       f"拒绝 {txn_stats.get('rejected_row_count', 0)} 行）",
                affected_purposes=['transaction_trends','fx_analysis'], evidence_ref=evidence_ref,
            ))
    if txn_stats.get('rejected_row_count', 0) > 0 and txn_stats.get('valid_row_count', 0) > 0:
        diags.append(Diagnostic(
            code='TRANSACTION_ROW_INVALID', field='transactions', severity='warning',
            reason=f"{txn_stats['rejected_row_count']} 行被拒（金额/描述无效），"
                   f"有效 {txn_stats['valid_row_count']} 行",
            affected_purposes=['transaction_trends'], evidence_ref=evidence_ref,
        ))
    return diags


def diagnose_format(fingerprint: Dict, rule: Dict, evidence_ref: Optional[dict] = None) -> List[Diagnostic]:
    """格式变化：结构指纹偏离人工确认基线 → FORMAT_CHANGED。无基线时静默。"""
    accepted = ((rule.get('quality') or {}).get('accepted_structure_fingerprints') or [])
    if not accepted:
        return []
    if fingerprint_matches_baseline(fingerprint, accepted):
        return []
    return [Diagnostic(
        code='FORMAT_CHANGED', field='structure', severity='warning',
        reason='表头/列数偏离已确认的结构基线，需人工确认新模板',
        affected_purposes=['transaction_trends'], evidence_ref=evidence_ref,
    )]


def missing_fields_by_purpose(rule: Dict, fields_meta: Dict) -> Dict[str, List[str]]:
    """用途级必填检查（quality.required_fields_by_purpose）。

    返回 {purpose: [缺失字段]}；与 validate_rules.required_statement_fields
    （账单级合法性判据，决定 parse_status）分离，不影响 parse_status。
    """
    by_purpose = (rule.get('quality') or {}).get('required_fields_by_purpose') or {}
    out: Dict[str, List[str]] = {}
    for purpose, fields in by_purpose.items():
        missing = []
        for f in fields or []:
            meta = fields_meta.get(f) or {}
            src = meta.get('source')
            val = meta.get('value')
            if src in ('source_missing', 'extraction_failed') or (val is None or val == ''):
                missing.append(f)
        if missing:
            out[purpose] = missing
    return out


def events_from_diagnostics(diagnostics: List[Diagnostic], account_name: str, bank_code: str,
                            template_version: str, evidence_refs: List[dict]) -> List[dict]:
    """把诊断按 (code, field) 归并成待持久化事件描述列表。

    每个诊断一个事件（case_key 稳定）；statement_recorded 事件由保存层另行产生。
    返回 [{'case_key', 'event_type', 'diagnostics': [Diagnostic], 'evidence_refs'}]。
    """
    grouped: Dict[tuple, List[Diagnostic]] = {}
    for d in diagnostics:
        if d.code not in DIAGNOSTIC_CODES:
            continue
        grouped.setdefault((d.code, d.field or ''), []).append(d)
    events = []
    for (code, fld), diags in sorted(grouped.items()):
        case_key = make_case_key(account_name, bank_code, template_version, code, fld)
        events.append({
            'case_key': case_key,
            'event_type': 'quality_detected',
            'diagnostics': diags,
            'evidence_refs': evidence_refs,
        })
    return events
