#!/usr/bin/env python3
"""邮件同步与提炼模块（从 mail_client.py 抽取，满足单文件 <1500 行约束）。

包含：
- fetch_recent_emails_and_summarize: 双通道收网（账单正则 + LLM 摘要）。llm_enabled=False 时
  仅入库正文 + 标 status='pending'，留待 enrich 阶段提炼（分层：拉取不依赖 LLM）。
- reprocess_by_status: 重试指定状态(failed/pending)的邮件，复用 _retry_llm_summary 的 429 退避。
- _retry_llm_summary: 带 429 退避的 LLM 摘要（修正原 mail_client 中 time 未导入的潜在 NameError）。

约束：本模块禁止顶层 import mail_client（避免循环依赖）；load_accounts/DB_PATH 在函数内按需延迟导入。
"""

import json
import os
import sys
import email
import time
from datetime import datetime, timezone, timedelta

from statement_db import (
    init_db,
    uid_exists,
    get_email_summary_status,
    check_summary_uid_match,
    upsert_email_summary,
    upsert_email_body,
)
from statement_models import EmailSummaryRecord
from mail_connect import (
    is_graph_api,
    connect_pop3,
    fetch_summaries_graph,
    _month_subtract,
)
from mail_parse import (
    decode_mime,
    _to_text,
    extract_email_content,
    _parse_email_datetime,
)
from mail_llm import (
    extract_email_summary_by_llm,
    is_noise_email,
)
from statement_parser import (
    load_rule_files,
    identify_rule,
    validate_and_save_email_message,
)


def fetch_recent_emails_and_summarize(months=1, llm_enabled=True):
    """双通道收网网关命令：批量抓取解析正则账单，并对其他邮件进行 LLM 结构化摘要提炼。

    llm_enabled=False（fetch_only）：通道 A/B 保持（无 LLM），通道 C 仅 upsert_email_body
    + 插入 status='pending' 的摘要记录，不调 LLM；已入库(任意 status)的邮件一律跳过，只入全新邮件。
    """
    from mail_client import DB_PATH, load_accounts  # 延迟导入避免循环依赖

    init_db(DB_PATH)
    rules = load_rule_files()

    cutoff = _month_subtract(datetime.now(timezone.utc), int(months))
    mode_label = '拉取+LLM提炼' if llm_enabled else '仅拉取(无LLM,标pending)'
    print(f'🚀 正在执行双通道同步指令 [{mode_label}]：获取最近 {months} 个月邮件...')
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

                is_dup_bill = uid_exists(DB_PATH, uid, account_name)
                status, retry_cnt = get_email_summary_status(DB_PATH, account_name, uid)
                is_dup_summary = (status in ('processed', 'skipped', 'noise') or (status == 'failed' and retry_cnt >= 3))
                if not llm_enabled:
                    # fetch-only: pending 与 failed(retry<3) 也跳过（正文已存或交由 enrich/reprocess），只入全新邮件
                    is_dup_summary = is_dup_summary or status in ('pending', 'failed')
                # 防 POP3 UID 回收: 取邮件标题头校验 subject 是否匹配
                if is_dup_summary and status == 'processed':
                    # 用 TOP 只取标题头(不拉正文), 确认 uid 是否对应同一封
                    _check_subj = None
                    if is_graph:
                        _check_subj = msg_obj.get('subject', '')
                    elif mail:
                        try:
                            _resp, _lines, _size = mail.top(int(uid), 0)
                            _hdr = email.message_from_bytes(b"\r\n".join(_lines))
                            _check_subj = decode_mime(_hdr.get('Subject', ''))
                        except Exception:
                            pass
                    if _check_subj:
                        if not check_summary_uid_match(DB_PATH, account_name, uid, _check_subj):
                            is_dup_summary = False
                            print(f'  ⚠️ UID={uid} 已被回收 (新主题={_check_subj[:30]}), 重新处理')

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
                        # Graph 正文同样落库，供 enrich 阶段离线读取（真分层）
                        upsert_email_body(DB_PATH, account_name, uid,
                                          raw_html=msg_obj.get('html'),
                                          plain_text=body_text,
                                          markdown_tables="")
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
                        upsert_email_body(DB_PATH, account_name, uid,
                                          raw_html=content.get('html'),
                                          plain_text=body_text,
                                          markdown_tables=content.get('markdown'))

                    # 双通道分流 A: 账单正则识别
                    rule, score = identify_rule(subj, frm, body_text, rules)
                    if rule:
                        print(f'💳 [账单通道] 命中规则 {rule.get("rule_id")} (UID={uid}) 主题: {subj[:30]}')
                        validate_and_save_email_message(msg, uid, rules=rules, account_name=account_name)
                        
                        # 补充插入 email_summaries，确保账单能在 Meilisearch 搜索引擎和前端界面中展示
                        rec = EmailSummaryRecord(
                            account_name=account_name,
                            uid=uid,
                            sender=frm,
                            subject=subj,
                            email_date=date_str,
                            category='BILL',
                            importance='high',
                            summary=f"💳 信用卡账单已解析 ({rule.get('bank_code')})",
                            actions_json="[]",
                            status="processed",
                            retry_count=0,
                            processed_at=datetime.now(timezone.utc).isoformat()
                        )
                        upsert_email_summary(DB_PATH, rec)
                        
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

                    # 双通道分流 C: LLM 通用提炼（或 fetch-only 时仅标 pending）
                    if not llm_enabled:
                        rec = EmailSummaryRecord(
                            account_name=account_name,
                            uid=uid,
                            sender=frm,
                            subject=subj,
                            email_date=date_str,
                            summary="",
                            status="pending",
                            retry_count=0,
                        )
                        upsert_email_summary(DB_PATH, rec)
                        new_summaries += 1
                        continue

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

    _emit_high_imp_push(high_importance_summaries)


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


