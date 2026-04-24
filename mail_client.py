#!/usr/bin/env python3
"""163 邮箱邮件技能 - 支持发送、读取、搜索、下载（含 HTML 表格解析）"""

import json, os, sys, smtplib, imaplib, poplib, email, re, glob
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from email.mime.text import MIMEText
from email.header import decode_header
from email.utils import parsedate_to_datetime
from html.parser import HTMLParser
from statement_models import StatementRecord, ValidationIssue, ValidationResult, StatementTransactionRecord
from statement_db import init_db, upsert_statement, save_validation_run, replace_transactions, get_recent_statements, get_summary_by_bank_month, get_reconciliation_rows, uid_exists, get_transactions_above_amount

CONFIG_CANDIDATES = [
    os.path.expanduser('email-config.local.json'),
    os.path.expanduser('email-config.json'),
    os.path.expanduser('email-config.example.json'),
]
DOWNLOAD_DIR = os.path.expanduser('email-downloads')
RULES_DIR = os.path.expanduser('rules')
VALIDATION_REPORT_DIR = os.path.expanduser('validation-reports')
DB_PATH = os.path.expanduser('statements.db')

class HTMLTableParser(HTMLParser):
    """解析 HTML 表格"""
    def __init__(self):
        super().__init__()
        self.tables = []
        self.current_table = []
        self.current_row = []
        self.current_cell = []
        self.in_cell = False
    
    def handle_starttag(self, tag, attrs):
        if tag == 'table':
            self.current_table = []
        elif tag == 'tr':
            self.current_row = []
        elif tag in ['td', 'th']:
            self.in_cell = True
            self.current_cell = []
    
    def handle_endtag(self, tag):
        if tag == 'table' and self.current_table:
            self.tables.append(self.current_table)
        elif tag == 'tr' and self.current_row:
            self.current_table.append(self.current_row)
        elif tag in ['td', 'th']:
            self.in_cell = False
            self.current_row.append(''.join(self.current_cell).strip())
    
    def handle_data(self, data):
        if self.in_cell:
            self.current_cell.append(data.strip())

def load_config():
    try:
        for config_path in CONFIG_CANDIDATES:
            if os.path.exists(config_path):
                with open(config_path, 'r', encoding='utf-8') as f:
                    return json.load(f)
        raise FileNotFoundError('未找到配置文件：email-config.local.json / email-config.json / email-config.example.json')
    except Exception as e:
        print(f'错误：配置文件 - {e}')
        sys.exit(1)

def decode_mime(s):
    if not s: return ''
    result = []
    for part, enc in decode_header(s):
        if isinstance(part, bytes):
            result.append(part.decode(enc or 'utf-8', errors='ignore'))
        else:
            result.append(str(part))
    return ''.join(result)

def _to_text(v):
    if v is None:
        return ''
    if isinstance(v, bytes):
        return v.decode('utf-8', errors='ignore')
    return str(v)

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

def setup_storage():
    init_db(DB_PATH)
    print(f'✅ SQLite 已初始化：{DB_PATH}')

def show_recent_statements(months=3):
    init_db(DB_PATH)
    rows = get_recent_statements(DB_PATH, months)
    if not rows:
        print(f'📭 最近 {months} 个月暂无账单记录')
        return
    print(f'📄 最近 {months} 个月账单记录（{len(rows)} 条）\n')
    for r in rows:
        print(
            f"UID={r['uid']} bank={r['bank_code']} month={r['statement_month'] or '-'} "
            f"due={r['due_date'] or '-'} total={r['total_due'] or '-'} min={r['minimum_due'] or '-'} "
            f"txn_count={r['txn_count']} txn_sum={r['txn_sum']}"
        )

