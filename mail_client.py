#!/usr/bin/env python3
"""163 邮箱邮件技能 - 支持发送、读取、搜索、下载（含 HTML 表格解析）"""

import json, os, sys, smtplib, imaplib, poplib, email, re, glob, base64
from datetime import datetime, timezone, timedelta

# Windows 终端中文编码与 emoji 兼容性适配
if sys.platform.startswith('win'):
    try:
        sys.stdout.reconfigure(encoding='utf-8')
        sys.stderr.reconfigure(encoding='utf-8')
    except Exception:
        pass
from decimal import Decimal, InvalidOperation
from email.mime.text import MIMEText
from email.header import decode_header
from email.utils import parsedate_to_datetime
from html.parser import HTMLParser
from statement_models import StatementRecord, ValidationIssue, ValidationResult, StatementTransactionRecord, EmailSummaryRecord
from statement_db import init_db, upsert_statement, save_validation_run, replace_transactions, get_recent_statements, get_summary_by_bank_month, get_reconciliation_rows, uid_exists, get_transactions_above_amount, upsert_email_summary, get_email_summary_status, get_email_summary_by_id, get_recent_email_headers, get_potential_missed_emails


from mail_connect import (MailAuthError, retry_with_backoff, is_graph_api, connect_pop3, connect_smtp, connect_imap, _graph_api_request, fetch_summaries_graph, send_email_graph, _month_subtract)
from mail_parse import (HTMLTableParser, decode_mime, _to_text, html_to_text, parse_html_tables, tables_to_markdown, extract_email_content, _parse_email_datetime)
from mail_llm import (
    call_llm,
    slice_and_summarize_long_email,
    extract_email_summary_by_llm,
    load_static_noise_rules,
    is_noise_email,
)


from statement_parser import (
    _safe_decimal, _parse_date, _normalize_statement_month,
    _resolve_monthly_day_date, _infer_cmb_due_date,
    _apply_due_date_fallbacks, _apply_amount_fallbacks,
    _apply_statement_date_month_fallbacks, _infer_date_from_mmdd,
    _infer_date_from_any, parse_hx_transactions_from_markdown,
    parse_cmb_transactions_from_markdown, parse_spdb_transactions_from_markdown,
    parse_icbc_transactions_from_markdown, parse_citic_transactions_from_body,
    load_rule_files, _match_score, identify_rule, extract_statement_by_rule,
    validate_statement_by_rule, _validation_result_to_dict,
    validate_and_save_email_message,
)

CONFIG_CANDIDATES = [
    os.path.expanduser('email-config.local.json'),
    os.path.expanduser('email-config.json'),
    os.path.expanduser('email-config.example.json'),
]
DOWNLOAD_DIR = os.path.expanduser('email-downloads')
RULES_DIR = os.path.expanduser('rules')
VALIDATION_REPORT_DIR = os.path.expanduser('validation-reports')
DB_PATH = os.path.expanduser('statements.db')

BANK_NAME_MAP = {
    'HX': '华夏银行',
    'CMB': '招商银行',
    'SPDB': '浦发银行',
    'CMBC': '民生银行',
    'ICBC': '工商银行',
    'CITIC': '中信银行',
}

def get_display_width(s):
    if s is None:
        return 0
    s = str(s)
    width = 0
    for char in s:
        o = ord(char)
        if (0x1100 <= o <= 0x115F or
            0x2E80 <= o <= 0x303F or
            0x3040 <= o <= 0x309F or
            0x30A0 <= o <= 0x30FF or
            0x3100 <= o <= 0x312F or
            0x3130 <= o <= 0x318F or
            0x3190 <= o <= 0x319F or
            0x31A0 <= o <= 0x31BF or
            0x31C0 <= o <= 0x31EF or
            0x31F0 <= o <= 0x31FF or
            0x3200 <= o <= 0x32FF or
            0x3300 <= o <= 0x33FF or
            0x3400 <= o <= 0x4DBF or
            0x4E00 <= o <= 0x9FFF or
            0xF900 <= o <= 0xFAFF or
            0xFE30 <= o <= 0xFE4F or
            0xFF00 <= o <= 0xFFEF):
            width += 2
        else:
            width += 1
    return width

def pad_string(s, width, alignment='left'):
    s = str(s) if s is not None else ''
    cur_width = get_display_width(s)
    padding = width - cur_width
    if padding <= 0:
        return s
    if alignment == 'right':
        return ' ' * padding + s
    elif alignment == 'center':
        left = padding // 2
        right = padding - left
        return ' ' * left + s + ' ' * right
    else:
        return s + ' ' * padding

def print_table(headers, rows, alignments=None):
    if not headers:
        return
    
    str_rows = []
    for r in rows:
        str_rows.append([str(x) if x is not None else '-' for x in r])
        
    cols_count = len(headers)
    if alignments is None:
        alignments = ['left'] * cols_count
        
    col_widths = []
    for col_idx in range(cols_count):
        max_w = get_display_width(headers[col_idx])
        for row in str_rows:
            if col_idx < len(row):
                max_w = max(max_w, get_display_width(row[col_idx]))
        col_widths.append(max_w)
        
    top_border = '┌─' + '─┬─'.join('─' * w for w in col_widths) + '─┐'
    middle_border = '├─' + '─┼─'.join('─' * w for w in col_widths) + '─┤'
    bottom_border = '└─' + '─┴─'.join('─' * w for w in col_widths) + '─┘'
    
    print(top_border)
    header_line = '│ ' + ' │ '.join(pad_string(headers[i], col_widths[i], alignments[i]) for i in range(cols_count)) + ' │'
    print(header_line)
    print(middle_border)
    for row in str_rows:
        row_line = '│ ' + ' │ '.join(pad_string(row[i] if i < len(row) else '', col_widths[i], alignments[i]) for i in range(cols_count)) + ' │'
        print(row_line)
    print(bottom_border)

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


def load_accounts():
    """从配置中加载所有邮箱账号。支持单邮箱（email）与多邮箱（emails）配置。"""
    config = load_config()
    accounts = []
    if "emails" in config and isinstance(config["emails"], list):
        accounts = config["emails"]
    elif "email" in config and isinstance(config["email"], dict):
        accounts = [config["email"]]
    return accounts


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
    
    headers = ["UID", "银行", "账单月份", "账单日期", "到期还款日", "应还款总额", "最低应还额", "明细笔数", "明细总值"]
    alignments = ["left", "left", "center", "center", "center", "right", "right", "right", "right"]
    
    table_rows = []
    for r in rows:
        bank_display = f"{r['bank_code']}({BANK_NAME_MAP.get(r['bank_code'], '未知银行')})"
        table_rows.append([
            r['uid'],
            bank_display,
            r['statement_month'] or '-',
            r['statement_date'] or '-',
            r['due_date'] or '-',
            r['total_due'] or '-',
            r['minimum_due'] or '-',
            r['txn_count'],
            r['txn_sum']
        ])
    print_table(headers, table_rows, alignments)

