"""Deterministic source evidence. No inference from merchants, regions or ratios."""
import json
import re
from decimal import Decimal, InvalidOperation
from mail_quality import dedupe_table_blocks, split_markdown_table_blocks, normalize_header_cell
from statement_models import StatementTransactionRecord, Diagnostic

HEADER_ALIASES = {
 'txn_date': ['交易日','交易日期','transaction date'],
 'post_date': ['记账日','记账日期','入账日期','入账日','post date'],
 'description': ['交易描述','交易摘要','交易说明','摘要','商户名称','description'],
 'amount': ['人民币金额','入账金额','交易金额','金额','settlement amount'],
 'currency': ['币种','入账币种','结算币种','settlement currency'],
 'original_amount': ['原币金额','原始金额','original amount'],
 'original_currency': ['原币币种','原始币种','original currency'],
 'location': ['地区','交易地区'],
 'ignored': ['序号','卡号后四位','卡号','交易卡号'],
}
CURRENCIES = {'CNY':'CNY','RMB':'CNY','人民币':'CNY','USD':'USD','美元':'USD',
 'JPY':'JPY','日元':'JPY','EUR':'EUR','欧元':'EUR','HKD':'HKD','港币':'HKD',
 'GBP':'GBP','英镑':'GBP','AUD':'AUD','澳元':'AUD','CAD':'CAD','加元':'CAD',
 'SGD':'SGD','新加坡元':'SGD','CHF':'CHF','瑞士法郎':'CHF','KRW':'KRW','韩元':'KRW'}

def currency_evidence(text):
 tokens = re.findall(r'(?<![A-Za-z])[A-Za-z]{3}(?![A-Za-z])|人民币|美元|日元|欧元|港币|英镑|澳元|加元|新加坡元|瑞士法郎|韩元', text or '')
 currencies = {CURRENCIES[t.upper() if t.isascii() else t] for t in tokens if (t.upper() if t.isascii() else t) in CURRENCIES}
 return next(iter(currencies)) if len(currencies)==1 else None

def number(text):
 cleaned = re.sub(r'CNY|RMB|USD|JPY|EUR|HKD|GBP|AUD|CAD|SGD|CHF|KRW|人民币|美元|日元|欧元|港币|英镑|澳元|加元|新加坡元|瑞士法郎|韩元', '', text or '', flags=re.I)
 cleaned = re.sub(r'[,\s￥¥$€£]', '', cleaned)
 try:
  value = Decimal(cleaned)
  return value if value.is_finite() else None
 except InvalidOperation:
  return None

def semantic_header(cell, extra=None):
 norm = normalize_header_cell(cell)
 for key, vals in HEADER_ALIASES.items():
  if norm in [normalize_header_cell(v) for v in vals]: return key
 # A currency-qualified amount header is explicit evidence, not a default.
 stripped = re.sub(r'\s*[(（]?(?:CNY|RMB|USD|JPY|EUR|HKD|GBP|人民币|美元|日元)[)）]?\s*', '', cell, flags=re.I)
 if stripped != cell: return semantic_header(stripped, extra)
 for canonical, variants in (extra or {}).items():
  if norm in [normalize_header_cell(v) for v in [canonical]+list(variants or [])]:
   return semantic_header(canonical)
 return None

def transaction_blocks(markdown, rule):
 out=[]
 aliases=(rule.get('quality') or {}).get('table_header_aliases')
 for block in dedupe_table_blocks(split_markdown_table_blocks(markdown)):
  cells=[c.strip() for c in block[0].strip().strip('|').split('|')]
  semantics=[semantic_header(c, aliases) for c in cells]
  # The date column identifies the detail region even after critical columns disappear.
  if 'txn_date' in semantics or {'description', 'amount'}.issubset(set(semantics)):
   out.append((block,cells,semantics))
 return out

def detail_fingerprint(markdown, rule):
 headers=[s for _,_,s in transaction_blocks(markdown,rule)]
 return {'table_headers':headers,'column_counts':[len(h) for h in headers], 'table_block_count':len(headers)}