def _enrich_one(db_path, r, body_text):
    """对单封已就绪正文的邮件做 降噪判定 + LLM 摘要 + 落库。
    返回 (state, rec)：state ∈ {'noise','ok','fail'}；ok 时 rec.importance 可能为 high。
    不捕获异常（由调用方 try/except 兜底）。"""
    acct_name = r["account_name"]; uid = r["uid"]
    subj = r["subject"] or ""; frm = r["sender"] or ""
    date_str = r["email_date"] or ""

    is_noise, cat, imp = is_noise_email(frm, subj, body_text, db_path)
    if is_noise:
        rec = EmailSummaryRecord(
            account_name=acct_name, uid=uid, sender=frm, subject=subj,
            email_date=date_str, category=cat, importance=imp,
            summary="[自动降噪拦截] 发件人或主题命中过滤规则", actions_json="[]",
            status="noise", retry_count=r["retry_count"],
            processed_at=datetime.now(timezone.utc).isoformat(),
        )
        upsert_email_summary(db_path, rec)
        return 'noise', rec

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
        upsert_email_summary(db_path, rec)
        return 'ok', rec

    new_retry = r["retry_count"] + 1
    rec = EmailSummaryRecord(
        account_name=acct_name, uid=uid, sender=frm, subject=subj,
        email_date=date_str, summary=f"[大模型分析失败]: {err}",
        status="failed", retry_count=new_retry,
    )
    upsert_email_summary(db_path, rec)
    return 'fail', rec


def _emit_high_imp_push(high_importance_summaries):
    """打印高优邮件卡片 + JSON_PUSH 区域，供 lite_agent 捕获推送。无则不输出。"""
    if not high_importance_summaries:
        return
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
            "deadline_raw": r.deadline_raw,
        }
        high_summaries_list.append(summary_dict)
        print(f"[{r.account_name}] [{r.category}] 重要度: {r.importance} | {r.subject}")
        print(f"   摘要: {r.summary}")
        if r.deadline:
            print(f"   截止时间: {r.deadline} (原文: {r.deadline_raw})")
    print("\n--- JSON_PUSH_START ---")
    print(json.dumps(high_summaries_list, ensure_ascii=False))
    print("--- JSON_PUSH_END ---")