def show_statement_report(months=3):
    init_db(DB_PATH)
    rows = get_summary_by_bank_month(DB_PATH, months)
    if not rows:
        print(f'📭 最近 {months} 个月暂无可汇总数据')
        return

    print(f'📊 最近 {months} 个月按银行/月份汇总\n')
    headers = ["银行", "账单月份", "账单总数", "应还总额", "最低还款总额", "交易明细总额", "对账差额", "境外交易币种", "境外交易明细"]
    alignments = ["left", "center", "right", "right", "right", "right", "right", "center", "left"]
    
    table_rows = []
    for r in rows:
        bank_code = r['bank_code']
        bank_display = f"{bank_code}({BANK_NAME_MAP.get(bank_code, '未知银行')})"
        table_rows.append([
            bank_display,
            r['ym'],
            r['statement_count'],
            r['sum_total_due'],
            r['sum_minimum_due'],
            r['sum_txn_amount'],
            r['sum_reconcile_diff'],
            r['foreign_codes'],
            r['foreign_amount_breakdown']
        ])
    print_table(headers, table_rows, alignments)


def show_reconcile(months=3, tolerance=1.0):
    init_db(DB_PATH)
    rows = get_reconciliation_rows(DB_PATH, months)
    if not rows:
        print(f'📭 最近 {months} 个月暂无可对账记录')
        return
    tol = float(tolerance)
    print(f'🧾 最近 {months} 个月对账检查 (tolerance={tol})\n')
    headers = ["UID", "银行", "账单月份", "应还款总额", "交易明细总额", "对账差额", "对账状态"]
    alignments = ["left", "left", "center", "right", "right", "right", "center"]
    
    table_rows = []
    for r in rows:
        bank_code = r['bank_code']
        bank_display = f"{bank_code}({BANK_NAME_MAP.get(bank_code, '未知银行')})"
        diff = float(r['reconcile_diff'])
        status = '✅ PASS' if abs(diff) <= tol else '❌ CHECK'
        table_rows.append([
            r['uid'],
            bank_display,
            r['statement_month'] or '-',
            r['total_due'] or '-',
            r['txn_sum'],
            r['reconcile_diff'],
            status
        ])
    print_table(headers, table_rows, alignments)


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
    
    headers = ["UID", "银行", "账单月份", "交易日期", "记账日期", "交易商户描述", "交易金额", "币种", "境外位置", "原币金额"]
    alignments = ["left", "left", "center", "center", "center", "left", "right", "center", "center", "right"]
    
    table_rows = []
    for r in rows:
        bank_code = r['bank_code']
        bank_display = f"{bank_code}({BANK_NAME_MAP.get(bank_code, '未知银行')})"
        table_rows.append([
            r['uid'],
            bank_display,
            r['statement_month'] or '-',
            r['txn_date'] or '-',
            r['post_date'] or '-',
            r['description'] or '-',
            r['amount'],
            r['currency'] or '-',
            r['txn_location_code'] or '-',
            r['original_amount'] or '-'
        ])
    print_table(headers, table_rows, alignments)

def pop3_fetch_message_by_uid(uid, account_name=None):
    """按 UID 拉取原始邮件，返回 message 对象。支持通过数据库反查或首账户兜底选定连接。"""
    accounts = load_accounts()
    if not accounts:
        raise ValueError("No email configurations found.")
        
    target_config = None
    if account_name:
        for acc in accounts:
            if acc.get('account') == account_name:
                target_config = acc
                break
    else:
        # 反查 email_summaries 中的 account_name
        import sqlite3
        conn = sqlite3.connect(DB_PATH)
        try:
            cur = conn.cursor()
            cur.execute("SELECT account_name FROM email_summaries WHERE uid = ? LIMIT 1", (str(uid),))
            row = cur.fetchone()
            if row:
                act_name = row[0]
                for acc in accounts:
                    if acc.get('account') == act_name:
                        target_config = acc
                        break
        except Exception:
            pass
        finally:
            conn.close()
            
    # 如果没查到，兜底用第一个配置账号
    if not target_config:
        target_config = accounts[0]
        
    if is_graph_api(target_config):
        from oauth_helper import get_valid_oauth_token
        import urllib.request
        import urllib.error
        import time
        token = get_valid_oauth_token(target_config)
        url = f"https://graph.microsoft.com/v1.0/me/messages/{uid}/$value"
        req = urllib.request.Request(url, headers={'Authorization': f'Bearer {token}'})
        for attempt in range(3):
            try:
                with urllib.request.urlopen(req, timeout=30) as response:
                    mime_bytes = response.read()
                    return email.message_from_bytes(mime_bytes)
            except urllib.error.HTTPError as e:
                if e.code == 429:
                    retry_after = int(e.headers.get('Retry-After', 5))
                    time.sleep(retry_after)
                    continue
                raise ValueError(f"Graph API fetch MIME error: {e.code}") from e
        raise ValueError("Graph API fetch MIME max retries exceeded.")
        
    mail = connect_pop3(target_config)
    try:
        _, headers, _ = mail.retr(int(uid))
        msg = email.message_from_bytes(b'\r\n'.join(headers))
        return msg
    finally:
        try:
            mail.quit()
        except Exception:
            pass

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

def validate_email_by_uid(uid, account_name=None):
    """按规则解析并输出校验报告。"""
    try:
        msg = pop3_fetch_message_by_uid(uid, account_name=account_name)
        validate_and_save_email_message(msg, uid, account_name=account_name)
    except Exception as e:
        print(f'验证失败：{e}')

def send_email(to, subject, text):
    accounts = load_accounts()
    if not accounts:
        raise ValueError("No email accounts configured")
    account_config = accounts[0]
    
    if is_graph_api(account_config):
        send_email_graph(account_config, to, subject, text)
        print(f"✅ 邮件已发送给 {to} (via Graph API)")
        return
        
    s = connect_smtp(account_config)
    try:
        from email.mime.text import MIMEText
        from email.mime.multipart import MIMEMultipart
        
        msg = MIMEMultipart()
        msg['From'] = account_config['account']
        msg['To'] = to
        msg['Subject'] = subject
        msg.attach(MIMEText(text, 'plain', 'utf-8'))
        
        s.send_message(msg)
        print(f"✅ 邮件已发送给 {to}")
    finally:
        s.quit()

