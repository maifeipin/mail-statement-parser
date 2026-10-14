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
import re
import sqlite3
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Optional

from mail_quality import normalize_merchant
from statement_evidence import stored_purpose, confirmed_currency

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
    index = today.year * 12 + today.month - 1 - (months - 1)
    return f'{index // 12:04d}-{index % 12 + 1:02d}'


def _connect_ro(db_path):
    from urllib.parse import quote
    conn = sqlite3.connect('file:' + quote(os.path.abspath(db_path), safe='/') + '?mode=ro', uri=True)
    conn.row_factory = sqlite3.Row
    conn.execute('BEGIN')
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


def _field_source(raw, field, value):
    try:
        meta=json.loads(raw or '{}').get('meta', {}).get(field, {})
        return meta.get('source') if meta.get('value') == value else None
    except (ValueError, TypeError, AttributeError):
        return None


def _due_date_source(raw, due_date):
    return _field_source(raw, 'due_date', due_date)


def snapshot(db_path, months=1, detail_limit=20, today=None):
    """版本化账单快照（billing_snapshot CLI 的 data 部分，权威财务读模型契约 §4）。"""
    from datetime import date
    if isinstance(today, str):
        today_dt = datetime.fromisoformat(today.replace('Z', '+00:00'))
        today_date = today_dt.date()
    elif isinstance(today, datetime):
        today_dt = today
        today_date = today.date()
    elif isinstance(today, date):
        today_dt = datetime(today.year, today.month, today.day, tzinfo=timezone.utc)
        today_date = today
    else:
        today_dt = datetime.now(timezone.utc)
        today_date = today_dt.date()

    months = max(1, min(24, int(months)))
    detail_limit = max(0, min(100, int(detail_limit)))
    start = month_start(today_dt, months)
    conn = _connect_ro(db_path)
    try:
        # Latest month per mailbox/bank, including older data as an explicit fallback.
        database_rows = [dict(r) for r in conn.execute(
            "SELECT * FROM statements ORDER BY bank_code, statement_month DESC, id DESC"
        ).fetchall()]
        failed_rows = [r for r in database_rows if r.get('parse_status') == 'failed']
        all_rows = [r for r in database_rows if r.get('parse_status', 'ok') == 'ok']
        unscoped_rows = [r for r in all_rows if not re.fullmatch(
            r'[1-9][0-9]{3}-(?:0[1-9]|1[0-2])', str(r.get('statement_month') or ''))]
        all_rows = [r for r in all_rows if r not in unscoped_rows]

        latest = {}
        for row in all_rows:
            key = (row['account_name'], row['bank_code'])
            latest[key] = max(latest.get(key, ''), row['statement_month'] or '')

        selected = [row for row in all_rows if (row['statement_month'] or '') >= start or
                    row['statement_month'] == latest.get((row['account_name'], row['bank_code']))]

        availability = {
            r['id']: {
                purpose: stored_purpose(r.get('raw_fields_json'), purpose)
                for purpose in ('financial_summary', 'transaction_trends', 'fx_analysis', 'repayment_reminder')
            }
            for r in selected
        }

        balances = {}
        balance_table = conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='statement_balances'").fetchone()
        for row in selected:
            entries = [dict(b) for b in conn.execute(
                "SELECT currency, total_due, minimum_due, currency_source FROM statement_balances WHERE statement_id=?",
                (row['id'],)
            ).fetchall()] if balance_table else []
            if not entries:
                entries = [{'currency': 'UNKNOWN', 'currency_source': None, 'total_due': row['total_due'], 'minimum_due': row['minimum_due']}]
            for b in entries:
                b['currency_confirmed'] = bool(confirmed_currency(b.get('currency'), b.get('currency_source')))
                if not b['currency_confirmed']:
                    b['reported_currency'] = b['currency']
                    b['currency'] = 'UNKNOWN'
            balances[row['id']] = entries

        txns = []
        anchored = 'statement_id' in {row[1] for row in conn.execute("PRAGMA table_info(statement_transactions)")}
        for row in selected:
            query = ("SELECT * FROM statement_transactions WHERE statement_id=? ORDER BY txn_date, id"
                     if anchored else "SELECT * FROM statement_transactions WHERE bank_code=? AND uid=? AND account_name=? ORDER BY txn_date, id")
            params = (row['id'],) if anchored else (row['bank_code'], row['uid'], row['account_name'])
            for txn in conn.execute(query, params).fetchall():
                item = dict(txn)
                item.update(original_currency_display(item))
                item['purpose_availability'] = availability[row['id']]
                txns.append(item)
        txns = list({row['id']: row for row in txns}.values())

        anomalies = [f"账单 #{row['id']} 解析失败，已排除出账期选择、金额汇总及交易明细，需离线重解析。"
                     for row in failed_rows]
        anomalies.extend(f"账单 #{row['id']} 账期缺失或格式异常，未纳入账期金额统计。" for row in unscoped_rows)
        groups = {}
        for row in selected:
            groups.setdefault((row['account_name'], row['bank_code'], row['statement_month']), []).append(row['id'])
            amount = decimal_amount(row['total_due'])
            if amount is None:
                anomalies.append(f"账单 #{row['id']} 应还金额缺失或无法无歧义解析：{row['total_due']!r}。")
            if row['statement_month'] and row['statement_month'] > today_date.strftime('%Y-%m'):
                anomalies.append(f"账单 #{row['id']} 账期晚于当前月份，需核对解析。")
            if not row['is_paid']:
                due = str(row.get('due_date') or '')
                try:
                    days = (date.fromisoformat(due[:10]) - today_date).days
                    if days < 0:
                        anomalies.append(f"账单 #{row['id']} 数据库仍标为未还款，记录的到期日 {due} 已过；需核实实际还款状态。")
                    elif days <= 7:
                        anomalies.append(f"账单 #{row['id']} 数据库标为未还款，{days} 天内到期（{due}）。")
                except ValueError:
                    anomalies.append(f"账单 #{row['id']} 未还款记录的到期日缺失或格式异常：{due!r}。")
        for key, ids in groups.items():
            if len(ids) > 1:
                anomalies.append(f"{key[1]} / {key[2]} / {key[0]} 有 {len(ids)} 条账单（ID {ids}）；可能包含多卡或重复记录，未经核实不合并计费。")
        for (account, bank), month in latest.items():
            if month < today_date.strftime('%Y-%m'):
                anomalies.append(f'{bank} / {account} 最新账期为 {month}，当前月账单尚未入库；本报告包含该最新历史账期。')

        sums = {}
        counts = {}
        unknown_txns = []
        gated_txns = []
        unknown_orig_count = 0
        for txn in txns:
            amount = decimal_amount(txn['amount'])
            if amount is None:
                anomalies.append(f"交易 #{txn['id']} 金额缺失或无法解析。")
                continue
            c_src = txn.get('currency_source')
            is_confirmed = confirmed_currency(txn.get('currency'), c_src)
            gate = txn['purpose_availability']['transaction_trends']
            if gate['status'] == 'blocked':
                gated_txns.append(txn)
            if is_confirmed and gate['status'] != 'blocked':
                key = (txn.get('currency'), txn.get('direction') or '未标方向')
                sums[key] = sums.get(key, Decimal('0')) + amount
                counts[key] = counts.get(key, 0) + 1
            elif not is_confirmed:
                unknown_txns.append(txn)
            if txn.get('original_amount') is not None and not confirmed_currency(txn.get('original_currency'), txn.get('original_currency_source')):
                unknown_orig_count += 1

        if unknown_txns:
            anomalies.append(f"存在 {len(unknown_txns)} 笔未确认币种的交易，未纳入确认合计，需核实。")
        for row in selected:
            for purpose, gate in availability[row['id']].items():
                if gate['status'] != 'available':
                    anomalies.append(f"账单 #{row['id']} 用途 {purpose}: {gate['status']}；原因 {gate['reasons']}。")
        if not selected:
            anomalies.append('数据库无账单，无法生成账单分析。')

        seen_pairs = {}
        for r in selected:
            seen_pairs.setdefault((r['account_name'], r['bank_code']), []).append(r['statement_month'])
        py, pm = (today_date.year, today_date.month - 1) if today_date.month > 1 else (today_date.year - 1, 12)
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

        compact_statements = []
        for row in selected:
            item = {
                'id': row['id'],
                'statement_id': row['id'],
                'account': row['account_name'],
                'account_name': row['account_name'],
                'bank': row['bank_code'],
                'bank_code': row['bank_code'],
                'month': row['statement_month'],
                'statement_month': row['statement_month'],
                'updated_at': row.get('updated_at'),
                'due_date_source': _due_date_source(row.get('raw_fields_json'), row['due_date']),
                'due_date': row['due_date'],
                'total_due_source': _field_source(row.get('raw_fields_json'), 'total_due', row['total_due']),
                'total_due': row['total_due'],
                'minimum_due': row['minimum_due'],
                'is_paid': row['is_paid'],
                'created_at': row['created_at'],
                'message_id': row.get('message_id'),
                'purpose_availability': availability[row['id']],
                'balances': balances[row['id']],
            }
            compact_statements.append(item)

        transaction_totals = [
            {'currency': k[0], 'direction': k[1], 'amount': str(v), 'count': counts[k]}
            for k, v in sorted(sums.items())
        ]

        return {
            'schema_version': 1,
            'generated_at': datetime.now(timezone.utc).isoformat(),
            'as_of': today_date.isoformat(),
            'months': months,
            'scope_start_month': start,
            'scope': f'账期 {start} 起，另含各邮箱/银行最新历史账期',
            'latest_months': [{'account': key[0], 'bank': key[1], 'month': value} for key, value in latest.items()],
            'database_statement_count': len(database_rows),
            'failed_statement_count': len(failed_rows),
            'unscoped_statement_count': len(unscoped_rows),
            'statement_count': len(selected),
            'statements': compact_statements,
            'transaction_count': len(txns),
            'transaction_totals': transaction_totals,
            'unknown_currency_transactions': unknown_txns,
            'gated_transactions': gated_txns,
            'transaction_totals_complete': not unscoped_rows and not unknown_txns and not gated_txns and all(g['transaction_trends']['status'] == 'available' for g in availability.values()),
            'transactions': txns,
            'unknown_currency_original_amounts': unknown_orig_count,
            'coverage_gaps': coverage_gaps,
            'stale_accounts': stale_accounts,
            'anomalies': anomalies,
            'limitations': [
                '账单应还金额没有独立币种字段，不跨账单直接求和。',
                '交易按原币种和原方向分别汇总，不把还款、退款或外币直接当作消费。',
                '未还款状态来自数据库标记，不能据此确认现实中的逾期。',
                '余额、上期结转和利息等口径未核实，交易合计与应还金额不直接作为对账差异。',
            ],
            'model_detail_limit': detail_limit,
        }
    finally:
        conn.close()


