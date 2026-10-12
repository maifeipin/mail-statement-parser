#!/usr/bin/env python3
"""163 邮箱邮件技能 - 支持发送、读取、搜索、下载（含 HTML 表格解析）"""

import json, os, sys, email, re, glob, time, sqlite3
from typing import Optional, Dict, Any, List, Tuple
from datetime import datetime, timezone, timedelta

# Windows 终端中文编码与 emoji 兼容性适配
if sys.platform.startswith('win'):
    try:
        sys.stdout.reconfigure(encoding='utf-8')
        sys.stderr.reconfigure(encoding='utf-8')
    except Exception:
        pass
from statement_models import EmailSummaryRecord
from statement_db import init_db, get_recent_statements, get_summary_by_bank_month, get_reconciliation_rows, uid_exists, get_transactions_above_amount, upsert_email_summary, get_email_summary_status, get_email_summary_by_id, get_recent_email_headers, get_potential_missed_emails, get_unacked_quality_events, ack_quality_event, mark_statement_paid_by_id, update_summary_status, load_noise_rules, add_noise_rule, delete_noise_rule


from mail_connect import (MailAuthError, AccountBudgetExceeded, account_deadline, retry_with_backoff, is_graph_api, connect_pop3, connect_smtp, connect_imap, _graph_api_request, fetch_summaries_graph, send_email_graph, _month_subtract, pop3_uidl_map, pop3_retr_by_uid)
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
    validate_and_save_email_message, reparse_cached_statement,
)
from mail_sync import (
    fetch_recent_emails_and_summarize,
    reprocess_failed_emails,
    reprocess_by_status,
)

CONFIG_CANDIDATES = [
    os.path.expanduser('email-config.local.json'),
    os.path.expanduser('email-config.json'),
    os.path.expanduser('email-config.example.json'),
]
from parser_paths import resolve_parser_paths, ParserPaths, validate_write_target

_DEFAULT_PATHS = resolve_parser_paths()
DOWNLOAD_DIR = _DEFAULT_PATHS.download_dir
RULES_DIR = _DEFAULT_PATHS.rules_dir
VALIDATION_REPORT_DIR = _DEFAULT_PATHS.report_dir
DB_PATH = _DEFAULT_PATHS.db_path

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
        # 稳定身份：uid 为账户内持久 UIDL，映射到当前会话序号后再 retr；
        # 历史纯数字序号 uid 由 pop3_retr_by_uid 兜底（可能漂移，调用方需校验主题）
        _, headers, _ = pop3_retr_by_uid(mail, uid)
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

def validate_email_by_uid(uid, account_name=None, *, context=None):
    """按规则解析并输出校验报告。"""
    if context is None:
        context = resolve_parser_paths()
    try:
        msg = pop3_fetch_message_by_uid(uid, account_name=account_name)
        raw_bytes = msg.as_bytes() if hasattr(msg, "as_bytes") else None
        validate_and_save_email_message(
            msg, uid, account_name=account_name, context=context,
            raw_bytes=raw_bytes, transport_kind="pop3_uidl", is_synthesized=False
        )
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
        from mail_connect import _resolve_account
        target_config = _resolve_account(account_name, accounts)
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
        from mail_connect import _resolve_account
        target_config = _resolve_account(account_name, accounts)
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


def _safe_uid_for_fname(uid):
    """UID（POP3 UIDL / Graph id）清洗为文件名片段，防特殊字符注入路径。"""
    return re.sub(r'[^A-Za-z0-9_.-]', '_', str(uid))


def _downloaded_file_exists(account_name, uid, output_dir=None):
    """幂等检查：该账户该 UID 是否已有下载文件（uid 经文件名清洗，与保存侧一致）。"""
    output_dir = output_dir or DOWNLOAD_DIR
    safe = _safe_uid_for_fname(uid)
    try:
        return any(
            f.startswith(f'email_acc_{account_name}_uid{safe}_') or f.startswith(f'email_uid{safe}_')
            for f in os.listdir(output_dir)
        )
    except FileNotFoundError:
        return False


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

    # 生成文件名，带 UID 避免批量下载时冲突，附加账户前缀隔离（UID 经清洗防路径注入）
    safe_subj = re.sub(r'[^\w\s-]', '', subj[:50]).strip().replace(' ', '_') or 'email'
    acc_prefix = f"acc_{account_name}_" if account_name else ""
    fname = f'email_{acc_prefix}uid{_safe_uid_for_fname(uid)}_{safe_subj}'

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


def _collect_recent_bill_uids(months, rules, target_banks, email_config=None, on_candidate=None):
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
                            if on_candidate:
                                on_candidate(uid, None)
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

        # 稳定身份：候选 UID 使用账户内持久 UIDL，重连后经映射回序号取信
        uidl_by_num = pop3_uidl_map(mail)
        if not uidl_by_num:
            raise ValueError('服务器未提供 UIDL，拒绝使用会话序号持久化邮件')
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
                            stable_uid = uidl_by_num.get(i)
                            if not stable_uid:
                                raise ValueError('候选邮件缺少 UIDL')
                            candidates.append((stable_uid, b, subj, frm, str(dt) if dt else ''))
                            if on_candidate:
                                on_candidate(stable_uid, msg)
                            matched = True
                            break
                    if matched: break

                if not matched:
                    stats['skipped_unmatched'] += 1

            except Exception:
                stats['skipped_error'] += 1
    finally:
        try:
            mail.close()
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

            for uid, bank_code, subj, frm, date_str in candidates:
                try:
                    # 幂等：检查是否已有该 uid 的文件（附加账户前缀）
                    if _downloaded_file_exists(account_name, uid, output_dir):
                        print(f'⏭️  账户 {account_name} UID={str(uid)[:24]} 已下载，跳过')
                        skipped_old += 1
                        continue
                    _, headers, _ = pop3_retr_by_uid(mail, uid)
                    msg = email.message_from_bytes(b'\r\n'.join(headers))
                    _save_email_message(uid, msg, output_dir=output_dir, format='md', account_name=account_name)
                    downloaded += 1
                except Exception as e:
                    print(f"⚠️ UID={str(uid)[:24]} 下载异常: {e}")
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


def validate_recent_bank_emails(months=3, *, context=None):
    """专用指令：按规则关键字匹配，批量解析校验并写入 SQLite（每账户单会话复用）。"""
    if context is None:
        context = resolve_parser_paths()
    init_db(context.db_path)
    rules = load_rule_files(context=context)
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
        gated = 0
        skipped_db = 0
        is_graph = is_graph_api(account_config)
        mail = None
        if not is_graph:
            # 单会话复用：一次连接完成全部候选取信+校验，替代逐封重连
            try:
                mail = connect_pop3(account_config)
            except Exception as e:
                print(f'❌ 账户 {account_name} 连接失败：{e}')
                continue
        try:
            for uid, bank_code, subj, frm, date_str in candidates:
                # 幂等：检查是否已在 DB 中
                _uid_str = str(uid)
                if uid_exists(DB_PATH, _uid_str, account_name):
                    print(f'⏭️  UID={_uid_str[:24]} 已在数据库，跳过')
                    skipped_db += 1
                    continue
                try:
                    if is_graph:
                        msg = pop3_fetch_message_by_uid(uid, account_name=account_name)
                        raw_bytes = msg.as_bytes() if hasattr(msg, "as_bytes") else None
                        t_kind = "graph_api"
                        is_synth = True
                    else:
                        _, headers, _ = pop3_retr_by_uid(mail, uid)
                        raw_bytes = b'\r\n'.join(headers)
                        msg = email.message_from_bytes(raw_bytes)
                        t_kind = "pop3_uidl"
                        is_synth = False
                    result = validate_and_save_email_message(
                        msg, uid, rules=rules, account_name=account_name, context=context,
                        raw_bytes=raw_bytes, transport_kind=t_kind, is_synthesized=is_synth
                    )
                    st = (result or {}).get('status')
                    if st in ('saved', 'parse_failed'):
                        ok += 1
                    elif st == 'not_statement':
                        gated += 1  # 门禁拦截：非账单不计失败
                    else:
                        fail += 1
                except Exception as e:
                    print(f'❌ UID={_uid_str[:24]} 写库失败：{e}')
                    fail += 1
        finally:
            if mail:
                try:
                    mail.quit()
                except Exception:
                    pass

        print(
            f'✅ 账户 {account_name} 写库完成：成功 {ok} 封，已在库跳过 {skipped_db} 封，门禁拦截 {gated} 封，失败 {fail} 封，'
            f'过期邮件 {skipped_old} 封，未匹配 {skipped_unmatched} 封，异常 {skipped_error} 封'
        )


