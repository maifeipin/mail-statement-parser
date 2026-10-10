#!/usr/bin/env python3
"""历史重复账单清洗脚本（默认 Dry-run 只读预览，--apply 才执行，单事务可回滚）。

清洗范围：
1. __orphan__ 归属回填：仅接受 Message-ID 硬证据（与非占位账户账单同 银行+Message-ID
   且唯一目标账户）。uid/email_summaries 证据不可靠（POP3 序号漂移正是历史事故根因），
   default 是旧归属占位，一律不接受；证据不足的保留 __orphan__，不揣测归属。
2. 同账户+银行+Message-ID 的重复账单（POP3 序号漂移导致的重复入库）：
   - 分组基于「回填后有效账户」，跨账户同封邮件先回填再合并；
   - 主记录选取：已还款优先 > 交易数多优先 > 最早 id；继承 is_paid / paid_at（还款证据链）；
   - 交易按「基准份」整份重挂（同内容多笔交易保留重数，不做逐行内容去重）；
   - 组内全部成员均为幽灵（非账单）时整组跳过合并，交给幽灵流程删除；
   - validation_runs 归属重挂到主 uid（审计历史不丢）；
   - 旧 ID → 新 ID 映射写入 cleanup_id_map。
3. 幽灵账单：主题不命中模板 subject_patterns 的非账单（如 CMB“每日信用管家”日报），
   删除账单及其交易；validation_runs 保留作审计。

安全性：
- Dry-run 以只读连接运行，不执行任何 schema 迁移、不建表、不写任何数据；
- --apply 时：先做迁移前的一致性备份（SQLite backup API），再迁移 schema，
  然后 BEGIN IMMEDIATE 取写锁，计划/执行/核对在同一事务同一快照内完成，
  核对不通过整体回滚（exit 2）。

用法：
  python3 scripts/clean_duplicate_statements.py --db statements.db            # Dry-run 预览（只读）
  python3 scripts/clean_duplicate_statements.py --db statements.db --apply    # 执行（一致性备份 + 单事务）
"""

import argparse
import json
import os
import sqlite3
import sys
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

RULES_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'rules')
PLACEHOLDER_ACCOUNTS = ('__orphan__', 'default')

# 交易定位：statement_id 锚点优先，旧库（未回填 statement_id）回退三元组
TXN_LOCATE = "(statement_id = ? OR (statement_id IS NULL AND account_name = ? AND uid = ? AND bank_code = ?))"


def _load_subject_patterns():
    """rule_id -> subject_patterns（小写），供幽灵账单门禁判定。"""
    import glob
    patterns = {}
    for path in sorted(glob.glob(os.path.join(RULES_DIR, '*.json'))):
        try:
            with open(path, 'r', encoding='utf-8') as f:
                rule = json.load(f)
            pats = rule.get('match_rules', {}).get('subject_patterns', [])
            if rule.get('rule_id'):
                patterns[rule['rule_id']] = [p.lower() for p in pats if p]
        except Exception as e:
            print(f'⚠️ 规则加载失败 {path}: {e}')
    return patterns


def _snapshot(cur):
    cur.execute("SELECT COUNT(*) FROM statements")
    stmts = cur.fetchone()[0]
    cur.execute("SELECT COUNT(*) FROM statement_transactions")
    txns = cur.fetchone()[0]
    cur.execute("SELECT COUNT(*) FROM statements WHERE is_paid = 1")
    paid = cur.fetchone()[0]
    return {'statements': stmts, 'transactions': txns, 'paid_statements': paid}


def _txn_count(cur, statement_id, account_name, uid, bank_code):
    cur.execute(
        f"SELECT COUNT(*) FROM statement_transactions WHERE {TXN_LOCATE}",
        (statement_id, account_name, uid, bank_code),
    )
    return cur.fetchone()[0]


def _txn_ids(cur, statement_id, account_name, uid, bank_code):
    """定位某份账单名下的全部交易 id。"""
    cur.execute(
        f"SELECT id FROM statement_transactions WHERE {TXN_LOCATE}",
        (statement_id, account_name, uid, bank_code),
    )
    return [r[0] for r in cur.fetchall()]