def original_currency_display(txn):
    """Display source tokens separately from confirmed currencies and locations."""
    try:
        raw = json.loads(txn.get('raw_row_json') or '{}')
    except (TypeError, ValueError):
        raw = {}
    if not isinstance(raw, dict):
        raw = {}
    evidence = raw.get('currency_evidence') or {}
    original = (evidence.get('original') or {}) if isinstance(evidence, dict) else {}
    if not isinstance(original, dict):
        original = {}
    cell = str(original.get('cell') or '')
    amount_cell = str(original.get('amount_cell') or '')
    if not amount_cell and isinstance(raw.get('column_semantics'), list) and isinstance(raw.get('cols'), list):
        semantics, cols = raw['column_semantics'], raw['cols']
        if semantics.count('original_amount') == 1:
            index = semantics.index('original_amount')
            if index < len(cols):
                amount_cell = str(cols[index] or '')
    tokens = list(dict.fromkeys(re.findall(r'(?<![A-Za-z])[A-Za-z]{3}(?![A-Za-z])|[￥¥$＄€£₩]', amount_cell)))
    return {'original_currency_token': cell[:100] or '/'.join(tokens)[:100],
            'original_amount_raw': amount_cell[:200],
            'original_currency_confirmed': confirmed_currency(txn.get('original_currency'), txn.get('original_currency_source'))}


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
                       t.original_amount, t.original_currency, t.currency_source, t.original_currency_source,
                       s.raw_fields_json, s.id AS statement_id, s.statement_month, s.account_name
                FROM statement_transactions t
                JOIN statements s ON s.id = t.statement_id
                WHERE s.parse_status = 'ok' AND s.statement_month >= ? {acct_clause}
                ORDER BY t.id""",
            params).fetchall()
        quality_statements=[{'statement_id':r['id'],'purpose_availability':{
            p:stored_purpose(r['raw_fields_json'],p) for p in ('transaction_trends','fx_analysis')}}
            for r in conn.execute(f"SELECT s.id,s.raw_fields_json FROM statements s WHERE s.parse_status='ok' AND s.statement_month>=? {acct_clause}",params)]
    finally:
        conn.close()

    m_filter = merchant_of(merchant) if merchant else None
    stats = {}  # (m, month) -> {cur -> cur_agg}
    excluded=[]
    fx_excluded=[]
    partial=[]
    merchant_fx_pairs = {}  # m -> {fx_key -> [{'month': m, 'rate': Decimal, 'settlement': Decimal, 'original': Decimal}]}

    for r in rows:
        d = decimal_amount(r['amount'])
        if d is None:
            continue
        m = merchant_of(r['description'])
        if m_filter and m != m_filter:
            continue
        month = r['statement_month'] or (r['txn_date'] or '')[:7] or 'unknown'
        gate=stored_purpose(r['raw_fields_json'],'transaction_trends')
        if gate['status']=='blocked' or not confirmed_currency(r['currency'],r['currency_source']):
            excluded.append({'statement_id':r['statement_id'],'merchant':m,'month':month,'amount':r['amount'],
                'reported_currency':r['currency'],'currency_source':r['currency_source'],'purpose_availability':gate})
            continue
        if gate['status']=='partial':
            partial.append({'statement_id':r['statement_id'],'merchant':m,'month':month,'purpose_availability':gate})
        cur = r['currency']
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
        if orig_amt is not None and not confirmed_currency(orig_cur,r['original_currency_source']):
            fx_excluded.append({'statement_id':r['statement_id'],'merchant':m,'month':month,'original_amount':orig_amt,'reported_currency':orig_cur,'currency_source':r['original_currency_source']})
        if confirmed_currency(orig_cur,r['original_currency_source']) and orig_amt is not None and stored_purpose(r['raw_fields_json'],'fx_analysis')['status'] != 'blocked':
            od = decimal_amount(orig_amt)
            if od is not None and od != 0:
                rate = abs(d) / abs(od)
                fx_key = f"{orig_cur}/{cur}"
                fx_entry = cur_agg['fx_pairs'].setdefault(fx_key, {
                    'rates': [],
                    'total_settlement_amount': Decimal('0'),
                    'total_original_amount': Decimal('0')
                })
                fx_entry['rates'].append(rate)
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
                            'sample_rates': [str(x.quantize(Decimal('0.000001'))) for x in rates],
                            'sample_rate': str(rates[0].quantize(Decimal('0.000001'))),
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
                'sample_rates': [str(r.quantize(Decimal('0.000001'))) for r in pair_rates],
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
        'complete': not excluded and not partial and all(s['purpose_availability']['transaction_trends']['status']=='available' for s in quality_statements),
        'fx_complete': not fx_excluded and all(s['purpose_availability']['fx_analysis']['status']=='available' for s in quality_statements),
        'fx_excluded_transactions': fx_excluded,
        'quality_statements': quality_statements,
        'excluded_transactions': excluded,
        'partial_statements': list({(r['statement_id'],r['merchant'],r['month']):r for r in partial}.values()),
        'limitations': [
            '商户名为规范化形式（大小写/全半角归一），原始描述保留在交易明细',
            '折算比例为实际入账/原币金额，不等于央行汇率，也不自动认定包含某项费用',
            '金额使用 Decimal 精确累计，避免浮点误差',
        ],
    }
