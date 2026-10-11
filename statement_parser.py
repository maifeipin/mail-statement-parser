#!/usr/bin/env python3
"""账单解析与校验模块。

从 mail_client.py 抽取，包含：银行解析函数（HX/CMB/SPDB/ICBC/CITIC）、
规则匹配与校验、字段提取与规范化、落库。
依赖 statement_models / statement_db / mail_parse，不 import mail_client。
"""

import re
import json
import hashlib
import os
import glob
from datetime import datetime, timezone, timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path

from parser_paths import resolve_parser_paths, validate_write_target, ParserPaths
from statement_models import (
    StatementRecord, StatementTransactionRecord,
    ValidationResult, ValidationIssue,
    Diagnostic, QualityEventRecord, StatementBalanceRecord, FIELD_SOURCES,
)
from statement_db import (
    init_db, upsert_statement, save_validation_run, replace_transactions,
    save_statement_with_transactions, find_message_id_conflicts, MessageIdentityConflict,
    upsert_quality_event, capture_quality_cases, confirm_quality_sources,
    find_canonical_by_transport, associate_transport_identity, get_transport_identity, save_statement_evidence_index, get_statement_evidence,
)
import statement_identity as si
from mail_parse import decode_mime, _to_text, extract_email_content, _parse_email_datetime
from mail_quality import (
    split_markdown_table_blocks, dedupe_table_blocks, structure_fingerprint,
    detect_suspicious_statement, diagnose_currency, diagnose_coverage, diagnose_format,
    diagnose_missing_field, missing_fields_by_purpose, events_from_diagnostics,
    make_case_key,
)

from statement_evidence import field_meta, parse_semantic_transactions, detail_fingerprint, purpose_availability, confirmed_currency

# Resolution evidence must expire after parser/rule changes, not a manually bumped constant.
PARSER_EVIDENCE_VERSION = 'r6.2-' + hashlib.sha256(b''.join(
    (Path(__file__).parent / name).read_bytes()
    for name in ('statement_parser.py','statement_evidence.py','mail_quality.py')
)).hexdigest()[:16]


def evidence_version(rule):
    return PARSER_EVIDENCE_VERSION + '-' + hashlib.sha256(
        json.dumps({k:v for k,v in rule.items() if k != '_path'}, sort_keys=True, ensure_ascii=False).encode()).hexdigest()[:16]

# 路径常量（保持向后兼容/诊断值；内部全面使用显式 context）
_DEFAULT_PATHS = resolve_parser_paths()
RULES_DIR = _DEFAULT_PATHS.rules_dir
DB_PATH = _DEFAULT_PATHS.db_path
VALIDATION_REPORT_DIR = _DEFAULT_PATHS.report_dir


def _safe_decimal(v, strip_tokens=None):
    if v is None:
        return None
    s = str(v)
    for token in strip_tokens or []:
        s = s.replace(token, '')
    s = s.strip()
    if not s:
        return None
    try:
        value = Decimal(s)
        return value if value.is_finite() else None
    except InvalidOperation:
        return None

def _parse_date(v, date_formats=None):
    if not v:
        return None
    s = str(v).strip()
    for fmt in (date_formats or []):
        try:
            return datetime.strptime(s, fmt).date().isoformat()
        except ValueError:
            continue
    # 容错：先把中文日期替换后再尝试
    s2 = s.replace('年', '-').replace('月', '-').replace('日', '').replace('.', '-')
    for fmt in ('%Y-%m-%d', '%Y/%m/%d'):
        try:
            return datetime.strptime(s2, fmt).date().isoformat()
        except ValueError:
            continue
    return None

def _normalize_statement_month(v):
    if not v:
        return None
    s = str(v).strip()
    # 注意 alternation 顺序：两位月份（10-12）必须先于 0?[1-9] 尝试，
    # 否则 '2026/10/10' 会被截成 '2026-01'（生产 CMB 曾真实发生）。
    m = re.search(r'(20\d{2})年(1[0-2]|0?[1-9])月', s)
    if m:
        return f'{m.group(1)}-{int(m.group(2)):02d}'
    m = re.search(r'(20\d{2})[-/](1[0-2]|0?[1-9])', s)
    if m:
        return f'{m.group(1)}-{int(m.group(2)):02d}'
    m = re.search(r'(20\d{2})[-/](1[0-2]|0?[1-9])[-/](\d{1,2})', s)
    if m:
        return f'{m.group(1)}-{int(m.group(2)):02d}'
    return s

def _resolve_monthly_day_date(v, statement_month):
    """把“每月11日”解析为 YYYY-MM-DD，月份来自 statement_month。"""
    if not v or not statement_month:
        return None
    m = re.search(r'每月\s*(\d{1,2})\s*日', str(v))
    sm = re.search(r'(20\d{2})-(\d{2})', str(statement_month))
    if not m or not sm:
        return None
    day = int(m.group(1))
    year = int(sm.group(1))
    month = int(sm.group(2))
    try:
        return datetime(year, month, day).date().isoformat()
    except ValueError:
        return None


def _infer_cmb_due_date(body_text, statement_date):
    """招商账单还款日兜底：仅接受还款标签附近的日期，不使用账单日/广告日期估算。"""
    if not body_text:
        return None

    # 1) 优先从中英标签附近提取
    label_patterns = [
        r'(?:到期还款日|最后还款日|Payment\s*Due\s*Date)\D{0,24}(20\d{2}[/-]\d{1,2}[/-]\d{1,2})',
        r'(?:到期还款日|最后还款日|Payment\s*Due\s*Date)\D{0,24}(20\d{2}年\d{1,2}月\d{1,2}日)',
    ]
    for p in label_patterns:
        m = re.search(p, body_text, flags=re.IGNORECASE)
        if not m:
            continue
        d = _parse_date(m.group(1), ['%Y-%m-%d', '%Y/%m/%d', '%Y年%m月%d日'])
        if d:
            return d

    # Unlabeled dates (statement period / advertisements) do not prove repayment dates.
    return None


def _apply_due_date_fallbacks(rule, body_text, fields):
    """银行特定兜底：当 due_date 为空时尝试补齐。"""
    if fields.get('due_date'):
        return
    bank_code = (rule or {}).get('bank_code')
    if bank_code == 'CMB':
        fields['due_date'] = _infer_cmb_due_date(body_text, fields.get('statement_date'))


def _apply_amount_fallbacks(rule, body_text, fields):
    """银行特定兜底：当 total_due/minimum_due 为空时尝试补齐。

    ICBC 0 元账单不打印“需还款明细”表，主正则匹不到 → NULL，与解析失败不可区分。
    0 元账单的特征：本期交易汇总合计行“本期余额”=0.00（欠款账单为负数）。
    故当 total_due 缺失且本期余额合计=0 时，显式置 0，区分“已还清”与“解析失败”。
    """
    if fields.get('total_due'):
        return
    bank_code = (rule or {}).get('bank_code')
    if bank_code != 'ICBC':
        return
    # 合计行：合计 上期余额/RMB 收入/RMB 支出/RMB 余额合计/RMB（空格可有可无）
    # 取最后一个 4 段合计匹配（正文“本期交易汇总”段在后）。
    matches = re.findall(
        r'合计\s+([-0-9.,]+)/RMB\s*([-0-9.,]+)/RMB\s*([-0-9.,]+)/RMB\s*([-0-9.,]+)/RMB',
        body_text or '',
    )
    if not matches:
        return
    try:
        balance = float(matches[-1][3].replace(',', ''))
    except (ValueError, IndexError):
        return
    if abs(balance) < 0.005:  # 本期余额合计 = 0 → 已还清
        fields['total_due'] = '0'
        fields['minimum_due'] = '0'