def plan_ghosts(cur, subject_patterns):
    """主题不命中模板 subject_patterns 的账单判为幽灵（非账单混入）。"""
    cur.execute("SELECT id, account_name, uid, bank_code, rule_id, subject FROM statements")
    ghosts = []
    for row in cur.fetchall():
        sid, account_name, uid, bank_code, rule_id, subject = row
        pats = subject_patterns.get(rule_id or '')
        if not pats:
            continue  # 无模板主题特征，无法判定，保留
        subj_l = (subject or '').lower()
        if not any(p in subj_l for p in pats):
            ghosts.append({
                'id': sid, 'account_name': account_name, 'uid': uid,
                'bank_code': bank_code, 'rule_id': rule_id, 'subject': subject,
                'txn_count': _txn_count(cur, sid, account_name, uid, bank_code),
            })
    return ghosts


def plan_orphan_msgid_fixes(cur):
    """__orphan__ 归属回填：仅 Message-ID 硬证据（非占位账户存在同 银行+Message-ID 账单且唯一）。

    返回 {statement_id: {id, uid, bank_code, message_id, account_name}}。
    uid / email_summaries 证据与 default 占位一律不接受。
    """
    cur.execute(
        """SELECT o.id, o.uid, o.bank_code, o.message_id, n.account_name
           FROM statements o
           JOIN statements n
             ON n.message_id = o.message_id AND n.bank_code = o.bank_code
            AND n.account_name NOT IN (?, ?) AND n.id <> o.id
           WHERE o.account_name = ?
             AND o.message_id IS NOT NULL AND o.message_id <> ''
           ORDER BY o.id""",
        (*PLACEHOLDER_ACCOUNTS, PLACEHOLDER_ACCOUNTS[0]),
    )
    by_id = {}
    for sid, uid, bank_code, message_id, target in cur.fetchall():
        info = by_id.setdefault(sid, {'id': sid, 'uid': uid, 'bank_code': bank_code,
                                      'message_id': message_id, 'accounts': []})
        if target not in info['accounts']:
            info['accounts'].append(target)
    fixes = {}
    for sid, info in by_id.items():
        if len(set(info['accounts'])) == 1:  # 唯一目标账户才回填
            info['account_name'] = info['accounts'][0]
            fixes[sid] = {k: v for k, v in info.items() if k != 'accounts'}
    return fixes


def plan_merges(cur, orphan_fixes, ghost_ids):
    """按「有效账户（回填后）+银行+Message-ID」分组建组并选定主记录与基准份。

    - 全幽灵组（组内所有成员均判为非账单）跳过合并，整组交给幽灵流程删除；
    - 主记录：已还款优先 > 交易数多优先 > 最早 id；
    - 基准份：交易数最多（并列取主记录、再取 id 小），其交易整份保留（重数不丢）。
    """
    cur.execute(
        """SELECT id, account_name, uid, bank_code, message_id, subject, statement_month,
                  total_due, is_paid, paid_at
           FROM statements WHERE message_id IS NOT NULL AND message_id <> ''"""
    )
    rows = [dict(zip([d[0] for d in cur.description], r)) for r in cur.fetchall()]
    for r in rows:
        fix = orphan_fixes.get(r['id'])
        r['effective_account'] = fix['account_name'] if fix else r['account_name']
        r['txn_count'] = _txn_count(cur, r['id'], r['account_name'], r['uid'], r['bank_code'])

    groups_map = {}
    for r in rows:
        key = (r['effective_account'], r['bank_code'], r['message_id'])
        groups_map.setdefault(key, []).append(r)

    groups = []
    for (account_name, bank_code, message_id), members in sorted(groups_map.items()):
        if len(members) < 2:
            continue
        if all(m['id'] in ghost_ids for m in members):
            continue  # 全幽灵组：非账单重复入库，整组由幽灵流程删除，不做无意义合并
        primary = sorted(members, key=lambda r: (-(r['is_paid'] or 0), -r['txn_count'], r['id']))[0]
        base = sorted(members, key=lambda r: (-r['txn_count'], r['id'] != primary['id'], r['id']))[0]
        groups.append({
            'account_name': account_name, 'bank_code': bank_code, 'message_id': message_id,
            'primary': primary, 'base': base,
            'duplicates': [r for r in members if r['id'] != primary['id']],
            'members': members,
        })
    return groups


