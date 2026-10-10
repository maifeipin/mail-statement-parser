#!/usr/bin/env python3
"""账单确定性统计读模型（重构契约 §4）：快照、分币种合计、商户趋势的唯一实现。

规则：
- 金额全程 Decimal，输出字符串；NaN/Infinity 一律拒绝（不作合法金额）；
- 不跨币种硬加；支出（debit）与退款/入账（credit）分开列示；
- amount 自带符号（credit 为负），净额直接求和，不再按 direction 反号；
- 原币折算比例仅当原币币种已知且两边金额有效、原币非零时计算，
  称「实际折算比例」（不是央行汇率）；
- parse_status='failed' 的账单排除；缺账单月份标记未覆盖，不补零；
- 月度统计读取全部符合条件的明细，LLM 样本截断不在此层发生。
"""

import json
import os
import sqlite3
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Optional

from mail_quality import normalize_merchant

MERCHANT_NORM_VERSION = 'v1.0'

# 显式商户映射（可选文件，项目根 merchant_aliases.json）：{"原名片段": "标准名"}
_ALIAS_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'merchant_aliases.json')


def _load_aliases():
    try:
        with open(_ALIAS_PATH, encoding='utf-8') as f:
            data = json.load(f)
        return {k: v for k, v in data.items() if isinstance(k, str) and isinstance(v, str)}
    except (OSError, ValueError):
        return {}


_ALIASES = _load_aliases()


def decimal_amount(value) -> Optional[Decimal]:
    """严格 Decimal 解析；空/NaN/Infinity/非法 → None（不得作为合法金额）。"""
    if value is None:
        return None
    s = str(value).strip().replace(',', '')
    if not s:
        return None
    try:
        d = Decimal(s)
    except InvalidOperation:
        return None
    if d.is_nan() or d.is_infinite():
        return None
    return d


def month_start(today, months):
    y, m = today.year, today.month
    for _ in range(months):
        m -= 1
        if m == 0:
            m = 12
            y -= 1
    return f'{y:04d}-{m:02d}'