def _apply_statement_date_month_fallbacks(rule, email_date, fields):
    """账单日期/月兜底：提取失败时，使用邮件头日期补齐。"""
    if not fields:
        return

    bank_code = (rule or {}).get('bank_code')
    msg_dt = _parse_email_datetime(email_date)

    # SPDB 在部分模板中仅能稳定拿到还款日，账单日常缺失，优先用邮件日期兜底。
    if not fields.get('statement_date') and bank_code == 'SPDB' and msg_dt:
        fields['statement_date'] = msg_dt.date().isoformat()

    if not fields.get('statement_month'):
        m = re.match(r'^(\d{4})-(\d{2})-(\d{2})$', str(fields.get('statement_date') or '').strip())
        if m:
            fields['statement_month'] = f'{m.group(1)}-{m.group(2)}'
        elif msg_dt:
            fields['statement_month'] = f'{msg_dt.year:04d}-{msg_dt.month:02d}'


def _infer_date_from_mmdd(mmdd, statement_month):
    """将 MM/DD 推断为 YYYY-MM-DD，优先使用账单月份年份。"""
    if not mmdd or not statement_month:
        return None
    raw = str(mmdd).strip()
    m = re.match(r'^(\d{2})/(\d{2})$', raw)
    if not m:
        m = re.match(r'^(\d{2})(\d{2})$', raw)
    sm = re.match(r'^(\d{4})-(\d{2})$', str(statement_month).strip())
    if not m or not sm:
        return None
    mm = int(m.group(1))
    dd = int(m.group(2))
    year = int(sm.group(1))
    stmt_month = int(sm.group(2))
    # 跨年修正：账单月为1月，交易可能落在上一年12月。
    if stmt_month == 1 and mm == 12:
        year -= 1
    try:
        return datetime(year, mm, dd).date().isoformat()
    except ValueError:
        return None


def _infer_date_from_any(v, statement_month):
    if not v:
        return None
    s = str(v).strip()
    if re.match(r'^\d{4}[-/.]\d{1,2}[-/.]\d{1,2}$', s):
        return _parse_date(s, ['%Y-%m-%d', '%Y/%m/%d', '%Y.%m.%d'])
    return _infer_date_from_mmdd(s, statement_month)


def _dedup_markdown_tables(markdown_text):
    """重复渲染的 HTML 表格只在块级去重一次；块内行不去重（保留真实交易重数）。"""
    if not markdown_text:
        return ''
    blocks = dedupe_table_blocks(split_markdown_table_blocks(markdown_text))
    return '\n\n'.join('\n'.join(b) for b in blocks)


def _count_candidate_rows(markdown_text):
    """候选数据行：| 开头且非表头/分隔行（供覆盖缺口诊断）。"""
    n = 0
    for line in (markdown_text or '').splitlines():
        s = line.strip()
        if not s.startswith('|'):
            continue
        first = s.strip('|').split('|')[0].strip()
        if not first or first == '---' or any(x in s for x in ('交易日', '记账日', '入账日期',
                                                               '主卡明细', '卡号后四位', '交易日期')):
            continue
        n += 1
    return n


def parse_hx_transactions_from_markdown(uid, statement_month, markdown_text):
    """从华夏账单 Markdown 表中提取交易明细。

    去重策略（重构契约 §2.2）：重复表块只解析一次；同内容两笔真实交易保留重数，
    不用内容唯一键消除（回归矩阵：相同日期商户金额的两笔真实交易）。
    """
    if not markdown_text:
        return []
    markdown_text = _dedup_markdown_tables(markdown_text)
    rows = []
    for line in markdown_text.splitlines():
        s = line.strip()
        if not s.startswith('|'):
            continue
        cols = [c.strip() for c in s.strip('|').split('|')]
        if len(cols) < 5:
            continue
        if cols[0] == '交易日' or cols[0] == '---':
            continue
        if not re.match(r'^\d{2}/\d{2}$', cols[0]):
            continue
        amount_raw = cols[4]
        d = _safe_decimal(amount_raw, [',', ' ', '￥', '¥'])
        if d is None:
            continue
        rows.append(
            StatementTransactionRecord(
                uid=str(uid),
                bank_code='HX',
                txn_date=_infer_date_from_mmdd(cols[0], statement_month),
                post_date=_infer_date_from_mmdd(cols[1], statement_month),
                description=cols[2],
                amount=str(d),
                currency='CNY',
                direction='credit' if d < 0 else 'debit',
                raw_row_json=json.dumps({'cols': cols}, ensure_ascii=False),
            )
        )
    return rows


def parse_cmb_transactions_from_markdown(uid, statement_month, markdown_text):
    """从招商账单 Markdown 表中提取交易明细（表块级去重，保留行级重数）。"""
    if not markdown_text:
        return []
    markdown_text = _dedup_markdown_tables(markdown_text)
    rows = []
    for line in markdown_text.splitlines():
        s = line.strip()
        if not s.startswith('|'):
            continue
        cols = [c.strip() for c in s.strip('|').split('|')]
        if len(cols) < 6:
            continue
        if any(x in s for x in ('交易日', '记账日', '---')):
            continue

        txn_col = None
        post_col = None
        desc_col = None
        amt_col = None

        if re.match(r'^\d{4}$', cols[1]) and re.match(r'^\d{4}$', cols[2]):
            txn_col, post_col, desc_col, amt_col = 1, 2, 3, 4
        elif re.match(r'^\d{2}/\d{2}$', cols[0]) and re.match(r'^\d{2}/\d{2}$', cols[1]):
            txn_col, post_col, desc_col, amt_col = 0, 1, 2, 3
        else:
            continue

        d = _safe_decimal(cols[amt_col], [',', ' ', '￥', '¥'])
        if d is None:
            continue
        desc = cols[desc_col]
        if not desc:
            continue

        txn_location_code = cols[6].strip() if len(cols) > 6 else None
        if txn_location_code:
            txn_location_code = txn_location_code.upper()

        original_amount = None
        if len(cols) > 7:
            od = _safe_decimal(cols[7], [',', ' ', '￥', '¥'])
            if od is not None:
                original_amount = str(od)

        rows.append(
            StatementTransactionRecord(
                uid=str(uid),
                bank_code='CMB',
                txn_date=_infer_date_from_any(cols[txn_col], statement_month),
                post_date=_infer_date_from_any(cols[post_col], statement_month),
                description=desc,
                amount=str(d),
                currency='CNY',
                txn_location_code=txn_location_code,
                original_amount=original_amount,
                direction='credit' if d < 0 else 'debit',
                raw_row_json=json.dumps({'cols': cols}, ensure_ascii=False),
            )
        )
    return rows