def show_statement_report(months=3):
    init_db(DB_PATH)
    rows = get_summary_by_bank_month(DB_PATH, months)
    if not rows:
        print(f'📭 最近 {months} 个月暂无可汇总数据')
        return

    bank_name_map = {
        'HX': '华夏银行',
        'CMB': '招商银行',
        'SPDB': '浦发银行',
        'CMBC': '民生银行',
    }

    print(f'📊 最近 {months} 个月按银行/月份汇总\n')
    print('bank\tym\tcount\tsum_total_due\tsum_minimum_due\tsum_txn_amount\tsum_reconcile_diff\tforeign_codes\tforeign_amount_breakdown')
    for r in rows:
        bank_code = r['bank_code']
        bank_display = f"{bank_code}({bank_name_map.get(bank_code, '未知银行')})"
        print(
            f"{bank_display}\t{r['ym']}\t{r['statement_count']}\t"
            f"{r['sum_total_due']}\t{r['sum_minimum_due']}\t{r['sum_txn_amount']}\t{r['sum_reconcile_diff']}\t"
            f"{r['foreign_codes']}\t{r['foreign_amount_breakdown']}"
        )


def show_reconcile(months=3, tolerance=1.0):
    init_db(DB_PATH)
    rows = get_reconciliation_rows(DB_PATH, months)
    if not rows:
        print(f'📭 最近 {months} 个月暂无可对账记录')
        return
    tol = float(tolerance)
    print(f'🧾 最近 {months} 个月对账检查 (tolerance={tol})\n')
    print('uid\tbank\tmonth\ttotal_due\ttxn_sum\tdiff\tstatus')
    for r in rows:
        diff = float(r['reconcile_diff'])
        status = 'PASS' if abs(diff) <= tol else 'CHECK'
        print(
            f"{r['uid']}\t{r['bank_code']}\t{r['statement_month'] or '-'}\t"
            f"{r['total_due'] or '-'}\t{r['txn_sum']}\t{r['reconcile_diff']}\t{status}"
        )


def show_transactions_over(amount, months=None):
    """独立命令：查询金额大于阈值的交易明细。"""
    init_db(DB_PATH)
    rows = get_transactions_above_amount(DB_PATH, float(amount), months)
    if not rows:
        if months is None:
            print(f'📭 暂无金额 >= {amount} 的交易明细')
        else:
            print(f'📭 最近 {months} 个月暂无金额 >= {amount} 的交易明细')
        return

    scope_text = '全部历史'
    if months is not None:
        scope_text = f'最近 {months} 个月'

    print(f'📌 {scope_text} 金额 >= {amount} 的交易明细（{len(rows)} 条）\n')
    print('uid\tbank\tmonth\ttxn_date\tpost_date\tdescription\tamount\tcurrency\tlocation\toriginal_amount')
    for r in rows:
        print(
            f"{r['uid']}\t{r['bank_code']}\t{r['statement_month'] or '-'}\t"
            f"{r['txn_date'] or '-'}\t{r['post_date'] or '-'}\t{r['description'] or '-'}\t"
            f"{r['amount']}\t{r['currency'] or '-'}\t{r['txn_location_code'] or '-'}\t{r['original_amount']}"
        )

def pop3_fetch_message_by_uid(uid):
    """按 UID 拉取原始邮件，返回 message 对象。"""
    config = load_config()
    mail = poplib.POP3_SSL(config['email']['pop3']['host'], config['email']['pop3']['port'])
    mail.user(config['email']['account'])
    mail.pass_(config['email']['authCode'])
    try:
        _, headers, _ = mail.retr(int(uid))
        msg = email.message_from_bytes(b'\r\n'.join(headers))
        return msg
    finally:
        mail.quit()

def classify_email_by_uid(uid):
    rules = load_rule_files()
    if not rules:
        print('❌ 未找到规则文件，请先在 rules 目录下放置 *.json')
        return

    try:
        msg = pop3_fetch_message_by_uid(uid)
        subj = decode_mime(msg.get('Subject', ''))
        frm = decode_mime(msg.get('From', ''))
        content = extract_email_content(msg)
        body_text = (content.get('plain', '') + '\n' + content.get('markdown', '')).strip()
        rule, score = identify_rule(subj, frm, body_text, rules)
        if not rule:
            print('❌ 未匹配到规则')
            return
        print(f'✅ 匹配规则：{rule.get("rule_id")} (bank={rule.get("bank_code")}, score={score})')
        print(f'   主题：{subj}')
        print(f'   发件人：{frm}')
    except Exception as e:
        print(f'❌ 分类失败：{e}')

