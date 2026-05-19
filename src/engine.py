"""Core retained-earnings rolling logic and edge-case checks."""
from __future__ import annotations

import difflib
import re
from typing import Any, Dict, List, Optional

import pandas as pd

from src.models import CheckResult, REResult, RequiredInput, RollResponse

# ── Account-name patterns ─────────────────────────────────────────────────────

_RE_PATTERNS = [
    r"retained", r"earnings", r"\bre\b", r"deficit",
    r"r/e", r"b/f", r"brought forward", r"accumulated adj",
]
_DIST_PATTERNS = [
    r"distribution", r"dividend", r"draw", r"withdrawal", r"owner.s draw",
]
_STOCK_PATTERNS = [
    r"stock", r"capital", r"paid.in", r"common", r"preferred", r"share", r"par",
]

# Schedule L line numbers that hold retained earnings
_SL_RE_LINES = {"25", "25a", "25b"}


def _matches(name: str, patterns: List[str]) -> bool:
    n = name.lower()
    return any(re.search(p, n) for p in patterns)


# ── Ledger helpers ────────────────────────────────────────────────────────────

def _beginning_re(ledger: pd.DataFrame) -> float:
    """Return the opening retained-earnings balance from the trial balance.

    Positive = credit balance (normal), negative = debit balance (deficit).
    Identifies the RE account by name; falls back to any non-stock, non-distribution
    equity account when the name doesn't match standard patterns.
    """
    equity = ledger[ledger["account_type"] == "equity"]

    re_rows = equity[
        equity["account_name"].apply(lambda n: _matches(n, _RE_PATTERNS))
        & ~equity["account_name"].apply(lambda n: _matches(n, _STOCK_PATTERNS))
    ]

    if re_rows.empty:
        # Fallback: equity that isn't stock or distributions
        re_rows = equity[
            ~equity["account_name"].apply(lambda n: _matches(n, _STOCK_PATTERNS))
            & ~equity["account_name"].apply(lambda n: _matches(n, _DIST_PATTERNS))
        ]

    return float(re_rows["credit"].sum() - re_rows["debit"].sum())


def _distributions(ledger: pd.DataFrame) -> float:
    """Return total distributions / dividends paid (always ≥ 0)."""
    equity = ledger[ledger["account_type"] == "equity"]
    dist_rows = equity[
        equity["account_name"].apply(lambda n: _matches(n, _DIST_PATTERNS))
    ]
    return float(max(0.0, dist_rows["debit"].sum() - dist_rows["credit"].sum()))


def _net_income(ledger: pd.DataFrame) -> float:
    revenue = ledger[ledger["account_type"] == "revenue"]["credit"].sum()
    expenses = ledger[ledger["account_type"] == "expense"]["debit"].sum()
    return float(revenue - expenses)


def _schedule_l_re(sl: pd.DataFrame, col: str) -> float:
    re_rows = sl[sl["line_number"].isin(_SL_RE_LINES)]
    if re_rows.empty:
        re_rows = sl[sl["description"].str.lower().str.contains("retained", na=False)]
    return float(re_rows[col].sum())