def read_emails_pop3(limit=10, account_name=None):
    accounts = load_accounts()
    if not accounts:
        print("❌ 未配置任何邮箱账户。")
        return
        
    target_config = None
    if account_name:
        for acc in accounts:
            if acc.get('account') == account_name:
                target_config = acc
                break
        if not target_config:
            print(f"❌ 找不到配置账号: {account_name}")
            return
    else:
        target_config = accounts[0]
        
    try:
        if is_graph_api(target_config):
            from oauth_helper import get_valid_oauth_token
            token = get_valid_oauth_token(target_config)
            url = f"https://graph.microsoft.com/v1.0/me/messages?$top={limit}&$select=id,subject,from,receivedDateTime&$orderby=receivedDateTime desc"
            resp = _graph_api_request(url, token)
            if not resp or 'value' not in resp or not resp['value']:
                print(f'📭 邮箱 {target_config.get("account")} 为空或无匹配')
                return
            msgs = resp['value']
            print(f'📬 最新 {len(msgs)} 封邮件 ({target_config.get("account")} via Graph):\n')
            for msg in msgs:
                uid = msg['id']
                subj = msg.get('subject', '')
                frm = msg.get('from', {}).get('emailAddress', {}).get('address', '')
                date = msg.get('receivedDateTime', '')
                print(f"UID: {uid}\n[{date[:25]}] {frm}\n主题：{subj}\n{'-'*60}")
            return
            
        mail = connect_pop3(target_config)
        num = len(mail.list()[1])
        if num == 0:
            print(f'📭 邮箱 {target_config.get("account")} 为空')
            mail.quit()
            return
        start = max(1, num - limit + 1)
        print(f'📬 最新 {min(limit, num)} 封邮件 ({target_config.get("account")} via POP3):\n')
        for i in range(num, start-1, -1):
            _, headers, _ = mail.retr(i)
            msg = email.message_from_bytes(b'\r\n'.join(headers))
            subj = decode_mime(msg.get('Subject', ''))
            frm = decode_mime(msg.get('From', ''))
            date = msg.get('Date', '')
            print(f"UID: {i}\n[{date[:25]}] {frm}\n主题：{subj}\n{'-'*60}")
        mail.quit()
    except Exception as e:
        print(f'❌ 连接失败: {e}')


def search_emails_pop3(keyword, limit=20, account_name=None):
    accounts = load_accounts()
    if not accounts:
        print("❌ 未配置任何邮箱账户。")
        return
        
    target_config = None
    if account_name:
        for acc in accounts:
            if acc.get('account') == account_name:
                target_config = acc
                break
        if not target_config:
            print(f"❌ 找不到配置账号: {account_name}")
            return
    else:
        target_config = accounts[0]
        
    try:
        if is_graph_api(target_config):
            from oauth_helper import get_valid_oauth_token
            import urllib.parse
            token = get_valid_oauth_token(target_config)
            params = {
                "$search": f'"{keyword}"',
                "$select": "id,subject,from,receivedDateTime",
                "$top": limit * 3
            }
            url = f"https://graph.microsoft.com/v1.0/me/messages?{urllib.parse.urlencode(params)}"
            resp = _graph_api_request(url, token)
            
            if not resp or 'value' not in resp or not resp['value']:
                print(f'📭 邮箱 {target_config.get("account")} 中未搜到含有 "{keyword}" 的邮件。')
                return
                
            msgs = resp['value'][:limit*3]
            print(f'🔍 在 {target_config.get("account")} (via Graph) 中搜索 "{keyword}"...\n')
            results = []
            for msg in msgs:
                uid = msg['id']
                subj = msg.get('subject', '')
                frm = msg.get('from', {}).get('emailAddress', {}).get('address', '')
                date = msg.get('receivedDateTime', '')
                results.append((uid, subj, frm, date))
                if len(results) <= limit:
                    print(f"UID: {uid}\n[{date[:25]}] {frm}\n主题：{subj}\n{'-'*60}")
            print(f'共找到 {len(results)} 封匹配邮件。')
            return

        mail = connect_pop3(target_config)
        num = len(mail.list()[1])
        if num == 0:
            print(f'📭 邮箱 {target_config.get("account")} 为空')
            mail.quit()
            return
        results = []
        kw = keyword.lower()
        print(f'🔍 在 {target_config.get("account")} (via POP3) 中搜索 "{keyword}"...\n')
        skipped = 0
        for i in range(num, 0, -1):
            try:
                _, headers, _ = mail.retr(i)
                content_txt = b'\r\n'.join(headers).decode('utf-8', errors='ignore')
                msg = email.message_from_string(content_txt)
                subj = decode_mime(msg.get('Subject', ''))
                frm = decode_mime(msg.get('From', ''))
                date = msg.get('Date', '')
                if kw in subj.lower() or kw in frm.lower() or kw in content_txt.lower():
                    results.append((i, subj, frm, date))
                    if len(results) <= limit:
                        print(f"UID: {i}\n[{date[:25]}] {frm}\n主题：{subj}\n{'-'*60}")
                if len(results) >= limit * 3:
                    break
            except Exception:
                skipped += 1
                if skipped > 100: break
        print(f'共找到 {len(results)} 封匹配邮件。')
        mail.quit()
    except Exception as e:
        print(f'❌ 搜索失败: {e}')


def _save_email_message(uid, msg, output_dir=None, format='md', account_name=None):
    """保存已拉取的 message 到本地（支持 Markdown/HTML）。"""
    if output_dir is None:
        output_dir = DOWNLOAD_DIR
    os.makedirs(output_dir, exist_ok=True)

    subj = decode_mime(msg.get('Subject', '无主题'))
    frm = decode_mime(msg.get('From', ''))
    date = msg.get('Date', datetime.now().strftime('%a, %d %b %Y %H:%M:%S'))

    # 提取邮件内容（包括 HTML 表格）
    content = extract_email_content(msg)

    # 生成文件名，带 UID 避免批量下载时冲突，附加账户前缀隔离
    safe_subj = re.sub(r'[^\w\s-]', '', subj[:50]).strip().replace(' ', '_') or 'email'
    acc_prefix = f"acc_{account_name}_" if account_name else ""
    fname = f'email_{acc_prefix}uid{uid}_{safe_subj}'

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


