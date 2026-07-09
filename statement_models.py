#!/usr/bin/env python3
"""Domain entities for parsed statements and validation results."""

from dataclasses import dataclass, field
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
    errors: List[ValidationIssue] = field(default_factory=list)
    warnings: List[ValidationIssue] = field(default_factory=list)


@dataclass
class StatementRecord:
    uid: str
    message_id: str
    bank_code: str
    rule_id: str
    subject: str
    sender: str
    email_date: str
    statement_month: Optional[str] = None
    statement_date: Optional[str] = None
    due_date: Optional[str] = None
    total_due: Optional[str] = None
    minimum_due: Optional[str] = None
    raw_fields_json: str = "{}"


@dataclass
class StatementTransactionRecord:
    uid: str
    bank_code: str
    txn_date: Optional[str] = None
    post_date: Optional[str] = None
    description: str = ""
    amount: Optional[str] = None
    currency: str = "CNY"
    txn_location_code: Optional[str] = None
    original_amount: Optional[str] = None
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