def sync_recent_bank_emails(months=3, account_budget_s=60, total_budget_s=270, *, context=None):
    """Scan, archive and parse in one POP3 session, with bounded account deadlines.

    No LLM is used. The global deadline leaves room for the caller's 300s limit
    and always emits a receipt for every configured account.
    """
    if context is None:
        context = resolve_parser_paths()
    init_db(context.db_path)
    rules = load_rule_files(context=context)
    accounts = load_accounts()
    validate_write_target(context, context.download_dir)
    os.makedirs(context.download_dir, exist_ok=True)
    target_banks = {'HX', 'CMB', 'SPDB', 'CMBC', 'ICBC', 'CITIC'}
    account_results = []
    overall_deadline = time.monotonic() + total_budget_s
    for account_config in accounts:
        account_name = account_config.get('account', 'default')
        acct = {'account': account_name, 'status': 'ok', 'candidates': 0,
                'downloaded': 0, 'saved': 0, 'parse_failed': 0, 'gated': 0,
                'skipped_dup': 0, 'errors': 0, 'scan_errors': 0}
        account_results.append(acct)
        allowed = min(account_budget_s, overall_deadline - time.monotonic())
        print(f'\n🚀 账户 {account_name} 单次遍历同步（最近 {months} 个月，预算 {max(0, allowed):.1f}s）')
        if not rules:
            acct.update(status='error', error='未找到账单规则')
            continue

        def process_candidate(uid, msg):
            acct['candidates'] += 1
            uid = str(uid)
            need_download = not _downloaded_file_exists(account_name, uid)
            need_db = not uid_exists(DB_PATH, uid, account_name)
            if not need_download and not need_db:
                acct['skipped_dup'] += 1
                return
            if msg is None:  # Graph MIME preserves Message-ID and HTML evidence.
                msg = pop3_fetch_message_by_uid(uid, account_name=account_name)
            raw_bytes = msg.as_bytes() if hasattr(msg, "as_bytes") else None
            t_kind = "graph_api" if is_graph else "pop3_uidl"
            is_synth = is_graph
            if need_download:
                _save_email_message(uid, msg, output_dir=DOWNLOAD_DIR, format='md', account_name=account_name)
                acct['downloaded'] += 1
            if need_db:
                result = validate_and_save_email_message(
                    msg, uid, rules=rules, account_name=account_name, context=context,
                    raw_bytes=raw_bytes, transport_kind=t_kind, is_synthesized=is_synth
                )
                key = {'saved': 'saved', 'parse_failed': 'parse_failed',
                       'not_statement': 'gated'}.get((result or {}).get('status'), 'errors')
                acct[key] += 1

        try:
            with account_deadline(allowed):
                _, stats = _collect_recent_bill_uids(months, rules, target_banks,
                                                     account_config, on_candidate=process_candidate)
                acct['scan_errors'] = stats['skipped_error']
                acct['errors'] += stats['skipped_error']
                if acct['errors'] or acct['parse_failed']:
                    acct['status'] = 'partial'
        except AccountBudgetExceeded:
            acct.update(status='timeout', error=f'account/overall time budget exhausted ({max(0, allowed):.1f}s)')
        except Exception as exc:
            acct.update(status='error', error=str(exc))
            acct['errors'] += 1
        print(f"🏁 账户 {account_name} 状态 {acct['status']}：入库 {acct['saved']}，"
              f"解析失败 {acct['parse_failed']}，异常 {acct['errors']}")

    # 协议版本 + 质量事件计数（重构契约 §3：保留旧标记兼容，追加 protocol_version）
    try:
        from statement_db import get_unacked_quality_events
        unacked_events = get_unacked_quality_events(DB_PATH, limit=1000)
    except Exception:
        unacked_events = []
    summary = {'command': 'sync3m', 'protocol_version': 2, 'months': int(months), 'accounts': account_results,
               'errors': [] if accounts else ['未配置邮箱账户'],
               'quality_events': {
                   'unacked_count': len(unacked_events),
                   'detected': sum(1 for e in unacked_events if e.get('event_type') == 'quality_detected'),
                   'updated': sum(1 for e in unacked_events if e.get('event_type') == 'quality_updated'),
                   'resolved': sum(1 for e in unacked_events if e.get('event_type') == 'quality_resolved'),
                   'statement_recorded': sum(1 for e in unacked_events if e.get('event_type') == 'statement_recorded'),
               },
               'totals': {key: sum(a[key] for a in account_results) for key in
                          ('candidates', 'downloaded', 'saved', 'parse_failed', 'gated',
                           'skipped_dup', 'errors', 'scan_errors')}}
    print('--- JSON_SUMMARY_START ---')
    print(json.dumps(summary, ensure_ascii=False))
    print('--- JSON_SUMMARY_END ---')
    return summary


def due_soon_bank_bills(months=3, days=7, output_dir=None):
    from statement_db import get_unpaid_statements
    from datetime import datetime
    today = datetime.now().date()
    rows = get_unpaid_statements(DB_PATH, months=int(months))
    
    print(f"🚀 执行专用指令：查询最近未还款账单（临近 {days} 天）")
    
    warn_missing_due = []
    due_soon_items = []
    
    for r in rows:
        if r['repayment_availability']['status'] != 'available':
            warn_missing_due.append({'bank_code':r['bank_code'],'subject':r['subject'], 'reason':'还款用途未确认，需重解析/人工核查'})
            continue
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
                    'currency': r['currency'],
                    'subject': r['subject']
                })
        except ValueError:
            warn_missing_due.append({'bank_code': r['bank_code'], 'subject': r['subject'], 'reason': f"日期格式错误: {due_date_str}"})
            
    if due_soon_items:
        due_soon_items.sort(key=lambda x: x['days_left'])
        print("\n=== ⚠️ 临期账单 (<= {}天) ===".format(days))
        for item in due_soon_items:
            print(f"[{item['bank_code']}] {item['subject']}")
            print(f"   应还: {item['total_due']} {item['currency']} | 还款日: {item['due_date']} (剩余 {item['days_left']} 天)")
    elif warn_missing_due:
        print("\n⚠️ 存在无法核验还款用途的账单，不能确认是否有临期事项。")
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
        print(f"   应还: {r['total_due']} {r['currency']} | 还款日: {r['due_date'] or '未知'} | 用途: {r['repayment_availability']['status']}")

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

def cli_mail_headers(limit=15):
    errors = []
    data = {'headers': []}
    try:
        conn = _connect_ro(DB_PATH)
        try:
            rows = conn.execute("""SELECT id,account_name,uid,sender,subject,email_date,
                category,importance,status,processed_at FROM email_summaries
                ORDER BY email_date DESC,id DESC LIMIT ?""", (min(100,int(limit)),)).fetchall()
            data['headers'] = [dict(row) for row in rows]
        finally:
            conn.close()
    except Exception as exc:
        errors.append(f'mail_headers: {exc}')
    return _emit_envelope('mail_headers', data, errors)