def _collect_recent_bill_uids(months, rules, target_banks, email_config=None):
    """扫描最近N个月账单邮件，返回候选UID与统计信息。"""
    if not email_config:
        accounts = load_accounts()
        email_config = accounts[0] if accounts else {}
        
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

    if is_graph_api(email_config):
        try:
            emails = fetch_summaries_graph(email_config, int(months))
            for msg in emails:
                stats['scanned'] += 1
                dt = _parse_email_datetime(msg.get('email_date', ''))
                if dt and dt < cutoff:
                    stats['skipped_old'] += 1
                    break
                    
                subj = msg.get('subject', '')
                frm = msg.get('sender', '')
                uid = msg.get('uid')
                
                subj_lower = subj.lower()
                matched = False
                for b, kw_list in keyword_map_lower.items():
                    for kw in kw_list:
                        if kw in subj_lower:
                            candidates.append((uid, b, subj, frm, str(dt) if dt else ''))
                            matched = True
                            break
                    if matched: break
                
                if not matched:
                    stats['skipped_unmatched'] += 1
        except Exception as e:
            stats['skipped_error'] += 1
            print(f"⚠️ Graph API collecting recent bills error: {e}")
        return candidates, stats

    mail = connect_pop3(email_config)
    try:
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

                subj_lower = subj.lower()
                matched = False
                for b, kw_list in keyword_map_lower.items():
                    for kw in kw_list:
                        if kw in subj_lower:
                            candidates.append((i, b, subj, frm, str(dt) if dt else ''))
                            matched = True
                            break
                    if matched: break

                if not matched:
                    stats['skipped_unmatched'] += 1

            except Exception:
                stats['skipped_error'] += 1
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

    accounts = load_accounts()
    if not accounts:
        print("📭 未配置任何邮箱账户。")
        return

    if output_dir is None:
        output_dir = DOWNLOAD_DIR
    os.makedirs(output_dir, exist_ok=True)

    target_banks = {'HX', 'CMB', 'SPDB', 'CMBC', 'ICBC', 'CITIC'}
    
    for account_config in accounts:
        account_name = account_config.get('account', 'default')
        print(f'\n🚀 开始同步账户 {account_name} 批量下载最近 {months} 个月账单')
        
        candidates, stats = _collect_recent_bill_uids(months, rules, target_banks, account_config)
        cutoff = stats['cutoff']
        keyword_map = stats['keyword_map']

        matched = len(candidates)
        downloaded = 0
        skipped_old = stats['skipped_old']
        skipped_unmatched = stats['skipped_unmatched']
        skipped_error = stats['skipped_error']

        if not candidates:
            print(
                f'📭 账户 {account_name} 没有新匹配可下载账单：已处理/跳过旧邮件 {skipped_old} 封，'
                f'未匹配 {skipped_unmatched} 封，异常 {skipped_error} 封'
            )
            continue

        try:
            mail = connect_pop3(account_config)

            for uid in candidates:
                try:
                    # 幂等：检查是否已有该 uid 的文件（附加账户前缀）
                    existing = [f for f in os.listdir(output_dir) if f.startswith(f'email_acc_{account_name}_uid{uid}_') or f.startswith(f'email_uid{uid}_')]
                    if existing:
                        print(f'⏭️  账户 {account_name} UID={uid} 已下载，跳过')
                        skipped_old += 1
                        continue
                    _, headers, _ = mail.retr(int(uid))
                    msg = email.message_from_bytes(b'\r\n'.join(headers))
                    _save_email_message(uid, msg, output_dir=output_dir, format='md', account_name=account_name)
                    downloaded += 1
                except Exception as e:
                    print(f"⚠️ UID={uid} 下载异常: {e}")
                    skipped_error += 1
                    continue

            try:
                mail.quit()
            except Exception:
                pass
            print(
                f'✅ 账户 {account_name} 执行完成：匹配 {matched} 封，下载 {downloaded} 封，'
                f'已处理/旧邮件 {skipped_old} 封，未匹配 {skipped_unmatched} 封，异常跳过 {skipped_error} 封'
            )
        except Exception as e:
            print(f'❌ 账户 {account_name} 批量下载失败：{e}')


def validate_recent_bank_emails(months=3):
    """专用指令：按规则关键字匹配，批量解析校验并写入 SQLite。"""
    init_db(DB_PATH)
    rules = load_rule_files()
    if not rules:
        print('❌ 未找到规则文件，请先在 rules 目录下放置 *.json')
        return

    accounts = load_accounts()
    if not accounts:
        print("📭 未配置任何邮箱账户。")
        return

    target_banks = {'HX', 'CMB', 'SPDB', 'CMBC', 'ICBC', 'CITIC'}
    
    for account_config in accounts:
        account_name = account_config.get('account', 'default')
        print(f'\n🚀 开始同步账户 {account_name} 批量写库最近 {months} 个月账单')
        
        candidates, stats = _collect_recent_bill_uids(months, rules, target_banks, account_config)
        cutoff = stats['cutoff']

        skipped_old = stats['skipped_old']
        skipped_unmatched = stats['skipped_unmatched']
        skipped_error = stats['skipped_error']

        if not candidates:
            print(
                f'📭 账户 {account_name} 没有新账单待写库：跳过旧邮件 {skipped_old} 封，'
                f'未匹配 {skipped_unmatched} 封，异常 {skipped_error} 封'
            )
            continue

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
                validate_email_by_uid(uid, account_name=account_name)
                ok += 1
            except Exception as e:
                print(f'❌ UID={uid} 写库失败：{e}')
                fail += 1

        print(
            f'✅ 账户 {account_name} 写库完成：成功 {ok} 封，已在库跳过 {skipped_db} 封，失败 {fail} 封，'
            f'过期邮件 {skipped_old} 封，未匹配 {skipped_unmatched} 封，异常 {skipped_error} 封'
        )


def due_soon_bank_bills(months=3, days=7, output_dir=None):
    from statement_db import get_unpaid_statements
    from datetime import datetime
    today = datetime.now().date()
    rows = get_unpaid_statements(DB_PATH, months=int(months))
    
    print(f"🚀 执行专用指令：查询最近未还款账单（临近 {days} 天）")
    
    warn_missing_due = []
    due_soon_items = []
    
    for r in rows:
        due_date_str = r['due_date']
        if not due_date_str:
            warn_missing_due.append({'bank_code': r['bank_code'], 'subject': r['subject'], 'reason': '无还款日'})
            continue
            
        try:
            due_dt = datetime.strptime(due_date_str, '%Y-%m-%d').date()
            days_left = (due_dt - today).days
            if 0 <= days_left <= int(days):
                due_soon_items.append({
                    'bank_code': r['bank_code'],
                    'due_date': due_date_str,
                    'days_left': days_left,
                    'total_due': r['total_due'],
                    'subject': r['subject']
                })
        except ValueError:
            warn_missing_due.append({'bank_code': r['bank_code'], 'subject': r['subject'], 'reason': f"日期格式错误: {due_date_str}"})
            
    if due_soon_items:
        due_soon_items.sort(key=lambda x: x['days_left'])
        print("\n=== ⚠️ 临期账单 (<= {}天) ===".format(days))
        for item in due_soon_items:
            print(f"[{item['bank_code']}] {item['subject']}")
            print(f"   应还: {item['total_due']} RMB | 还款日: {item['due_date']} (剩余 {item['days_left']} 天)")
    else:
        print("\n✅ 没有临近 {} 天内的待还账单。".format(days))
        
    if warn_missing_due:
        print("\n=== ⚠️ 异常项 (无法判定临期) ===")
        for w in warn_missing_due:
            print(f"[{w['bank_code']}] {w['subject']} - {w['reason']}")

def show_unpaid_statements():
    from statement_db import get_unpaid_statements
    rows = get_unpaid_statements(DB_PATH)
    if not rows:
        print("✅ 目前没有未还款的账单。")
        return
        
    print("=== 待还款账单列表 ===")
    for r in rows:
        print(f"[{r['bank_code']}] {r['statement_month']} - {r['subject']}")
        print(f"   应还: {r['total_due']} RMB | 还款日: {r['due_date'] or '未知'}")

def mark_statement_paid_cmd(bank_code, statement_month=None):
    from statement_db import mark_statement_paid
    count = mark_statement_paid(DB_PATH, bank_code, statement_month)
    if count > 0:
        if statement_month:
            if count > 1:
                print(f"✅ 已标记 {bank_code} 的 {statement_month} 账单为已还清（共 {count} 条，同月重复账单一并核销）")
            else:
                print(f"✅ 已标记 {bank_code} 的 {statement_month} 账单为已还清（共 1 条）")
        else:
            print(f"✅ 已标记 {bank_code} 最新一条未还账单为已还清（共 {count} 条）")
    else:
        print(f"❌ 未找到匹配的 {bank_code} 未还账单记录。")