def parse_spdb_transactions_from_markdown(uid, statement_month, markdown_text):
    """从浦发账单 Markdown 表中提取交易明细（表块级去重，保留行级重数）。"""
    if not markdown_text:
        return []
    markdown_text = _dedup_markdown_tables(markdown_text)
    rows = []
    for line in markdown_text.splitlines():
        s = line.strip()
        if not s.startswith('|'):
            continue
        cols = [c.strip() for c in s.strip('|').split('|')]
        if len(cols) < 4:
            continue
        if any(x in s for x in ('交易日', '记账日', '---')):
            continue

        amount_idx = -1
        amount_val = None
        for i, c in enumerate(cols):
            d = _safe_decimal(c, [',', ' ', '￥', '¥'])
            if d is not None:
                amount_idx = i
                amount_val = d
                break
        if amount_idx < 0:
            continue

        date_val = None
        desc_val = None
        for c in cols:
            if re.match(r'^\d{4}[-/.]\d{1,2}[-/.]\d{1,2}$', c) or re.match(r'^\d{2}/\d{2}$', c) or re.match(r'^\d{4}$', c):
                date_val = c
            elif c and c != cols[amount_idx] and len(c) > 2:
                desc_val = c
                break
        if not desc_val:
            continue

        rows.append(
            StatementTransactionRecord(
                uid=str(uid),
                bank_code='SPDB',
                txn_date=_infer_date_from_any(date_val, statement_month),
                post_date=None,
                description=desc_val,
                amount=str(amount_val),
                currency='CNY',
                direction='credit' if amount_val < 0 else 'debit',
                raw_row_json=json.dumps({'cols': cols}, ensure_ascii=False),
            )
        )
    return rows


def parse_icbc_transactions_from_markdown(uid, statement_month, markdown_text):
    """从工行账单 Markdown 表中提取交易明细（表块级去重，保留行级重数）。"""
    if not markdown_text:
        return []
    markdown_text = _dedup_markdown_tables(markdown_text)
    rows = []
    for line in markdown_text.splitlines():
        s = line.strip()
        if not s.startswith('|'):
            continue
        cols = [c.strip() for c in s.strip('|').split('|')]
        if len(cols) < 7:
            continue
        if any(x in s for x in ('交易日', '记账日', '---', '主卡明细', '卡号后四位')):
            continue

        if not re.match(r'^\d{4}$', cols[0]):
            continue
        if not re.match(r'^\d{4}-\d{2}-\d{2}$', cols[1]):
            continue
        if not re.match(r'^\d{4}-\d{2}-\d{2}$', cols[2]):
            continue

        desc = cols[4]
        if not desc:
            continue

        book_col = cols[6]
        d = _safe_decimal(book_col, [',', ' ', '￥', '¥', '/RMB', '/CNY', '(支出)', '(收入)'])
        if d is None:
            continue

        direction = 'debit'
        if '收入' in book_col:
            d = -d
            direction = 'credit'
        elif '支出' in book_col:
            direction = 'debit'

        rows.append(
            StatementTransactionRecord(
                uid=str(uid),
                bank_code='ICBC',
                txn_date=_parse_date(cols[1], ['%Y-%m-%d']),
                post_date=_parse_date(cols[2], ['%Y-%m-%d']),
                description=desc,
                amount=str(d),
                currency='CNY',
                direction=direction,
                raw_row_json=json.dumps({'cols': cols}, ensure_ascii=False),
            )
        )
    return rows

def parse_citic_transactions_from_body(uid, statement_month, body_text):
    """从中信账单纯文本正文中提取交易明细。

    中信账单交易行格式（HTML 转纯文本后，无固定分隔符）：
      YYYYMMDDYYYYMMDD4234描述CNY金额CNY金额
    例：20260117202601174234财付通－居家物业CNY1258.00CNY1258.00

    不做内容行去重：同内容多笔真实交易保留重数（重复渲染由 Message-ID 幂等与清洗兜底）。
    """
    if not body_text:
        return []
    rows = []
    # 匹配: txn_date(8位) post_date(8位) card_last4(4位) desc CNY amount CNY amount
    pattern = re.compile(
        r'(\d{8})(\d{8})(\d{4})'          # txn_date, post_date, card_last4
        r'(.+?)'                            # description (non-greedy)
        r'CNY\s*([+-]?[0-9,]+\.[0-9]{2})'  # txn currency amount
        r'CNY\s*([+-]?[0-9,]+\.[0-9]{2})', # settlement amount
    )
    for m in pattern.finditer(body_text):
        txn_raw, post_raw, _card, desc, trx_amt_str, setl_amt_str = m.groups()
        desc = desc.strip()
        if not desc:
            continue
        # 用结算金额入库
        d = _safe_decimal(setl_amt_str, [',', ' '])
        if d is None:
            continue
        txn_date = _parse_date(
            f'{txn_raw[:4]}-{txn_raw[4:6]}-{txn_raw[6:]}', ['%Y-%m-%d']
        )
        post_date = _parse_date(
            f'{post_raw[:4]}-{post_raw[4:6]}-{post_raw[6:]}', ['%Y-%m-%d']
        )
        rows.append(
            StatementTransactionRecord(
                uid=str(uid),
                bank_code='CITIC',
                txn_date=txn_date,
                post_date=post_date,
                description=desc,
                amount=str(d),
                currency='CNY',
                currency_source='extracted',
                direction='credit' if d < 0 else 'debit',
                raw_row_json=json.dumps(
                    {'txn': txn_raw, 'post': post_raw, 'desc': desc,
                     'trx_amt': trx_amt_str, 'setl_amt': setl_amt_str},
                    ensure_ascii=False,
                ),
            )
        )
    return rows


def load_rule_files(rules_dir=None, *, context=None):
    """加载规则文件（当前支持 JSON）。只读访问，不主动创建目录。"""
    if context is not None:
        target_dir = context.rules_dir
    elif rules_dir is not None:
        target_dir = os.path.abspath(rules_dir)
    elif RULES_DIR != _DEFAULT_PATHS.rules_dir:
        target_dir = os.path.abspath(RULES_DIR)
    else:
        target_dir = _DEFAULT_PATHS.rules_dir

    if not os.path.isdir(target_dir):
        return []

    rules = []
    for path in sorted(glob.glob(os.path.join(target_dir, '*.json'))):
        try:
            with open(path, 'r', encoding='utf-8') as f:
                rule = json.load(f)
                rule['_path'] = path
                rules.append(rule)
        except Exception as e:
            print(f'⚠️ 规则加载失败：{path} - {e}')
    return rules

def _match_score(text, patterns):
    if not patterns:
        return 0
    text_l = (text or '').lower()
    score = 0
    for p in patterns:
        if p.lower() in text_l:
            score += 1
    return score

