#!/usr/bin/env python3
"""回填 email_bodies 表：为历史邮件补充正文内容。

按 email_summaries 中 id 不在 email_bodies 的记录，按账户分组后通过
POP3/Graph API 逐一拉取正文写入，不触发 LLM 摘要。

用法: python scripts/backfill_email_bodies.py
"""

import sys, os, time, sqlite3, poplib

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from mail_connect import connect_pop3, fetch_summaries_graph, is_graph_api
from mail_parse import extract_email_content
from statement_db import init_db, upsert_email_body

DB = os.path.expanduser("statements.db")


def main():
    init_db(DB)

    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()
    cur.execute(
        "SELECT es.id, es.account_name, es.uid "
        "FROM email_summaries es "
        "WHERE NOT EXISTS ("
        "  SELECT 1 FROM email_bodies eb "
        "  WHERE eb.account_name = es.account_name AND eb.uid = es.uid"
        ") "
        "ORDER BY es.account_name, es.id"
    )
    rows = cur.fetchall()
    conn.close()

    if not rows:
        print("✅ 无待回填邮件，email_bodies 已完整。")
        return

    print(f"📋 待回填: {len(rows)} 封")

    from collections import defaultdict
    by_acct = defaultdict(list)
    for r in rows:
        by_acct[r["account_name"]].append(r)

    # 加载账户配置
    import mail_client
    accounts = mail_client.load_accounts()
    acct_cfg = {a.get("account", "default"): a for a in accounts}

    total_ok = total_skip = total_fail = 0

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
                gemails = fetch_summaries_graph(cfg, months=3)
                for ge in gemails:
                    msg_by_uid[ge.get("uid")] = ge
                print(f"  Graph 拉取 {len(gemails)} 封(用于 uid 匹配)")
            except Exception as e:
                print(f"  ❌ Graph 拉取失败: {e},跳过该账户")
                total_skip += len(items)
                continue
        else:
            try:
                mail = connect_pop3(cfg)
            except Exception as e:
                print(f"  ❌ POP3 连接失败: {e},跳过该账户")
                total_skip += len(items)
                continue

        try:
            for idx, r in enumerate(items, 1):
                uid = r["uid"]
                uid_short = (uid[:20] + "...") if len(uid) > 23 else uid
                try:
                    if is_graph:
                        ge = msg_by_uid.get(uid)
                        if not ge:
                            print(f"  [{idx}/{len(items)}] UID={uid_short} Graph未匹配,跳过")
                            total_skip += 1
                            time.sleep(0.5)
                            continue
                        raw_html = ge.get("html", "")
                        plain_text = ge.get("body", "")
                        markdown = ""
                    else:
                        import email as em
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
                        msg = em.message_from_bytes(b"\r\n".join(raw_lines))
                        content = extract_email_content(msg)
                        raw_html = content.get("html", "")
                        plain_text = (content.get("plain", "") + "\n" + content.get("markdown", "")).strip()
                        markdown = content.get("markdown", "")

                    upsert_email_body(DB, acct_name, uid,
                                      raw_html=raw_html,
                                      plain_text=plain_text,
                                      markdown_tables=markdown)
                    total_ok += 1
                    print(f"  [{idx}/{len(items)}] UID={uid_short} ✅ {len(plain_text)} chars")
                except poplib.error_proto as pe:
                    print(f"  [{idx}/{len(items)}] UID={uid_short} ❌ POP3错误(可能已删除): {pe}")
                    total_fail += 1
                except Exception as e:
                    print(f"  [{idx}/{len(items)}] UID={uid_short} ❌ {e}")
                    total_fail += 1
                time.sleep(1)
        finally:
            if not is_graph and mail:
                try:
                    mail.quit()
                except Exception:
                    pass

    print(f"\n🏁 回填完成: ✅成功 {total_ok}, ❌失败 {total_fail}, ⏭️跳过 {total_skip}")

    conn = sqlite3.connect(DB)
    row = conn.execute("SELECT count(*), avg(content_len) FROM email_bodies").fetchone()
    conn.close()
    print(f"📊 email_bodies 总计: {row[0]} 条, 平均正文长度: {int(row[1] or 0)} chars")


if __name__ == "__main__":
    main()