def reprocess_by_status(db_path=None, status='failed', limit=None):
    """重试指定状态的邮件：重新连接邮箱拉取正文 + LLM 摘要。

    status='failed': 仅 retry_count<3（原有补跑语义）。
    status='pending': 处理 fetch_only 阶段入库的待提炼邮件（enrich）。
    limit: 限制单次处理量（防积压拖死超时）；None 表示不限。
    支持 POP3 与 Graph API 双通道，每封间隔 2s 防限流，
    异常路径也落库递增 retry_count 以避免无限重试。
    """
    import urllib.error, sqlite3
    from collections import defaultdict
    from statement_db import init_db as _init_db, upsert_email_summary as _upsert
    from mail_client import DB_PATH, load_accounts  # 延迟导入避免循环依赖

    if db_path is None:
        db_path = DB_PATH
    _init_db(db_path)

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()
    if status == 'failed':
        cur.execute(
            "SELECT account_name, uid, sender, subject, email_date, retry_count "
            "FROM email_summaries WHERE status='failed' AND retry_count < 3"
        )
    else:
        sql = ("SELECT account_name, uid, sender, subject, email_date, retry_count "
               "FROM email_summaries WHERE status=?")
        if limit:
            sql += " ORDER BY id ASC LIMIT ?"
            cur.execute(sql, (status, limit))
        else:
            cur.execute(sql, (status,))
    rows = cur.fetchall()
    conn.close()
    print(f"📋 待处理 {status} 邮件: {len(rows)} 封" + (f" (限量 {limit})" if limit else ""))

    total_ok = total_noise = total_fail = total_skip = 0
    high_importance_summaries = []

    if status == 'pending':
        # 真分层：直接读本地 email_bodies，不连邮箱；缺正文降级标 failed 交下轮 reprocess 重拉
        from statement_db import get_email_body
        for idx, r in enumerate(rows, 1):
            acct_name = r["account_name"]; uid = r["uid"]
            subj = r["subject"] or ""; frm = r["sender"] or ""
            date_str = r["email_date"] or ""
            uid_short = (uid[:20] + "...") if len(uid) > 23 else uid
            try:
                body_text = get_email_body(db_path, acct_name, uid)
                if not body_text:
                    rec = EmailSummaryRecord(account_name=acct_name, uid=uid, sender=frm, subject=subj,
                        email_date=date_str, summary="[enrich缺本地正文,转failed待重拉]",
                        status="failed", retry_count=r["retry_count"])
                    _upsert(db_path, rec)
                    total_fail += 1
                    print(f"  [{idx}/{len(rows)}] UID={uid_short} ⚠️ 缺本地正文->failed")
                    continue
                state, rec = _enrich_one(db_path, r, body_text)
                if state == 'ok':
                    total_ok += 1
                    if rec.importance == 'high':
                        high_importance_summaries.append(rec)
                    print(f"  [{idx}/{len(rows)}] UID={uid_short} ✅ {rec.category}/{rec.importance}: {(rec.summary or '')[:40]}")
                elif state == 'noise':
                    total_noise += 1
                    print(f"  [{idx}/{len(rows)}] UID={uid_short} 🔕 降噪")
                else:
                    total_fail += 1
                    print(f"  [{idx}/{len(rows)}] UID={uid_short} ❌ {rec.summary}")
                time.sleep(2)
            except Exception as e:
                print(f"  [{idx}/{len(rows)}] UID={uid_short} ❌ 处理异常: {e}")
                try:
                    new_retry = (r["retry_count"] or 0) + 1
                    rec = EmailSummaryRecord(account_name=acct_name, uid=uid, sender=frm, subject=subj,
                        email_date=date_str, summary=f"[处理异常]: {e}", status="failed", retry_count=new_retry)
                    _upsert(db_path, rec)
                except Exception:
                    pass
                total_fail += 1
                time.sleep(1)
        print(f"\n🏁 处理完成: ✅成功 {total_ok}, 🔕降噪 {total_noise}, ❌失败 {total_fail}, ⏭️跳过 {total_skip}")
        _emit_high_imp_push(high_importance_summaries)
        return

    by_acct = defaultdict(list)
    for r in rows:
        by_acct[r["account_name"]].append(r)

    accounts = load_accounts()
    acct_cfg = {a.get("account", "default"): a for a in accounts}

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
                        upsert_email_body(db_path, acct_name, uid,
                                          raw_html=ge.get("html", ""),
                                          plain_text=ge.get("body", ""),
                                          markdown_tables="")
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
                        upsert_email_body(db_path, acct_name, uid,
                                          raw_html=content.get("html"),
                                          plain_text=body_text,
                                          markdown_tables=content.get("markdown"))

                    if not body_text:
                        body_text = subj

                    state, rec = _enrich_one(db_path, r, body_text)
                    if state == 'ok':
                        total_ok += 1
                        if rec.importance == 'high':
                            high_importance_summaries.append(rec)
                        print(f"  [{idx}/{len(items)}] UID={uid_short} ✅ {rec.category}/{rec.importance}: {(rec.summary or '')[:40]}")
                    elif state == 'noise':
                        total_noise += 1
                        print(f"  [{idx}/{len(items)}] UID={uid_short} 🔕 降噪")
                    else:
                        total_fail += 1
                        print(f"  [{idx}/{len(items)}] UID={uid_short} ❌ {rec.summary}")
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

    print(f"\n🏁 处理完成: ✅成功 {total_ok}, 🔕降噪 {total_noise}, ❌失败 {total_fail}, ⏭️跳过 {total_skip}")

    _emit_high_imp_push(high_importance_summaries)


def reprocess_failed_emails(db_path=None):
    """向后兼容薄包装：补跑 failed 邮件。"""
    return reprocess_by_status(db_path=db_path, status='failed', limit=None)