def identify_rule(subject, sender, body_text, rules):
    """按规则匹配模板，返回最佳匹配规则与分数。

    入库门禁：声明了 subject_patterns 的规则必须至少命中一个主题特征，
    防止发件人单独命中导致非账单邮件（如“每日信用管家”日报）混入账单流。
    """
    best = None
    best_score = -1
    for r in rules:
        m = r.get('match_rules', {})
        sender_score = _match_score(sender, m.get('sender_patterns', [])) * 5
        subject_score = _match_score(subject, m.get('subject_patterns', [])) * 3
        body_score = _match_score(body_text, m.get('body_signatures', []))
        if m.get('subject_patterns') and subject_score == 0:
            continue
        if sender_score == 0 and subject_score == 0:
            continue
        base = sender_score + subject_score + body_score
        if base <= 0:
            continue
        score = base + int(r.get('priority', 0))
        if score > best_score:
            best = r
            best_score = score
    return best, best_score

def extract_statement_by_rule(rule, subject, body_text, return_meta=False):
    """提取账单字段。return_meta=True 时返回 (fields, meta)：meta 为
    {字段: {value, source, rule_version?}}，source ∈ FIELD_SOURCES（重构契约 §2.1）。"""
    fields = {}
    raw_values = {}
    matched_values = {}
    rules = rule.get('extract_rules', {}).get('statement_fields', {})
    for name, cfg in rules.items():
        source = subject if cfg.get('source') == 'subject' else body_text
        pattern = cfg.get('pattern')
        value = None
        if pattern:
            m = re.search(pattern, source or '', flags=re.IGNORECASE)
            if m:
                group_index = int(cfg.get('group', 1))
                if m.groups() and group_index <= len(m.groups()):
                    value = m.group(group_index).strip()
                elif m.groups():
                    value = m.group(1).strip()
                else:
                    value = m.group(0).strip()
        fields[name] = value
        raw_values[name] = value
        matched_values[name] = m.group(0) if pattern and m else None

    normalize = rule.get('normalize_rules', {})
    date_formats = normalize.get('date_formats', [])
    amount_tokens = normalize.get('amount_strip_tokens', [])

    if fields.get('statement_month'):
        fields['statement_month'] = _normalize_statement_month(fields['statement_month'])

    if fields.get('statement_date'):
        parsed = _parse_date(fields['statement_date'], date_formats)
        if not parsed:
            parsed = _resolve_monthly_day_date(fields['statement_date'], fields.get('statement_month'))
        fields['statement_date'] = parsed

    if fields.get('due_date'):
        fields['due_date'] = _parse_date(fields['due_date'], date_formats)

    pre_fallback = {k: fields.get(k) for k in fields}  # fallback 前快照（inferred 判据）
    if not (raw_values.get('due_date') is not None and fields.get('due_date') is None):
        _apply_due_date_fallbacks(rule, body_text, fields)
    _apply_amount_fallbacks(rule, body_text, fields)

    for amt_key in ('total_due', 'minimum_due'):
        if fields.get(amt_key) is not None:
            d = _safe_decimal(fields[amt_key], amount_tokens)
            fields[amt_key] = str(d) if d is not None else None

    if not return_meta:
        return fields
    # 字段来源状态（重构契约 §2.1）：fallback 改变的值标 inferred（规则推算），
    # 金额比较用 Decimal 语义（'1,234.56' 与 '1234.56' 相等，不算 fallback 改变）。
    rule_ver = rule.get('template_version') or rule.get('rule_id')
    meta = {}
    for k, v in fields.items():
        inferred = (v is not None and pre_fallback.get(k) is None)
        cfg = rules[k]
        meta[k] = field_meta(k, v, raw_values.get(k), matched_values.get(k), cfg,
                             subject if cfg.get('source') == 'subject' else body_text,
                             inferred, rule_ver)
        if inferred:
            before=field_meta(k,None,raw_values.get(k),matched_values.get(k),cfg,
                              subject if cfg.get('source') == 'subject' else body_text)
            meta[k]['evidence']['before_fallback']=before
    return fields, meta

def validate_statement_by_rule(rule, fields):
    result = ValidationResult(passed=True)
    vr = rule.get('validate_rules', {})

    # 必填校验
    for name in vr.get('required_statement_fields', []):
        if fields.get(name) is None or fields.get(name) == '':
            result.errors.append(ValidationIssue(code='PARSE_REQUIRED_FIELD_MISSING', field=name, severity='error'))

    # 规则校验
    for rcfg in vr.get('rules', []):
        rid = rcfg.get('id', 'rule')
        severity = rcfg.get('severity', 'error')
        rtype = rcfg.get('type')
        left_name = rcfg.get('left')
        right_name = rcfg.get('right')
        op = rcfg.get('op', '>=')
        skip_if_missing = rcfg.get('skip_if_missing', False)
        left = fields.get(left_name)
        right = fields.get(right_name)

        if skip_if_missing and (left is None or right is None):
            continue

        ok = True
        if rtype == 'date_compare':
            if not left or not right:
                ok = False
            else:
                if op == '>=':
                    ok = left >= right
                elif op == '<=':
                    ok = left <= right
                elif op == '==':
                    ok = left == right
        elif rtype == 'amount_compare':
            dl = _safe_decimal(left)
            dr = _safe_decimal(right)
            if dl is None or dr is None:
                ok = False
            else:
                if dr < 0 and rid == 'minimum_le_total':
                    ok = True
                else:
                    if op == '>=':
                        ok = dl >= dr
                    elif op == '<=':
                        ok = dl <= dr
                    elif op == '==':
                        ok = dl == dr

        if not ok:
            payload = ValidationIssue(
                code='VALIDATION_RULE_FAIL',
                rule_id=rid,
                left=left_name,
                right=right_name,
                op=op,
                severity=severity,
            )
            if severity == 'warning':
                result.warnings.append(payload)
            else:
                result.errors.append(payload)

    if result.errors:
        result.passed = False
    return result

def _validation_result_to_dict(vr):
    return {
        'passed': vr.passed,
        'errors': [
            {
                'code': x.code,
                'field': x.field,
                'rule_id': x.rule_id,
                'left': x.left,
                'right': x.right,
                'op': x.op,
                'severity': x.severity,
            }
            for x in vr.errors
        ],
        'warnings': [
            {
                'code': x.code,
                'field': x.field,
                'rule_id': x.rule_id,
                'left': x.left,
                'right': x.right,
                'op': x.op,
                'severity': x.severity,
            }
            for x in vr.warnings
        ],
    }