def validate_email_by_uid(uid):
    """按规则解析并输出校验报告。"""
    rules = load_rule_files()
    if not rules:
        print('❌ 未找到规则文件，请先在 rules 目录下放置 *.json')
        return

    try:
        msg = pop3_fetch_message_by_uid(uid)
        subj = decode_mime(msg.get('Subject', ''))
        frm = decode_mime(msg.get('From', ''))
        content = extract_email_content(msg)
        body_text = (content.get('plain', '') + '\n' + content.get('markdown', '')).strip()
        rule, score = identify_rule(subj, frm, body_text, rules)
        if not rule:
            print('❌ 未匹配到规则，无法验证')
            return

        fields = extract_statement_by_rule(rule, subj, body_text)
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
        if txns:
            txn_count = replace_transactions(DB_PATH, str(uid), statement.bank_code, txns)

        run_id = save_validation_run(
            DB_PATH,
            uid=str(uid),
            bank_code=_to_text(rule.get('bank_code', '')),
            rule_id=_to_text(rule.get('rule_id', '')),
            passed=vr.passed,
            report_path=out,
            errors=vr.errors,
            warnings=vr.warnings,
        )

        status = '✅ 通过' if vr.passed else '❌ 未通过'
        print(f'{status}：{out}')
        print(f'规则：{rule.get("rule_id")}')
        print(f'错误数：{len(vr.errors)}，告警数：{len(vr.warnings)}')
        print(f'交易入库数：{txn_count}')
        print(f'数据库记录 run_id：{run_id}')
    except Exception as e:
        print(f'❌ 验证失败：{e}')

def html_to_text(html):
    """将 HTML 转换为纯文本"""
    if not html:
        return ''
    
    # 移除 script 和 style
    html = re.sub(r'<script[^>]*>.*?</script>', '', html, flags=re.DOTALL|re.IGNORECASE)
    html = re.sub(r'<style[^>]*>.*?</style>', '', html, flags=re.DOTALL|re.IGNORECASE)
    
    # 替换常见标签
    html = re.sub(r'<br\s*/?>', '\n', html, flags=re.IGNORECASE)
    html = re.sub(r'</p>', '\n\n', html, flags=re.IGNORECASE)
    html = re.sub(r'</div>', '\n', html, flags=re.IGNORECASE)
    html = re.sub(r'</h[1-6]>', '\n', html, flags=re.IGNORECASE)
    
    # 移除所有 HTML 标签
    text = re.sub(r'<[^>]+>', '', html)
    
    # 解码 HTML 实体
    text = text.replace('&nbsp;', ' ')
    text = text.replace('&lt;', '<')
    text = text.replace('&gt;', '>')
    text = text.replace('&amp;', '&')
    text = text.replace('&quot;', '"')
    text = text.replace('&#39;', "'")
    
    # 清理空白
    text = re.sub(r'\n\s*\n', '\n\n', text)
    text = text.strip()
    
    return text

def parse_html_tables(html):
    """解析 HTML 中的表格"""
    parser = HTMLTableParser()
    try:
        parser.feed(html)
    except:
        pass
    return parser.tables

def tables_to_markdown(tables):
    """将表格转换为 Markdown 格式"""
    if not tables:
        return ''
    
    md_parts = []
    for table_idx, table in enumerate(tables, 1):
        if len(table) < 2:
            continue
        
        md_parts.append(f'\n### 表格 {table_idx}\n')
        
        # 表头
        header = table[0]
        md_parts.append('| ' + ' | '.join(header) + ' |')
        md_parts.append('| ' + ' | '.join(['---'] * len(header)) + ' |')
        
        # 数据行
        for row in table[1:]:
            # 确保行长度与表头一致
            while len(row) < len(header):
                row.append('')
            md_parts.append('| ' + ' | '.join(row[:len(header)]) + ' |')
        
        md_parts.append('')
    
    return '\n'.join(md_parts)

