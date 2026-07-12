#!/usr/bin/env python3
"""账单解析与校验模块。

从 mail_client.py 抽取，包含：银行解析函数（HX/CMB/SPDB/ICBC/CITIC）、
规则匹配与校验、字段提取与规范化、落库。
依赖 statement_models / statement_db / mail_parse，不 import mail_client。
"""

import re
import json
import os
import glob
from datetime import datetime, timezone, timedelta
from decimal import Decimal, InvalidOperation

from statement_models import (
    StatementRecord, StatementTransactionRecord,
    ValidationResult, ValidationIssue,
)
from statement_db import (
    init_db, upsert_statement, save_validation_run, replace_transactions,
)
from mail_parse import decode_mime, _to_text, extract_email_content, _parse_email_datetime

# 路径常量（与 mail_client 同级目录，os.path.expanduser("rules") 解析结果相同）
RULES_DIR = os.path.expanduser("rules")
DB_PATH = os.path.expanduser("statements.db")
VALIDATION_REPORT_DIR = os.path.expanduser("validation-reports")


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
        return Decimal(s)
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
    m = re.search(r'(20\d{2})年(0?[1-9]|1[0-2])月', s)
    if m:
        return f'{m.group(1)}-{int(m.group(2)):02d}'
    m = re.search(r'(20\d{2})[-/](0?[1-9]|1[0-2])', s)
    if m:
        return f'{m.group(1)}-{int(m.group(2)):02d}'
    m = re.search(r'(20\d{2})[-/](0?[1-9]|1[0-2])[-/](\d{1,2})', s)
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
    """招商账单还款日兜底：优先标签匹配，失败时按账单日+18天择优估算。"""
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

    # 2) 标签缺失时，从正文日期候选中按 statement_date+18 天择优
    if not statement_date:
        return None
    try:
        stmt_dt = datetime.strptime(statement_date, '%Y-%m-%d').date()
    except ValueError:
        return None

    candidates = set(re.findall(r'(20\d{2}[/-]\d{1,2}[/-]\d{1,2})', body_text))
    if not candidates:
        return None

    parsed_candidates = []
    for c in candidates:
        d = _parse_date(c, ['%Y-%m-%d', '%Y/%m/%d'])
        if not d:
            continue
        try:
            dd = datetime.strptime(d, '%Y-%m-%d').date()
        except ValueError:
            continue
        # 常见信用卡还款日在账单日后 7~40 天内
        if stmt_dt <= dd <= stmt_dt + timedelta(days=40):
            parsed_candidates.append(dd)

    if not parsed_candidates:
        return None

    expected = stmt_dt + timedelta(days=18)
    best = min(parsed_candidates, key=lambda x: (abs((x - expected).days), x))
    return best.isoformat()


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