def apply_orphan_fixes(cur, fixes, now):
    """回填账单账户归属（仅 Message-ID 证据）。在合并之后执行：组内同 uid 副本已删除，
    唯一键 (account_name, uid, bank_code) 不再冲突；若仍冲突（同 uid 但非同 Message-ID
    的另一封账单占用），保留 __orphan__ 并告警，不强行回填。"""
    fixed = 0
    for f in sorted(fixes.values(), key=lambda x: x['id']):
        try:
            cur.execute(
                "UPDATE statements SET account_name = ?, updated_at = ? WHERE id = ? AND account_name = '__orphan__'",
                (f['account_name'], now, f['id']),
            )
        except sqlite3.IntegrityError:
            # 同 uid 但非同 Message-ID 的另一封账单占用目标唯一键：保留 __orphan__，不强行回填
            print(f'⚠️ 孤儿 id={f["id"]} 回填 {f["account_name"]} 撞唯一键（同 uid 异 Message-ID），保留 __orphan__')
            continue
        if not cur.rowcount:
            continue  # 已被合并删除或非孤儿，无需回填
        fixed += 1
        # 交易 / 解析缓存 / 审计记录随账单一起迁移，保持重解析与审计可用；
        # 目标侧已有同键记录（同一封邮件的另一份缓存）时跳过即可。
        try:
            cur.execute(
                f"UPDATE statement_transactions SET account_name = ? WHERE {TXN_LOCATE} AND account_name = '__orphan__'",
                (f['account_name'], f['id'], '__orphan__', f['uid'], f['bank_code']),
            )
            cur.execute(
                "UPDATE email_bodies SET account_name = ? WHERE account_name = '__orphan__' AND uid = ?",
                (f['account_name'], f['uid']),
            )
            cur.execute(
                "UPDATE validation_runs SET account_name = ? WHERE account_name = '__orphan__' AND uid = ? AND bank_code = ?",
                (f['account_name'], f['uid'], f['bank_code']),
            )
        except sqlite3.IntegrityError as e:
            print(f'⚠️ 孤儿 id={f["id"]} 账单已回填，但关联缓存迁移被跳过（目标侧已存在同键记录）：{e}')
    return fixed