def download_email_pop3(uid, output_dir=None, format='md', account_name=None):
    """下载邮件，支持 HTML 表格解析"""
    if output_dir is None:
        output_dir = DOWNLOAD_DIR
    os.makedirs(output_dir, exist_ok=True)
    
    try:
        # 统一使用 pop3_fetch_message_by_uid 抓取（包含反查和探测逻辑）
        msg = pop3_fetch_message_by_uid(uid, account_name=account_name)
        
        # 确定实际的账户名以设置文件名隔离前缀
        actual_account = account_name
        if not actual_account:
            import sqlite3
            conn = sqlite3.connect(DB_PATH)
            try:
                cur = conn.cursor()
                cur.execute("SELECT account_name FROM email_summaries WHERE uid = ? LIMIT 1", (str(uid),))
                row = cur.fetchone()
                if row:
                    actual_account = row[0]
            except Exception:
                pass
            finally:
                conn.close()
                
        _save_email_message(uid, msg, output_dir=output_dir, format=format, account_name=actual_account)
        print(f"✅ 下载成功: UID={uid}")
        
    except Exception as e:
        print(f'❌ 下载失败：{e}')
        import traceback
        traceback.print_exc()

def test_connection():
    accounts = load_accounts()
    if not accounts:
        print("❌ 未配置任何邮箱账户。")
        return
        
    for account_config in accounts:
        acc = account_config['account']
        print(f'📧 测试账户：{acc}')
        print(f'----------------------------------------')
        
        if is_graph_api(account_config):
            try:
                from oauth_helper import get_valid_oauth_token
                token = get_valid_oauth_token(account_config)
                _graph_api_request("https://graph.microsoft.com/v1.0/me/messages?$top=1", token)
                print('✅ Graph API 连接及收信 OK')
            except Exception as e:
                print(f'❌ Graph API 收信错误: {e}')
                
            try:
                _graph_api_request("https://graph.microsoft.com/v1.0/me", token)
                print('✅ Graph API Profile OK')
            except Exception as e:
                print(f'❌ Graph API Profile 错误: {e}')
        else:
            # 测试 SMTP
            try:
                s = connect_smtp(account_config)
                s.quit()
                print('✅ SMTP OK')
            except Exception as e:
                print(f'❌ SMTP: {e}')
            
            # 测试 POP3
            try:
                p = connect_pop3(account_config)
                p.quit()
                print('✅ POP3 OK')
            except Exception as e:
                print(f'❌ POP3: {e}')
            
            # 测试 IMAP
            try:
                i = connect_imap(account_config)
                i.logout()
                print('✅ IMAP 登录 OK')
            except Exception as e:
                print(f'❌ IMAP: {e}')
        print()

def interactive_menu():
    init_db(DB_PATH)
    while True:
        print("\n" + "=" * 50)
        print("         💳  信用卡账单助手 交互控制台  💳        ")
        print("=" * 50)
        print("  [1] 🔌  测试邮箱连通性 (POP3/SMTP/IMAP)")
        print("  [2] 📥  批量下载最近账单邮件 (邮件 -> 本地Markdown)")
        print("  [3] 📝  批量解析并导入数据库 (本地 -> SQLite)")
        print("  [4] 🧾  查看对账差异报表 (应还款 vs 交易明细)")
        print("  [5] 📊  生成银行/月度财务汇总报表 (含境外交易)")
        print("  [6] ⏰  检查还款日临期账单 (还款提醒)")
        print("  [7] 🔍  按金额筛选查询交易明细")
        print("  [8] 📄  查看最近账单记录汇总")
        print("  [0] ❌  退出程序")
        print("=" * 50)
        
        choice = input("请输入选项 [0-8]: ").strip()
        if choice == '0':
            print("\n👋 感谢使用，再见！")
            break
        elif choice == '1':
            print("\n🔌 开始测试邮箱连接...")
            test_connection()
        elif choice == '2':
            months_str = input("请输入回溯月数 [默认 3]: ").strip()
            months = int(months_str) if months_str.isdigit() else 3
            print(f"\n📥 开始下载最近 {months} 个月的账单邮件...")
            download_recent_bank_emails(months)
        elif choice == '3':
            months_str = input("请输入回溯月数 [默认 3]: ").strip()
            months = int(months_str) if months_str.isdigit() else 3
            print(f"\n📝 开始批量解析并导入 SQLite...")
            validate_recent_bank_emails(months)
        elif choice == '4':
            months_str = input("请输入回溯月数 [默认 3]: ").strip()
            months = int(months_str) if months_str.isdigit() else 3
            tol_str = input("请输入允许对账偏差金额 [默认 1.0]: ").strip()
            try:
                tolerance = float(tol_str) if tol_str else 1.0
            except ValueError:
                tolerance = 1.0
            print(f"\n🧾 最近 {months} 个月对账差异报表 (偏差偏差阈值: {tolerance}):")
            show_reconcile(months, tolerance)
        elif choice == '5':
            months_str = input("请输入回溯月数 [默认 3]: ").strip()
            months = int(months_str) if months_str.isdigit() else 3
            print(f"\n📊 最近 {months} 个月财务汇总报表:")
            show_statement_report(months)
        elif choice == '6':
            months_str = input("请输入回溯月数 [默认 3]: ").strip()
            months = int(months_str) if months_str.isdigit() else 3
            days_str = input("请输入临期天数阈值 [默认 7]: ").strip()
            days = int(days_str) if days_str.isdigit() else 7
            print(f"\n⏰ 临期还款日检查 (时间跨度: {months}个月, 临期天数: {days}天):")
            due_soon_bank_bills(months, days)
        elif choice == '7':
            amount_str = input("请输入大额交易金额阈值 (例如 500): ").strip()
            try:
                amount = float(amount_str)
            except ValueError:
                print("⚠️ 输入金额格式错误，请输入有效的数字！")
                continue
            months_str = input("请输入回溯月数 [直接回车查询全部历史]: ").strip()
            months = int(months_str) if months_str.isdigit() else None
            print(f"\n🔍 查询金额 >= {amount} 的交易明细:")
            show_transactions_over(amount, months)
        elif choice == '8':
            months_str = input("请输入回溯月数 [默认 3]: ").strip()
            months = int(months_str) if months_str.isdigit() else 3
            print(f"\n📄 最近 {months} 个月账单记录汇总:")
            show_recent_statements(months)
        else:
            print("⚠️ 未知选项，请重新输入！")
            
        input("\n按回车键继续...")