def _new_accounts(prior: pd.DataFrame, current: pd.DataFrame) -> List[Dict[str, Any]]:
    prior_nums = set(prior["account_number"])
    prior_names = set(prior["account_name"].str.lower())
    RE_RELEVANT = {"revenue", "expense", "equity"}
    new = current[
        ~current["account_number"].isin(prior_nums)
        & ~current["account_name"].str.lower().isin(prior_names)
        & current["account_type"].isin(RE_RELEVANT)
    ]

    result = []
    for _, row in new.iterrows():
        acct_type = row["account_type"]
        # Prefer same-type matches; only fall back to any-type if nothing found
        same_type_names = prior.loc[
            prior["account_type"] == acct_type, "account_name"
        ].str.lower().tolist()
        all_names = prior["account_name"].str.lower().tolist()

        matched_name = None
        for candidate_list in (same_type_names, all_names):
            hits = difflib.get_close_matches(
                row["account_name"].lower(), candidate_list, n=1, cutoff=0.4
            )
            if hits:
                matched_name = hits[0]
                break

        net_balance = round(float(row["credit"]) - float(row["debit"]), 2)
        if matched_name:
            match_row = prior[prior["account_name"].str.lower() == matched_name].iloc[0]
            confidence = difflib.SequenceMatcher(
                None, row["account_name"].lower(), matched_name
            ).ratio()
            # Discard cross-type matches with low confidence — likely false positives
            if match_row["account_type"] != acct_type and confidence < 0.55:
                matched_name = None

        if matched_name:
            match_row = prior[prior["account_name"].str.lower() == matched_name].iloc[0]
            confidence = difflib.SequenceMatcher(
                None, row["account_name"].lower(), matched_name
            ).ratio()
            result.append({
                "account_number": row["account_number"],
                "account_name": row["account_name"],
                "account_type": acct_type,
                "net_balance": net_balance,
                "suggested_number": match_row["account_number"],
                "suggested_name": match_row["account_name"],
                "suggested_type": match_row["account_type"],
                "confidence": round(confidence, 2),
            })
        else:
            result.append({
                "account_number": row["account_number"],
                "account_name": row["account_name"],
                "account_type": acct_type,
                "net_balance": net_balance,
                "suggested_number": None,
                "suggested_name": None,
                "suggested_type": None,
                "confidence": 0.0,
            })
    return result


def _fmt(v: float) -> str:
    return f"${v:,.0f}"


# ── Main function ─────────────────────────────────────────────────────────────