def parse_semantic_transactions(uid, month, markdown, rule, date_parser):
 blocks=transaction_blocks(markdown, rule)
 if not blocks: return None, {'candidate_row_count':0,'rejected_row_count':0,'table_block_count':0}, []
 rows=[]; rejected=0; candidates=0; diags=[]
 declaration=(rule.get('quality') or {}).get('currency_declaration') or {}
 for block,headers,semantics in blocks:
  mapping={s:i for i,s in enumerate(semantics) if s and s!='ignored'}
  ambiguous=any(semantics.count(s)>1 for s in mapping)
  for line in block[1:]:
   cols=[c.strip() for c in line.strip().strip('|').split('|')]
   if all(not c or re.fullmatch(r':?-{2,}:?',c) for c in cols): continue
   candidates+=1
   get=lambda key: cols[mapping[key]] if key in mapping and mapping[key]<len(cols) else ''
   raw={'headers':headers,'cols':cols,'column_semantics':semantics}
   amount=number(get('amount')); txn_date=date_parser(get('txn_date'),month)
   if ambiguous or len(cols)!=len(headers) or amount is None or not get('description') or not txn_date:
    rejected+=1
    diags.append(Diagnostic(code='TRANSACTION_ROW_INVALID', field='transactions',
     reason='Detail row has missing/ambiguous columns or invalid date/amount/description',
     affected_purposes=['transaction_trends','fx_analysis'], evidence_ref=raw))
    continue
   def cur_for(curkey,amountkey,declkey):
    # Explicit currency cells are claims: ambiguous/unsupported text must not
    # fall through to an otherwise valid currency-qualified amount header.
    if get(curkey) and not currency_evidence(get(curkey)):
     return None,None
    evidences=[currency_evidence(get(curkey)),currency_evidence(get(amountkey))]
    if amountkey in mapping: evidences.append(currency_evidence(headers[mapping[amountkey]]))
    explicit={x for x in evidences if x}
    if len(explicit)>1: return None,None
    if explicit: return next(iter(explicit)),'extracted'
    # Only independently reviewed declarations, never hard-coded historical defaults.
    declared=declaration.get(declkey) if declaration.get('evidence') else None
    return (declared,'rule_declared') if declared in set(CURRENCIES.values()) else (None,None)
   cur,cur_source=cur_for('currency','amount','settlement_currency')
   orig,orig_source=cur_for('original_currency','original_amount','original_currency')
   original=number(get('original_amount')) if get('original_amount') else None
   if get('original_amount') and original is None:
    diags.append(Diagnostic(code='TRANSACTION_ROW_INVALID', field='original_amount',
     reason='Original amount exists but cannot be normalized',
     affected_purposes=['fx_analysis'], evidence_ref=raw))
   raw['currency_evidence']={'settlement':{'currency':cur,'source':cur_source,'cell':get('currency'),'amount_cell':get('amount')},
    'original':{'currency':orig,'source':orig_source,'cell':get('original_currency'),'amount_cell':get('original_amount')}}
   rows.append(StatementTransactionRecord(uid=str(uid),bank_code=rule['bank_code'],
    txn_date=txn_date,post_date=date_parser(get('post_date'),month),description=get('description'),amount=str(amount),
    currency=cur or 'UNKNOWN',currency_source=cur_source,original_amount=str(original) if original is not None else None,
    original_currency=orig,original_currency_source=orig_source,txn_location_code=get('location') or None,
    direction='credit' if amount<0 else 'debit',raw_row_json=json.dumps(raw,ensure_ascii=False)))
 return rows,{'candidate_row_count':candidates,'rejected_row_count':rejected,'table_block_count':len(blocks)},diags

def field_meta(name, value, raw, matched_text, cfg, source_text, inferred=False, rule_version=None):
 evidence={'source':cfg.get('source','body_text'),'matched_text':matched_text,'raw_value':raw}
 if value is not None:
  state='inferred' if inferred else 'extracted'
 elif raw is not None:
  state='extraction_failed'; evidence['reason']='matched value failed normalization'
 else:
  probe=cfg.get('source_probe') or {}
  label=probe.get('label_pattern'); region=probe.get('region_pattern')
  lm=re.search(label,source_text or '',re.I) if label else None
  if lm:
   state='extraction_failed'; evidence['label_text']=lm.group(0); evidence['reason']='source label exists but extraction rule did not match'
  elif label and region and re.search(region,source_text or '',re.I):
   state='source_missing'; evidence['checked_region']=region; evidence['reason']='confirmed region present; declared source label absent'
  else:
   state='unknown'; evidence['reason']='no reliable source label/region evidence'
 result={'value':value,'source':state,'evidence':evidence}
 if inferred:
  result['rule_version']=rule_version
  result['evidence']['reason']='value derived by rule fallback; not directly extracted'
 return result

def purpose_availability(diagnostics, fields_meta=None):
 purposes={p:{'status':'available','reasons':[]} for p in ('financial_summary','transaction_trends','fx_analysis','repayment_reminder')}
 for d in diagnostics:
  d=d.to_dict() if hasattr(d,'to_dict') else d
  for p in d.get('affected_purposes') or []:
   if p in purposes:
    purposes[p]['reasons'].append({'code':d.get('code'),'field':d.get('field'),'reason':d.get('reason')})
    if purposes[p]['status']!='blocked':
     purposes[p]['status']='partial' if p in ('financial_summary','transaction_trends','fx_analysis') else 'blocked'
    if d.get('code')=='TRANSACTION_COVERAGE_GAP' and p in ('transaction_trends','fx_analysis'):
     purposes[p]['status']='blocked'
 for field in ('due_date','total_due','statement_month'):
  meta=(fields_meta or {}).get(field) or {}
  if meta and (meta.get('source')!='extracted' or meta.get('value') is None):
   purposes['repayment_reminder']['status']='blocked'
   purposes['repayment_reminder']['reasons'].append({'field':field,'code':'FIELD_UNCONFIRMED','reason':meta.get('source')})
 return purposes


def stored_purpose(raw_fields_json, purpose):
 """Decode a persisted gate without treating missing/broken evidence as verified."""
 try:
  data=json.loads(raw_fields_json or '{}')
  gate=data.get('quality',{}).get('purpose_availability',{}).get(purpose,{})
  if gate.get('status') in ('available','partial','blocked'):
   return {'status':gate['status'],'reasons':gate.get('reasons') or []}
 except (ValueError,TypeError,AttributeError):
  pass
 return {'status':'blocked' if purpose=='repayment_reminder' else 'partial',
         'reasons':[{'code':'QUALITY_UNVERIFIED','reason':'No verified purpose evidence; reparse required'}]}


def confirmed_currency(currency, source):
 return source in ('extracted','rule_declared') and currency in set(CURRENCIES.values())
