# Milestone Checklist (Execution + Acceptance)

## M1. Standards Freeze
- [ ] Finalize statement and transaction data dictionary.
- [ ] Finalize validation equations and tolerance policy.
- [ ] Finalize error code taxonomy.
- [ ] Finalize report JSON contract.

Acceptance:
- [ ] Signed-off schema document.
- [ ] Example parse output validated against schema.

## M2. Parser Core Framework
- [ ] Implement rule loader (YAML schema validation).
- [ ] Implement template matcher (sender/subject/body signatures).
- [ ] Implement extraction engine (regex/table selectors).
- [ ] Implement normalization engine (date/amount/currency).
- [ ] Implement validation engine (field/statement/transaction checks).
- [ ] Implement deterministic output hashing.

Acceptance:
- [ ] Core unit tests pass >= 95% target coverage.
- [ ] Re-run determinism test pass rate = 100%.

## M3. Bank Adapters (HX/CMB/SPDB)
- [ ] Implement HX rules v1.
- [ ] Implement CMB rules v1.
- [ ] Implement SPDB rules v1.
- [ ] Build fallback strategy for minor format drift.

Acceptance:
- [ ] Each bank: latest 3 months parse success >= 99%.
- [ ] Required field completeness meets threshold.

## M4. Validation and Regression Pipeline
- [ ] Build golden dataset repository.
- [ ] Build batch replay runner.
- [ ] Build validation report generator.
- [ ] Build regression comparator with failure gates.

Acceptance:
- [ ] No critical regression diff on golden set.
- [ ] Gate logic blocks releases when thresholds fail.

## M5. Full-History Verification
- [ ] Run full historical replay in batches.
- [ ] Classify all failures by error code.
- [ ] Fix rule gaps and rerun until stable.

Acceptance:
- [ ] Full-history parse success >= 98% first target, then >= 99.5%.
- [ ] All P0/P1 issues closed or explicitly accepted.

## M6. Production Operation
- [ ] Enable default recent-3-month job.
- [ ] Enable dedicated full-history job.
- [ ] Enable alert rules (due-date/large amount/FX).
- [ ] Publish runbook and on-call checklist.

Acceptance:
- [ ] 2 consecutive cycles with stable outputs.
- [ ] Alert precision reviewed and approved.

## Required Artifacts Per Milestone
- [ ] Design/decision notes.
- [ ] Test evidence and metric snapshot.
- [ ] Validation report sample.
- [ ] Known risks and next actions.
