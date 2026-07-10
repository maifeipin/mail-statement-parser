#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""补跑脚本：重试 email_summaries 中 status=failed 且 retry_count<3 的邮件。

绕过 fetch_summaries 的"连续10封已处理提前退出"(旧 failed 扫不到),直接按 db 里的 failed uid 精准重试。
复用 mail_client 的拉取(POP3/Graph) + extract_email_content + is_noise_email + extract_email_summary_by_llm + upsert_email_summary。
带 429 退避(遇限流 sleep 30*attempt 秒重试) + POP3 断线重连 + 每封间隔 2s 避免触发限流。

用法: cd /home/liteagent/mail-statement-parser && python3 reprocess_failed.py
"""
import sys, os, time, json, sqlite3, email, urllib.error
from datetime import datetime, timezone
from collections import defaultdict

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import mail_client
from statement_models import EmailSummaryRecord

DB = mail_client.DB_PATH


def retry_llm_summary(subj, frm, body_text, date_str, max_attempts=4):
    """带 429 退避的 LLM 摘要。返回 (result_dict|None, error_str|None)。"""
    last = None
    for attempt in range(1, max_attempts + 1):
        try:
            return mail_client.extract_email_summary_by_llm(subj, frm, body_text, date_str), None
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
    mail_client.init_db(DB)
    conn = sqlite3.connect(DB)
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

    accounts = mail_client.load_accounts()
    acct_cfg = {a.get("account", "default"): a for a in accounts}

    total_ok = total_noise = total_fail = total_skip = 0

    for acct_name, items in by_acct.items():
        cfg = acct_cfg.get(acct_name)
        if not cfg:
            print(f"\n⚠️ 账户 {acct_name} 未配置,跳过 {len(items)} 封")
            total_skip += len(items)
            continue
        print(f"\n🔌 === 账户 {acct_name}: {len(items)} 封 ===")
        is_graph = mail_client.is_graph_api(cfg)

        msg_by_uid = {}
        mail = None
        if is_graph:
            try:
                gemails = mail_client.fetch_summaries_graph(cfg, months=2)
                for ge in gemails:
                    msg_by_uid[ge.get("uid")] = ge
                print(f"  Graph 拉取 {len(gemails)} 封(用于 uid 匹配)")
            except Exception as e:
                print(f"  ❌ Graph 拉取失败: {e},跳过该账户")
                total_fail += len(items)
                continue
        else:
            try:
                mail = mail_client.connect_pop3(cfg)
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
                                        mail = mail_client.connect_pop3(cfg)
                                    except Exception as _ce:
                                        raise Exception(f"POP3重连失败: {_ce}")
                                else:
                                    raise
                        msg = email.message_from_bytes(b"\r\n".join(raw_lines))
                        content = mail_client.extract_email_content(msg)
                        body_text = (content.get("plain", "") + "\n" + content.get("markdown", "")).strip()

                    if not body_text:
                        body_text = subj  # 兜底,至少有主题

                    is_noise, cat, imp = mail_client.is_noise_email(frm, subj, body_text)
                    if is_noise:
                        rec = EmailSummaryRecord(
                            account_name=acct_name, uid=uid, sender=frm, subject=subj,
                            email_date=date_str, category=cat, importance=imp,
                            summary="[自动降噪拦截] 发件人或主题命中过滤规则", actions_json="[]",
                            status="noise", retry_count=r["retry_count"],
                            processed_at=datetime.now(timezone.utc).isoformat(),
                        )
                        mail_client.upsert_email_summary(DB, rec)
                        total_noise += 1
                        print(f"  [{idx}/{len(items)}] UID={uid_short} 🔕 降噪")
                    else:
                        res, err = retry_llm_summary(subj, frm, body_text, date_str)
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
                            mail_client.upsert_email_summary(DB, rec)
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
                            mail_client.upsert_email_summary(DB, rec)
                            total_fail += 1
                            print(f"  [{idx}/{len(items)}] UID={uid_short} ❌ {err}")
                    time.sleep(2)
                except Exception as e:
                    print(f"  [{idx}/{len(items)}] UID={uid_short} ❌ 处理异常: {e}")
                    # 异常路径也落库递增 retry_count,避免 retry<3 条件下被无限重试(与内层 LLM 失败分支一致)
                    try:
                        new_retry = (r["retry_count"] or 0) + 1
                        rec = EmailSummaryRecord(
                            account_name=acct_name, uid=uid, sender=frm, subject=subj,
                            email_date=date_str, summary=f"[处理异常]: {e}",
                            status="failed", retry_count=new_retry,
                        )
                        mail_client.upsert_email_summary(DB, rec)
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


if __name__ == "__main__":
    main()