def extract_email_content(msg):
    """提取邮件正文（支持 HTML 和纯文本）"""
    result = {
        'plain': '',
        'html': '',
        'tables': [],
        'markdown': ''
    }
    
    if msg.is_multipart():
        for part in msg.walk():
            content_type = part.get_content_type()
            charset = part.get_content_charset() or 'utf-8'
            
            try:
                payload = part.get_payload(decode=True)
                if not payload:
                    continue
                
                content = payload.decode(charset, errors='ignore')
                
                if content_type == 'text/plain':
                    result['plain'] += content
                elif content_type == 'text/html':
                    result['html'] += content
            except:
                pass
    else:
        try:
            payload = msg.get_payload(decode=True)
            if payload:
                charset = msg.get_content_charset() or 'utf-8'
                content = payload.decode(charset, errors='ignore')
                if msg.get_content_type() == 'text/html':
                    result['html'] = content
                else:
                    result['plain'] = content
        except:
            pass
    
    # 如果有 HTML，转换为文本并提取表格
    if result['html']:
        result['plain'] = html_to_text(result['html']) or result['plain']
        result['tables'] = parse_html_tables(result['html'])
        result['markdown'] = tables_to_markdown(result['tables'])
    
    return result

def send_email(to, subject, text):
    config = load_config()
    try:
        server = smtplib.SMTP_SSL(config['email']['smtp']['host'], config['email']['smtp']['port'])
        server.login(config['email']['account'], config['email']['authCode'])
        msg = MIMEText(text, 'plain', 'utf-8')
        msg['From'] = config['email']['account']
        msg['To'] = to
        msg['Subject'] = subject
        server.send_message(msg)
        server.quit()
        print(f'✅ 发送成功：{to} - {subject}')
    except Exception as e:
        print(f'❌ 发送失败：{e}')

def read_emails_pop3(limit=10):
    config = load_config()
    try:
        mail = poplib.POP3_SSL(config['email']['pop3']['host'], config['email']['pop3']['port'])
        mail.user(config['email']['account'])
        mail.pass_(config['email']['authCode'])
        num = len(mail.list()[1])
        if num == 0:
            print('📭 邮箱为空')
            mail.quit()
            return
        start = max(1, num - limit + 1)
        print(f'📬 最新 {min(limit, num)} 封邮件:\n')
        for i in range(num, start-1, -1):
            _, headers, _ = mail.retr(i)
            msg = email.message_from_bytes(b'\r\n'.join(headers))
            subj = decode_mime(msg.get('Subject', ''))
            frm = decode_mime(msg.get('From', ''))
            date = msg.get('Date', '')
            print(f"UID: {i}\n[{date[:25]}] {frm}\n主题：{subj}\n{'-'*60}")
        mail.quit()
    except Exception as e:
        print(f'❌ POP3 读取失败：{e}')

def search_emails_pop3(keyword, limit=20):
    config = load_config()
    try:
        mail = poplib.POP3_SSL(config['email']['pop3']['host'], config['email']['pop3']['port'])
        mail.user(config['email']['account'])
        mail.pass_(config['email']['authCode'])
        num = len(mail.list()[1])
        if num == 0:
            print('📭 邮箱为空')
            mail.quit()
            return
        results = []
        kw = keyword.lower()
        print(f'🔍 搜索 "{keyword}"...\n')
        skipped = 0
        for i in range(num, 0, -1):
            try:
                _, headers, _ = mail.retr(i)
                content = b'\r\n'.join(headers).decode('utf-8', errors='ignore')
                msg = email.message_from_string(content)
                subj = decode_mime(msg.get('Subject', ''))
                frm = decode_mime(msg.get('From', ''))
                date = msg.get('Date', '')
                if kw in subj.lower() or kw in frm.lower() or kw in content.lower():
                    results.append((i, subj, frm, date))
                    if len(results) <= limit:
                        print(f"UID: {i}\n[{date[:25]}] {frm}\n主题：{subj}\n{'-'*60}")
                if len(results) >= limit * 3:
                    break
            except Exception:
                skipped += 1
                continue
        mail.quit()
        if not results:
            print(f'📭 未找到匹配邮件')
        else:
            print(f'\n共找到 {len(results)} 封')
        if skipped:
            print(f'⚠️ 跳过 {skipped} 封异常邮件（可能包含超长行）')
    except Exception as e:
        print(f'❌ POP3 搜索失败：{e}')