def parse_hx_transactions_from_markdown(uid, statement_month, markdown_text):
    """从华夏账单 Markdown 表中提取交易明细。"""
    if not markdown_text:
        return []
    rows = []
    seen = set()
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
        key = (cols[0], cols[1], cols[2], str(d))
        if key in seen:
            continue
        seen.add(key)
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
    """从招商账单 Markdown 表中提取交易明细。"""
    if not markdown_text:
        return []
    rows = []
    seen = set()
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

        key = (cols[txn_col], cols[post_col], desc, str(d), txn_location_code or '', original_amount or '')
        if key in seen:
            continue
        seen.add(key)

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
    """从浦发账单 Markdown 表中提取交易明细（若邮件包含明细表）。"""
    if not markdown_text:
        return []
    rows = []
    seen = set()
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

        key = (date_val or '', desc_val, str(amount_val))
        if key in seen:
            continue
        seen.add(key)

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
    """从工行账单 Markdown 表中提取交易明细。"""
    if not markdown_text:
        return []
    rows = []
    seen = set()
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

        key = (cols[0], cols[1], cols[2], desc, str(d))
        if key in seen:
            continue
        seen.add(key)

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
    """
    if not body_text:
        return []
    rows = []
    seen = set()
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
        key = (txn_raw, post_raw, desc, str(d))
        if key in seen:
            continue
        seen.add(key)
        rows.append(
            StatementTransactionRecord(
                uid=str(uid),
                bank_code='CITIC',
                txn_date=txn_date,
                post_date=post_date,
                description=desc,
                amount=str(d),
                currency='CNY',
                direction='credit' if d < 0 else 'debit',
                raw_row_json=json.dumps(
                    {'txn': txn_raw, 'post': post_raw, 'desc': desc,
                     'trx_amt': trx_amt_str, 'setl_amt': setl_amt_str},
                    ensure_ascii=False,
                ),
            )
        )
    return rows


def load_rule_files():
    """加载规则文件（当前支持 JSON）。"""
    os.makedirs(RULES_DIR, exist_ok=True)
    rules = []
    for path in sorted(glob.glob(os.path.join(RULES_DIR, '*.json'))):
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
    """按规则匹配模板，返回最佳匹配规则与分数。"""
    best = None
    best_score = -1
    for r in rules:
        m = r.get('match_rules', {})
        sender_score = _match_score(sender, m.get('sender_patterns', [])) * 5
        subject_score = _match_score(subject, m.get('subject_patterns', [])) * 3
        body_score = _match_score(body_text, m.get('body_signatures', []))
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

def extract_statement_by_rule(rule, subject, body_text):
    fields = {}
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

    _apply_due_date_fallbacks(rule, body_text, fields)
    _apply_amount_fallbacks(rule, body_text, fields)

    for amt_key in ('total_due', 'minimum_due'):
        if fields.get(amt_key) is not None:
            d = _safe_decimal(fields[amt_key], amount_tokens)
            fields[amt_key] = str(d) if d is not None else None

    return fields

def validate_statement_by_rule(rule, fields):
    result = ValidationResult(passed=True)
    vr = rule.get('validate_rules', {})

    # 必填校验
    for name in vr.get('required_statement_fields', []):
        if not fields.get(name):
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

def validate_and_save_email_message(msg, uid, rules=None, account_name=None):
    """提取的重用逻辑：从已下载的 msg 解析、校验并落库账单"""
    if rules is None:
        rules = load_rule_files()
    if not rules:
        print('未找到规则文件，请先在 rules 目录下放置 *.json')
        return

    account_name = account_name or 'default'
    try:
        subj = decode_mime(msg.get('Subject', ''))
        frm = decode_mime(msg.get('From', ''))
        content = extract_email_content(msg)
        body_text = (content.get('plain', '') + '\n' + content.get('markdown', '')).strip()
        from statement_db import upsert_email_body
        upsert_email_body(DB_PATH, account_name or 'default', str(uid),
                          raw_html=content.get('html'),
                          plain_text=body_text,
                          markdown_tables=content.get('markdown'))
        rule, score = identify_rule(subj, frm, body_text, rules)
        if not rule:
            print('未匹配到规则，无法验证')
            return

        fields = extract_statement_by_rule(rule, subj, body_text)
        _apply_statement_date_month_fallbacks(rule, _to_text(msg.get('Date', '')), fields)
        vr = validate_statement_by_rule(rule, fields)

        validation_dict = _validation_result_to_dict(vr)
        report = {
            'report_meta': {
                'generated_at': datetime.now(timezone.utc).isoformat(),
                'uid': str(uid),
                'rule_id': rule.get('rule_id'),
                'bank_code': rule.get('bank_code'),
                'match_score': score
            },
            'email_header': {
                'subject': subj,
                'from': frm,
                'date': _to_text(msg.get('Date', ''))
            },
            'parsed_statement': fields,
            'validation': validation_dict
        }

        os.makedirs(VALIDATION_REPORT_DIR, exist_ok=True)
        ts = datetime.now().strftime('%Y%m%d_%H%M%S')
        out = os.path.join(VALIDATION_REPORT_DIR, f'validation_uid_{uid}_{ts}.json')
        with open(out, 'w', encoding='utf-8') as f:
            json.dump(report, f, ensure_ascii=False, indent=2)

        # 落库：结构化账单 + 本次校验运行记录
        init_db(DB_PATH)
        statement = StatementRecord(
            account_name=account_name,
            uid=str(uid),
            message_id=_to_text(msg.get('Message-ID', '')).strip(),
            bank_code=_to_text(rule.get('bank_code', '')),
            rule_id=_to_text(rule.get('rule_id', '')),
            subject=subj,
            sender=frm,
            email_date=_to_text(msg.get('Date', '')),
            statement_month=fields.get('statement_month'),
            statement_date=fields.get('statement_date'),
            due_date=fields.get('due_date'),
            total_due=fields.get('total_due'),
            minimum_due=fields.get('minimum_due'),
            raw_fields_json=json.dumps(fields, ensure_ascii=False),
        )
        upsert_statement(DB_PATH, statement)

        txn_count = 0
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
        if txns:
            for _t in txns:
                _t.account_name = account_name
            txn_count = replace_transactions(DB_PATH, account_name, str(uid), statement.bank_code, txns)

        run_id = save_validation_run(
            DB_PATH,
            account_name,
            uid=str(uid),
            bank_code=_to_text(rule.get('bank_code', '')),
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
    except Exception as e:
        print(f'验证失败：{e}')