def _fetch_missed_emails(db_path: str, limit: int = 15) -> list:
    conn = _connect_ro(db_path)
    try:
        cur = conn.cursor()
        import datetime
        cutoff_date = (datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=7)).isoformat()
        cur.execute(
            """
            SELECT id, account_name, uid, sender, subject, email_date, category, importance, status, summary, processed_at
            FROM email_summaries
            WHERE importance != 'high'
              AND category != 'Spam'
              AND status IN ('processed', 'skipped')
              AND email_date >= ?
            ORDER BY email_date DESC, id DESC
            LIMIT ?
            """,
            (cutoff_date, limit)
        )
        return list(cur.fetchall())
    finally:
        conn.close()


def cli_mail_missed(limit=15):
    errors = []
    data = {'missed': []}
    try:
        data['missed'] = [dict(row) for row in _fetch_missed_emails(DB_PATH,min(100,int(limit)))]
    except Exception as exc:
        errors.append(f'mail_missed: {exc}')
    return _emit_envelope('mail_missed', data, errors)


def _connect_ro(db_path: str):
    """URI 编码的只读连接。若文件不存在抛出 FileNotFoundError，绝不自动建库。"""
    if not os.path.exists(db_path):
        raise FileNotFoundError(f"数据库文件不存在：{db_path}")
    import urllib.parse
    quoted = urllib.parse.quote(os.path.abspath(db_path), safe="/")
    conn = sqlite3.connect(f"file:{quoted}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def _emit_envelope(command, data=None, errors=None):
    """版本化 JSON 回执：lite_agent integrations/mail/client.py 的消费契约。"""
    envelope = {
        'schema_version': 1,
        'command': command,
        'status': 'ok' if not errors else 'error',
        'data': data if data is not None else {},
        'errors': errors or [],
    }
    print(json.dumps(envelope, ensure_ascii=False))
    return envelope


def cli_quality_events(limit=100, kind=None):
    errors = []
    data = {'events': []}
    try:
        # 只读命令严禁调用 init_db 触发迁移
        events = get_unacked_quality_events(DB_PATH, limit=max(1, int(limit)),
            event_types=('quality_detected', 'quality_updated', 'quality_resolved') if kind == 'quality' else None)
        for e in events:
            try:
                e['diagnostics'] = json.loads(e.get('diagnostics_json') or '[]')
            except ValueError:
                e['diagnostics'] = []
            try:
                e['evidence_refs'] = json.loads(e.get('evidence_refs_json') or '[]')
            except ValueError:
                e['evidence_refs'] = []
            e.pop('diagnostics_json', None)
            e.pop('evidence_refs_json', None)
            e.pop('observation_keys_json', None)
        data['events'] = events
        data['count'] = len(events)
    except Exception as exc:
        errors.append(f'quality_events: {exc}')
    return _emit_envelope('quality_events', data, errors)


def cli_quality_ack(event_id, todo_id, revision=None):
    errors = []
    data = {}
    try:
        data = ack_quality_event(DB_PATH, event_id, todo_id, revision=revision)
        if data.get('acked') is not True:
            errors.append(f"quality_ack rejected: {data.get('reason')} (revision={revision})")
    except Exception as exc:
        errors.append(f'quality_ack: {exc}')
        data = {'acked': False, 'event_id': event_id, 'todo_id': todo_id, 'reason': 'error'}
    return _emit_envelope('quality_ack', data, errors)


def cli_billing_snapshot(months=1, detail_limit=20, as_of=None):
    from billing_readmodel import snapshot
    errors = []
    data = {}
    try:
        data = snapshot(DB_PATH, months=max(1, int(months)), detail_limit=max(0, int(detail_limit)), today=as_of)
    except Exception as exc:
        errors.append(f'billing_snapshot: {exc}')
    return _emit_envelope('billing_snapshot', data, errors)


def cli_billing_trends(months=3, merchant=None, account=None):
    from billing_readmodel import trends
    errors = []
    data = {}
    try:
        # 只读命令严禁调用 init_db 触发迁移
        data = trends(DB_PATH, months=max(1, int(months)), merchant=merchant, account=account)
    except Exception as exc:
        errors.append(f'billing_trends: {exc}')
    return _emit_envelope('billing_trends', data, errors)


def cli_mark_paid_by_id(statement_id):
    """精确到逻辑账单的还款标记，输出统一 envelope（契约 §3）。"""
    errors = []
    data = {}
    try:
        sid = int(statement_id)
        n = mark_statement_paid_by_id(DB_PATH, sid)
        data = {'statement_id': sid, 'updated': bool(n)}
    except Exception as exc:
        errors.append(f'mark_paid: {exc}')
        data = {'statement_id': statement_id, 'updated': False}
    return _emit_envelope('mark_paid', data, errors)


def cli_identity_preview(statement_id: int, account: str, transport_kind: str,
                         transport_uid: str, bank: str, eml_path: Optional[str] = None):
    errors = []
    data = {}
    try:
        from parser_paths import resolve_parser_paths
        from statement_db import get_statement_evidence
        import statement_identity as si

        context = resolve_parser_paths()
        conn = sqlite3.connect(f'file:{context.db_path}?mode=ro', uri=True)
        conn.row_factory = sqlite3.Row
        cur = conn.cursor()
        cur.execute("SELECT * FROM statements WHERE id = ?", (statement_id,))
        target_row = cur.fetchone()
        conn.close()

        if not target_row:
            errors.append(f"Statement ID {statement_id} not found")
            return _emit_envelope('identity_preview', data, errors)

        target_dict = dict(target_row)
        if target_dict['account_name'] != account or target_dict['bank_code'] != bank:
            errors.append(
                f"Target statement mismatch: ({target_dict['account_name']}/{target_dict['bank_code']}) "
                f"!= ({account}/{bank})"
            )
            return _emit_envelope('identity_preview', data, errors)

        source_raw_bytes = None
        source_eml_path = None
        if eml_path and os.path.exists(eml_path):
            source_eml_path = eml_path
            with open(eml_path, 'rb') as fp:
                source_raw_bytes = fp.read()
        else:
            default_eml = os.path.join(context.download_dir, f"{transport_uid}.eml")
            if os.path.exists(default_eml):
                source_eml_path = default_eml
                with open(default_eml, 'rb') as fp:
                    source_raw_bytes = fp.read()

        if not source_raw_bytes:
            errors.append(f"Source EML not found for UID {transport_uid}")
            return _emit_envelope('identity_preview', data, errors)

        source_ev = si.build_evidence_from_raw_eml(
            source_raw_bytes, account_name=account, bank_code=bank,
            transport_kind=transport_kind, transport_uid=str(transport_uid)
        )

        target_raw_bytes = None
        target_archive_path = None

        # 优先从 statement_evidence_index 读取可信目标归档 (Gap 5 P1)
        ev_index = get_statement_evidence(context.db_path, statement_id)
        if ev_index and ev_index.get('archive_path') and os.path.exists(ev_index['archive_path']):
            target_archive_path = ev_index['archive_path']
            with open(target_archive_path, 'rb') as fp:
                target_raw_bytes = fp.read()
        else:
            cand_eml = os.path.join(context.download_dir, f"{target_dict['uid']}.eml")
            if os.path.exists(cand_eml):
                target_archive_path = cand_eml
                with open(cand_eml, 'rb') as fp:
                    target_raw_bytes = fp.read()

        from statement_db import save_statement_evidence_index
        if target_raw_bytes:
            target_ev = si.build_evidence_from_raw_eml(
                target_raw_bytes, account_name=account, bank_code=bank,
                transport_kind=transport_kind, transport_uid=str(target_dict['uid'])
            )
            if target_ev.get('coverage_status') == 'complete':
                target_archive_path = si.archive_evidence_eml(
                    context.data_root, target_raw_bytes, target_ev['raw_content_hash'], context=context
                )
                save_statement_evidence_index(
                    context.db_path,
                    statement_id=statement_id,
                    account_name=account,
                    bank_code=bank,
                    message_id=target_dict.get('message_id') or '',
                    raw_content_hash=target_ev['raw_content_hash'],
                    evidence_digest=target_ev.get('complete_content_digest') or '',
                    coverage_status='complete',
                    archive_path=target_archive_path,
                    evidence_version=si.NORMALIZATION_VERSION
                )
        else:
            target_ev = {
                'normalization_version': si.NORMALIZATION_VERSION,
                'account_name': account,
                'bank_code': bank,
                'transport_kind': transport_kind,
                'transport_uid': str(target_dict['uid']),
                'headers': {
                    'message_id': target_dict.get('message_id') or '',
                    'subject': target_dict.get('subject') or '',
                    'from': target_dict.get('sender') or '',
                    'date': target_dict.get('email_date') or '',
                    'headers_fingerprint': '',
                },
                'message_id': target_dict.get('message_id') or '',
                'coverage_status': 'coverage_unknown',
                'complete_mime': False,
                'complete_content_digest': None,
                'comparable_body_digest': None,
                'missing_reason': 'target_raw_eml_not_found',
            }

        anchor_snapshot = {
            'statement_id': statement_id,
            'uid': target_dict['uid'],
            'message_id': target_dict.get('message_id'),
            'statement_month': target_dict.get('statement_month'),
            'total_due': target_dict.get('total_due'),
            'is_paid': target_dict.get('is_paid'),
        }

        # 原子归档源端 EML 为内容寻址证据 (T3)
        source_archive_path = None
        if source_raw_bytes and source_ev.get('raw_content_hash'):
            source_archive_path = si.archive_evidence_eml(
                context.data_root, source_raw_bytes, source_ev['raw_content_hash'], context=context
            )

        source_ref = {
            'archive_path': os.path.abspath(source_archive_path) if source_archive_path else None,
            'eml_path': os.path.abspath(source_eml_path) if source_eml_path else None,
            'raw_content_hash': source_ev.get('raw_content_hash'),
            'complete_content_digest': source_ev.get('complete_content_digest'),
        }
        target_ref = {
            'statement_id': statement_id,
            'archive_path': os.path.abspath(target_archive_path) if target_archive_path else None,
            'raw_content_hash': target_ev.get('raw_content_hash'),
            'complete_content_digest': target_ev.get('complete_content_digest'),
        }

        plan = si.generate_identity_plan(
            source_ev, target_ev,
            target_statement_id=statement_id,
            anchor_snapshot=anchor_snapshot,
            source_ref=source_ref,
            target_ref=target_ref
        )

        diff_report = {
            'plan_id': plan['plan_id'],
            'plan_hash': plan['plan_hash'],
            'target_statement_id': statement_id,
            'metadata_diff': {
                'subject': {'source': source_ev['headers'].get('subject'), 'target': target_ev['headers'].get('subject')},
                'from': {'source': source_ev['headers'].get('from'), 'target': target_ev['headers'].get('from')},
                'date': {'source': source_ev['headers'].get('date'), 'target': target_ev['headers'].get('date')},
                'message_id': {'source': source_ev.get('message_id'), 'target': target_ev.get('message_id')},
            },
            'mime_tree': {
                'source_parts_count': source_ev.get('parts_count'),
                'target_parts_count': target_ev.get('parts_count'),
                'source_tree_nodes': source_ev.get('tree_nodes', []),
                'target_tree_nodes': target_ev.get('tree_nodes', []),
            },
            'body_and_digest': {
                'source_comparable_body_digest': source_ev.get('comparable_body_digest'),
                'target_comparable_body_digest': target_ev.get('comparable_body_digest'),
                'source_complete_content_digest': source_ev.get('complete_content_digest'),
                'target_complete_content_digest': target_ev.get('complete_content_digest'),
                'source_raw_hash': source_ev.get('raw_content_hash'),
                'target_raw_hash': target_ev.get('raw_content_hash'),
            },
            'coverage': {
                'source_coverage_status': source_ev.get('coverage_status'),
                'target_coverage_status': target_ev.get('coverage_status'),
                'source_missing_reason': source_ev.get('missing_reason'),
                'target_missing_reason': target_ev.get('missing_reason'),
            }
        }
        diff_path = si.save_identity_diff_report(context.data_root, plan['plan_id'], diff_report, context=context)
        report_path = si.save_identity_plan_report(context.data_root, plan, context=context)

        data = {
            'plan_id': plan['plan_id'],
            'plan_hash': plan['plan_hash'],
            'status': plan['status'],
            'report_path': report_path,
            'diff_path': diff_path,
            'target_statement_id': statement_id,
            'transport_key': {
                'account': account,
                'transport_kind': transport_kind,
                'transport_uid': str(transport_uid),
                'bank': bank,
            },
            'summary': {
                'reasons': plan['reasons'],
                'source_digest': source_ev.get('complete_content_digest'),
                'target_digest': target_ev.get('complete_content_digest'),
                'can_confirm': plan['status'] in ('associated_existing', 'insufficient_evidence')
            }
        }
    except Exception as exc:
        errors.append(f"identity_preview: {exc}")
    return _emit_envelope('identity_preview', data, errors)


def cli_identity_confirm(plan_path: str, plan_hash: str, actor: str, reason: str):
    errors = []
    data = {}
    try:
        from parser_paths import resolve_parser_paths
        from statement_db import associate_transport_identity
        import statement_identity as si

        if not actor or not actor.strip():
            errors.append("必须提供明确的 --actor（操作人标识），禁止自动或空操作人")
            return _emit_envelope('identity_confirm', data, errors)
        if not reason or not reason.strip():
            errors.append("必须提供明确的 --reason（确认理由）")
            return _emit_envelope('identity_confirm', data, errors)

        if not os.path.exists(plan_path):
            errors.append(f"Plan file not found: {plan_path}")
            return _emit_envelope('identity_confirm', data, errors)

        with open(plan_path, 'r', encoding='utf-8') as fp:
            plan = json.load(fp)

        # 1. 重算规范化计划哈希 (R1: 防字段篡改)
        computed_hash = si.compute_plan_hash(plan)
        if computed_hash != plan_hash:
            errors.append(f"Plan content hash mismatch: computed={computed_hash} != provided={plan_hash}")
            return _emit_envelope('identity_confirm', data, errors)
        if plan.get('plan_hash') != plan_hash:
            errors.append(f"Stored plan_hash mismatch: {plan.get('plan_hash')} != {plan_hash}")
            return _emit_envelope('identity_confirm', data, errors)

        # 2. 版本与必需字段校验
        if plan.get('schema_version') != 1:
            errors.append(f"Invalid schema_version: {plan.get('schema_version')}")
            return _emit_envelope('identity_confirm', data, errors)
        if plan.get('normalization_version') != 'v1':
            errors.append(f"Invalid normalization_version: {plan.get('normalization_version')}")
            return _emit_envelope('identity_confirm', data, errors)

        for req_field in ('account_name', 'bank_code', 'transport_kind', 'transport_uid',
                         'target_statement_id', 'anchor_snapshot', 'source_evidence_summary',
                         'target_evidence_summary'):
            if not plan.get(req_field):
                errors.append(f"Missing required plan field: {req_field}")
                return _emit_envelope('identity_confirm', data, errors)

        # 3. 时效性校验
        expires_at_str = plan.get('expires_at')
        if not expires_at_str:
            errors.append("Plan missing expires_at timestamp")
            return _emit_envelope('identity_confirm', data, errors)
        exp_dt = datetime.fromisoformat(expires_at_str)
        if datetime.now(timezone.utc) > exp_dt:
            errors.append(f"Plan has expired at {expires_at_str}")
            return _emit_envelope('identity_confirm', data, errors)

        # 4. 状态白名单校验 (仅允许 associated_existing 或 insufficient_evidence 进入人工流程)
        plan_status = plan.get('status')
        if plan_status not in ('associated_existing', 'insufficient_evidence'):
            errors.append(f"Plan status '{plan_status}' is not eligible for confirmation (only associated_existing or insufficient_evidence allowed)")
            return _emit_envelope('identity_confirm', data, errors)

        # 5. 重读并重核源文件和目标归档证据 (Gap 2 P0)
        source_ref = plan.get('source_ref') or {}
        source_eml_path = source_ref.get('eml_path')
        if not source_eml_path or not os.path.exists(source_eml_path):
            errors.append(f"Source EML file not found at confirmation time: {source_eml_path}")
            return _emit_envelope('identity_confirm', data, errors)

        with open(source_eml_path, 'rb') as fp:
            re_read_source_bytes = fp.read()
        re_read_source_ev = si.build_evidence_from_raw_eml(
            re_read_source_bytes,
            account_name=plan['account_name'],
            bank_code=plan['bank_code'],
            transport_kind=plan['transport_kind'],
            transport_uid=plan['transport_uid']
        )
        if re_read_source_ev.get('raw_content_hash') != source_ref.get('raw_content_hash'):
            errors.append(
                f"Source EML file was replaced or modified after preview: "
                f"current_hash={re_read_source_ev.get('raw_content_hash')} != preview_hash={source_ref.get('raw_content_hash')}"
            )
            return _emit_envelope('identity_confirm', data, errors)
        if re_read_source_ev.get('complete_content_digest') != source_ref.get('complete_content_digest'):
            errors.append(
                f"Source EML evidence digest has changed after preview: "
                f"current_digest={re_read_source_ev.get('complete_content_digest')} != preview_digest={source_ref.get('complete_content_digest')}"
            )
            return _emit_envelope('identity_confirm', data, errors)

        # 目标归档重核 (T3: 声明归档则缺失必须拒绝，禁止回退到计划摘要)
        target_ref = plan.get('target_ref') or {}
        target_archive_path = target_ref.get('archive_path')
        if target_archive_path:
            if not os.path.exists(target_archive_path):
                errors.append(
                    f"Target archive declared in plan is missing or deleted at confirm time: {target_archive_path}"
                )
                return _emit_envelope('identity_confirm', data, errors)
            with open(target_archive_path, 'rb') as fp:
                re_read_target_bytes = fp.read()
            re_read_target_ev = si.build_evidence_from_raw_eml(
                re_read_target_bytes,
                account_name=plan['account_name'],
                bank_code=plan['bank_code'],
                transport_kind=plan['transport_kind'],
                transport_uid=str(plan['target_statement_id'])
            )
            if re_read_target_ev.get('raw_content_hash') != target_ref.get('raw_content_hash'):
                errors.append(
                    f"Target EML archive was modified after preview: "
                    f"current_hash={re_read_target_ev.get('raw_content_hash')} != preview_hash={target_ref.get('raw_content_hash')}"
                )
                return _emit_envelope('identity_confirm', data, errors)
        else:
            re_read_target_ev = None

        context = resolve_parser_paths()
        method = 'digest_equal' if plan_status == 'associated_existing' else 'manual_verified'

        assoc_res = associate_transport_identity(
            context.db_path,
            account_name=plan['account_name'],
            transport_kind=plan['transport_kind'],
            transport_uid=plan['transport_uid'],
            bank_code=plan['bank_code'],
            statement_id=plan['target_statement_id'],
            message_id=plan['source_evidence_summary']['message_id'] or '',
            evidence_digest=re_read_source_ev.get('complete_content_digest'),
            evidence_version=plan.get('normalization_version', 'v1'),
            raw_content_hash=re_read_source_ev.get('raw_content_hash'),
            method=method,
            actor=actor.strip(),
            reason=reason.strip(),
            plan_id=plan['plan_id'],
            plan_hash=plan_hash,
            source_evidence=re_read_source_ev,
            target_evidence=re_read_target_ev,
            anchor_snapshot=plan.get('anchor_snapshot'),
            target_ref=target_ref,
            source_ref=source_ref,
            source_raw_bytes=re_read_source_bytes,
            plan_path=plan_path,
        )

        # 归档源端 EML
        if re_read_source_bytes and re_read_source_ev.get('raw_content_hash'):
            si.archive_evidence_eml(
                context.data_root, re_read_source_bytes,
                re_read_source_ev['raw_content_hash'], context=context
            )

        data = {
            'confirmed': True,
            'statement_id': plan['target_statement_id'],
            'transport_uid': plan['transport_uid'],
            'method': method,
            'idempotent': assoc_res.get('idempotent', False),
        }
    except Exception as exc:
        errors.append(f"identity_confirm: {exc}")
    return _emit_envelope('identity_confirm', data, errors)


def cli_identity_status(statement_id: Optional[int] = None, account: Optional[str] = None,
                        transport_uid: Optional[str] = None):
    errors = []
    data = {'identities': [], 'audits': []}
    try:
        from parser_paths import resolve_parser_paths
        context = resolve_parser_paths()
        conn = sqlite3.connect(f'file:{context.db_path}?mode=ro', uri=True)
        conn.row_factory = sqlite3.Row
        cur = conn.cursor()

        q_id = "SELECT * FROM statement_transport_identities WHERE 1=1"
        p_id = []
        if statement_id:
            q_id += " AND statement_id = ?"
            p_id.append(statement_id)
        if account:
            q_id += " AND account_name = ?"
            p_id.append(account)
        if transport_uid:
            q_id += " AND transport_uid = ?"
            p_id.append(str(transport_uid))
        q_id += " ORDER BY id DESC LIMIT 50"

        try:
            cur.execute(q_id, p_id)
            data['identities'] = [dict(r) for r in cur.fetchall()]
        except sqlite3.OperationalError:
            data['identities'] = []

        q_au = "SELECT * FROM statement_identity_audit WHERE 1=1"
        p_au = []
        if statement_id:
            q_au += " AND statement_id = ?"
            p_au.append(statement_id)
        if account:
            q_au += " AND account_name = ?"
            p_au.append(account)
        if transport_uid:
            q_au += " AND transport_uid = ?"
            p_au.append(str(transport_uid))
        q_au += " ORDER BY id DESC LIMIT 50"

        try:
            cur.execute(q_au, p_au)
            data['audits'] = [dict(r) for r in cur.fetchall()]
        except sqlite3.OperationalError:
            data['audits'] = []

        conn.close()
    except Exception as exc:
        errors.append(f"identity_status: {exc}")
    return _emit_envelope('identity_status', data, errors)




def cli_mail_stats():
    errors = []
    data = {}
    try:
        import urllib.parse
        quoted = urllib.parse.quote(os.path.abspath(DB_PATH), safe="/")
        conn = sqlite3.connect(f"file:{quoted}?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
        try:
            cur = conn.cursor()
            total = cur.execute("SELECT count(*) FROM email_summaries").fetchone()[0]
            if total == 0:
                data = {"total": 0, "categories": [], "importance": [], "statuses": {}, "accounts": [], "success_rate": 0.0}
            else:
                cat_rows = [dict(r) for r in cur.execute("SELECT category, count(*) as cnt FROM email_summaries GROUP BY category ORDER BY cnt DESC").fetchall()]
                imp_rows = [dict(r) for r in cur.execute("SELECT importance, count(*) as cnt FROM email_summaries GROUP BY importance ORDER BY cnt DESC").fetchall()]
                status_rows = cur.execute("SELECT status, count(*) as cnt FROM email_summaries GROUP BY status").fetchall()
                status_map = {r["status"]: r["cnt"] for r in status_rows}
                acct_rows = [dict(r) for r in cur.execute("SELECT account_name, count(*) as cnt FROM email_summaries GROUP BY account_name ORDER BY cnt DESC").fetchall()]
                success_rate = round((status_map.get("processed", 0) + status_map.get("noise", 0)) / total * 100, 1) if total > 0 else 0.0
                data = {
                    "total": total,
                    "categories": cat_rows,
                    "importance": imp_rows,
                    "statuses": status_map,
                    "accounts": acct_rows,
                    "success_rate": success_rate,
                }
        finally:
            conn.close()
    except Exception as exc:
        errors.append(f"mail_stats: {exc}")
    return _emit_envelope('mail_stats', data, errors)


def cli_mail_search(keyword, limit=20, account_name=None, offset=0):
    errors = []
    data = {"results": [], "total": 0}
    if not keyword:
        errors.append("必须提供搜索关键词")
        return _emit_envelope('mail_search', data, errors)
    try:
        limit_num = max(1, min(100, int(limit)))
    except (ValueError, TypeError):
        limit_num = 20
    try:
        import urllib.parse
        quoted = urllib.parse.quote(os.path.abspath(DB_PATH), safe="/")
        conn = sqlite3.connect(f"file:{quoted}?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
        try:
            sql = (
                "SELECT es.subject, es.sender, es.email_date, es.category, es.importance, "
                "es.status, es.account_name, es.uid, "
                "substr(CAST(eb.plain_text AS BLOB),1,8192) AS plain_text, "
                "length(CAST(eb.plain_text AS BLOB)) AS body_bytes "
                "FROM email_bodies eb JOIN email_summaries es "
                "ON eb.account_name=es.account_name AND eb.uid=es.uid "
                "WHERE (eb.plain_text LIKE ? OR es.subject LIKE ? OR es.sender LIKE ?) "
            )
            params = [f"%{keyword}%"] * 3
            if account_name:
                sql += "AND es.account_name = ? "
                params.append(account_name)
            sql += "ORDER BY es.id DESC LIMIT ? OFFSET ?"
            params.extend([limit_num + 1, max(0, int(offset))])
            rows = [dict(r) for r in conn.execute(sql, params).fetchall()]
            data['has_more'] = len(rows) > limit_num
            rows = rows[:limit_num]
            for row in rows:
                row['plain_text'] = (row['plain_text'] or b'').decode('utf-8', errors='ignore')
                row['is_truncated'] = (row.pop('body_bytes') or 0) > 8192
            data["results"] = rows
            data["total"] = len(rows)
            data['offset'] = max(0, int(offset))
        finally:
            conn.close()
    except Exception as exc:
        errors.append(f"mail_search: {exc}")
    return _emit_envelope('mail_search', data, errors)


def _truncate_utf8(text: str, max_bytes: int) -> Tuple[str, bool]:
    encoded = text.encode('utf-8')
    if len(encoded) <= max_bytes:
        return text, False
    chunk = encoded[:max_bytes]
    while chunk:
        try:
            return chunk.decode('utf-8'), True
        except UnicodeDecodeError:
            chunk = chunk[:-1]
    return "", True


def cli_mail_summary_get(summary_id):
    errors = []
    data = {"summary": None}
    try:
        sid = int(summary_id)
        conn = _connect_ro(DB_PATH)
        try:
            cur = conn.cursor()
            cur.execute("SELECT * FROM email_summaries WHERE id = ?", (sid,))
            row = cur.fetchone()
            if row:
                data["summary"] = dict(row)
            else:
                errors.append(f"未找到 ID={sid} 的邮件摘要记录")
        finally:
            conn.close()
    except Exception as exc:
        errors.append(f"mail_summary_get: {exc}")
    return _emit_envelope('mail_summary_get', data, errors)


def cli_mail_mark_processed(summary_id):
    errors = []
    data = {"summary_id": summary_id, "updated": False}
    try:
        sid = int(summary_id)
        success = update_summary_status(DB_PATH, sid, 'processed')
        data["updated"] = bool(success)
        if not success:
            errors.append(f"标记邮件 ID={sid} 失败")
    except Exception as exc:
        errors.append(f"mail_mark_processed: {exc}")
    return _emit_envelope('mail_mark_processed', data, errors)


def cli_mail_noise_rules_list():
    errors = []
    data = {"rules": []}
    try:
        conn = _connect_ro(DB_PATH)
        try:
            has_table = conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='noise_rules'").fetchone()
            if has_table:
                cur = conn.cursor()
                cur.execute("SELECT pattern_type, pattern_value FROM noise_rules")
                data["rules"] = [tuple(r) for r in cur.fetchall()]
            else:
                data["rules"] = []
        finally:
            conn.close()
    except Exception as exc:
        errors.append(f"mail_noise_rules_list: {exc}")
    return _emit_envelope('mail_noise_rules_list', data, errors)


def cli_mail_noise_rule_add(pattern_type, pattern_value, summary_id=None):
    errors = []
    data = {"pattern_type": pattern_type, "pattern_value": pattern_value, "added": False}
    try:
        if not pattern_type or not pattern_value:
            errors.append("必须提供 pattern_type 和 pattern_value")
        elif pattern_type not in ('sender_domain', 'sender_email'):
            errors.append(f"不支持的降噪规则类型：{pattern_type}（仅支持 sender_domain 或 sender_email）")
        else:
            val = pattern_value.strip().lower() if pattern_type == 'sender_domain' else pattern_value.strip()
            conn = sqlite3.connect(DB_PATH)
            try:
                conn.execute('BEGIN IMMEDIATE')
                now = datetime.now(timezone.utc).isoformat()
                if summary_id is not None:
                    sid = int(summary_id)
                    row = conn.execute("SELECT id FROM email_summaries WHERE id = ?", (sid,)).fetchone()
                    if not row:
                        conn.rollback()
                        errors.append(f"未找到 ID={sid} 的邮件摘要记录")
                        return _emit_envelope('mail_noise_rule_add', data, errors)

                    cur_u = conn.execute(
                        "UPDATE email_summaries SET status = ?, processed_at = ? WHERE id = ?",
                        ('noise', now, sid)
                    )
                    if cur_u.rowcount == 0:
                        conn.rollback()
                        errors.append(f"更新邮件摘要 ID={sid} 状态失败")
                        return _emit_envelope('mail_noise_rule_add', data, errors)


                conn.execute(
                    """
                    INSERT OR IGNORE INTO noise_rules (pattern_type, pattern_value, created_at)
                    VALUES (?, ?, ?)
                    """,
                    (pattern_type, val, now)
                )
                conn.commit()
                data["added"] = True
                if summary_id is not None:
                    data["summary_id"] = sid
                    data["status"] = "noise"
            except Exception:
                conn.rollback()
                raise
            finally:
                conn.close()
    except Exception as exc:
        errors.append(f"mail_noise_rule_add: {exc}")
    return _emit_envelope('mail_noise_rule_add', data, errors)


def cli_mail_noise_rule_delete(pattern_value, pattern_type=None):
    errors = []
    data = {"pattern_value": pattern_value, "deleted": False}
    try:
        if not pattern_value:
            errors.append("必须提供待删除的 pattern_value")
        else:
            val = pattern_value.strip()
            conn = sqlite3.connect(DB_PATH)
            try:
                conn.execute('BEGIN IMMEDIATE')
                deleted_count = 0
                if pattern_type is None:
                    c1 = conn.execute("DELETE FROM noise_rules WHERE pattern_type = 'sender_domain' AND pattern_value = ?", (val.lower(),))
                    c2 = conn.execute("DELETE FROM noise_rules WHERE pattern_type = 'sender_email' AND pattern_value = ?", (val,))
                    deleted_count = c1.rowcount + c2.rowcount
                elif pattern_type in ('sender_domain', 'sender_email'):
                    c = conn.execute("DELETE FROM noise_rules WHERE pattern_type = ? AND pattern_value = ?", (pattern_type, val.lower() if pattern_type == 'sender_domain' else val))
                    deleted_count = c.rowcount
                else:
                    errors.append(f"不支持的 pattern_type: {pattern_type}")

                if deleted_count > 0:
                    conn.commit()
                    data["deleted"] = True
                else:
                    conn.rollback()
                    if not errors:
                        errors.append(f"未找到匹配的降噪过滤规则值：{val}")
            except Exception:
                conn.rollback()
                raise
            finally:
                conn.close()
    except Exception as exc:
        errors.append(f"mail_noise_rule_delete: {exc}")
    return _emit_envelope('mail_noise_rule_delete', data, errors)


def cli_mail_view_original(account, uid, max_bytes=50000):
    errors = []
    data = {"subject": "", "sender": "", "email_date": "", "content": "", "is_truncated": False}
    try:
        if not account or not uid:
            errors.append("必须提供 account 和 uid")
        else:
            try:
                max_bytes_int = max(1, min(50000, int(max_bytes)))
            except (ValueError, TypeError):
                max_bytes_int = 50000

            conn = _connect_ro(DB_PATH)
            try:
                cur = conn.cursor()
                cur.execute(
                    "SELECT es.subject, es.sender, es.email_date, eb.raw_html, eb.plain_text "
                    "FROM email_bodies eb "
                    "JOIN email_summaries es ON eb.account_name=es.account_name AND eb.uid=es.uid "
                    "WHERE eb.account_name=? AND eb.uid=?",
                    (account, uid)
                )
                row = cur.fetchone()
                if not row:
                    errors.append(f"找不到该邮件原文 (Account: {account}, UID: {uid})")
                else:
                    subject, sender, date, raw_html, plain_text = row
                    content = raw_html if raw_html else (plain_text or "")
                    content_truncated, is_truncated = _truncate_utf8(content, max_bytes_int)
                    data = {
                        "subject": subject or "",
                        "sender": sender or "",
                        "email_date": date or "",
                        "content": content_truncated,
                        "is_truncated": is_truncated,
                    }
            finally:
                conn.close()
    except Exception as exc:
        errors.append(f"mail_view_original: {exc}")
    return _emit_envelope('mail_view_original', data, errors)


def _parse_cmd_args(argv, allowed_options, positional_options=()):
    """严格解析命令行选项（契约 §3）。
    allowed_options: {'flag_name': {'type': 'int'|'str', 'default': val, 'min': 1}}
    返回: (parsed_dict, positional_list, errors_list)
    """
    parsed = {k: v.get('default') for k, v in allowed_options.items()}
    positional = []
    errors = []
    i = 0
    seen_options = set()

    while i < len(argv):
        arg = argv[i]
        if arg.startswith('--'):
            opt_name = arg[2:]
            if opt_name not in allowed_options:
                errors.append(f"未知选项：'{arg}'")
                i += 1
                continue
            if opt_name in seen_options:
                errors.append(f"重复选项：'{arg}'")
            seen_options.add(opt_name)
            spec = allowed_options[opt_name]
            if i + 1 >= len(argv) or argv[i + 1].startswith('--'):
                errors.append(f"选项 '{arg}' 缺少参数值")
                i += 1
                continue
            raw_val = argv[i + 1]
            i += 2
            if spec.get('type') == 'int':
                try:
                    val = int(raw_val)
                    min_v = spec.get('min')
                    if min_v is not None and val < min_v:
                        errors.append(f"选项 '{arg}' 参数值非法：'{raw_val}'，必须 >= {min_v}")
                    else:
                        parsed[opt_name] = val
                except ValueError:
                    errors.append(f"选项 '{arg}' 参数值非法：'{raw_val}'，必须为整数")
            else:
                parsed[opt_name] = raw_val
        else:
            positional.append(arg)
            i += 1

    if len(positional) > len(positional_options):
        errors.append('多余位置参数：' + ' '.join(positional[len(positional_options):]))
    for name, raw_val in zip(positional_options, positional):
        if name in seen_options:
            errors.append(f"位置参数与选项 '--{name}' 冲突")
            continue
        spec = allowed_options[name]
        if spec.get('type') == 'int':
            try:
                val = int(raw_val)
                if spec.get('min') is not None and val < spec['min']:
                    errors.append(f"{name} 参数非法：'{raw_val}'，必须 >= {spec['min']}")
                else:
                    parsed[name] = val
            except ValueError:
                errors.append(f"{name} 参数非法：'{raw_val}'，必须为整数")
        else:
            parsed[name] = raw_val
    return parsed, positional, errors


def _parse_flag(argv, flag):
    """兼容旧调用：解析 --flag value；返回 (值, 去除该 flag 后的位置参数列表)。"""
    positional = []
    value = None
    i = 0
    while i < len(argv):
        if argv[i] == flag and i + 1 < len(argv):
            value = argv[i + 1]
            i += 2
            continue
        positional.append(argv[i])
        i += 1
    return value, positional


def main():
    if len(sys.argv) < 2:
        interactive_menu()
        sys.exit(0)
    cmd = sys.argv[1]
    if cmd == 'menu':
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
    elif cmd == 'download_bank_bills':
        months = int(sys.argv[2]) if len(sys.argv) > 2 and sys.argv[2].isdigit() else 3
        download_recent_bank_emails(months)

    elif cmd == 'sync3m':
        months = int(sys.argv[2]) if len(sys.argv) > 2 and sys.argv[2].isdigit() else 3
        sync_recent_bank_emails(months)

    elif cmd == 'reparse_cached':
        if len(sys.argv) < 4:
            print('用法：reparse_cached <account_name> <uid>')
            sys.exit(1)
        result = reparse_cached_statement(sys.argv[2], sys.argv[3], context=resolve_parser_paths())
        print(json.dumps(result, ensure_ascii=False))

    elif cmd == 'unpaid':
        show_unpaid_statements()
    elif cmd == 'mark_paid':
        opts, _, errs = _parse_cmd_args(sys.argv[2:], {'statement-id': {'type':'int','min':1}})
        if opts['statement-id'] is None:
            errs.append('必须提供 --statement-id')
        if errs:
            _emit_envelope('mark_paid', {'updated':False}, errs)
            sys.exit(1)
        if cli_mark_paid_by_id(opts['statement-id'])['status'] != 'ok':
            sys.exit(1)

    elif cmd == 'identity_preview':
        opts, _, errs = _parse_cmd_args(sys.argv[2:], {
            'statement-id': {'type': 'int', 'min': 1},
            'account': {'type': 'str'},
            'transport-kind': {'type': 'str', 'default': 'pop3_uidl'},
            'transport-uid': {'type': 'str'},
            'bank': {'type': 'str'},
            'eml': {'type': 'str'}
        })
        for req in ('statement-id', 'account', 'transport-uid', 'bank'):
            if not opts.get(req):
                errs.append(f'必须提供 --{req}')
        if errs:
            _emit_envelope('identity_preview', {}, errs)
            sys.exit(1)
        res = cli_identity_preview(
            statement_id=opts['statement-id'], account=opts['account'],
            transport_kind=opts.get('transport-kind') or 'pop3_uidl',
            transport_uid=opts['transport-uid'], bank=opts['bank'],
            eml_path=opts.get('eml')
        )
        if res.get('status') != 'ok':
            sys.exit(1)

    elif cmd == 'identity_confirm':
        opts, _, errs = _parse_cmd_args(sys.argv[2:], {
            'plan': {'type': 'str'},
            'plan-hash': {'type': 'str'},
            'actor': {'type': 'str'},
            'reason': {'type': 'str'}
        })
        for req in ('plan', 'plan-hash', 'actor', 'reason'):
            if not opts.get(req):
                errs.append(f'必须提供 --{req}')
        if errs:
            _emit_envelope('identity_confirm', {}, errs)
            sys.exit(1)
        res = cli_identity_confirm(
            plan_path=opts['plan'], plan_hash=opts['plan-hash'],
            actor=opts['actor'], reason=opts['reason']
        )
        if res.get('status') != 'ok':
            sys.exit(1)

    elif cmd == 'identity_status':
        opts, _, errs = _parse_cmd_args(sys.argv[2:], {
            'statement-id': {'type': 'int', 'min': 1},
            'account': {'type': 'str'},
            'transport-uid': {'type': 'str'}
        })
        if errs:
            _emit_envelope('identity_status', {}, errs)
            sys.exit(1)
        res = cli_identity_status(
            statement_id=opts.get('statement-id'),
            account=opts.get('account'),
            transport_uid=opts.get('transport-uid')
        )
        if res.get('status') != 'ok':
            sys.exit(1)

    elif cmd == 'quality_events':
        opts, _, errs = _parse_cmd_args(sys.argv[2:], {
            'limit': {'type': 'int', 'default': 100, 'min': 1},
            'kind': {'type': 'str'}
        }, ('limit',))
        if opts.get('kind') not in (None, 'quality'):
            errs.append('kind must be quality')
        if errs:
            _emit_envelope('quality_events', {'events': [], 'count': 0}, errs)
            sys.exit(1)
        res = cli_quality_events(opts['limit'], kind=opts.get('kind'))
        if res.get('status') != 'ok':
            sys.exit(1)

    elif cmd == 'quality_ack':
        opts, _, errs = _parse_cmd_args(sys.argv[2:], {
            'event-id': {'type': 'str'},
            'todo-id': {'type': 'str'},
            'revision': {'type': 'int', 'min': 1}
        }, ('event-id', 'todo-id'))
        if not opts['event-id'] or not opts['todo-id'] or not opts.get('revision'):
            errs.append('用法：quality_ack --event-id <ID> --todo-id <ID> --revision <N>')
        if errs:
            _emit_envelope('quality_ack', {'acked': False}, errs)
            sys.exit(1)
        res = cli_quality_ack(opts['event-id'], opts['todo-id'], revision=opts.get('revision'))
        if res.get('status') != 'ok':
            sys.exit(1)

    elif cmd == 'billing_snapshot':
        opts, _, errs = _parse_cmd_args(sys.argv[2:], {
            'months': {'type': 'int', 'default': 1, 'min': 1},
            'detail-limit': {'type': 'int', 'default': 20, 'min': 0},
            'as-of': {'type': 'str'}
        }, ('months', 'detail-limit'))
        if errs:
            _emit_envelope('billing_snapshot', {}, errs)
            sys.exit(1)
        res = cli_billing_snapshot(opts['months'], opts['detail-limit'], opts.get('as-of'))
        if res.get('status') != 'ok':
            sys.exit(1)

    elif cmd == 'billing_trends':
        opts, _, errs = _parse_cmd_args(sys.argv[2:], {
            'months': {'type': 'int', 'default': 3, 'min': 1},
            'merchant': {'type': 'str'}, 'account': {'type': 'str'}
        }, ('months',))
        if errs:
            _emit_envelope('billing_trends', {}, errs)
            sys.exit(1)
        res = cli_billing_trends(opts['months'], opts['merchant'], opts['account'])
        if res.get('status') != 'ok':
            sys.exit(1)

    elif cmd == 'due_soon_bills':
        months = int(sys.argv[2]) if len(sys.argv) > 2 and sys.argv[2].isdigit() else 3
        days = int(sys.argv[3]) if len(sys.argv) > 3 and sys.argv[3].isdigit() else 7
        due_soon_bank_bills(months, days)
    elif cmd == 'fetch_summaries':
        months = int(sys.argv[2]) if len(sys.argv) > 2 and sys.argv[2].isdigit() else 1
        fetch_recent_emails_and_summarize(months)
    elif cmd == 'fetch_only':
        months = int(sys.argv[2]) if len(sys.argv) > 2 and sys.argv[2].isdigit() else 1
        fetch_recent_emails_and_summarize(months, llm_enabled=False)
    elif cmd == 'enrich_pending':
        limit = int(sys.argv[2]) if len(sys.argv) > 2 and sys.argv[2].isdigit() else 30
        reprocess_by_status(status='pending', limit=limit)
    elif cmd in ('mail_headers','mail_missed'):
        opts, _, errs = _parse_cmd_args(sys.argv[2:], {'limit': {'type': 'int', 'default':15,'min':1}})
        if errs:
            _emit_envelope(cmd, {}, errs)
            sys.exit(1)
        res = (cli_mail_headers if cmd == 'mail_headers' else cli_mail_missed)(opts['limit'])
        if res['status'] != 'ok':
            sys.exit(1)
    elif cmd == 'validate_bank_bills':
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

    elif cmd == 'mail_stats':
        res = cli_mail_stats()
        if res.get('status') != 'ok':
            sys.exit(1)

    elif cmd == 'mail_search':
        opts, _, errs = _parse_cmd_args(sys.argv[2:], {
            'keyword': {'type': 'str'},
            'limit': {'type': 'int', 'default': 20, 'min': 1},
            'account': {'type': 'str'},
            'offset': {'type': 'int', 'default': 0, 'min': 0}
        }, ('keyword',))
        if errs:
            _emit_envelope('mail_search', {'results': [], 'total': 0}, errs)
            sys.exit(1)
        res = cli_mail_search(opts['keyword'], opts.get('limit', 20), opts.get('account'), opts.get('offset', 0))
        if res.get('status') != 'ok':
            sys.exit(1)

    elif cmd == 'mail_summary_get':
        opts, _, errs = _parse_cmd_args(sys.argv[2:], {
            'id': {'type': 'int', 'min': 1}
        }, ('id',))
        if errs:
            _emit_envelope('mail_summary_get', {'summary': None}, errs)
            sys.exit(1)
        res = cli_mail_summary_get(opts['id'])
        if res.get('status') != 'ok':
            sys.exit(1)

    elif cmd == 'mail_mark_processed':
        opts, _, errs = _parse_cmd_args(sys.argv[2:], {
            'id': {'type': 'int', 'min': 1}
        }, ('id',))
        if errs:
            _emit_envelope('mail_mark_processed', {'updated': False}, errs)
            sys.exit(1)
        res = cli_mail_mark_processed(opts['id'])
        if res.get('status') != 'ok':
            sys.exit(1)

    elif cmd == 'mail_noise_rules_list':
        res = cli_mail_noise_rules_list()
        if res.get('status') != 'ok':
            sys.exit(1)

    elif cmd == 'mail_noise_rule_add':
        opts, _, errs = _parse_cmd_args(sys.argv[2:], {
            'type': {'type': 'str'},
            'value': {'type': 'str'},
            'summary-id': {'type': 'int', 'min': 1}
        }, ('type', 'value'))
        if errs:
            _emit_envelope('mail_noise_rule_add', {'added': False}, errs)
            sys.exit(1)
        res = cli_mail_noise_rule_add(opts['type'], opts['value'], opts.get('summary-id'))
        if res.get('status') != 'ok':
            sys.exit(1)

    elif cmd == 'mail_noise_rule_delete':
        opts, _, errs = _parse_cmd_args(sys.argv[2:], {
            'value': {'type': 'str'},
            'type': {'type': 'str'}
        }, ('value',))
        if errs:
            _emit_envelope('mail_noise_rule_delete', {'deleted': False}, errs)
            sys.exit(1)
        res = cli_mail_noise_rule_delete(opts['value'], opts.get('type'))
        if res.get('status') != 'ok':
            sys.exit(1)

    elif cmd == 'mail_view_original':
        opts, _, errs = _parse_cmd_args(sys.argv[2:], {
            'account': {'type': 'str'},
            'uid': {'type': 'str'},
            'max-bytes': {'type': 'int', 'default': 50000, 'min': 1}
        }, ('account', 'uid'))
        if errs:
            _emit_envelope('mail_view_original', {}, errs)
            sys.exit(1)
        res = cli_mail_view_original(opts['account'], opts['uid'], opts.get('max-bytes', 50000))
        if res.get('status') != 'ok':
            sys.exit(1)

    elif cmd == 'reprocess_failed':
        reprocess_failed_emails()
    else:
        _emit_envelope(cmd, {}, [f'不支持的命令：{cmd}'])
        sys.exit(1)

if __name__ == '__main__':
    main()
