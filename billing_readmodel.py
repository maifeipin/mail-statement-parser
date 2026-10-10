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

    支出与退款分开列示；折算比例仅当原币币种已知且金额有效时计算。"""
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
    stats = {}   # (merchant, month) -> {currency, direction -> {count, amount}}
    fx_samples = {}  # merchant -> [rates]
    for r in rows:
        d = decimal_amount(r['amount'])
        if d is None:
            continue
        m = merchant_of(r['description'])
        if m_filter and m != m_filter:
            continue
        month = r['statement_month'] or (r['txn_date'] or '')[:7] or 'unknown'
        bucket = stats.setdefault((m, month), {})
        key = (r['currency'] or 'UNKNOWN', r['direction'] or 'unknown')
        agg = bucket.setdefault(key, {'count': 0, 'amount': Decimal('0')})
        agg['count'] += 1
        agg['amount'] += d
        # 实际折算比例（原币币种已知 + 两边金额有效 + 原币非零）
        if r['original_currency'] and r['original_amount']:
            od = decimal_amount(r['original_amount'])
            if od is not None and od != 0:
                fx_samples.setdefault(m, []).append(str((d / od).quantize(Decimal('0.000001'))))

    merchants = []
    all_months = sorted({m for (_, m) in stats}, reverse=True)
    for m in sorted({mm for (mm, _) in stats}):
        entries = []
        for month in all_months:
            bucket = stats.get((m, month))
            if not bucket:
                continue  # 无交易月份不列（不补零）
            row = {'month': month, 'currencies': []}
            for (cur, dirn), agg in sorted(bucket.items()):
                debit = agg if dirn == 'debit' else None
                credit = agg if dirn == 'credit' else None
                row['currencies'].append({
                    'currency': cur,
                    'debit_count': (debit or {}).get('count', 0),
                    'debit_amount': str((debit or {}).get('amount', Decimal('0'))),
                    'credit_count': (credit or {}).get('count', 0),
                    'credit_amount': str((credit or {}).get('amount', Decimal('0'))),
                    'net': str(agg['amount']),  # amount 已带符号，直接求和不双重反号
                })
            entries.append(row)
        merchants.append({
            'merchant': m,
            'merchant_norm_version': MERCHANT_NORM_VERSION,
            'months': entries,
            'avg_fx_rate_sample': (fx_samples.get(m) or [None])[0] if m in fx_samples else None,
            'fx_rate_note': ('实际折算比例（入账金额/原币金额），非央行汇率' if m in fx_samples else None),
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