def fetch_recent_emails_and_summarize(months=1):
    """双通道收网网关命令：批量抓取解析正则账单，并对其他邮件进行 LLM 结构化摘要提炼。"""
    import json
    from datetime import datetime, timezone
    
    init_db(DB_PATH)
    rules = load_rule_files()
    
    cutoff = _month_subtract(datetime.now(timezone.utc), int(months))
    print(f'🚀 正在执行双通道同步指令：获取最近 {months} 个月邮件...')
    print(f'📅 统计截止范围：{cutoff.date().isoformat()} 起')
    
    accounts = load_accounts()
    if not accounts:
        print("📭 未配置任何邮箱账户。")
        return
        
    scanned_total = 0
    new_bills_total = 0
    new_summaries_total = 0
    skipped_dup_total = 0
    errors_total = 0
    
    high_importance_summaries = []
    
    for account_config in accounts:
        account_name = account_config.get('account', 'default')
        print(f'\n🔌 正在连接邮箱 {account_name} ...')
        
        is_graph = is_graph_api(account_config)
        graph_emails = []
        mail = None
        
        if is_graph:
            print(f'🔗 [Graph API] 获取 {account_name} 近 {months} 个月的邮件摘要...')
            try:
                graph_emails = fetch_summaries_graph(account_config, months)
                num = len(graph_emails)
                print(f'✅ [Graph API] 获取到 {num} 封邮件。开始匹配...')
            except Exception as e:
                print(f'❌ 无法连接 Graph API {account_name}: {e}')
                errors_total += 1
                continue
        else:
            try:
                mail = connect_pop3(account_config)
                num = len(mail.list()[1])
                if num == 0:
                    print(f'📭 邮箱 {account_name} 中没有任何邮件。')
                    continue
                print(f'🧩 邮箱 {account_name} 邮件总数: {num} 封，开始最新邮件反向匹配...')
            except Exception as e:
                print(f'❌ 无法连接邮箱 {account_name}: {e}')
                errors_total += 1
                continue
            
        scanned = 0
        new_bills = 0
        new_summaries = 0
        skipped_dup = 0
        errors = 0
        
        try:
            consecutive_old = 0
            consecutive_skip = 0
            
            for index in range(num):
                scanned += 1
                if is_graph:
                    msg_obj = graph_emails[index]
                    uid = msg_obj['uid']
                else:
                    i = num - index
                    uid = str(i)
                
                is_dup_bill = uid_exists(DB_PATH, uid)
                status, retry_cnt = get_email_summary_status(DB_PATH, account_name, uid)
                is_dup_summary = (status in ('processed', 'skipped', 'noise') or (status == 'failed' and retry_cnt >= 3))
                
                if is_dup_bill or is_dup_summary:
                    skipped_dup += 1
                    consecutive_skip += 1
                    if consecutive_skip >= 10:
                        print(f'📅 连续遇到 {consecutive_skip} 封已处理邮件，提早结束扫描。')
                        break
                    continue
                else:
                    consecutive_skip = 0
                    
                try:
                    if is_graph:
                        subj = msg_obj['subject']
                        frm = msg_obj['sender']
                        date_str = msg_obj['email_date']
                        dt = _parse_email_datetime(date_str)
                        if dt and dt < cutoff:
                            consecutive_old += 1
                            if consecutive_old >= 10:
                                print(f'📅 连续遇到 {consecutive_old} 封旧邮件，提早结束扫描。')
                                break
                            continue
                        else:
                            consecutive_old = 0
                        body_text = msg_obj['body']
                        
                        # Graph 邮件需自己组装简单的 EmailMessage 供 validate_and_save_email_message 解析使用，如果命中规则
                        # 但实际上为了规则通道，它可能会使用 mail_client.extract_email_content，
                        # 这里为了兼容，若命中规则直接构建 dummy msg 或触发下载。
                        # 对于提取摘要通道，只用 body_text 即可。
                        # 所以我们构造一个简单的 message
                        from email.message import EmailMessage
                        msg = EmailMessage()
                        msg['Subject'] = subj
                        msg['From'] = frm
                        msg['Date'] = date_str
                        msg.set_content(body_text)
                        # 保留 HTML 原文，使账单交易明细解析器能从 HTML 表格提取 markdown
                        if msg_obj.get('html'):
                            msg.add_alternative(msg_obj['html'], subtype='html')
                    else:
                        _, headers, _ = mail.retr(i)
                        msg = email.message_from_bytes(b'\r\n'.join(headers))
                        subj = decode_mime(msg.get('Subject', ''))
                        frm = decode_mime(msg.get('From', ''))
                        date_str = _to_text(msg.get('Date', ''))
                        
                        dt = _parse_email_datetime(date_str)
                        if dt and dt < cutoff:
                            consecutive_old += 1
                            if consecutive_old >= 10:
                                print(f'📅 连续遇到 {consecutive_old} 封旧邮件，提早结束扫描。')
                                break
                            continue
                        else:
                            consecutive_old = 0
                        
                        content = extract_email_content(msg)
                        body_text = (content.get('plain', '') + '\n' + content.get('markdown', '')).strip()
                    
                    # 双通道分流 A: 账单正则识别
                    rule, score = identify_rule(subj, frm, body_text, rules)
                    if rule:
                        print(f'💳 [账单通道] 命中规则 {rule.get("rule_id")} (UID={uid}) 主题: {subj[:30]}')
                        validate_and_save_email_message(msg, uid, rules=rules)
                        new_bills += 1
                        continue
                    
                    # 双通道分流 B: 启发式降噪
                    is_noise, cat, imp = is_noise_email(frm, subj, body_text, DB_PATH)
                    if is_noise:
                        print(f'🔕 [降噪拦截] 自动匹配规则 (UID={uid}) 主题: {subj[:30]}')
                        rec = EmailSummaryRecord(
                            account_name=account_name,
                            uid=uid,
                            sender=frm,
                            subject=subj,
                            email_date=date_str,
                            category=cat,
                            importance=imp,
                            summary="[自动降噪拦截] 发件人或主题命中过滤规则",
                            actions_json="[]",
                            status="noise",
                            retry_count=0,
                            processed_at=datetime.now(timezone.utc).isoformat()
                        )
                        rec_id = upsert_email_summary(DB_PATH, rec)
                        rec.id = rec_id
                        new_summaries += 1
                        continue
                    
                    # 双通道分流 C: LLM 通用提炼
                    print(f'🤖 [大模型通道] 开始处理 (UID={uid}) 主题: {subj[:30]}')
                    try:
                        res = extract_email_summary_by_llm(subj, frm, body_text, date_str)
                        rec = EmailSummaryRecord(
                            account_name=account_name,
                            uid=uid,
                            sender=frm,
                            subject=subj,
                            email_date=date_str,
                            category=res.get("category", "Work"),
                            importance=res.get("importance", "low"),
                            summary=res.get("summary", ""),
                            actions_json=json.dumps(res.get("actions", []), ensure_ascii=False),
                            deadline=res.get("deadline"),
                            deadline_raw=res.get("deadline_raw"),
                            status="processed",
                            retry_count=retry_cnt,
                            processed_at=datetime.now(timezone.utc).isoformat()
                        )
                        rec_id = upsert_email_summary(DB_PATH, rec)
                        rec.id = rec_id
                        new_summaries += 1
                        
                        if rec.importance == 'high':
                            high_importance_summaries.append(rec)
                    except Exception as llm_err:
                        print(f'❌ [大模型通道] 提取失败 (UID={uid}): {llm_err}')
                        new_retry = retry_cnt + 1
                        rec = EmailSummaryRecord(
                            account_name=account_name,
                            uid=uid,
                            sender=frm,
                            subject=subj,
                            email_date=date_str,
                            summary=f"[大模型分析失败]: {llm_err}",
                            status="failed",
                            retry_count=new_retry
                        )
                        rec_id = upsert_email_summary(DB_PATH, rec)
                        rec.id = rec_id
                        errors += 1
                        
                except Exception as mail_err:
                    print(f'❌ [邮件错误] 解析出错 (UID={uid}): {mail_err}')
                    errors += 1
                    continue
        finally:
            if not is_graph and mail:
                try:
                    mail.quit()
                except Exception:
                    pass
            print(f'🏁 账户 {account_name} 扫描完成：新入库账单 {new_bills} 封，通用提炼 {new_summaries} 封，跳过重复 {skipped_dup} 封，处理失败 {errors} 封')
            scanned_total += scanned
            new_bills_total += new_bills
            new_summaries_total += new_summaries
            skipped_dup_total += skipped_dup
            errors_total += errors

    print(f'\n🏁 全局同步完成：总扫描 {scanned_total} 封，新入库账单 {new_bills_total} 封，通用提炼 {new_summaries_total} 封，跳过重复 {skipped_dup_total} 封，全局失败 {errors_total} 封')
    
    if high_importance_summaries:
        print("\n=== 🔥 发现高优待推送邮件 ===")
        high_summaries_list = []
        for r in high_importance_summaries:
            summary_dict = {
                "id": r.id,
                "account_name": r.account_name,
                "uid": r.uid,
                "sender": r.sender,
                "subject": r.subject,
                "email_date": r.email_date,
                "category": r.category,
                "importance": r.importance,
                "summary": r.summary,
                "actions": json.loads(r.actions_json),
                "deadline": r.deadline,
                "deadline_raw": r.deadline_raw
            }
            high_summaries_list.append(summary_dict)
            print(f"[{r.account_name}] [{r.category}] 重要度: {r.importance} | {r.subject}")
            print(f"   摘要: {r.summary}")
            if r.deadline:
                print(f"   截止时间: {r.deadline} (原文: {r.deadline_raw})")
        
        # 打印输出 JSON_PUSH 区域，便于 lite_agent 捕获推送
        print("\n--- JSON_PUSH_START ---")
        print(json.dumps(high_summaries_list, ensure_ascii=False))
        print("--- JSON_PUSH_END ---")