def validate_and_save_email_message(msg, uid, rules=None, account_name=None, *, context=None, raw_bytes=None, transport_kind='pop3_uidl', is_synthesized=False):
    """从已下载的 msg 解析、校验并落库账单（三态分流）。

    返回 dict，status 取值：
      - 'saved'        合法账单已原子入库（金额 0=已还清、负值=溢缴款/退款结转均为合法取值）
      - 'parse_failed' 命中账单模板但校验未过：保留原文与校验记录，statements 标 parse_status='failed'，
                       排除出有效账单统计，供修复后离线重解析
      - 'not_statement' 未命中账单模板（非账单），不入 statements 表
      - 'error'        处理异常
    """
    if context is None:
        kwargs = {}
        if DB_PATH != _DEFAULT_PATHS.db_path:
            kwargs['db_path'] = DB_PATH
        if VALIDATION_REPORT_DIR != _DEFAULT_PATHS.report_dir:
            kwargs['report_dir'] = VALIDATION_REPORT_DIR
        context = resolve_parser_paths(**kwargs)
    elif isinstance(context, dict):
        context = resolve_parser_paths(**context)
    elif isinstance(context, ParserPaths):
        pass
    else:
        raise TypeError(f"context 必须为 ParserPaths 实例或 dict，收到: {type(context).__name__}")

    validate_write_target(context, context.db_path)
    validate_write_target(context, context.report_dir)

    if rules is None:
        rules = load_rule_files(context=context)
    if not rules:
        print('未找到规则文件，请先在 rules 目录下放置 *.json')
        return {'status': 'error', 'error': 'no_rules'}

    account_name = account_name or 'default'
    try:
        out = None
        subj = decode_mime(msg.get('Subject', ''))
        frm = decode_mime(msg.get('From', ''))
        content = extract_email_content(msg)
        body_text = (content.get('plain', '') + '\n' + content.get('markdown', '')).strip()
        from statement_db import upsert_email_body
        validate_write_target(context, context.db_path)
        init_db(context.db_path)
        upsert_email_body(context.db_path, account_name or 'default', str(uid),
                          raw_html=content.get('html'),
                          plain_text=body_text,
                          markdown_tables=content.get('markdown'))
        rule, score = identify_rule(subj, frm, body_text, rules)
        if not rule:
            # 未命中模板 ≠ 非账单：疑似账单进入质量检查（已知非账单已在主题门禁排除），
            # 事件留痕供人工确认新模板；不入账单库、不放松入库门禁。
            suspicious = detect_suspicious_statement(frm, subj, body_text, rules)
            if suspicious is not None:
                template_version = 'unknown'
                ev = QualityEventRecord(
                    case_key=make_case_key(account_name, '', template_version, suspicious.code, ''),
                    event_type='quality_detected', account_name=account_name,
                    template_version=template_version, diagnostics=[suspicious],
                    evidence_refs=[{'kind': 'email', 'account': account_name, 'uid': str(uid),
                                    'subject': subj[:80],
                                    'message_id': _to_text(msg.get('Message-ID', '')).strip() or None,
                                    'content_hash': hashlib.sha256(body_text.encode()).hexdigest(),
                        'parser_version': PARSER_EVIDENCE_VERSION}],
                )
                try:
                    init_db(context.db_path)
                    event_id = upsert_quality_event(context.db_path, ev)
                    print(f'⚠️ 疑似账单未命中模板，已记录质量事件：{event_id[:12]}')
                except Exception as qe:
                    print(f'疑似账单事件写入失败（不影响跳过入库）：{qe}')
                    event_id = None
                return {'status': 'not_statement', 'suspected': True,
                        'quality_event_id': event_id, 'diagnostic': suspicious.to_dict()}
            print('未命中账单模板（非账单），跳过入库')
            return {'status': 'not_statement'}

        parser_version = evidence_version(rule)
        captured_cases = capture_quality_cases(context.db_path, account_name, rule.get('bank_code'))
        fields, fields_meta = extract_statement_by_rule(rule, subj, body_text, return_meta=True)
        pre_outer = {k: fields.get(k) for k in fields}
        _apply_statement_date_month_fallbacks(rule, _to_text(msg.get('Date', '')), fields)
        # 外层 fallback（SPDB 账单日从邮件日期推导等）同样标记 inferred
        rule_ver = rule.get('template_version') or rule.get('rule_id')
        for k, v in fields.items():
            if k in fields_meta and fields_meta[k].get('value') == v:
                continue
            if v is None:
                fields_meta[k] = {'value': None, 'source': 'unknown'}
            elif pre_outer.get(k) != v:
                fields_meta[k] = {'value': v, 'source': 'inferred', 'rule_version': rule_ver,
                    'evidence': {'basis':'statement/date fallback', 'email_date':_to_text(msg.get('Date','')),
                                 'previous_evidence':fields_meta.get(k)}}
            else:
                fields_meta[k] = {'value': v, 'source': 'extracted'}
        vr = validate_statement_by_rule(rule, fields)

        # 邮件身份：优先标准 Message-ID；缺失时使用确定性的账户内备用身份，禁止空串入库
        message_id = _to_text(msg.get('Message-ID', '')).strip()
        if not message_id:
            message_id = f'<{account_name}.{uid}@local.generated>'

        bank_code = _to_text(rule.get('bank_code', ''))

        # 1. 优先查 statement_transport_identities: 当前 uid 是否已有关联记录 (R3 严格核验内容)
        canonical = find_canonical_by_transport(
            context.db_path, account_name, str(uid), bank_code=bank_code, transport_kind=transport_kind
        )
        if canonical:
            # U3: Parser 调用统一事务服务 associate_transport_identity，复用内容等价规范化规则
            source_ev = si.build_evidence_from_email_message(
                msg, raw_bytes=raw_bytes, account_name=account_name, bank_code=bank_code,
                transport_kind=transport_kind, transport_uid=str(uid), is_synthesized=is_synthesized
            )
            assoc_res = associate_transport_identity(
                context.db_path,
                account_name=account_name,
                transport_kind=transport_kind,
                transport_uid=str(uid),
                bank_code=bank_code,
                statement_id=canonical['id'],
                message_id=message_id,
                source_raw_bytes=raw_bytes,
                source_evidence=source_ev,
                evidence_digest=source_ev.get('complete_content_digest'),
                raw_content_hash=source_ev.get('raw_content_hash'),
                method='digest_equal',
                actor='parser_sync',
                reason='canonical identity verification'
            )
            if assoc_res.get('status') in ('already_associated', 'associated'):
                return {
                    'status': 'associated_existing',
                    'statement_id': canonical['id'],
                    'uid': str(uid),
                    'bank_code': bank_code,
                    'message_id': message_id,
                    'idempotent': True,
                }
            elif assoc_res.get('status') == 'repeated_key_unverified':
                return {
                    'status': 'repeated_key_unverified',
                    'statement_id': canonical['id'],
                    'uid': str(uid),
                    'bank_code': bank_code,
                    'message_id': message_id,
                    'idempotent': True,
                }

        # 2. Message-ID 冲突检测与内容证据比对 (R3 候选必须唯一，R5 归档闭环)
        conflicts = find_message_id_conflicts(context.db_path, account_name, message_id, bank_code, str(uid))
        if conflicts:
            if len(conflicts) > 1:
                print(f"⚠️ 多锚点歧义冲突：存在 {len(conflicts)} 个相同 Message-ID 的历史账单，拒绝盲目自动关联")
                vr.errors.append(ValidationIssue(
                    code='MESSAGE_ID_CONFLICT', field='message_id', severity='error',
                    left=str(uid), right=','.join(str(c['uid']) for c in conflicts), op='ne'))
                return {
                    'status': 'identity_conflict',
                    'error': f'Multiple conflicting anchor statements ({len(conflicts)}) exist for Message-ID {message_id}',
                    'conflict_ids': [c['id'] for c in conflicts]
                }

            conflict_uids = ','.join(str(c['uid']) for c in conflicts)
            source_ev = si.build_evidence_from_email_message(
                msg, raw_bytes=raw_bytes, account_name=account_name, bank_code=bank_code,
                transport_kind=transport_kind, transport_uid=str(uid), is_synthesized=is_synthesized
            )
            if raw_bytes and source_ev.get('raw_content_hash'):
                si.archive_evidence_eml(context.data_root, raw_bytes, source_ev['raw_content_hash'], context=context)

            target_cand = conflicts[0]
            target_id = target_cand['id']
            target_uid = target_cand['uid']

            target_raw_bytes = None
            target_ev_info = get_statement_evidence(context.db_path, target_id)
            if target_ev_info and target_ev_info.get('archive_path') and os.path.exists(target_ev_info['archive_path']):
                try:
                    with open(target_ev_info['archive_path'], 'rb') as fp:
                        target_raw_bytes = fp.read()
                except Exception:
                    pass
            if not target_raw_bytes:
                target_eml_path = os.path.join(context.download_dir, f"{target_uid}.eml")
                if os.path.exists(target_eml_path):
                    try:
                        with open(target_eml_path, 'rb') as fp:
                            target_raw_bytes = fp.read()
                    except Exception:
                        pass

            if target_raw_bytes:
                target_ev = si.build_evidence_from_raw_eml(
                    target_raw_bytes, account_name=account_name, bank_code=bank_code,
                    transport_kind=transport_kind, transport_uid=str(target_uid)
                )
            else:
                target_ev = {
                    'normalization_version': si.NORMALIZATION_VERSION,
                    'account_name': account_name,
                    'bank_code': bank_code,
                    'transport_kind': transport_kind,
                    'transport_uid': str(target_uid),
                    'headers': {
                        'message_id': message_id,
                        'subject': target_cand['subject'] or '',
                        'from': '',
                        'date': '',
                        'headers_fingerprint': '',
                    },
                    'message_id': message_id,
                    'coverage_status': 'coverage_unknown',
                    'complete_mime': False,
                    'complete_content_digest': None,
                    'comparable_body_digest': None,
                    'missing_reason': 'target_raw_eml_not_found',
                }

            decision, reasons, details = si.compare_evidence(source_ev, target_ev)
            if decision == 'associated_existing':
                associate_transport_identity(
                    context.db_path,
                    account_name=account_name,
                    transport_kind=transport_kind,
                    transport_uid=str(uid),
                    bank_code=bank_code,
                    statement_id=target_id,
                    message_id=message_id,
                    evidence_digest=source_ev.get('complete_content_digest'),
                    evidence_version=si.NORMALIZATION_VERSION,
                    raw_content_hash=source_ev.get('raw_content_hash'),
                    method='digest_equal',
                    actor='auto_sync',
                    reason='content digest equal',
                    source_evidence=source_ev,
                    target_evidence=target_ev,
                    source_raw_bytes=raw_bytes,
                )
                print(f'🔗 运输身份自动关联成功：UID={str(uid)[:20]} 关联至既有账单 id={target_id}')
                return {
                    'status': 'associated_existing',
                    'statement_id': target_id,
                    'uid': str(uid),
                    'bank_code': bank_code,
                    'message_id': message_id,
                    'method': 'digest_equal',
                    'idempotent': False,
                }
            elif decision == 'identity_conflict':
                print(f'⚠️ Message-ID 冲突：UID={str(uid)[:24]} 与已存记录 {conflict_uids[:40]} 相同，但内容存在差异')
                vr.errors.append(ValidationIssue(
                    code='MESSAGE_ID_CONFLICT', field='message_id', severity='error',
                    left=str(uid), right=conflict_uids, op='ne'))
            else:
                print(f'⚠️ Message-ID 冲突且证据不足：UID={str(uid)[:24]}，原因：{"; ".join(reasons)}')
                vr.warnings.append(ValidationIssue(
                    code='INSUFFICIENT_EVIDENCE', field='message_id', severity='warning',
                    left=str(uid), right=conflict_uids, op='ne'))

        validation_dict = _validation_result_to_dict(vr)
        parse_status = 'ok' if vr.passed else 'failed'
        report = {
            'report_meta': {
                'generated_at': datetime.now(timezone.utc).isoformat(),
                'uid': str(uid),
                'message_id': message_id,
                'rule_id': rule.get('rule_id'),
                'bank_code': rule.get('bank_code'),
                'match_score': score,
                'parse_status': parse_status
            },
            'email_header': {
                'subject': subj,
                'from': frm,
                'date': _to_text(msg.get('Date', ''))
            },
            'parsed_statement': fields,
            'validation': validation_dict
        }

        ts = datetime.now().strftime('%Y%m%d_%H%M%S')
        out = os.path.join(context.report_dir, f'validation_uid_{uid}_{ts}.json')
        validate_write_target(context, out)
        os.makedirs(context.report_dir, exist_ok=True)
        with open(out, 'w', encoding='utf-8') as f:
            json.dump(report, f, ensure_ascii=False, indent=2)

        # 落库：账单 + 交易明细单事务原子保存（即使新明细为空也清空旧明细，防重解析残留）
        init_db(context.db_path)
        statement = StatementRecord(
            account_name=account_name,
            uid=str(uid),
            message_id=message_id,
            bank_code=bank_code,
            rule_id=_to_text(rule.get('rule_id', '')),
            subject=subj,
            sender=frm,
            email_date=_to_text(msg.get('Date', '')),
            statement_month=fields.get('statement_month'),
            statement_date=fields.get('statement_date'),
            due_date=fields.get('due_date'),
            total_due=fields.get('total_due'),
            minimum_due=fields.get('minimum_due'),
            raw_fields_json=json.dumps({'fields': fields, 'meta': fields_meta}, ensure_ascii=False),
            parse_status=parse_status,
        )

        txns = []
        if statement.bank_code == 'HX':
            txns = parse_hx_transactions_from_markdown(uid, statement.statement_month, content.get('markdown', ''))
        elif statement.bank_code == 'CMB':
            txns = parse_cmb_transactions_from_markdown(uid, statement.statement_month, content.get('markdown', ''))
        elif statement.bank_code == 'SPDB':
            txns = parse_spdb_transactions_from_markdown(uid, statement.statement_month, content.get('markdown', ''))
        elif statement.bank_code == 'ICBC':
            txns = parse_icbc_transactions_from_markdown(uid, statement.statement_month, content.get('markdown', ''))
        elif statement.bank_code == 'CITIC':
            txns = parse_citic_transactions_from_body(uid, statement.statement_month, content.get('plain', ''))
        for _t in txns:
            _t.account_name = account_name

        quality_cfg = rule.get('quality') or {}
        currency_decl = quality_cfg.get('currency_declaration') or {}
        settlement_currency = currency_decl.get('settlement_currency') if currency_decl.get('evidence') else None
        if not confirmed_currency(settlement_currency, 'rule_declared'):
            settlement_currency = None
        semantic_txns, semantic_stats, row_diags = parse_semantic_transactions(
            uid, statement.statement_month, content.get('markdown', ''), rule, _infer_date_from_any)
        if semantic_txns is not None:
            txns = semantic_txns
        else:
            # Legacy positional extraction is retained as raw evidence, never currency proof.
            for t in txns:
                if statement.bank_code=='CITIC' and t.currency_source=='extracted':
                    continue
                t.currency = 'UNKNOWN'
                t.currency_source = None
        for t in txns:
            t.account_name = account_name
        # 交易行统计（覆盖缺口诊断输入）：候选/有效/拒绝 + 声明笔数（可配置正则）
        candidate_rows = semantic_stats['candidate_row_count'] if semantic_txns is not None else _count_candidate_rows(content.get('markdown', ''))
        declared_txn_count = 0
        declared_pat = quality_cfg.get('declared_txn_pattern')
        if declared_pat:
            dm = re.search(declared_pat, body_text or '')
            if dm:
                try:
                    declared_txn_count = int(dm.group(1))
                except (ValueError, IndexError):
                    declared_txn_count = 0
        txn_stats = {
            'candidate_row_count': candidate_rows,
            'valid_row_count': len(txns),
            'rejected_row_count': max(0, candidate_rows - len(txns)),
            'table_block_count': semantic_stats['table_block_count'] if semantic_txns is not None else len(split_markdown_table_blocks(content.get('markdown', ''))),
            'declared_txn_count': declared_txn_count,
            'original_amount_count': sum(1 for t in txns if t.original_amount is not None),
            'original_currency_known': sum(1 for t in txns if t.original_amount is not None and getattr(t, 'original_currency_source', None) in ('extracted','rule_declared') and getattr(t, 'original_currency', None) not in (None,'UNKNOWN')),
            'settlement_currency_unknown': sum(1 for t in txns if not t.currency_source or t.currency == 'UNKNOWN'),
        }

        # 多币种应还余额：模板声明币种 → rule_declared；未声明 → UNKNOWN 显式展示（不猜）
        balances = []
        if fields.get('total_due') is not None or fields.get('minimum_due') is not None:
            balances.append(StatementBalanceRecord(
                currency=settlement_currency or 'UNKNOWN',
                total_due=fields.get('total_due'),
                minimum_due=fields.get('minimum_due'),
                currency_source='rule_declared' if settlement_currency else None,
                evidence=(currency_decl.get('evidence') and {'declaration': currency_decl['evidence']}) or {},
            ))

        # ---------- 诊断组装（用途隔离，重构契约 §2.1）----------
        template_version = _to_text(rule.get('template_version') or rule.get('rule_id', ''))
        evidence_ref = {'kind': 'statement', 'account': account_name, 'uid': str(uid),
                        'message_id': message_id, 'report_path': out,
                        'content_hash': hashlib.sha256(body_text.encode()).hexdigest(),
                        'parser_version': parser_version}
        diagnostics = list(row_diags)
        for field,meta in fields_meta.items():
            if meta.get('source')=='inferred':
                before=(meta.get('evidence') or {}).get('before_fallback') or {}
                diag=diagnose_missing_field(field,before.get('source','unknown'),
                    'Primary extraction unavailable; inferred fallback requires confirmation',evidence_ref)
                if diag:
                    diagnostics.append(diag)
        # Use the same evidence classification for required validation and purpose gates.
        for issue in vr.errors:
            if issue.code == 'PARSE_REQUIRED_FIELD_MISSING' and issue.field:
                meta = fields_meta.get(issue.field) or {}
                d = diagnose_missing_field(issue.field, meta.get('source', 'unknown'),
                    (meta.get('evidence') or {}).get('reason', 'missing required field'), evidence_ref)
                if d:
                    diagnostics.append(d)
        # 用途级缺失（required_fields_by_purpose）：阻用途不降级 parse_status
        for purpose, missing in (missing_fields_by_purpose(rule, fields_meta) or {}).items():
            for f in missing:
                meta_src = (fields_meta.get(f) or {}).get('source', 'unknown')
                diag = diagnose_missing_field(f, meta_src,
                                              f'{purpose} 需要字段 {f} 但来源状态为 {meta_src}',
                                              evidence_ref=evidence_ref)
                if diag:
                    diag.affected_purposes = list(set(list(diag.affected_purposes) + [purpose]))
                    if not any(d.code == diag.code and d.field == diag.field for d in diagnostics):
                        diagnostics.append(diag)
                    else:
                        for existing_diag in diagnostics:
                            if existing_diag.code == diag.code and existing_diag.field == diag.field:
                                existing_diag.affected_purposes = sorted(set(existing_diag.affected_purposes + diag.affected_purposes))
        diagnostics.extend(diagnose_currency(txn_stats, evidence_ref))
        diagnostics.extend(diagnose_coverage(txn_stats, evidence_ref))
        # 结构指纹（仅当模板已配置确认基线才比较；金额/排版变化不进指纹）
        fingerprint = detail_fingerprint(content.get('markdown', ''), rule)
        diagnostics.extend(diagnose_format(fingerprint, rule, evidence_ref))
        if balances and any(b.currency == 'UNKNOWN' for b in balances):
            diagnostics.append(Diagnostic(code='CURRENCY_UNKNOWN', field='balance_currency',
                reason='Statement balance has no independent currency evidence',
                affected_purposes=['financial_summary','repayment_reminder'], evidence_ref=evidence_ref))
        quality_data = {'diagnostics': [d.to_dict() for d in diagnostics],
                        'purpose_availability': purpose_availability(diagnostics, fields_meta),
                        'txn_stats': txn_stats, 'fingerprint': fingerprint,
                        'baseline_status': 'reviewed_fixture' if quality_cfg.get('accepted_structure_fingerprints') else 'unavailable',
                        'parser_version': parser_version}
        statement.raw_fields_json = json.dumps({'fields': fields, 'meta': fields_meta, 'quality': quality_data}, ensure_ascii=False)
        report['field_evidence'] = fields_meta
        report['quality'] = quality_data
        with open(out, 'w', encoding='utf-8') as report_file:
            json.dump(report, report_file, ensure_ascii=False, indent=2)
        quality_event_recs = [
            QualityEventRecord(
                case_key=e['case_key'], event_type=e['event_type'],
                account_name=account_name, bank_code=bank_code,
                template_version=template_version,
                diagnostics=e['diagnostics'], evidence_refs=e['evidence_refs'],
            )
            for e in events_from_diagnostics(diagnostics, account_name, bank_code, template_version,
                                             [evidence_ref])
        ]

        # Missing diagnostics are not proof. Only fields actually evaluated and
        # confirmed by this parse may close an existing case.
        verified_fields = {f for f, m in fields_meta.items()
                           if m.get('source') == 'extracted' and m.get('value') is not None}
        if semantic_txns is not None and not txn_stats['rejected_row_count']:
            verified_fields.add('transactions')
            if quality_cfg.get('accepted_structure_fingerprints') and not any(d.code=='FORMAT_CHANGED' for d in diagnostics):
                verified_fields.add('structure')
            if txns and not txn_stats['settlement_currency_unknown']:
                verified_fields.add('currency')
            if txn_stats['original_amount_count'] and txn_stats['original_currency_known'] == txn_stats['original_amount_count'] and not any(d.field=='original_amount' for d in diagnostics):
                verified_fields.update(('original_currency','original_amount'))
        if settlement_currency:
            verified_fields.add('balance_currency')
        verified_fields -= {d.field for d in diagnostics}
        resolution_results = []
        statement_id, txn_count = save_statement_with_transactions(
            context.db_path, statement, txns,
            balances=balances, quality_events=quality_event_recs,
            quality_confirmation={
                'captured_cases': captured_cases, 'evidence_ref': evidence_ref,
                'current_case_keys': {e.case_key for e in quality_event_recs},
                'template_version': template_version, 'parser_version': parser_version,
                'parse_passed': vr.passed, 'verified_fields': verified_fields,
                'results': resolution_results,
            },
        )

        # 建立证据索引并归档不可变 EML 原文 (E1 R3, R5)
        if raw_bytes and statement_id:
            try:
                source_ev = si.build_evidence_from_email_message(
                    msg, raw_bytes=raw_bytes, account_name=account_name, bank_code=bank_code,
                    transport_kind=transport_kind, transport_uid=str(uid), is_synthesized=is_synthesized
                )
                raw_hash = source_ev.get("raw_content_hash")
                if raw_hash:
                    archive_path = si.archive_evidence_eml(context.data_root, raw_bytes, raw_hash, context=context)
                    save_statement_evidence_index(
                        context.db_path,
                        statement_id=statement_id,
                        account_name=account_name,
                        bank_code=bank_code,
                        message_id=message_id,
                        raw_content_hash=raw_hash,
                        evidence_digest=source_ev.get("complete_content_digest") or "",
                        coverage_status=source_ev.get("coverage_status") or "coverage_unknown",
                        archive_path=archive_path,
                        evidence_version=si.NORMALIZATION_VERSION
                    )
                    associate_transport_identity(
                        context.db_path,
                        account_name=account_name,
                        transport_kind=transport_kind,
                        transport_uid=str(uid),
                        bank_code=bank_code,
                        statement_id=statement_id,
                        message_id=message_id,
                        evidence_digest=source_ev.get("complete_content_digest"),
                        evidence_version=si.NORMALIZATION_VERSION,
                        raw_content_hash=raw_hash,
                        method="digest_equal",
                        actor="auto_sync",
                        reason="initial statement parse",
                        source_evidence=source_ev,
                        target_evidence=source_ev
                    )
            except Exception as e_ev:
                ev_err = f"证据归档/索引失败: {e_ev}"
                vr.warnings.append(ValidationIssue(code='EVIDENCE_INDEX_FAILED', field='evidence', severity='warning'))
                print(f"⚠️ {ev_err}")

        run_id = save_validation_run(
            context.db_path,
            account_name,
            uid=str(uid),
            bank_code=bank_code,
            rule_id=_to_text(rule.get('rule_id', '')),
            passed=vr.passed,
            report_path=out,
            errors=vr.errors,
            warnings=vr.warnings,
        )

        status = '通过' if vr.passed else '未通过'
        print(f'{status}：{out}')
        print(f'规则：{rule.get("rule_id")}')
        print(f'错误数：{len(vr.errors)}，告警数：{len(vr.warnings)}')
        print(f'交易入库数：{txn_count}')
        print(f'数据库记录 run_id：{run_id}')
        return {
            'status': 'saved' if vr.passed else 'parse_failed',
            'statement_id': statement_id,
            'txn_count': txn_count,
            'run_id': run_id,
            'errors': len(vr.errors),
            'warnings': len(vr.warnings),
            'report_path': out,
            'bank_code': bank_code,
            'template_version': template_version,
            'message_id': message_id,
            'parse_status': parse_status,
            'evidence_status': 'failed' if any(getattr(w, 'code', None) == 'EVIDENCE_INDEX_FAILED' for w in vr.warnings) else 'indexed',
            'diagnostics': [d.to_dict() for d in diagnostics],
            'txn_stats': txn_stats,
            'purpose_availability': quality_data['purpose_availability'],
            'baseline_status': quality_data['baseline_status'],
            'resolution_results': resolution_results,
        }
    except MessageIdentityConflict as exc:
        # Keep the new transport's original body/report and validation audit;
        # reject duplicate financial rows rather than overwriting old evidence.
        issue = ValidationIssue(code='MESSAGE_ID_CONFLICT', field='message_id', severity='error')
        bank_c = bank_code if 'bank_code' in locals() else ''
        rule_c = _to_text(rule.get('rule_id', '')) if ('rule' in locals() and rule) else ''
        warns = vr.warnings if 'vr' in locals() else []
        run_id = save_validation_run(context.db_path, account_name, uid=str(uid), bank_code=bank_c,
                    rule_id=rule_c, passed=False, report_path=out or '',
                    errors=[issue], warnings=warns)
        print(f'身份冲突：{exc}；原账单及交易保持不变，已归档新邮件证据')
        return {'status': 'identity_conflict', 'run_id': run_id, 'report_path': out, 'error': str(exc)}
    except Exception as e:
        print(f'验证失败：{e}')
        return {'status': 'error', 'error': str(e)}