def _month_subtract(dt, months):
    """按自然月回退，不依赖第三方库。"""
    year = dt.year
    month = dt.month - int(months)
    while month <= 0:
        month += 12
        year -= 1
    day = min(dt.day, [31, 29 if (year % 4 == 0 and (year % 100 != 0 or year % 400 == 0)) else 28,
                       31, 30, 31, 30, 31, 31, 30, 31, 30, 31][month - 1])
    return dt.replace(year=year, month=month, day=day)


def _parse_email_datetime(date_str):
    if not date_str:
        return None
    try:
        dt = parsedate_to_datetime(str(date_str))
        if dt is None:
            return None
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except Exception:
        return None


def _save_email_message(uid, msg, output_dir=None, format='md'):
    """保存已拉取的 message 到本地（支持 Markdown/HTML）。"""
    if output_dir is None:
        output_dir = DOWNLOAD_DIR
    os.makedirs(output_dir, exist_ok=True)

    subj = decode_mime(msg.get('Subject', '无主题'))
    frm = decode_mime(msg.get('From', ''))
    date = msg.get('Date', datetime.now().strftime('%a, %d %b %Y %H:%M:%S'))

    # 提取邮件内容（包括 HTML 表格）
    content = extract_email_content(msg)

    # 生成文件名，带 UID 避免批量下载时冲突
    safe_subj = re.sub(r'[^\w\s-]', '', subj[:50]).strip().replace(' ', '_') or 'email'
    fname = f'email_uid{uid}_{safe_subj}'

    saved_paths = {}
    write_html = format in ('html', 'both')
    write_md = format in ('md', 'both')

    if write_html and content['html']:
        html_path = os.path.join(output_dir, f'{fname}.html')
        with open(html_path, 'w', encoding='utf-8') as f:
            f.write(f'''<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<title>{subj}</title>
<style>
body {{ font-family: Arial, sans-serif; margin: 20px; }}
table {{ border-collapse: collapse; width: 100%; margin: 20px 0; }}
th, td {{ border: 1px solid #ddd; padding: 8px; text-align: left; }}
th {{ background-color: #4CAF50; color: white; }}
tr:nth-child(even) {{ background-color: #f2f2f2; }}
</style>
</head>
<body>
<h1>{subj}</h1>
<p><strong>发件人:</strong> {frm}</p>
<p><strong>日期:</strong> {date}</p>
<hr>
{content['html']}
</body>
</html>''')
        print(f'✅ HTML 已下载：{html_path}')
        saved_paths['html'] = html_path

    if write_md:
        md_path = os.path.join(output_dir, f'{fname}.md')
        with open(md_path, 'w', encoding='utf-8') as f:
            f.write(f'# {subj}\n\n')
            f.write(f'**发件人**: {frm}  \n')
            f.write(f'**日期**: {date}\n\n')
            f.write('---\n\n')

            if content['markdown']:
                f.write('## 账单详情\n\n')
                f.write(content['markdown'])
                f.write('\n---\n\n')

            if content['plain']:
                f.write('## 邮件正文\n\n')
                f.write(content['plain'])
            elif not content['markdown']:
                f.write('[无内容]')

        print(f'✅ Markdown 已下载：{md_path}')
        saved_paths['md'] = md_path

    if not saved_paths:
        print('⚠️ 未生成文件：可能仅请求 HTML 且邮件不含 HTML 内容')

    print(f'   主题：{subj}')
    print(f'   发件人：{frm}')
    if content['tables']:
        print(f'   表格数：{len(content["tables"])}')
    return saved_paths


