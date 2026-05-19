from __future__ import annotations
from typing import Any, Dict, List, Literal, Optional
from pydantic import BaseModel, Field


class ColumnMapping(BaseModel):
    """Maps non-canonical column names to the canonical ones the engine expects.

    For a bookkeeper changeover (Scenario 07) where the export uses different
    column names, provide one of these with the actual names in the file.
    """
    account_number: str = "account_number"
    account_name: str = "account_name"
    account_type: str = "account_type"
    debit: str = "debit"
    credit: str = "credit"
    # If the file has a single signed net-balance column instead of debit/credit,
    # name it here. Positive = debit balance, negative = credit balance.
    ending_balance: Optional[str] = None


class AccountTypeMapping(BaseModel):
    """Maps variant account-type labels to canonical ones.

    Extend or override the defaults when the new bookkeeper uses different
    terminology (e.g. 'Current Asset' instead of 'Asset').
    """
    asset: list[str] = Field(default_factory=lambda: [
        "asset", "current asset", "fixed asset", "bank",
        "other asset", "other current asset", "noncurrent asset",
        "current_asset", "fixed_asset",
    ])
    contra_asset: list[str] = Field(default_factory=lambda: [
        "contra-asset", "contra asset", "contra_asset",
    ])
    liability: list[str] = Field(default_factory=lambda: [
        "liability", "current liability", "non-current liability",
        "noncurrent liability", "long-term liability", "other liability",
        "other current liability", "current_liability", "non_current_liability",
    ])
    equity: list[str] = Field(default_factory=lambda: [
        "equity", "owner's equity", "shareholders equity",
        "stockholders equity",
    ])
    revenue: list[str] = Field(default_factory=lambda: [
        "revenue", "income", "sales", "other income",
    ])
    expense: list[str] = Field(default_factory=lambda: [
        "expense", "cost of goods sold", "cogs", "cost of sales",
        "direct costs", "overhead", "cost of services",
        "direct_costs", "other expense",
    ])


class SchemaDetection(BaseModel):
    """Result of inspecting a ledger CSV's column layout."""
    is_canonical: bool
    detected_columns: list[str]
    canonical_columns: list[str] = [
        "account_number", "account_name", "account_type", "debit", "credit"
    ]
    suggested_mapping: Dict[str, str] = {}
    missing_canonical: List[str] = []


class CheckResult(BaseModel):
    check: str
    status: Literal["pass", "warn", "fail"]
    message: str
    detail: Dict[str, Any] = {}


class RequiredInput(BaseModel):
    """Describes a piece of information the caller must supply before the
    rollover can be completed or fully trusted."""
    field: str
    label: str
    description: str
    options: Optional[List[str]] = None


class REResult(BaseModel):
    """The computed retained-earnings figures for both years."""
    prior_year: int
    current_year: int

    prior_beginning_re: float
    prior_net_income: float
    prior_distributions: float
    prior_computed_ending_re: float
    prior_schedule_l_re: float
    prior_book_schedule_l_diff: float

    current_beginning_re_book: float
    current_beginning_re_schedule_l: float
    current_beginning_mismatch: float
    current_net_income: float
    current_distributions: float
    current_computed_ending_re: float
    current_schedule_l_re: float
    current_book_schedule_l_diff: float

    has_deficit: bool
    has_distributions: bool
    has_book_schedule_l_difference: bool
    has_beginning_mismatch: bool
    new_accounts: List[Dict[str, Any]]


class RollResponse(BaseModel):
    """The full result of a retained-earnings rollover.

    status:
      'ok'             — all checks passed, result is trustworthy.
      'warnings'       — rolled successfully but the caller should review flagged items.
      'requires_input' — cannot complete without additional information from the caller.
      'error'          — input data is invalid; result is absent.

    next_step: plain-English guidance for what to do next,
      useful for both human display and AI-agent reasoning.
    """
    status: Literal["ok", "warnings", "requires_input", "error"]
    result: Optional[REResult] = None
    checks: List[CheckResult] = []
    warnings: List[str] = []
    required_inputs: List[RequiredInput] = []
    errors: List[str] = []
    next_step: Optional[str] = None


# ── Request shapes ────────────────────────────────────────────────────────────

class RollJsonRequest(BaseModel):
    """JSON body for POST /api/roll/json — preferred for AI-agent and
    software-to-software use where reading files is inconvenient."""
    prior_ledger_csv: str
    prior_schedule_l_csv: str
    current_ledger_csv: str
    current_schedule_l_csv: str
    prior_year: int = 2023
    current_year: int = 2024
    entity_name: Optional[str] = None
    column_mapping: Optional[ColumnMapping] = None
    account_type_mapping: Optional[AccountTypeMapping] = None
    resolutions: Optional[Dict[str, str]] = None


class DetectSchemaRequest(BaseModel):
    csv_content: str


class PreflightRequest(BaseModel):
    prior_ledger_csv: str
    current_ledger_csv: str
    account_type_mapping: Optional[AccountTypeMapping] = None


class PreflightResponse(BaseModel):
    new_accounts: List[Dict[str, Any]] = []
    column_mapping: Optional[ColumnMapping] = None
    error: Optional[str] = None