def roll_retained_earnings(
    prior_ledger: pd.DataFrame,
    prior_sl: pd.DataFrame,
    current_ledger: pd.DataFrame,
    current_sl: pd.DataFrame,
    prior_year: int,
    current_year: int,
    reader_warnings: Optional[List[str]] = None,
    resolutions: Optional[dict] = None,
) -> RollResponse:
    """Roll retained earnings from prior_year to current_year.

    All four DataFrames must already be in normalised form (see reader.py).
    Returns a RollResponse with status, computed figures, per-check results,
    warnings, required inputs, and guidance for the next step.
    """
    checks: List[CheckResult] = []
    warnings: List[str] = list(reader_warnings or [])
    required_inputs: List[RequiredInput] = []
    errors: List[str] = []
    res = resolutions or {}

    py = prior_year
    cy = current_year

    # ── Prior year ────────────────────────────────────────────────────────────

    p_beg_re = _beginning_re(prior_ledger)
    p_net_inc = _net_income(prior_ledger)
    p_dist = _distributions(prior_ledger)
    p_end_re = p_beg_re + p_net_inc - p_dist
    p_sl_re = _schedule_l_re(prior_sl, "end_of_year")
    p_diff = p_end_re - p_sl_re

    # Check: prior-year trial balance balances
    p_dr = prior_ledger["debit"].sum()
    p_cr = prior_ledger["credit"].sum()
    p_tb_off = abs(p_dr - p_cr)
    if p_tb_off < 1:
        checks.append(CheckResult(
            check="prior_trial_balance", status="pass",
            message=f"{py} trial balance is in balance.",
        ))
    else:
        checks.append(CheckResult(
            check="prior_trial_balance", status="fail",
            message=f"{py} trial balance is out of balance by {_fmt(p_tb_off)}.",
            detail={"difference": p_tb_off},
        ))
        errors.append(
            f"{py} trial balance does not balance (off by {_fmt(p_tb_off)}). "
            "Verify the ledger file."
        )

    # Check: prior-year book RE vs Schedule L RE
    if abs(p_diff) < 1:
        checks.append(CheckResult(
            check="prior_book_schedule_l_match", status="pass",
            message=f"{py} book RE matches Schedule L RE ({_fmt(p_end_re)}).",
        ))
    else:
        direction = "higher" if p_diff > 0 else "lower"
        checks.append(CheckResult(
            check="prior_book_schedule_l_match", status="warn",
            message=(
                f"{py} book RE ({_fmt(p_end_re)}) is {_fmt(abs(p_diff))} {direction} "
                f"than Schedule L RE ({_fmt(p_sl_re)}). "
                "Common cause: tax adjustments not posted back to the books."
            ),
            detail={"book_re": p_end_re, "schedule_l_re": p_sl_re, "difference": p_diff},
        ))
        warnings.append(f"{py} book-to-Schedule L difference: {_fmt(p_diff)}.")
        if "prior_year_re_basis" not in res:
            required_inputs.append(RequiredInput(
                field="prior_year_re_basis",
                label=f"Which {py} RE balance should carry into {cy}?",
                description=(
                    f"Book RE: {_fmt(p_end_re)} | Schedule L RE: {_fmt(p_sl_re)} | "
                    f"Difference: {_fmt(p_diff)}. "
                    "This is typically caused by tax adjustments (depreciation method, "
                    "capitalised expenses) that were not posted back to the GL. "
                    "The rollover uses the book basis by default."
                ),
                options=["book", "schedule_l"],
            ))

    # ── Current year ──────────────────────────────────────────────────────────

    c_beg_re_book = p_sl_re if res.get("prior_year_re_basis") == "schedule_l" else p_end_re
    c_beg_re_sl = _schedule_l_re(current_sl, "beginning_of_year")
    c_net_inc = _net_income(current_ledger)
    c_dist = _distributions(current_ledger)
    c_end_re = c_beg_re_book + c_net_inc - c_dist
    c_sl_re = _schedule_l_re(current_sl, "end_of_year")
    c_diff = c_end_re - c_sl_re
    beg_mismatch = c_beg_re_sl - p_sl_re

    # Check: Schedule L beginning-balance continuity
    if abs(beg_mismatch) < 1:
        checks.append(CheckResult(
            check="beginning_balance_continuity", status="pass",
            message=(
                f"{cy} Schedule L opening RE ({_fmt(c_beg_re_sl)}) "
                f"matches {py} Schedule L closing RE."
            ),
        ))
    else:
        checks.append(CheckResult(
            check="beginning_balance_continuity", status="fail",
            message=(
                f"{cy} Schedule L opening RE ({_fmt(c_beg_re_sl)}) does not match "
                f"{py} Schedule L closing RE ({_fmt(p_sl_re)}). "
                f"Difference: {_fmt(beg_mismatch)}. "
                "Possible causes: amended return, IRS audit adjustment, or data-entry error."
            ),
            detail={
                "prior_sl_ending": p_sl_re,
                "current_sl_beginning": c_beg_re_sl,
                "difference": beg_mismatch,
            },
        ))
        if "beginning_balance_explanation" not in res:
            required_inputs.append(RequiredInput(
                field="beginning_balance_explanation",
                label=f"Explain the {cy} opening balance mismatch",
                description=(
                    f"The {cy} Schedule L opening RE ({_fmt(c_beg_re_sl)}) differs from "
                    f"the {py} closing RE ({_fmt(p_sl_re)}) by {_fmt(beg_mismatch)}. "
                    "Provide a reconciling explanation before using these figures."
                ),
            ))

    # Check: current-year trial balance
    c_dr = current_ledger["debit"].sum()
    c_cr = current_ledger["credit"].sum()
    c_tb_off = abs(c_dr - c_cr)
    if c_tb_off < 1:
        checks.append(CheckResult(
            check="current_trial_balance", status="pass",
            message=f"{cy} trial balance is in balance.",
        ))
    else:
        checks.append(CheckResult(
            check="current_trial_balance", status="fail",
            message=f"{cy} trial balance is out of balance by {_fmt(c_tb_off)}.",
            detail={"difference": c_tb_off},
        ))
        errors.append(
            f"{cy} trial balance does not balance (off by {_fmt(c_tb_off)}). "
            "Verify the ledger file."
        )

    # Check: current-year book RE vs Schedule L RE
    if abs(c_diff) < 1:
        checks.append(CheckResult(
            check="current_book_schedule_l_match", status="pass",
            message=f"{cy} book RE matches Schedule L RE ({_fmt(c_end_re)}).",
        ))
    else:
        direction = "higher" if c_diff > 0 else "lower"
        checks.append(CheckResult(
            check="current_book_schedule_l_match", status="warn",
            message=(
                f"{cy} book RE ({_fmt(c_end_re)}) is {_fmt(abs(c_diff))} {direction} "
                f"than Schedule L RE ({_fmt(c_sl_re)})."
            ),
            detail={"book_re": c_end_re, "schedule_l_re": c_sl_re, "difference": c_diff},
        ))
        warnings.append(f"{cy} book-to-Schedule L difference: {_fmt(c_diff)}.")

    # Check: accumulated deficit
    has_deficit = c_end_re < 0 or p_end_re < 0
    if has_deficit:
        amt = min(p_end_re, c_end_re)
        checks.append(CheckResult(
            check="accumulated_deficit", status="warn",
            message=f"Accumulated deficit detected ({_fmt(amt)}). Review equity section.",
            detail={"deficit_amount": amt},
        ))
        warnings.append(f"Accumulated deficit: {_fmt(amt)}.")
    else:
        checks.append(CheckResult(
            check="accumulated_deficit", status="pass",
            message="No accumulated deficit.",
        ))

    # Check: distributions
    has_dist = p_dist > 0 or c_dist > 0
    if has_dist:
        checks.append(CheckResult(
            check="distributions", status="warn",
            message=(
                f"Distributions found — {py}: {_fmt(p_dist)}, {cy}: {_fmt(c_dist)}. "
                "These have been subtracted from retained earnings."
            ),
            detail={"prior_distributions": p_dist, "current_distributions": c_dist},
        ))
        warnings.append(
            f"Distributions: {py} {_fmt(p_dist)}, {cy} {_fmt(c_dist)}."
        )
    else:
        checks.append(CheckResult(
            check="distributions", status="pass",
            message="No distributions or dividends detected.",
        ))

    # Check: new accounts
    new_accts = _new_accounts(prior_ledger, current_ledger)
    if new_accts:
        names = [a["account_name"] for a in new_accts]
        checks.append(CheckResult(
            check="new_accounts", status="warn",
            message=(
                f"{len(new_accts)} account(s) in {cy} not present in {py}: "
                + ", ".join(names) + ". Verify Schedule L mapping."
            ),
            detail={"new_accounts": new_accts},
        ))
        warnings.append(f"{len(new_accts)} new account(s) in {cy}.")
    else:
        checks.append(CheckResult(
            check="new_accounts", status="pass",
            message=f"No new accounts in {cy} vs {py}.",
        ))

    # ── Overall status ────────────────────────────────────────────────────────

    if errors:
        status = "error"
        next_step = "Fix the input errors listed below and resubmit."
    elif required_inputs:
        status = "requires_input"
        next_step = (
            "One or more issues need your decision before the rollover is complete. "
            "Review required_inputs, supply the missing information, and resubmit. "
            "AI agents: add the resolved values to the 'options' field of the next request."
        )
    elif warnings:
        status = "warnings"
        next_step = (
            "The rollover completed but flagged items need review. "
            "Check each warning and confirm the figures are acceptable before filing."
        )
    else:
        status = "ok"
        next_step = (
            f"Rollover complete. "
            f"Use {_fmt(c_end_re)} as the {cy} ending retained earnings."
        )

    result = REResult(
        prior_year=py,
        current_year=cy,
        prior_beginning_re=p_beg_re,
        prior_net_income=p_net_inc,
        prior_distributions=p_dist,
        prior_computed_ending_re=p_end_re,
        prior_schedule_l_re=p_sl_re,
        prior_book_schedule_l_diff=p_diff,
        current_beginning_re_book=c_beg_re_book,
        current_beginning_re_schedule_l=c_beg_re_sl,
        current_beginning_mismatch=beg_mismatch,
        current_net_income=c_net_inc,
        current_distributions=c_dist,
        current_computed_ending_re=c_end_re,
        current_schedule_l_re=c_sl_re,
        current_book_schedule_l_diff=c_diff,
        has_deficit=has_deficit,
        has_distributions=has_dist,
        has_book_schedule_l_difference=abs(p_diff) >= 1 or abs(c_diff) >= 1,
        has_beginning_mismatch=abs(beg_mismatch) >= 1,
        new_accounts=new_accts,
    )

    return RollResponse(
        status=status,
        result=result,
        checks=checks,
        warnings=warnings,
        required_inputs=required_inputs,
        errors=errors,
        next_step=next_step,
    )