def _collect_recent_bill_uids(months, rules, target_banks):
    """扫描最近N个月账单邮件，返回候选UID与统计信息。"""
    config = load_config()
    cutoff = _month_subtract(datetime.now(timezone.utc), int(months))

    keyword_map = {}
    for r in rules:
        b = r.get('bank_code')
        if b not in target_banks:
            continue
        pats = r.get('match_rules', {}).get('subject_patterns', [])
        keyword_map.setdefault(b, set()).update([p for p in pats if p])

    keyword_map_lower = {
        b: [p.lower() for p in sorted(vals) if p]
        for b, vals in keyword_map.items()
    }

    stats = {
        'cutoff': cutoff,
        'keyword_map': keyword_map,
        'scanned': 0,
        'skipped_old': 0,
        'skipped_unmatched': 0,
        'skipped_error': 0,
    }
    candidates = []

    mail = poplib.POP3_SSL(config['email']['pop3']['host'], config['email']['pop3']['port'])
    try:
        mail.user(config['email']['account'])
        mail.pass_(config['email']['authCode'])

        num = len(mail.list()[1])
        if num == 0:
            return candidates, stats

        for i in range(num, 0, -1):
            stats['scanned'] += 1
            try:
                _, headers, _ = mail.retr(i)
                msg = email.message_from_bytes(b'\r\n'.join(headers))
                subj = decode_mime(msg.get('Subject', ''))
                frm = decode_mime(msg.get('From', ''))

                dt = _parse_email_datetime(msg.get('Date', ''))
                if dt and dt < cutoff:
                    stats['skipped_old'] += 1
                    break

                subj_l = subj.lower()
                subject_hit_banks = [
                    b for b, patterns in keyword_map_lower.items()
                    if any(p in subj_l for p in patterns)
                ]
                if not subject_hit_banks:
                    stats['skipped_unmatched'] += 1
                    continue

                content = extract_email_content(msg)
                body_text = (content.get('plain', '') + '\n' + content.get('markdown', '')).strip()
                rule, _ = identify_rule(subj, frm, body_text, rules)
                if not rule or rule.get('bank_code') not in target_banks:
                    stats['skipped_unmatched'] += 1
                    continue
                if rule.get('bank_code') not in subject_hit_banks:
                    stats['skipped_unmatched'] += 1
                    continue

                candidates.append(str(i))
            except Exception:
                stats['skipped_error'] += 1
                continue
    finally:
        try:
            mail.quit()
        except Exception:
            pass

    return candidates, stats


def download_recent_bank_emails(months=3, output_dir=None):
    """专用指令：按规则关键字匹配，批量下载最近 N 个月银行账单。"""
    rules = load_rule_files()
    if not rules:
        print('❌ 未找到规则文件，请先在 rules 目录下放置 *.json')
        return

    target_banks = {'HX', 'CMB', 'SPDB', 'CMBC'}
    candidates, stats = _collect_recent_bill_uids(months, rules, target_banks)
    cutoff = stats['cutoff']
    keyword_map = stats['keyword_map']

    print(f'🚀 执行专用指令：批量下载最近 {months} 个月账单')
    print(f'📅 时间范围：{cutoff.date().isoformat()} ~ {datetime.now(timezone.utc).date().isoformat()}')
    for b in sorted(keyword_map.keys()):
        print(f'   {b} 关键字：{" | ".join(sorted(keyword_map[b]))}')

    matched = len(candidates)
    downloaded = 0
    skipped_old = stats['skipped_old']
    skipped_unmatched = stats['skipped_unmatched']
    skipped_error = stats['skipped_error']

    if not candidates:
        print(
            f'📭 没有可下载账单：跳过旧邮件 {skipped_old} 封，'
            f'未匹配 {skipped_unmatched} 封，异常 {skipped_error} 封'
        )
        return

    if output_dir is None:
        output_dir = DOWNLOAD_DIR
    os.makedirs(output_dir, exist_ok=True)

    try:
        config = load_config()
        mail = poplib.POP3_SSL(config['email']['pop3']['host'], config['email']['pop3']['port'])
        mail.user(config['email']['account'])
        mail.pass_(config['email']['authCode'])

        for uid in candidates:
            try:
                # 幂等：检查是否已有该 uid 的文件
                existing = [f for f in os.listdir(output_dir) if f.startswith(f'email_uid{uid}_')]
                if existing:
                    print(f'⏭️  UID={uid} 已下载，跳过')
                    skipped_old += 1
                    continue
                _, headers, _ = mail.retr(int(uid))
                msg = email.message_from_bytes(b'\r\n'.join(headers))
                _save_email_message(uid, msg, output_dir=output_dir, format='md')
                downloaded += 1
            except Exception:
                skipped_error += 1
                continue

        try:
            mail.quit()
        except Exception:
            pass
        print(
            f'\n✅ 执行完成：匹配 {matched} 封，下载 {downloaded} 封，'
            f'跳过旧邮件 {skipped_old} 封，未匹配 {skipped_unmatched} 封，异常跳过 {skipped_error} 封'
        )
    except Exception as e:
        print(f'❌ 批量下载失败：{e}')