def show_headers(limit=15):
    """CLI subcommand: 打印最近拉取的邮件标题列表，输出格式为 JSON"""
    init_db(DB_PATH)
    rows = get_recent_email_headers(DB_PATH, limit)
    result = []
    for r in rows:
        result.append({
            "id": r["id"],
            "account_name": r["account_name"],
            "uid": r["uid"],
            "sender": r["sender"],
            "subject": r["subject"],
            "email_date": r["email_date"],
            "category": r["category"],
            "importance": r["importance"],
            "status": r["status"],
            "processed_at": r["processed_at"]
        })
    print(json.dumps(result, ensure_ascii=False))


def show_missed(limit=15):
    """CLI subcommand: 打印可能错过的非高优邮件列表，输出格式为 JSON"""
    init_db(DB_PATH)
    rows = get_potential_missed_emails(DB_PATH, limit)
    result = []
    for r in rows:
        result.append({
            "id": r["id"],
            "account_name": r["account_name"],
            "uid": r["uid"],
            "sender": r["sender"],
            "subject": r["subject"],
            "email_date": r["email_date"],
            "category": r["category"],
            "importance": r["importance"],
            "summary": r["summary"],
            "status": r["status"]
        })
    print(json.dumps(result, ensure_ascii=False))


def reprocess_failed_emails(db_path=None):
    """补跑 status=failed 且 retry_count<3 的邮件。固化自 reprocess_failed.py。

    支持 POP3 与 Graph API 双通道，每封间隔 2s 防限流，
    异常路径也落库递增 retry_count 以避免无限重试。
    """
    import urllib.error
    from collections import defaultdict
    from statement_db import init_db as _init_db, upsert_email_summary as _upsert
    from statement_models import EmailSummaryRecord

    if db_path is None:
        db_path = DB_PATH
    _init_db(db_path)

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()
    cur.execute(
        "SELECT account_name, uid, sender, subject, email_date, retry_count "
        "FROM email_summaries WHERE status='failed' AND retry_count < 3"
    )
    rows = cur.fetchall()
    conn.close()
    print(f"📋 待补跑 failed 邮件: {len(rows)} 封")

    by_acct = defaultdict(list)
    for r in rows:
        by_acct[r["account_name"]].append(r)

    accounts = load_accounts()
    acct_cfg = {a.get("account", "default"): a for a in accounts}

    total_ok = total_noise = total_fail = total_skip = 0

    for acct_name, items in by_acct.items():
        cfg = acct_cfg.get(acct_name)
        if not cfg:
            print(f"\n⚠️ 账户 {acct_name} 未配置,跳过 {len(items)} 封")
            total_skip += len(items)
            continue
        print(f"\n🔌 === 账户 {acct_name}: {len(items)} 封 ===")
        is_graph = is_graph_api(cfg)

        msg_by_uid = {}
        mail = None
        if is_graph:
            try:
                gemails = fetch_summaries_graph(cfg, months=2)
                for ge in gemails:
                    msg_by_uid[ge.get("uid")] = ge
                print(f"  Graph 拉取 {len(gemails)} 封(用于 uid 匹配)")
            except Exception as e:
                print(f"  ❌ Graph 拉取失败: {e},跳过该账户")
                total_fail += len(items)
                continue
        else:
            try:
                mail = connect_pop3(cfg)
            except Exception as e:
                print(f"  ❌ POP3 连接失败: {e},跳过该账户")
                total_fail += len(items)
                continue

        try:
            for idx, r in enumerate(items, 1):
                uid = r["uid"]
                subj = r["subject"] or ""
                frm = r["sender"] or ""
                date_str = r["email_date"] or ""
                uid_short = (uid[:20] + "...") if len(uid) > 23 else uid
                try:
                    if is_graph:
                        ge = msg_by_uid.get(uid)
                        if not ge:
                            print(f"  [{idx}/{len(items)}] UID={uid_short} Graph未匹配,跳过")
                            total_skip += 1
                            time.sleep(1)
                            continue
                        body_text = (ge.get("body", "") + "\n" + ge.get("html", "")).strip()
                    else:
                        raw_lines = None
                        for _att in range(3):
                            try:
                                _, raw_lines, _ = mail.retr(int(uid))
                                break
                            except Exception as _re:
                                if _att < 2:
                                    print(f"      (连接异常 {_re},重连重试 {_att+1}/3)")
                                    try:
                                        mail.quit()
                                    except Exception:
                                        pass
                                    time.sleep(1.5)
                                    try:
                                        mail = connect_pop3(cfg)
                                    except Exception as _ce:
                                        raise Exception(f"POP3重连失败: {_ce}")
                                else:
                                    raise
                        msg = email.message_from_bytes(b"\r\n".join(raw_lines))
                        content = extract_email_content(msg)
                        body_text = (content.get("plain", "") + "\n" + content.get("markdown", "")).strip()

                    if not body_text:
                        body_text = subj

                    is_noise, cat, imp = is_noise_email(frm, subj, body_text, db_path)
                    if is_noise:
                        rec = EmailSummaryRecord(
                            account_name=acct_name, uid=uid, sender=frm, subject=subj,
                            email_date=date_str, category=cat, importance=imp,
                            summary="[自动降噪拦截] 发件人或主题命中过滤规则", actions_json="[]",
                            status="noise", retry_count=r["retry_count"],
                            processed_at=datetime.now(timezone.utc).isoformat(),
                        )
                        _upsert(db_path, rec)
                        total_noise += 1
                        print(f"  [{idx}/{len(items)}] UID={uid_short} 🔕 降噪")
                    else:
                        res, err = _retry_llm_summary(subj, frm, body_text, date_str)
                        if res is not None:
                            rec = EmailSummaryRecord(
                                account_name=acct_name, uid=uid, sender=frm, subject=subj,
                                email_date=date_str,
                                category=res.get("category", "Work"),
                                importance=res.get("importance", "low"),
                                summary=res.get("summary", ""),
                                actions_json=json.dumps(res.get("actions", []), ensure_ascii=False),
                                deadline=res.get("deadline"),
                                deadline_raw=res.get("deadline_raw"),
                                status="processed", retry_count=r["retry_count"],
                                processed_at=datetime.now(timezone.utc).isoformat(),
                            )
                            _upsert(db_path, rec)
                            total_ok += 1
                            sm = (res.get("summary", "") or "")[:40]
                            print(f"  [{idx}/{len(items)}] UID={uid_short} ✅ {res.get('category')}/{res.get('importance')}: {sm}")
                        else:
                            new_retry = r["retry_count"] + 1
                            rec = EmailSummaryRecord(
                                account_name=acct_name, uid=uid, sender=frm, subject=subj,
                                email_date=date_str, summary=f"[大模型分析失败]: {err}",
                                status="failed", retry_count=new_retry,
                            )
                            _upsert(db_path, rec)
                            total_fail += 1
                            print(f"  [{idx}/{len(items)}] UID={uid_short} ❌ {err}")
                    time.sleep(2)
                except Exception as e:
                    print(f"  [{idx}/{len(items)}] UID={uid_short} ❌ 处理异常: {e}")
                    try:
                        new_retry = (r["retry_count"] or 0) + 1
                        rec = EmailSummaryRecord(
                            account_name=acct_name, uid=uid, sender=frm, subject=subj,
                            email_date=date_str, summary=f"[处理异常]: {e}",
                            status="failed", retry_count=new_retry,
                        )
                        _upsert(db_path, rec)
                    except Exception:
                        pass
                    total_fail += 1
                    time.sleep(1)
        finally:
            if not is_graph and mail:
                try:
                    mail.quit()
                except Exception:
                    pass

    print(f"\n🏁 补跑完成: ✅成功 {total_ok}, 🔕降噪 {total_noise}, ❌失败 {total_fail}, ⏭️跳过 {total_skip}")