def reparse_cached_statement(account_name, uid, *, context=None):
    """离线重解析：优先使用 email_bodies 已缓存正文重建邮件，避免网络重拉。

    元数据（主题/发件人/日期/Message-ID）取 statements 或 email_summaries 已存记录。
    返回与 validate_and_save_email_message 相同的 dict。
    """
    if context is None:
        kwargs = {}
        if DB_PATH != _DEFAULT_PATHS.db_path:
            kwargs['db_path'] = DB_PATH
        if VALIDATION_REPORT_DIR != _DEFAULT_PATHS.report_dir:
            kwargs['report_dir'] = VALIDATION_REPORT_DIR
        context = resolve_parser_paths(**kwargs)
    elif isinstance(context, dict):
        context = resolve_parser_paths(**context)
    elif isinstance(context, ParserPaths):
        pass
    else:
        raise TypeError(f"context 必须为 ParserPaths 实例或 dict，收到: {type(context).__name__}")

    validate_write_target(context, context.db_path)
    import sqlite3
    from email.message import EmailMessage
    init_db(context.db_path)
    conn = sqlite3.connect(context.db_path)
    conn.row_factory = sqlite3.Row
    try:
        body = conn.execute(
            "SELECT raw_html, plain_text, markdown_tables FROM email_bodies WHERE account_name = ? AND uid = ?",
            (account_name, str(uid))).fetchone()
        if not body or not (body['raw_html'] or body['plain_text'] or body['markdown_tables']):
            return {'status': 'error', 'error': 'no_cached_body'}
        meta = conn.execute(
            "SELECT subject, sender, email_date, message_id FROM statements WHERE account_name = ? AND uid = ? LIMIT 1",
            (account_name, str(uid))).fetchone()
        if not meta:
            meta = conn.execute(
                "SELECT subject, sender, email_date, NULL AS message_id FROM email_summaries WHERE account_name = ? AND uid = ? LIMIT 1",
                (account_name, str(uid))).fetchone()
    finally:
        conn.close()

    msg = EmailMessage()
    msg['Subject'] = (meta['subject'] if meta else '') or ''
    msg['From'] = (meta['sender'] if meta else '') or ''
    msg['Date'] = (meta['email_date'] if meta else '') or ''
    if meta and meta['message_id']:
        msg['Message-ID'] = meta['message_id']
    msg.set_content(body['plain_text'] or '(无纯文本正文)')
    if body['raw_html']:
        msg.add_alternative(body['raw_html'], subtype='html')
    return validate_and_save_email_message(msg, uid, account_name=account_name, context=context)

