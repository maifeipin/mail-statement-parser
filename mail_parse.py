#!/usr/bin/env python3
"""邮件解析模块（纯标准库叶子）。

从 mail_client.py 抽取，包含：HTML 表格解析、MIME 解码、HTML 转文本、
邮件正文提取、邮件时间解析等。仅依赖 Python 标准库，禁止 import mail_client。
"""

import re
from html.parser import HTMLParser
from email.header import decode_header
from email.utils import parsedate_to_datetime
from datetime import timezone


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


def decode_mime(s):
    if not s:
        return ''
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


def html_to_text(html):
    """将 HTML 转换为纯文本"""
    if not html:
        return ''

    # 移除 script 和 style
    html = re.sub(r'<script[^>]*>.*?</script>', '', html, flags=re.DOTALL | re.IGNORECASE)
    html = re.sub(r'<style[^>]*>.*?</style>', '', html, flags=re.DOTALL | re.IGNORECASE)

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
    except Exception:
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
            except Exception:
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
        except Exception:
            pass

    # 如果有 HTML，转换为文本并提取表格
    if result['html']:
        result['plain'] = html_to_text(result['html']) or result['plain']
        result['tables'] = parse_html_tables(result['html'])
        result['markdown'] = tables_to_markdown(result['tables'])

    return result


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