def apply_merges(cur, groups, now):
    merged_statements = 0
    removed_txns = 0
    for g in groups:
        p, base = g['primary'], g['base']
        # 1. 继承还款状态证据链（任一副本已还 → 主记录已还，保留最早 paid_at）
        for dup in g['duplicates']:
            if dup['is_paid'] and not p['is_paid']:
                cur.execute(
                    "UPDATE statements SET is_paid = 1, paid_at = COALESCE(?, paid_at), updated_at = ? WHERE id = ?",
                    (dup['paid_at'] or now, now, p['id']),
                )
                p['is_paid'], p['paid_at'] = 1, dup['paid_at'] or now
            elif dup['is_paid'] and p['is_paid'] and dup['paid_at'] and (not p['paid_at'] or dup['paid_at'] < p['paid_at']):
                cur.execute("UPDATE statements SET paid_at = ?, updated_at = ? WHERE id = ?", (dup['paid_at'], now, p['id']))
                p['paid_at'] = dup['paid_at']

        # 2. 基准份交易整份重挂到主记录（保留重数）；其它份交易全部删除。
        #    定位一律用成员「当前真实账户名」（回填孤儿的账户此刻仍是 __orphan__，
        #    账户回填在合并删除同 uid 副本之后执行，避免撞唯一键）。
        base_txn_ids = _txn_ids(cur, base['id'], base['account_name'], base['uid'], base['bank_code'])
        for tid in base_txn_ids:
            cur.execute(
                "UPDATE statement_transactions SET statement_id = ?, account_name = ?, uid = ?, bank_code = ? WHERE id = ?",
                (p['id'], g['account_name'], p['uid'], g['bank_code'], tid),
            )
        for m in g['members']:
            if m['id'] == base['id']:
                continue
            ids = _txn_ids(cur, m['id'], m['account_name'], m['uid'], m['bank_code'])
            for tid in ids:
                cur.execute("DELETE FROM statement_transactions WHERE id = ?", (tid,))
                removed_txns += 1

        # 3. 校验记录重挂到主记录（账户与 uid 都对齐组身份，审计历史保留）
        for m in g['members']:
            if m['id'] == p['id']:
                continue
            cur.execute(
                "UPDATE validation_runs SET uid = ?, account_name = ? WHERE account_name = ? AND uid = ? AND bank_code = ?",
                (p['uid'], g['account_name'], m['account_name'], m['uid'], g['bank_code']),
            )

        # 4. 删除重复账单 + 旧新 ID 映射
        for dup in g['duplicates']:
            cur.execute("DELETE FROM statements WHERE id = ?", (dup['id'],))
            cur.execute(
                """INSERT INTO cleanup_id_map (old_statement_id, new_statement_id, account_name, bank_code, message_id, reason, merged_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (dup['id'], p['id'], g['account_name'], g['bank_code'], g['message_id'],
                 'duplicate_message_id', now),
            )
            merged_statements += 1
    return merged_statements, removed_txns


def apply_ghosts(cur, ghosts, now):
    removed = 0
    removed_txns = 0
    for g in ghosts:
        ids = _txn_ids(cur, g['id'], g['account_name'], g['uid'], g['bank_code'])
        for tid in ids:
            cur.execute("DELETE FROM statement_transactions WHERE id = ?", (tid,))
            removed_txns += 1
        cur.execute("DELETE FROM statements WHERE id = ?", (g['id'],))
        if not cur.rowcount:
            continue  # 已被合并流程删除（防御：正常不会发生，计划已互斥）
        cur.execute(
            """INSERT INTO cleanup_id_map (old_statement_id, new_statement_id, account_name, bank_code, message_id, reason, merged_at)
               VALUES (?, NULL, ?, ?, ?, ?, ?)""",
            (g['id'], g['account_name'], g['bank_code'], None, 'ghost_not_statement', now),
        )
        removed += 1
    return removed, removed_txns


def _expected_after(before, groups, ghosts):
    """根据计划推导执行后的核对期望值（不依赖执行路径自身计数）。"""
    deleted_ids = set()
    for g in groups:
        deleted_ids.update(d['id'] for d in g['duplicates'])
    deleted_ids.update(g['id'] for g in ghosts)

    # statements：删除并集（合并副本 ∪ 幽灵）
    expected_stmts = before['statements'] - len(deleted_ids)
    return deleted_ids, expected_stmts


def main():
    ap = argparse.ArgumentParser(description='历史重复账单清洗（默认 Dry-run 只读预览）')
    ap.add_argument('--db', default='statements.db', help='SQLite 数据库路径')
    ap.add_argument('--apply', action='store_true', help='执行清洗（默认仅 Dry-run 预览）')
    ap.add_argument('--no-backup', action='store_true', help='apply 时不做一致性备份')
    args = ap.parse_args()

    if not os.path.exists(args.db):
        print(f'❌ 数据库不存在：{args.db}')
        sys.exit(1)

    subject_patterns = _load_subject_patterns()
    now = datetime.now().isoformat(timespec='seconds')

    conn = None
    try:
        if args.apply:
            # ---------- 执行路径：迁移前一致性备份 → schema 迁移 → 单事务 ----------
            if not args.no_backup:
                backup = f"{args.db}.bak-{datetime.now().strftime('%Y%m%d_%H%M%S')}"
                src = sqlite3.connect(args.db)
                dst = sqlite3.connect(backup)
                with dst:
                    src.backup(dst)
                src.close()
                dst.close()
                print(f'💾 已完成迁移前一致性备份：{backup}')

            from statement_db import init_db
            init_db(args.db)  # 仅 schema 迁移（无还款状态副作用）

            conn = sqlite3.connect(args.db, isolation_level=None)  # 显式事务控制
            cur = conn.cursor()
            cur.execute('BEGIN IMMEDIATE')  # 取写锁：计划/执行/核对同一事务同一快照
            cur.execute(
                """CREATE TABLE IF NOT EXISTS cleanup_id_map (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    old_statement_id INTEGER NOT NULL,
                    new_statement_id INTEGER,
                    account_name TEXT,
                    bank_code TEXT,
                    message_id TEXT,
                    reason TEXT,
                    merged_at TEXT NOT NULL
                )"""
            )
        else:
            # ---------- Dry-run：只读连接，零写入（不迁移、不建表） ----------
            conn = sqlite3.connect(f'file:{args.db}?mode=ro', uri=True)
            cur = conn.cursor()

        before = _snapshot(cur)
        ghosts = plan_ghosts(cur, subject_patterns)
        ghost_ids = {g['id'] for g in ghosts}
        orphan_fixes = plan_orphan_msgid_fixes(cur)
        groups = plan_merges(cur, orphan_fixes, ghost_ids)

        deleted_ids, expected_stmts = _expected_after(before, groups, ghosts)

        # 未触碰账单（既不在合并组内，也不是幽灵）——用于守恒核对基线
        touched_ids = set()
        for g in groups:
            touched_ids.update(m['id'] for m in g['members'])
        touched_ids.update(ghost_ids)
        if touched_ids:
            cur.execute("SELECT COUNT(*) FROM statements WHERE is_paid = 1 AND id NOT IN (%s)"
                        % ','.join('?' * len(touched_ids)), tuple(touched_ids))
            untouched_paid = cur.fetchone()[0]
        else:
            untouched_paid = before['paid_statements']
        # 已还款逻辑账单守恒：未触碰 paid + 每组(组内任一已还 ? 1 : 0)
        expected_paid = untouched_paid + sum(1 for g in groups if any(m['is_paid'] for m in g['members']))
        # 交易守恒：未触碰组交易原样 + 每组保留基准份交易 - 幽灵交易
        expected_txns = before['transactions'] \
            - sum(sum(m['txn_count'] for m in g['members']) - g['base']['txn_count'] for g in groups) \
            - sum(g['txn_count'] for g in ghosts)

        # ---------- 预览报告 ----------
        print('=' * 72)
        print(f"清洗{'执行' if args.apply else '预览（Dry-run，只读未修改任何数据）'}：{args.db}")
        print(f"清洗前：statements={before['statements']}，transactions={before['transactions']}，已还款={before['paid_statements']}")
        print('=' * 72)

        print(f'\n[0] __orphan__ 归属回填（仅 Message-ID 硬证据）：{len(orphan_fixes)} 条')
        for f in sorted(orphan_fixes.values(), key=lambda x: x['id']):
            print(f"  id={f['id']} uid={str(f['uid'])[:24]} {f['bank_code']} -> {f['account_name']}（同 Message-ID 非占位账户唯一）")
        # 保留口径：当前孤儿 - 将被合并删除的孤儿 dup - 将回填的孤儿（含作为组内 dup 被删的回填候选）
        cur.execute("SELECT id FROM statements WHERE account_name = '__orphan__'")
        orphan_ids = {r[0] for r in cur.fetchall()}
        orphan_stay = len(orphan_ids - deleted_ids - set(orphan_fixes.keys()))
        print(f"  （保留 __orphan__ {orphan_stay} 条：无 Message-ID 证据，不凭 uid/default 揣测归属；"
              f"另有 {len(orphan_ids & deleted_ids)} 条孤儿重复副本随合并删除）")

        print(f'\n[1] Message-ID 重复组：{len(groups)} 组（全幽灵组已跳过合并，交给[2]删除）')
        for g in groups:
            p, base = g['primary'], g['base']
            print(f"  {g['bank_code']} {g['account_name']} msgid={g['message_id'][:50]}")
            print(f"    主记录 id={p['id']} uid={str(p['uid'])[:24]} 月份={p['statement_month']} 应还={p['total_due']} "
                  f"已还={p['is_paid']} 交易={p['txn_count']}")
            for dup in g['duplicates']:
                extra = '  [跨账户回填]' if dup['account_name'] != g['account_name'] else ''
                print(f"    合并掉 id={dup['id']} uid={str(dup['uid'])[:24]} 已还={dup['is_paid']} 交易={dup['txn_count']}{extra}")
            if base['id'] != p['id']:
                print(f"    基准份 id={base['id']}（交易 {base['txn_count']} 条整份保留，重挂主记录）")

        print(f'\n[2] 幽灵账单（主题未命中模板门禁）：{len(ghosts)} 条')
        for g in ghosts:
            print(f"  id={g['id']} {g['bank_code']} {g['account_name']} uid={str(g['uid'])[:24]} "
                  f"rule={g['rule_id']} 交易={g['txn_count']} 主题={ (g['subject'] or '')[:40]}")

        print(f'\n预期结果：删除账单 {len(deleted_ids)} 条（合并副本 {sum(len(g["duplicates"]) for g in groups)} + 幽灵 {len(ghosts)}，'
              f'两名单已按并集去重）')
        print(f'预期清洗后：statements={expected_stmts}，transactions={expected_txns}，已还款（逻辑账单）={expected_paid}')

        if not args.apply:
            print('\n⚠️ Dry-run 结束：以上仅为预览（只读，未修改任何数据，含 schema）。确认无误后加 --apply 执行（迁移前一致性备份 + 单事务）。')
            return

        # ---------- 执行（已在 BEGIN IMMEDIATE 事务内） ----------
        # 顺序：先合并（删除组内同 uid 副本，回填不再撞唯一键）→ 再回填孤儿归属 → 最后删幽灵
        merged_statements, merged_removed_txns = apply_merges(cur, groups, now)
        fixed_orphans = apply_orphan_fixes(cur, orphan_fixes, now)
        removed_ghosts, ghost_removed_txns = apply_ghosts(cur, ghosts, now)

        after = _snapshot(cur)

        # ---------- 核对报告（期望值来自计划推导，独立于执行路径） ----------
        cur.execute("SELECT COUNT(*) FROM cleanup_id_map")
        map_rows = cur.fetchone()[0]
        group_paid_ok = True
        group_paid_detail = []
        for g in groups:
            cur.execute("SELECT is_paid FROM statements WHERE id = ?", (g['primary']['id'],))
            row = cur.fetchone()
            want = 1 if any(m['is_paid'] for m in g['members']) else 0
            got = (row[0] or 0) if row else -1
            if got != want:
                group_paid_ok = False
                group_paid_detail.append(f"组主记录 id={g['primary']['id']} 期望 is_paid={want} 实际 {got}")
        group_txn_ok = True
        group_txn_detail = []
        for g in groups:
            cur.execute(
                f"SELECT COUNT(*) FROM statement_transactions WHERE {TXN_LOCATE}",
                (g['primary']['id'], g['account_name'], g['primary']['uid'], g['bank_code']),
            )
            got = cur.fetchone()[0]
            want = g['base']['txn_count']
            if got != want:
                group_txn_ok = False
                group_txn_detail.append(f"组主记录 id={g['primary']['id']} 期望交易 {want} 条实际 {got} 条")

        checks = [
            ('statements 数量核对（并集去重）', after['statements'] == expected_stmts,
             f"{before['statements']} - 删除并集 {len(deleted_ids)} = {expected_stmts}，实际 {after['statements']}"),
            ('已还款逻辑账单守恒（非逐条副本）', after['paid_statements'] == expected_paid,
             f"预期 {expected_paid}（未触碰 {untouched_paid} + 合并组代表 "
             f"{expected_paid - untouched_paid}），实际 {after['paid_statements']}"),
            ('交易数量守恒（基准份保留重数）', after['transactions'] == expected_txns,
             f"预期 {expected_txns}（合并删 {merged_removed_txns} + 幽灵删 {ghost_removed_txns}），实际 {after['transactions']}"),
            ('合并组还款证据保留', group_paid_ok, '；'.join(group_paid_detail) or f"{len(groups)} 组全部通过"),
            ('合并组交易重数保留', group_txn_ok, '；'.join(group_txn_detail) or f"{len(groups)} 组全部通过"),
            ('cleanup_id_map 映射完整', map_rows == len(deleted_ids),
             f"删除 {len(deleted_ids)} 条 ↔ 映射 {map_rows} 行"),
        ]
        all_ok = all(ok for _, ok, _ in checks)
        print('\n---------- 核对报告 ----------')
        for name, ok, detail in checks:
            print(f"  {'✅' if ok else '❌'} {name}：{detail}")
        print(f"  合并重复账单 {merged_statements} 条（基准份外交易删除 {merged_removed_txns} 条）")
        print(f"  清理幽灵账单 {removed_ghosts} 条（含交易 {ghost_removed_txns} 条）")
        print(f"  __orphan__ 回填 {fixed_orphans} 条（Message-ID 证据）")
        print(f"  清洗后：statements={after['statements']}，transactions={after['transactions']}，已还款={after['paid_statements']}")

        if all_ok:
            conn.commit()
            print('\n✅ 清洗完成，事务已提交。')
        else:
            conn.rollback()
            print('\n❌ 核对未通过，事务已回滚，数据库未变更。')
            sys.exit(2)
    except sqlite3.Error as e:
        if conn is not None:
            try:
                conn.rollback()
            except Exception:
                pass
        print(f'❌ 数据库操作失败（若为缺列错误，说明 schema 过旧，请先部署新版代码完成一次初始化）：{e}')
        sys.exit(1)
    finally:
        if conn is not None:
            conn.close()


if __name__ == '__main__':
    main()
