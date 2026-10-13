#!/usr/bin/env python3
"""Domain entities for parsed statements and validation results."""

from dataclasses import dataclass, field as _dc_field
from typing import List, Optional


@dataclass
class ValidationIssue:
    code: str
    field: Optional[str] = None
    rule_id: Optional[str] = None
    severity: str = "error"
    left: Optional[str] = None
    right: Optional[str] = None
    op: Optional[str] = None


@dataclass
class ValidationResult:
    passed: bool = False
    errors: List[ValidationIssue] = _dc_field(default_factory=list)
    warnings: List[ValidationIssue] = _dc_field(default_factory=list)


# 字段值来源状态：不以真假值判断有效性（详见重构契约 §2.1）
#   extracted        原文提取成功
#   source_missing   已核查对应区域，原文未提供
#   extraction_failed 原文含可识别字段，但解析缺失或错位
#   not_applicable   原文明确不适用
#   inferred         规则推算（如账单日+18 天），须带 rule_version
#   unknown          无法确定原文是否提供
FIELD_SOURCES = ('extracted', 'source_missing', 'extraction_failed',
                 'not_applicable', 'inferred', 'unknown')

# 质量事件影响的业务用途：互相隔离（缺还款日不阻财务统计）
QUALITY_PURPOSES = ('financial_summary', 'repayment_reminder', 'fx_analysis', 'transaction_trends')

# 诊断代码（重构契约 §2.1）
DIAGNOSTIC_CODES = (
    'FORMAT_CHANGED',              # 结构指纹偏离已确认基线
    'EXTRACTION_FAILED',           # 原文有字段但提取失败/错位
    'FIELD_SOURCE_UNKNOWN',        # source evidence insufficient, require review
    'SOURCE_FIELD_MISSING',        # 原文未提供该字段
    'CURRENCY_UNKNOWN',            # 原币币种无证据（不得默认 CNY / 猜地区币种）
    'TRANSACTION_ROW_INVALID',     # 明细行解析被拒（金额/描述无效）
    'TRANSACTION_COVERAGE_GAP',    # 声明有交易或存在交易区域但有效行数为 0
    'UNKNOWN_STATEMENT_TEMPLATE',  # 未命中模板但疑似账单
    'RECONCILIATION_MISMATCH',     # 模板有完整对账公式且字段齐全时的差额告警
)


@dataclass
class Diagnostic:
    code: str
    field: Optional[str] = None
    severity: str = 'warning'
    reason: str = ''
    affected_purposes: List[str] = _dc_field(default_factory=list)
    evidence_ref: Optional[dict] = None

    def to_dict(self):
        return {
            'code': self.code, 'field': self.field, 'severity': self.severity,
            'reason': self.reason, 'affected_purposes': list(self.affected_purposes),
            'evidence_ref': self.evidence_ref,
        }

    @staticmethod
    def from_dict(d):
        return Diagnostic(
            code=d.get('code', ''), field=d.get('field'), severity=d.get('severity', 'warning'),
            reason=d.get('reason', ''), affected_purposes=list(d.get('affected_purposes') or []),
            evidence_ref=d.get('evidence_ref'),
        )


@dataclass
class QualityEventRecord:
    """持久化质量事件（statements.db.quality_events），由事件消费方拉取。"""
    case_key: str
    event_type: str                 # quality_detected / quality_updated / quality_resolved / statement_recorded
    account_name: Optional[str] = None
    bank_code: Optional[str] = None
    template_version: Optional[str] = None
    diagnostics: List[Diagnostic] = _dc_field(default_factory=list)
    evidence_refs: List[dict] = _dc_field(default_factory=list)
    occurrence_count: int = 1
    revision: int = 1               # 单调递增版本号，用于条件确认与并发保护
    cycle_id: int = 1               # 发生周期计数，解决后复发递增开启新周期
    event_id: Optional[str] = None  # 由 case_key 确定性派生（statement_db 负责）


@dataclass
class StatementBalanceRecord:
    """多币种应还余额（statement_balances），statements.total_due 保留为兼容字段。"""
    currency: str                   # 'CNY' / 'USD' / ...；无证据用 'UNKNOWN'
    total_due: Optional[str] = None
    minimum_due: Optional[str] = None
    currency_source: Optional[str] = None   # rule_declared / cell_symbol / legacy_assumed / NULL
    evidence: dict = _dc_field(default_factory=dict)


@dataclass
class StatementRecord:
    uid: str
    message_id: str
    bank_code: str
    rule_id: str
    subject: str
    sender: str
    email_date: str
    account_name: str = 'default'
    statement_month: Optional[str] = None
    statement_date: Optional[str] = None
    due_date: Optional[str] = None
    total_due: Optional[str] = None
    minimum_due: Optional[str] = None
    raw_fields_json: str = "{}"
    # 'ok' 合法账单；'failed' 命中账单模板但校验未过（保留证据，排除出财务汇总，供修复后重解析）
    parse_status: str = "ok"


@dataclass
class StatementTransactionRecord:
    uid: str
    bank_code: str
    account_name: str = 'default'
    statement_id: Optional[int] = None
    txn_date: Optional[str] = None
    post_date: Optional[str] = None
    description: str = ""
    amount: Optional[str] = None
    currency: str = "CNY"
    # 币种证据来源：extracted / rule_declared / cell_symbol / legacy_assumed / NULL（未知）。禁止从地区代码推导。
    currency_source: Optional[str] = None
    txn_location_code: Optional[str] = None
    original_amount: Optional[str] = None
    # 原币币种：仅当邮件提供明确证据（表头声明/文字标注）；无证据为 NULL，展示"未知"
    original_currency: Optional[str] = None
    original_currency_source: Optional[str] = None
    direction: Optional[str] = None
    raw_row_json: str = "{}"


@dataclass
class EmailSummaryRecord:
    account_name: str
    uid: str
    sender: str
    subject: str
    email_date: str
    category: Optional[str] = None
    importance: Optional[str] = None
    summary: Optional[str] = None
    actions_json: str = "[]"
    deadline: Optional[str] = None
    deadline_raw: Optional[str] = None
    status: str = "pending"
    retry_count: int = 0
    processed_at: Optional[str] = None
    id: Optional[int] = None