def validate_recent_bank_emails(months=3):
    """专用指令：按规则关键字匹配，批量解析校验并写入 SQLite。"""
    init_db(DB_PATH)
    rules = load_rule_files()
    if not rules:
        print('❌ 未找到规则文件，请先在 rules 目录下放置 *.json')
        return

    target_banks = {'HX', 'CMB', 'SPDB', 'CMBC'}
    candidates, stats = _collect_recent_bill_uids(months, rules, target_banks)
    cutoff = stats['cutoff']

    print(f'🚀 执行专用指令：批量写库最近 {months} 个月账单')
    print(f'📅 时间范围：{cutoff.date().isoformat()} ~ {datetime.now(timezone.utc).date().isoformat()}')

    skipped_old = stats['skipped_old']
    skipped_unmatched = stats['skipped_unmatched']
    skipped_error = stats['skipped_error']

    if not candidates:
        print(
            f'📭 没有可写库账单：跳过旧邮件 {skipped_old} 封，'
            f'未匹配 {skipped_unmatched} 封，异常 {skipped_error} 封'
        )
        return

    print(f'🧩 待写库账单 {len(candidates)} 封，开始校验并写库...')
    ok = 0
    fail = 0
    skipped_db = 0
    for uid in candidates:
        # 幂等：检查是否已在 DB 中
        if uid_exists(DB_PATH, str(uid)):
            print(f'⏭️  UID={uid} 已在数据库，跳过')
            skipped_db += 1
            continue
        try:
            validate_email_by_uid(uid)
            ok += 1
        except Exception as e:
            print(f'❌ UID={uid} 写库失败：{e}')
            fail += 1

    print(
        f'\n✅ 执行完成：成功 {ok} 封，已在库跳过 {skipped_db} 封，失败 {fail} 封，'
        f'过期邮件 {skipped_old} 封，未匹配 {skipped_unmatched} 封，异常 {skipped_error} 封'
    )

def download_email_pop3(uid, output_dir=None, format='md'):
    """下载邮件，支持 HTML 表格解析"""
    config = load_config()
    if output_dir is None:
        output_dir = DOWNLOAD_DIR
    os.makedirs(output_dir, exist_ok=True)
    
    try:
        mail = poplib.POP3_SSL(config['email']['pop3']['host'], config['email']['pop3']['port'])
        mail.user(config['email']['account'])
        mail.pass_(config['email']['authCode'])
        
        _, headers, _ = mail.retr(int(uid))
        msg = email.message_from_bytes(b'\r\n'.join(headers))
        
        _save_email_message(uid, msg, output_dir=output_dir, format=format)
        
        mail.quit()
        
    except Exception as e:
        print(f'❌ 下载失败：{e}')
        import traceback
        traceback.print_exc()

def test_connection():
    config = load_config()
    acc = config['email']['account']
    print(f'📧 测试：{acc}\n')
    
    try:
        s = smtplib.SMTP_SSL(config['email']['smtp']['host'], config['email']['smtp']['port'])
        s.login(acc, config['email']['authCode'])
        s.quit()
        print('✅ SMTP OK')
    except Exception as e:
        print(f'❌ SMTP: {e}')
    
    try:
        p = poplib.POP3_SSL(config['email']['pop3']['host'], config['email']['pop3']['port'])
        p.user(acc)
        p.pass_(config['email']['authCode'])
        p.quit()
        print('✅ POP3 OK')
    except Exception as e:
        print(f'❌ POP3: {e}')
    
    try:
        i = imaplib.IMAP4_SSL(config['email']['imap']['host'], config['email']['imap']['port'])
        i.login(acc, config['email']['authCode'])
        i.logout()
        print('✅ IMAP 登录 OK')
    except Exception as e:
        print(f'❌ IMAP: {e}')