def _retry_llm_summary(subj, frm, body_text, date_str, max_attempts=4):
    """带 429 退避的 LLM 摘要。返回 (result_dict|None, error_str|None)。"""
    import urllib.error
    last = None
    for attempt in range(1, max_attempts + 1):
        try:
            return extract_email_summary_by_llm(subj, frm, body_text, date_str), None
        except urllib.error.HTTPError as e:
            last = e
            if e.code == 429:
                wait = 30 * attempt
                print(f"    ⏳ 429 限流,等待 {wait}s 后重试 ({attempt}/{max_attempts})")
                time.sleep(wait)
                continue
            return None, f"HTTP {e.code}: {e.reason}"
        except Exception as e:
            last = e
            if attempt < max_attempts:
                time.sleep(5)
                continue
            return None, str(e)
    return None, f"429退避耗尽: {last}"


def main():
    if len(sys.argv) < 2:
        interactive_menu()
        sys.exit(0)
    
    cmd = sys.argv[1]
    if cmd in ('menu', 'interactive'):
        interactive_menu()
    elif cmd == 'test':
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
        account_name = sys.argv[3] if len(sys.argv) > 3 else None
        read_emails_pop3(limit, account_name)
    elif cmd == 'search':
        if len(sys.argv) < 3:
            print('用法：search <keyword> [limit] [account_name]')
            sys.exit(1)
        keyword = sys.argv[2]
        limit = int(sys.argv[3]) if len(sys.argv) > 3 and sys.argv[3].isdigit() else 20
        account_name = sys.argv[4] if len(sys.argv) > 4 else None
        search_emails_pop3(keyword, limit, account_name)
    elif cmd == 'download':
        if len(sys.argv) < 3:
            print('用法：download <uid> [account_name]')
            sys.exit(1)
        uid = sys.argv[2]
        format = 'both'
        if '--html' in sys.argv:
            format = 'html'
        elif '--md' in sys.argv:
            format = 'md'
        args = [arg for arg in sys.argv[3:] if not arg.startswith('--')]
        account_name = args[0] if args else None
        download_email_pop3(uid, format=format, account_name=account_name)
    elif cmd in ('download_bank_bills', 'exec3m'):
        months = int(sys.argv[2]) if len(sys.argv) > 2 and sys.argv[2].isdigit() else 3
        download_recent_bank_emails(months)

    elif cmd == 'unpaid':
        show_unpaid_statements()
    elif cmd == 'mark_paid':
        if len(sys.argv) < 3:
            print('用法：mark_paid <bank_code> [statement_month]')
            sys.exit(1)
        bank_code = sys.argv[2]
        month = sys.argv[3] if len(sys.argv) > 3 else None
        mark_statement_paid_cmd(bank_code, month)

    elif cmd == 'due_soon_bills':
        months = int(sys.argv[2]) if len(sys.argv) > 2 and sys.argv[2].isdigit() else 3
        days = int(sys.argv[3]) if len(sys.argv) > 3 and sys.argv[3].isdigit() else 7
        due_soon_bank_bills(months, days)
    elif cmd == 'fetch_summaries':
        months = int(sys.argv[2]) if len(sys.argv) > 2 and sys.argv[2].isdigit() else 1
        fetch_recent_emails_and_summarize(months)
    elif cmd == 'show_headers':
        limit = int(sys.argv[2]) if len(sys.argv) > 2 and sys.argv[2].isdigit() else 15
        show_headers(limit)
    elif cmd == 'show_missed':
        limit = int(sys.argv[2]) if len(sys.argv) > 2 and sys.argv[2].isdigit() else 15
        show_missed(limit)
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
    elif cmd in ('reprocess_failed', 'reprocess'):
        reprocess_failed_emails()
    else:
        print(f'未知命令：{cmd}')
        sys.exit(1)

if __name__ == '__main__':
    main()
