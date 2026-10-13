#!/usr/bin/env python3
"""回填 HX/CMB 结算列单元格符号币种(￥/¥→CNY, currency_source='cell_symbol')

背景: HX/CMB 已核实为人民币结算的账单模板, 其结算金额单元格含 ￥/¥ 符号,
但解析器此前未把该符号落成币种证据, 116 笔交易以 currency=UNKNOWN 落库。
Parser 侧已新增 quality.settlement_cell_symbol 窄规则(见 statement_evidence.py);
本脚本按同一规则回填存量, 并同步刷新受影响账单存量质量诊断中的
"入账币种无可靠证据"(CURRENCY_UNKNOWN/currency) 条目, 其余缺口(如
balance_currency、原币未确认)原样保留。

安全约束:
- 仅处理 currency='UNKNOWN' AND currency_source IS NULL AND bank_code IN ('HX','CMB')
  且 raw_row_json 结算 amount_cell 含 ￥/¥ 的行;
- 不修改金额、原始单元格证据、原币列、账单身份与还款状态;
- 默认 dry-run; --apply 时先备份数据库文件, 再在单事务内执行并做一致性核对。
"""

import argparse
import json
import os
import shutil
import sqlite3
import sys
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from statement_evidence import purpose_availability

SETTLEMENT_SYMBOLS = ('￥', '¥')
TARGET_BANKS = ('HX', 'CMB')
# CMB 八列契约(rules/CMB_TEMPLATE_V1.json accepted_structure_fingerprints):
# [ignored, txn_date, post_date, description, amount, currency, location, original_amount]
# 早期解析路径只存 {'cols': [...]} 原始行, 结算人民币金额固定在第 5 列(下标 4)。
CMB_LEGACY_COLS = 8
CMB_LEGACY_AMOUNT_COL = 4


def settlement_cell(bank_code, raw_row_json):
    try:
        raw = json.loads(raw_row_json or '{}')
    except json.JSONDecodeError:
        return ''
    cell = (raw.get('currency_evidence') or {}).get('settlement', {}).get('amount_cell')
    if cell:
        return cell
    cols = raw.get('cols') or []
    if bank_code == 'CMB' and len(cols) == CMB_LEGACY_COLS:
        return cols[CMB_LEGACY_AMOUNT_COL] or ''
    return ''


def collect_targets(conn):
    rows = conn.execute(
        "SELECT id, bank_code, statement_id, amount, raw_row_json FROM statement_transactions "
        "WHERE currency='UNKNOWN' AND currency_source IS NULL AND bank_code IN (?,?)",
        TARGET_BANKS).fetchall()
    targets, skipped = [], []
    for r in rows:
        cell = settlement_cell(r['bank_code'], r['raw_row_json'])
        if any(sym in cell for sym in SETTLEMENT_SYMBOLS):
            targets.append(dict(r))
        else:
            skipped.append({'id': r['id'], 'bank_code': r['bank_code'], 'amount_cell': cell})
    return targets, skipped


def consistency_snapshot(conn):
    snap = {}
    snap['txn_by_bank'] = {r['bank_code']: r['n'] for r in conn.execute(
        "SELECT bank_code, COUNT(*) n FROM statement_transactions GROUP BY bank_code")}
    snap['amount_sum'] = {r['bank_code']: r['s'] for r in conn.execute(
        "SELECT bank_code, ROUND(SUM(CAST(amount AS REAL)), 2) s FROM statement_transactions GROUP BY bank_code")}
    snap['original_amount_rows'] = conn.execute(
        "SELECT COUNT(*) FROM statement_transactions WHERE original_amount IS NOT NULL").fetchone()[0]
    snap['statements'] = {r['id']: (r['bank_code'], r['statement_month'], r['is_paid'], r['total_due'])
                          for r in conn.execute(
                              "SELECT id, bank_code, statement_month, is_paid, total_due FROM statements")}
    return snap


def refresh_statement_quality(conn, statement_id):
    row = conn.execute("SELECT raw_fields_json FROM statements WHERE id=?", (statement_id,)).fetchone()
    if not row or not row['raw_fields_json']:
        return None
    data = json.loads(row['raw_fields_json'])
    quality = data.get('quality') or {}
    diags = [d for d in (quality.get('diagnostics') or [])
             if not (d.get('code') == 'CURRENCY_UNKNOWN' and d.get('field') == 'currency')]
    remaining = conn.execute(
        "SELECT COUNT(*) FROM statement_transactions WHERE statement_id=? AND currency='UNKNOWN'",
        (statement_id,)).fetchone()[0]
    quality['diagnostics'] = diags
    quality['purpose_availability'] = purpose_availability(diags, data.get('meta'))
    if isinstance(quality.get('txn_stats'), dict):
        quality['txn_stats']['settlement_currency_unknown'] = remaining
    data['quality'] = quality
    return json.dumps(data, ensure_ascii=False)


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--db', required=True, help='statements.db 路径')
    ap.add_argument('--apply', action='store_true', help='实际执行(默认 dry-run)')
    args = ap.parse_args()

    conn = sqlite3.connect(args.db)
    conn.row_factory = sqlite3.Row
    targets, skipped = collect_targets(conn)
    by_bank = {}
    for t in targets:
        by_bank[t['bank_code']] = by_bank.get(t['bank_code'], 0) + 1
    stmt_ids = sorted({t['statement_id'] for t in targets if t['statement_id'] is not None})

    print(f"候选行: {len(targets)} (按银行: {by_bank}); 无符号证据跳过: {len(skipped)}")
    print(f"受影响账单: {stmt_ids}")
    for t in targets[:5]:
        print(f"  样例 id={t['id']} {t['bank_code']} amount={t['amount']}")
    if skipped:
        for s in skipped[:5]:
            print(f"  跳过 id={s['id']} {s['bank_code']} amount_cell={s['amount_cell']!r}")
    if not args.apply:
        print("DRY-RUN: 未做任何修改。加 --apply 执行。")
        return

    before = consistency_snapshot(conn)
    backup = f"{args.db}.bak-{datetime.now().strftime('%Y%m%d-%H%M%S')}"
    conn.close()
    shutil.copy2(args.db, backup)
    print(f"已备份: {backup}")

    conn = sqlite3.connect(args.db)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute('BEGIN')
        conn.executemany(
            "UPDATE statement_transactions SET currency='CNY', currency_source='cell_symbol' WHERE id=?",
            [(t['id'],) for t in targets])
        for sid in stmt_ids:
            refreshed = refresh_statement_quality(conn, sid)
            if refreshed is not None:
                conn.execute("UPDATE statements SET raw_fields_json=? WHERE id=?", (refreshed, sid))
        conn.commit()
    except Exception:
        conn.rollback()
        raise

    after = consistency_snapshot(conn)
    assert before == after, f"一致性核对失败: {before} != {after}"
    print(f"已更新 {len(targets)} 行; 金额/笔数/原币/账单身份/还款状态核对一致。")


if __name__ == '__main__':
    main()
