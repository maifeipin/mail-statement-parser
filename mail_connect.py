#!/usr/bin/env python3
"""邮件连接与管理模块。

从 mail_client.py 抽取的独立叶子模块，包含：连接工厂（POP3/SMTP/IMAP）、
Graph API 通道、退避重试、邮件发送。

约束：本模块禁止顶层 import mail_client（避免循环依赖）。依赖 mail_parse
的 html_to_text 与 oauth_helper（均为独立模块，安全引用）。
"""

import poplib
import smtplib
import imaplib
import base64
import urllib.request
import urllib.error
import urllib.parse
import json
import time
import random
from datetime import datetime, timezone

from mail_parse import html_to_text


class MailAuthError(Exception):
    """自定义邮件鉴权异常，遇到此类异常应当立即停止连接重试，防锁号"""
    pass


def retry_with_backoff(func, *args, max_retries=3, initial_delay=2, backoff_factor=2, **kwargs):
    """带指数退避和抖动的连接重试包装器，不重试 MailAuthError"""
    delay = initial_delay
    last_err = None
    for attempt in range(1, max_retries + 1):
        try:
            return func(*args, **kwargs)
        except MailAuthError as e:
            # 鉴权错误直接向外抛，不重试
            raise e
        except Exception as e:
            last_err = e
            # 过滤不需要重试的致命本地/格式错误
            err_name = e.__class__.__name__
            if err_name in ("ValueError", "TypeError", "KeyError", "NameError", "AttributeError"):
                raise e
            print(f"⚠️ 连接暂时失败 (尝试 {attempt}/{max_retries}): {e}。将在 {delay:.1f} 秒后重试...")
            time.sleep(delay + random.uniform(0, 0.5))
            delay *= backoff_factor
    if last_err is not None:
        raise last_err
    raise ValueError("max_retries must be greater than 0")


def is_graph_api(email_config):
    """判断是否应该走 Microsoft Graph API 通道 (目前默认针对 Outlook 个人账号)。"""
    if email_config.get('api_mode') == 'pop3':
        return False
    return email_config.get('provider') == 'outlook'


def connect_pop3(email_config):
    """POP3 统一连接工厂。支持 Basic (账号密码) 以及 OAuth2 (XOAUTH2) 双重通道。"""
    host = email_config['pop3']['host']
    port = email_config['pop3']['port']
    account = email_config['account']
    auth_type = email_config.get('auth_type', 'basic')

    def _connect():
        mail = poplib.POP3_SSL(host, port)
        try:
            if auth_type == 'oauth2':
                from oauth_helper import get_valid_oauth_token
                try:
                    access_token = get_valid_oauth_token(email_config)
                except ValueError as val_err:
                    raise MailAuthError(f"OAuth2 Token 刷新失败: {val_err}") from val_err
                auth_str = f"user={account}\x01auth=Bearer {access_token}\x01\x01"
                auth_b64 = base64.b64encode(auth_str.encode('utf-8')).decode('utf-8')
                mail._putline(b'AUTH XOAUTH2')
                resp = mail._getresp()
                if resp.startswith(b'+'):
                    mail._putline(auth_b64.encode('utf-8'))
                    resp = mail._getresp()
                if not resp.startswith(b'+OK'):
                    raise poplib.error_proto(resp)
            else:
                mail.user(account)
                mail.pass_(email_config['authCode'])
            return mail
        except poplib.error_proto as pe:
            err_msg = str(pe).lower()
            if any(word in err_msg for word in ("auth", "login", "user", "pass", "cred", "fail", "invalid")):
                raise MailAuthError(f"POP3 鉴权失败: {pe}") from pe
            raise pe
        except Exception as e:
            try:
                mail.quit()
            except Exception:
                pass
            raise e

    return retry_with_backoff(_connect)


def connect_smtp(email_config):
    """SMTP 统一连接工厂。支持 Basic 登录及 XOAUTH2。"""
    host = email_config['smtp']['host']
    port = email_config['smtp']['port']
    account = email_config['account']
    auth_type = email_config.get('auth_type', 'basic')
    secure = email_config['smtp'].get('secure', True)

    def _connect():
        if secure:
            server = smtplib.SMTP_SSL(host, port)
        else:
            server = smtplib.SMTP(host, port)
            server.ehlo()
            server.starttls()
            server.ehlo()

        try:
            if auth_type == 'oauth2':
                from oauth_helper import get_valid_oauth_token
                try:
                    access_token = get_valid_oauth_token(email_config)
                except ValueError as val_err:
                    raise MailAuthError(f"OAuth2 Token 刷新失败: {val_err}") from val_err
                auth_str = f"user={account}\x01auth=Bearer {access_token}\x01\x01"
                server.auth('XOAUTH2', lambda response=None: auth_str)
            else:
                server.login(account, email_config['authCode'])
            return server
        except smtplib.SMTPAuthenticationError as sae:
            raise MailAuthError(
                f"SMTP 鉴权失败 (代码 {sae.smtp_code}): {sae.smtp_error.decode('utf-8', errors='ignore') if isinstance(sae.smtp_error, bytes) else sae.smtp_error}"
            ) from sae
        except MailAuthError:
            try:
                server.quit()
            except Exception:
                pass
            raise
        except Exception as e:
            try:
                server.quit()
            except Exception:
                pass
            raise e

    return retry_with_backoff(_connect)