def main():
    if len(sys.argv) < 2:
        print('''163 邮箱工具
用法:
  python mail_client.py test                              - 测试连接
    python mail_client.py initdb                            - 初始化 SQLite 表
  python mail_client.py send <to> <subj> <text>           - 发送邮件
  python mail_client.py read [limit]                      - 读取最新邮件
  python mail_client.py search <kw> [limit]               - 搜索邮件
  python mail_client.py download <uid> [--html] [--md]    - 下载邮件 (支持表格)
    python mail_client.py download_bank_bills [months]      - 专用指令：下载多银行最近N个月账单
    python mail_client.py validate_bank_bills [months]      - 专用指令：批量写库（无需UID）
    python mail_client.py classify <uid>                    - 规则匹配（模板识别）
    python mail_client.py validate <uid>                    - 规则解析与校验报告
    python mail_client.py recent [months]                   - 最近账单视图（默认3个月）
    python mail_client.py report [months]                   - 银行/月份汇总（默认3个月）
    python mail_client.py txns_over <amount> [months]      - 交易明细查询：金额阈值过滤（默认全部历史）
    python mail_client.py reconcile [months] [tolerance]    - 对账差异检查

示例:
  python mail_client.py download 9982
  python mail_client.py download 9982 --html
  python mail_client.py download 9982 --md
    python mail_client.py download_bank_bills
    python mail_client.py validate_bank_bills 3
    python mail_client.py txns_over 500
    python mail_client.py txns_over 500 3
''')
        sys.exit(0)
    
    cmd = sys.argv[1]
    if cmd == 'test':
        test_connection()
    elif cmd == 'initdb':
        setup_storage()
    elif cmd == 'send':
        if len(sys.argv) < 5:
            print('用法：send <to> <subj> <text>')
            sys.exit(1)
        send_email(sys.argv[2], sys.argv[3], ' '.join(sys.argv[4:]))
    elif cmd == 'read':
        limit = int(sys.argv[2]) if len(sys.argv) > 2 and sys.argv[2].isdigit() else 10
        read_emails_pop3(limit)
    elif cmd == 'search':
        if len(sys.argv) < 3:
            print('用法：search <keyword>')
            sys.exit(1)
        search_emails_pop3(sys.argv[2], int(sys.argv[3]) if len(sys.argv) > 3 and sys.argv[3].isdigit() else 20)
    elif cmd == 'download':
        if len(sys.argv) < 3:
            print('用法：download <uid>')
            sys.exit(1)
        uid = sys.argv[2]
        format = 'both'
        if '--html' in sys.argv:
            format = 'html'
        elif '--md' in sys.argv:
            format = 'md'
        download_email_pop3(uid, format=format)
    elif cmd in ('download_bank_bills', 'exec3m'):
        months = int(sys.argv[2]) if len(sys.argv) > 2 and sys.argv[2].isdigit() else 3
        download_recent_bank_emails(months)
    elif cmd in ('validate_bank_bills', 'validate3m'):
        months = int(sys.argv[2]) if len(sys.argv) > 2 and sys.argv[2].isdigit() else 3
        validate_recent_bank_emails(months)
    elif cmd == 'classify':
        if len(sys.argv) < 3:
            print('用法：classify <uid>')
            sys.exit(1)
        classify_email_by_uid(sys.argv[2])
    elif cmd == 'validate':
        if len(sys.argv) < 3:
            print('用法：validate <uid>')
            sys.exit(1)
        validate_email_by_uid(sys.argv[2])
    elif cmd == 'recent':
        months = int(sys.argv[2]) if len(sys.argv) > 2 and sys.argv[2].isdigit() else 3
        show_recent_statements(months)
    elif cmd == 'report':
        months = int(sys.argv[2]) if len(sys.argv) > 2 and sys.argv[2].isdigit() else 3
        show_statement_report(months)
    elif cmd == 'reconcile':
        months = int(sys.argv[2]) if len(sys.argv) > 2 and sys.argv[2].isdigit() else 3
        tolerance = float(sys.argv[3]) if len(sys.argv) > 3 else 1.0
        show_reconcile(months, tolerance)
    elif cmd == 'txns_over':
        if len(sys.argv) < 3:
            print('用法：txns_over <amount> [months]')
            sys.exit(1)
        amount = float(sys.argv[2])
        months = int(sys.argv[3]) if len(sys.argv) > 3 and sys.argv[3].isdigit() else None
        show_transactions_over(amount, months)
    else:
        print(f'未知命令：{cmd}')
        sys.exit(1)

if __name__ == '__main__':
    main()
