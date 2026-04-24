# Bill Parser Development Plan (Rule-Based, No AI Semantics)

## 1. Scope and Goals
- Build bank-specific statement parsers using deterministic rules.
- Validate statement-level and transaction-level amounts, dates, and key descriptions.
- Support two flows:
  - Default flow: recent 3 months for alerts.
  - Full flow: historical backfill for yearly and special analysis.

## 2. Non-Negotiable Principles
- No semantic AI extraction in parsing path.
- All extracted fields must be traceable to source snippets.
- Every parse run must output a validation report.
- Rule versions must be immutable once released.

## 3. Architecture
- Ingestion: mailbox fetch and candidate filtering.
- Template Identification: bank + template-version matcher.
- Parsing: adapter plugin per bank and template version.
- Validation: field, statement, transaction, and cross-run checks.
- Storage: raw source, parsed records, validation results, error samples.
- Alerting: due-date, large amount, FX presence, unusual changes.

## 4. Standard Data Dictionary

### 4.1 Statement Fields
- bank_code
- card_last4
- statement_month
- statement_date
- due_date
- total_due
- minimum_due
- credit_limit
- available_limit
- cny_total
- fx_total
- installment_total
- message_uid
- message_id
- parser_version
- rule_version

### 4.2 Transaction Fields
- txn_date
- post_date
- value_date
- description
- merchant
- amount
- currency
- direction (debit|credit)
- original_amount
- exchange_rate
- fee_amount
- source_ref (line/table-cell reference)

### 4.3 Numeric and Date Rules
- Money type: Decimal only.
- Precision: currency smallest unit.
- Date format in storage: ISO-8601 date string.
- Never coerce invalid values silently.

## 5. Adapter Strategy
- One adapter package per bank.
- One rule file per bank template version.
- Rule file sections:
  - match_rules
  - extract_rules
  - normalize_rules
  - validate_rules
- Backward compatibility:
  - add new version file for template changes.
  - do not overwrite old released rules.

## 6. Validation Details and Acceptance Standards

### 6.1 Field-Level Validation
- Required statement fields present: 100%.
- Required transaction fields present: >= 99.5%.
- Numeric parse success (required numeric fields): 100%.
- Date parse success (required date fields): 100%.

### 6.2 Statement-Level Validation
- Balance equation must hold within tolerance:
  opening_balance + new_charges - payments +/- adjustments = closing_balance
- minimum_due <= total_due
- due_date >= statement_date
- If FX section exists, fx_total > 0 and currency data present.

### 6.3 Transaction-Level Validation
- Sum(transaction.amount by direction) must reconcile with statement aggregates.
- Every transaction must have non-empty description and valid amount.
- Duplicate transaction detection by hash:
  hash(txn_date, amount, description, merchant)

### 6.4 Cross-Run and Regression Validation
- Same input + same rule version => identical output hash.
- Golden dataset regression must have zero critical diff.
- Parse failure samples must be persisted with reason codes.

## 7. Full Validation Plan
- Build golden datasets for each bank and template version.
- Include edge cases:
  - negative adjustments
  - FX transactions
  - installments
  - refunds
  - malformed but real-world noisy text
- Run full historical replay in batches.
- Output coverage, accuracy, and failure taxonomy.

## 8. Milestones
1. Freeze dictionary and validation rules.
2. Implement parser framework and rule loader.
3. Implement adapters for HX/CMB/SPDB first.
4. Build automated validation and regression pipeline.
5. Run full history validation and close critical gaps.
6. Enable production default 3-month flow.

## 9. Release Gates
- No P0/P1 validation failures.
- Required-field completeness and amount/date accuracy meet thresholds.
- Regression pass on all golden datasets.
- Alert outputs verified on latest 3 monthly statements per bank.

## 10. Deliverables
- Rule schema and bank-specific rule files.
- Validation report artifacts (machine-readable + human summary).
- Regression baseline and comparison report.
- Runbook for default flow and full-history flow.