def connect_imap(email_config):
    """IMAP 统一连接工厂。支持 Basic 登录及 XOAUTH2。"""
    host = email_config['imap']['host']
    port = email_config['imap']['port']
    account = email_config['account']
    auth_type = email_config.get('auth_type', 'basic')

    def _connect():
        mail = imaplib.IMAP4_SSL(host, port)
        try:
            if auth_type == 'oauth2':
                from oauth_helper import get_valid_oauth_token
                try:
                    access_token = get_valid_oauth_token(email_config)
                except ValueError as val_err:
                    raise MailAuthError(f"OAuth2 Token 刷新失败: {val_err}") from val_err
                mail.authenticate('XOAUTH2', lambda x: f"user={account}\x01auth=Bearer {access_token}\x01\x01")
            else:
                mail.login(account, email_config['authCode'])
            return mail
        except imaplib.IMAP4.error as ie:
            err_msg = str(ie).lower()
            if any(word in err_msg for word in ("auth", "login", "cred", "fail", "invalid", "password")):
                raise MailAuthError(f"IMAP 鉴权失败: {ie}") from ie
            raise ie
        except MailAuthError:
            try:
                mail.logout()
            except Exception:
                pass
            raise
        except Exception as e:
            try:
                mail.logout()
            except Exception:
                pass
            raise e

    return retry_with_backoff(_connect)


def _graph_api_request(url, token, method="GET", json_data=None):
    headers = {
        'Authorization': f'Bearer {token}',
        'Accept': 'application/json'
    }
    if json_data is not None:
        headers['Content-Type'] = 'application/json'
        data = json.dumps(json_data).encode('utf-8')
    else:
        data = None

    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    max_retries = 3

    for attempt in range(max_retries):
        try:
            with urllib.request.urlopen(req, timeout=30) as response:
                if response.status == 204:
                    return None
                resp_body = response.read().decode('utf-8')
                return json.loads(resp_body) if resp_body else None
        except urllib.error.HTTPError as e:
            if e.code == 429:
                retry_after = e.headers.get('Retry-After', 5)
                try:
                    retry_after = int(retry_after)
                except ValueError:
                    retry_after = 5
                if attempt < max_retries - 1:
                    print(f"⚠️ Graph API 限流 (429 Too Many Requests)，等待 {retry_after} 秒后重试...")
                    time.sleep(retry_after)
                    continue
            elif 500 <= e.code < 600:
                # 5xx 为瞬时服务器错误，退避重试而非立即判定为鉴权失败
                if attempt < max_retries - 1:
                    wait = 2 ** attempt
                    print(f"⚠️ Graph API 服务器错误 ({e.code})，{wait} 秒后重试...")
                    time.sleep(wait)
                    continue
            err_body = e.read().decode('utf-8', errors='ignore')
            raise MailAuthError(f"Graph API Error {e.code}: {err_body}") from e
        except Exception as e:
            if attempt < max_retries - 1:
                time.sleep(2 ** attempt)
                continue
            raise e
    raise MailAuthError("Graph API Error: Max retries exceeded.")


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


def fetch_summaries_graph(email_config, months=1):
    from oauth_helper import get_valid_oauth_token

    token = get_valid_oauth_token(email_config)
    cutoff_date = _month_subtract(datetime.now(timezone.utc), months).strftime("%Y-%m-%dT00:00:00Z")

    base_url = "https://graph.microsoft.com/v1.0/me/messages"
    params = {
        "$filter": f"receivedDateTime ge {cutoff_date}",
        "$select": "id,subject,from,receivedDateTime,body",
        "$top": 100,
        "$orderby": "receivedDateTime desc"
    }
    url = f"{base_url}?{urllib.parse.urlencode(params)}"

    emails = []
    while url:
        resp = _graph_api_request(url, token)
        if not resp or 'value' not in resp:
            break

        for msg in resp['value']:
            try:
                uid = msg['id']
                from_addr = msg.get('from', {}).get('emailAddress', {}).get('address', '')
                from_name = msg.get('from', {}).get('emailAddress', {}).get('name', '')
                sender = f"{from_name} <{from_addr}>" if from_name else from_addr
                subject = msg.get('subject', '')

                dt_str = msg.get('receivedDateTime', '')
                if dt_str.endswith('Z'):
                    dt_str = dt_str[:-1] + '+00:00'
                dt_obj = datetime.fromisoformat(dt_str)
                email_date = dt_obj.strftime("%a, %d %b %Y %H:%M:%S %z")

                body_content = msg.get('body', {}).get('content', '')
                if msg.get('body', {}).get('contentType') == 'html':
                    body_text = html_to_text(body_content)
                else:
                    body_text = body_content

                emails.append({
                    "uid": uid,
                    "subject": subject,
                    "sender": sender,
                    "email_date": email_date,
                    "raw_date": email_date,
                    "body": body_text,
                    "html": body_content if msg.get('body', {}).get('contentType') == 'html' else ""
                })
            except Exception as e:
                print(f"⚠️ 跳过解析错误的 Graph 邮件: {e}")

        url = resp.get('@odata.nextLink')
    return emails


def send_email_graph(email_config, to_email, subject, content):
    from oauth_helper import get_valid_oauth_token
    token = get_valid_oauth_token(email_config)
    url = "https://graph.microsoft.com/v1.0/me/sendMail"

    payload = {
        "message": {
            "subject": subject,
            "body": {
                "contentType": "Text",
                "content": content
            },
            "toRecipients": [
                {
                    "emailAddress": {
                        "address": to_email
                    }
                }
            ]
        },
        "saveToSentItems": True
    }

    _graph_api_request(url, token, method="POST", json_data=payload)