def _connect_ro(db_path):
    conn = sqlite3.connect(f'file:{db_path}?mode=ro', uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def _months_list(start_month, today):
    """从 start_month 到当前月的闭区间月份列表（YYYY-MM）。"""
    out = []
    y, m = int(start_month[:4]), int(start_month[5:7])
    ty, tm = today.year, today.month
    while (y, m) <= (ty, tm):
        out.append(f'{y:04d}-{m:02d}')
        m += 1
        if m == 13:
            m = 1
            y += 1
    return out


def snapshot(db_path, months=1, detail_limit=20, today=None):
    """版本化账单快照（billing_snapshot CLI 的 data 部分，契约 §4）。"""
    today = today or datetime.now(timezone.utc)
    start = month_start(today, months)
    conn = _connect_ro(db_path)
    try:
        stmt_rows = conn.execute(
            """SELECT s.id, s.account_name, s.bank_code, s.statement_month, s.due_date,
                      s.total_due, s.minimum_due, s.is_paid, s.paid_at, s.parse_status,
                      s.message_id, s.created_at
               FROM statements s
               WHERE s.parse_status = 'ok' AND s.statement_month >= ?
               ORDER BY s.account_name, s.bank_code, s.statement_month""",
            (start,)).fetchall()
        statements = []
        balance_index = {}
        if stmt_rows:
            ids = [r['id'] for r in stmt_rows]
            ph = ','.join('?' * len(ids))
            for b in conn.execute(
                f"""SELECT statement_id, currency, total_due, minimum_due, currency_source
                    FROM statement_balances WHERE statement_id IN ({ph})""",
                    ids):
                balance_index.setdefault(b['statement_id'], []).append({
                    'currency': b['currency'], 'total_due': b['total_due'],
                    'minimum_due': b['minimum_due'], 'currency_source': b['currency_source'],
                })
        for r in stmt_rows:
            bal = balance_index.get(r['id'])
            if not bal:
                # 迁移期兜底：无余额行时用兼容字段展示，币种证据按未知处理
                bal = ([{'currency': 'CNY' if r['total_due'] is not None else 'UNKNOWN',
                         'total_due': r['total_due'], 'minimum_due': r['minimum_due'],
                         'currency_source': None}]
                       if (r['total_due'] is not None or r['minimum_due'] is not None) else [])
            statements.append({
                'statement_id': r['id'], 'account': r['account_name'], 'bank': r['bank_code'],
                'month': r['statement_month'], 'due_date': r['due_date'],
                'balances': bal, 'is_paid': r['is_paid'], 'message_id': r['message_id'],
            })

        txn_rows = conn.execute(
            """SELECT t.id, t.account_name, t.bank_code, t.txn_date, t.description,
                      t.amount, t.currency, t.direction, t.original_amount, t.original_currency
               FROM statement_transactions t
               JOIN statements s ON s.id = t.statement_id
               WHERE s.parse_status = 'ok' AND s.statement_month >= ?
               ORDER BY t.id""",
            (start,)).fetchall()
        txn_totals = {}
        unknown_orig_count = 0
        for r in txn_rows:
            d = decimal_amount(r['amount'])
            if d is None:
                continue
            key = (r['currency'] or 'UNKNOWN', r['direction'] or 'unknown')
            agg = txn_totals.setdefault(key, {'count': 0, 'amount': Decimal('0')})
            agg['count'] += 1
            agg['amount'] += d
            if r['original_amount'] and not r['original_currency']:
                unknown_orig_count += 1
        transaction_totals = [
            {'currency': k[0], 'direction': k[1], 'count': v['count'], 'amount': str(v['amount'])}
            for k, v in sorted(txn_totals.items())
        ]

        # 覆盖缺口：每个 (账户, 银行) 在「首期~末期」区间内应连续（区间外不推断，
        # 单期账户无缺口；当前月通常尚未出账单，不算缺口）。尾部落后单独表达。
        seen_pairs = {}
        for r in stmt_rows:
            seen_pairs.setdefault((r['account_name'], r['bank_code']), []).append(r['statement_month'])
        py, pm = (today.year, today.month - 1) if today.month > 1 else (today.year - 1, 12)
        prev_full_month = f'{py:04d}-{pm:02d}'
        coverage_gaps = []
        stale_accounts = []
        for (acct, bank), mons in seen_pairs.items():
            mons_set = set(mons)
            first, last = min(mons_set), max(mons_set)
            for m in _months_list(first, datetime(int(last[:4]), int(last[5:7]), 1)):
                if m not in mons_set:
                    coverage_gaps.append({'account': acct, 'bank': bank, 'missing_month': m})
            if last < prev_full_month:
                stale_accounts.append({'account': acct, 'bank': bank, 'last_statement_month': last})

        return {
            'schema_version': 1,
            'generated_at': datetime.now(timezone.utc).isoformat(),
            'months': months,
            'scope_start_month': start,
            'statement_count': len(statements),
            'statements': statements,
            'transaction_count': len(txn_rows),
            'transaction_totals': transaction_totals,
            'unknown_currency_original_amounts': unknown_orig_count,
            'coverage_gaps': coverage_gaps,
            'stale_accounts': stale_accounts,
            'model_detail_limit': detail_limit,
            'limitations': [
                '交易分组金额按入账币种与方向分别求和，不等同消费净额',
                '原币币种未知的交易不参与折算比例计算',
                '缺失账单月份标记为未覆盖，不补零',
            ],
        }
    finally:
        conn.close()


def merchant_of(desc):
    """商户规范化：显式映射 > trim/全半角/大小写归一。保留原描述与映射版本。"""
    d = (desc or '').strip()
    for frag, canon in _ALIASES.items():
        if frag and frag in d:
            return canon
    return normalize_merchant(d)


def trends(db_path, months=3, merchant=None, account=None):
    """商户/币种月度统计（billing_trends CLI 的 data 部分，契约 §6.2）。

    按 (merchant, month, currency) 聚合，debit 与 credit 分列且净额真正相消。
    未知收支方向与方向/符号冲突单独列示，不误计入支出或退款。
    原币统计与折算比例按 (original_currency/currency) 币种对及月份分组。"""
    today = datetime.now(timezone.utc)
    start = month_start(today, months)
    conn = _connect_ro(db_path)
    try:
        params = [start]
        acct_clause = ''
        if account:
            acct_clause = ' AND s.account_name = ? '
            params.append(account)
        rows = conn.execute(
            f"""SELECT t.txn_date, t.description, t.amount, t.currency, t.direction,
                       t.original_amount, t.original_currency, s.statement_month, s.account_name
                FROM statement_transactions t
                JOIN statements s ON s.id = t.statement_id
                WHERE s.parse_status = 'ok' AND s.statement_month >= ? {acct_clause}
                ORDER BY t.id""",
            params).fetchall()
    finally:
        conn.close()

    m_filter = merchant_of(merchant) if merchant else None
    stats = {}  # (m, month) -> {cur -> cur_agg}
    merchant_fx_pairs = {}  # m -> {fx_key -> [{'month': m, 'rate': Decimal, 'settlement': Decimal, 'original': Decimal}]}

    for r in rows:
        d = decimal_amount(r['amount'])
        if d is None:
            continue
        m = merchant_of(r['description'])
        if m_filter and m != m_filter:
            continue
        month = r['statement_month'] or (r['txn_date'] or '')[:7] or 'unknown'
        cur = r['currency'] or 'UNKNOWN'
        raw_dirn = r['direction']
        dirn = (raw_dirn or '').strip().lower()

        month_bucket = stats.setdefault((m, month), {})
        cur_agg = month_bucket.setdefault(cur, {
            'debit_count': 0,
            'debit_amount': Decimal('0'),
            'credit_count': 0,
            'credit_amount': Decimal('0'),
            'net': Decimal('0'),
            'unknown_direction_count': 0,
            'unknown_direction_amount': Decimal('0'),
            'direction_conflict_count': 0,
            'direction_conflict_amount': Decimal('0'),
            'fx_pairs': {},
        })

        # A3 核心：方向与符号严格分类，未知方向单独列示，冲突不强行掩盖
        if dirn == 'debit':
            if d >= 0:
                cur_agg['debit_count'] += 1
                cur_agg['debit_amount'] += d
                cur_agg['net'] += d
            else:
                # 标为 debit 支出但金额为负数：方向与符号冲突！
                cur_agg['direction_conflict_count'] += 1
                cur_agg['direction_conflict_amount'] += d
        elif dirn == 'credit':
            # credit 退款：规范化为负数计入 credit_amount 与 net
            cur_agg['credit_count'] += 1
            signed_credit = -abs(d)
            cur_agg['credit_amount'] += signed_credit
            cur_agg['net'] += signed_credit
        else:
            # 未知收支方向（None, 'unknown', 非法字符串）：单独列示，不进支出、不进确定性 net
            cur_agg['unknown_direction_count'] += 1
            cur_agg['unknown_direction_amount'] += d

        # A4 核心：实际折算比例（仅当原币币种已知 + 两边金额有效 + 原币非零）
        orig_cur = r['original_currency']
        orig_amt = r['original_amount']
        if orig_cur and orig_amt:
            od = decimal_amount(orig_amt)
            if od is not None and od != 0:
                rate = (abs(d) / abs(od)).quantize(Decimal('0.000001'))
                rate_str = str(rate)
                fx_key = f"{orig_cur}/{cur}"
                fx_entry = cur_agg['fx_pairs'].setdefault(fx_key, {
                    'rates': [],
                    'total_settlement_amount': Decimal('0'),
                    'total_original_amount': Decimal('0')
                })
                fx_entry['rates'].append(rate_str)
                fx_entry['total_settlement_amount'] += abs(d)
                fx_entry['total_original_amount'] += abs(od)

                merchant_fx_pairs.setdefault(m, {}).setdefault(fx_key, []).append({
                    'month': month,
                    'settlement_amount': abs(d),
                    'original_amount': abs(od),
                    'rate': rate,
                })

    merchants = []
    all_months = sorted({m for (_, m) in stats}, reverse=True)
    for m in sorted({mm for (mm, _) in stats}):
        entries = []
        for month in all_months:
            month_bucket = stats.get((m, month))
            if not month_bucket:
                continue
            row = {'month': month, 'currencies': []}
            for cur in sorted(month_bucket.keys()):
                agg = month_bucket[cur]
                cur_data = {
                    'currency': cur,
                    'debit_count': agg['debit_count'],
                    'debit_amount': str(agg['debit_amount']),
                    'credit_count': agg['credit_count'],
                    'credit_amount': str(agg['credit_amount']),
                    'net': str(agg['net']),
                }
                if agg['unknown_direction_count'] > 0:
                    cur_data['unknown_direction_count'] = agg['unknown_direction_count']
                    cur_data['unknown_direction_amount'] = str(agg['unknown_direction_amount'])
                if agg['direction_conflict_count'] > 0:
                    cur_data['direction_conflict_count'] = agg['direction_conflict_count']
                    cur_data['direction_conflict_amount'] = str(agg['direction_conflict_amount'])

                if agg['fx_pairs']:
                    cur_data['fx_pairs'] = {}
                    for k, v in sorted(agg['fx_pairs'].items()):
                        rates = v['rates']
                        count = len(rates)
                        simple_avg = (sum(Decimal(x) for x in rates) / count).quantize(Decimal('0.000001'))
                        tot_orig = v['total_original_amount']
                        tot_settle = v['total_settlement_amount']
                        weighted_avg = (tot_settle / tot_orig).quantize(Decimal('0.000001')) if tot_orig != 0 else simple_avg
                        cur_data['fx_pairs'][k] = {
                            'sample_rates': rates,
                            'sample_rate': rates[0],
                            'sample_count': count,
                            'simple_avg_rate': str(simple_avg),
                            'weighted_avg_rate': str(weighted_avg),
                            'total_settlement_amount': str(tot_settle),
                            'total_original_amount': str(tot_orig),
                        }
                row['currencies'].append(cur_data)
            entries.append(row)

        m_pairs = merchant_fx_pairs.get(m, {})
        fx_pairs_summary = {}
        for pair, items in sorted(m_pairs.items()):
            pair_rates = [it['rate'] for it in items]
            p_count = len(pair_rates)
            p_simple_avg = (sum(pair_rates) / p_count).quantize(Decimal('0.000001'))
            p_tot_settle = sum(it['settlement_amount'] for it in items)
            p_tot_orig = sum(it['original_amount'] for it in items)
            p_weighted_avg = (p_tot_settle / p_tot_orig).quantize(Decimal('0.000001')) if p_tot_orig != 0 else p_simple_avg
            fx_pairs_summary[pair] = {
                'count': p_count,
                'sample_rates': [str(r) for r in pair_rates],
                'simple_avg_rate': str(p_simple_avg),
                'weighted_avg_rate': str(p_weighted_avg),
                'total_settlement_amount': str(p_tot_settle),
                'total_original_amount': str(p_tot_orig),
            }

        # 商户顶层汇率指标：杜绝首条样本冒充平均值，多币种对禁止跨币种混算
        if len(fx_pairs_summary) == 1:
            single_pair = list(fx_pairs_summary.keys())[0]
            real_avg = fx_pairs_summary[single_pair]['simple_avg_rate']
            avg_fx_rate_sample = real_avg
            avg_fx_rate_algorithm = 'simple_arithmetic_mean'
            fx_rate_note = f'实际折算比例（币种对 {single_pair} 共 {fx_pairs_summary[single_pair]["count"]} 笔样本算术平均），非央行汇率'
        elif len(fx_pairs_summary) > 1:
            avg_fx_rate_sample = None
            avg_fx_rate_algorithm = 'disaggregated_by_pair'
            fx_rate_note = '存在多个币种对，不提供跨币种混合平均值；请参考 fx_pairs_summary 各币种对独立统计'
        else:
            avg_fx_rate_sample = None
            avg_fx_rate_algorithm = None
            fx_rate_note = None

        merchants.append({
            'merchant': m,
            'merchant_norm_version': MERCHANT_NORM_VERSION,
            'months': entries,
            'avg_fx_rate_sample': avg_fx_rate_sample,
            'avg_fx_rate_algorithm': avg_fx_rate_algorithm,
            'fx_pairs_summary': fx_pairs_summary,
            'fx_rate_note': fx_rate_note,
        })

    return {
        'schema_version': 1,
        'generated_at': datetime.now(timezone.utc).isoformat(),
        'months': months,
        'scope_start_month': start,
        'merchant_filter': m_filter,
        'account_filter': account,
        'merchants': merchants,
        'limitations': [
            '商户名为规范化形式（大小写/全半角归一），原始描述保留在交易明细',
            '折算比例为实际入账/原币金额，不等于央行汇率，也不自动认定包含某项费用',
            '金额使用 Decimal 精确累计，避免浮点误差',
        ],
    }
