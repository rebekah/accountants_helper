"""rolling_retained_earnings — Python library interface.

Quick start
-----------
    from rolling_retained_earnings.src import roll

    with open("ledger_2023.csv") as f:
        prior_ledger = f.read()
    # ... same for the other three files ...

    result = roll(
        prior_ledger_csv=prior_ledger,
        prior_schedule_l_csv=prior_sl,
        current_ledger_csv=current_ledger,
        current_schedule_l_csv=current_sl,
    )
    print(result.status)               # 'ok' | 'warnings' | 'requires_input' | 'error'
    print(result.result.current_computed_ending_re)
    print(result.model_dump_json(indent=2))

Non-canonical ledger (bookkeeper changeover)
--------------------------------------------
    from rolling_retained_earnings.src import roll, ColumnMapping, AccountTypeMapping

    result = roll(
        ...,
        column_mapping=ColumnMapping(
            account_number="gl_code",
            account_name="gl_description",
            account_type="classification",
            debit="period_debits",
            credit="period_credits",
        ),
        account_type_mapping=AccountTypeMapping(
            asset=["current asset", "fixed asset", "bank"],
            liability=["current liability", "non-current liability"],
        ),
    )
"""
from src.engine import roll_retained_earnings
from typing import Optional
from src.models import (
    AccountTypeMapping,
    ColumnMapping,
    REResult,
    RollResponse,
    SchemaDetection,
)
from src.reader import detect_schema, normalize_ledger, normalize_schedule_l


def roll(
    prior_ledger_csv: str,
    prior_schedule_l_csv: str,
    current_ledger_csv: str,
    current_schedule_l_csv: str,
    prior_year: int = 2023,
    current_year: int = 2024,
    column_mapping: Optional[ColumnMapping] = None,
    account_type_mapping: Optional[AccountTypeMapping] = None,
) -> RollResponse:
    """Roll retained earnings from prior_year into current_year.

    Args:
        prior_ledger_csv:      Year-end trial balance CSV for the prior year.
        prior_schedule_l_csv:  Schedule L CSV for the prior year.
        current_ledger_csv:    Year-end trial balance CSV for the current year.
        current_schedule_l_csv: Schedule L CSV for the current year.
        prior_year:            Fiscal year integer for the prior year (default 2023).
        current_year:          Fiscal year integer for the current year (default 2024).
        column_mapping:        Supply when the current-year ledger uses non-canonical
                               column names (e.g. after a bookkeeper changeover).
        account_type_mapping:  Supply when the current-year ledger uses non-standard
                               account type labels.

    Returns:
        RollResponse with status, computed RE figures, per-check results,
        warnings, required inputs, and next_step guidance.
    """
    all_warnings: list[str] = []

    # Auto-detect schema mismatch on current-year ledger
    if column_mapping is None:
        detection = detect_schema(current_ledger_csv)
        if not detection.is_canonical:
            return RollResponse(
                status="requires_input",
                required_inputs=[
                    {
                        "field": "column_mapping",
                        "label": "Current-year ledger has a non-standard schema",
                        "description": (
                            f"Expected columns: {detection.canonical_columns}. "
                            f"Found: {detection.detected_columns}. "
                            f"Suggested mapping: {detection.suggested_mapping}. "
                            "Pass a ColumnMapping to proceed."
                        ),
                        "options": None,
                    }
                ],
                next_step=(
                    "Call detect_schema() with the current-year ledger CSV to get a "
                    "suggested column mapping, then resubmit with column_mapping set."
                ),
            )

    prior_ledger, pw = normalize_ledger(prior_ledger_csv, account_type_mapping=account_type_mapping)
    all_warnings.extend(pw)

    current_ledger, cw = normalize_ledger(current_ledger_csv, column_mapping, account_type_mapping)
    all_warnings.extend(cw)

    prior_sl = normalize_schedule_l(prior_schedule_l_csv)
    current_sl = normalize_schedule_l(current_schedule_l_csv)

    response = roll_retained_earnings(
        prior_ledger, prior_sl,
        current_ledger, current_sl,
        prior_year, current_year,
        reader_warnings=all_warnings,
    )
    return response


__all__ = [
    "roll",
    "detect_schema",
    "normalize_ledger",
    "normalize_schedule_l",
    "roll_retained_earnings",
    "ColumnMapping",
    "AccountTypeMapping",
    "RollResponse",
    "REResult",
    "SchemaDetection",
]
