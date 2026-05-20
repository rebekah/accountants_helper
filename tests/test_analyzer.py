"""Tests for the autonomous bridge analyzer (src/analyzer.py).

Each test covers a distinct real-world scenario that requires the analyzer to
either suggest adjusting entries or flag account-mapping issues.  LLM calls are
suppressed by the conftest fixture — only the deterministic layer is tested.

Scenario coverage:
  1  Clean rollover — no adjustments needed
  2  Book RE > Schedule L RE (tax adjustments not posted back)
  3  Prior year clean; current year correctly carries forward beginning balance
  4  New accounts added in current year
  5  Accumulated deficit (negative retained earnings)
  6  Shareholder distributions reduce projected RE
  7  Bookkeeper changeover — non-canonical ledger schema with column mapping
  8  Schedule L RE > Book RE (reverse gap — SL higher)
  9  Current year net loss — projected RE decreases, no entry needed
  10 Combination: book-tax gap + current year distributions
"""
from __future__ import annotations

import asyncio
import textwrap
from pathlib import Path
from typing import Optional

import pytest

from src.analyzer import analyze
from src.models import AccountTypeMapping, ColumnMapping
from src.reader import normalize_ledger, normalize_schedule_l

SCENARIOS = Path(__file__).parent.parent / "scenarios"


# ── Shared helpers ────────────────────────────────────────────────────────────

def run_analyze(
    prior_ledger_csv: str,
    prior_sl_csv: str,
    current_ledger_csv: str,
    prior_year: int = 2023,
    current_year: int = 2024,
    column_mapping: Optional[ColumnMapping] = None,
    account_type_mapping: Optional[AccountTypeMapping] = None,
):
    prior_ledger, _ = normalize_ledger(
        prior_ledger_csv, account_type_mapping=account_type_mapping
    )
    current_ledger, _ = normalize_ledger(
        current_ledger_csv, column_mapping, account_type_mapping
    )
    prior_sl = normalize_schedule_l(prior_sl_csv)
    return asyncio.run(
        analyze(prior_ledger, prior_sl, current_ledger, prior_year, current_year)
    )


def scenario(name: str):
    """Return (prior_ledger_csv, prior_sl_csv, current_ledger_csv) for a scenario."""
    d = SCENARIOS / name
    return (
        (d / "ledger_2023.csv").read_text(),
        (d / "schedule_l_2023.csv").read_text(),
        (d / "ledger_2024.csv").read_text(),
    )


# ── Test 1: Clean rollover ────────────────────────────────────────────────────

def test_01_clean_rollover_no_adjustments_needed():
    """Scenario 01 — baseline clean rollover.

    Prior year book RE equals Schedule L RE ($220K).  The current year GL
    correctly carries that balance forward.  No adjusting entries should be
    suggested and no beginning-balance adjustment is needed.
    """
    pl, ps, cl = scenario("01_clean_rollover")
    result = run_analyze(pl, ps, cl)
    b = result.bridge

    assert result.status == "ok"
    assert abs(b.prior_book_tax_diff) < 1, "No book-to-Schedule L gap expected"
    assert abs(b.current_beg_adjustment) < 1, "No beginning RE adjustment expected"
    assert len(b.suggested_entries) == 0
    assert b.current_projected_ending_re == pytest.approx(336_000)
    assert b.llm_narrative is None  # no key set → deterministic only


# ── Test 2: Book RE > Schedule L RE ──────────────────────────────────────────

def test_02_book_higher_than_schedule_l_requires_debit_entry():
    """Scenario 02 — prior year GL carried $5K more income than the tax return.

    The $5,000 gap (common cause: M&E add-backs, non-deductible expenses not
    reversed in the GL) means the current year must start $5K lower than the
    GL shows.  The analyzer should suggest one journal entry that debits
    Retained Earnings by $5,000 to reduce the opening balance to the
    Schedule L basis.
    """
    pl, ps, cl = scenario("02_tax_adjustments_not_booked")
    result = run_analyze(pl, ps, cl)
    b = result.bridge

    assert result.status == "ok"
    assert b.prior_book_tax_diff == pytest.approx(5_000), \
        "Book should be $5K above Schedule L"
    assert b.current_beg_adjustment == pytest.approx(-5_000), \
        "Beginning RE needs to be reduced by $5K"
    assert len(b.suggested_entries) == 1

    entry = b.suggested_entries[0]
    assert entry.amount == pytest.approx(5_000)
    assert "Retained Earnings" in entry.debit_account, \
        "Entry should debit Retained Earnings"
    assert entry.source == "deterministic"

    assert b.current_projected_ending_re == pytest.approx(331_000)


# ── Test 3: Beginning balance already correct, no entry needed ───────────────

def test_03_correct_beginning_balance_no_entry_needed():
    """Scenario 03 — prior year book RE and Schedule L RE both equal $220K.

    The current year GL also opens at $220K.  Even though this scenario is
    labelled 'beginning balance mismatch' in the original 4-file roll tests
    (where the Schedule L continuity check fires), from the 3-file analyzer's
    perspective the starting point is already correct — no adjusting entry is
    needed.
    """
    pl, ps, cl = scenario("03_beginning_balance_mismatch")
    result = run_analyze(pl, ps, cl)
    b = result.bridge

    assert result.status == "ok"
    assert abs(b.prior_book_tax_diff) < 1
    assert abs(b.current_beg_adjustment) < 1
    assert len(b.suggested_entries) == 0
    assert b.current_projected_ending_re == pytest.approx(336_000)


# ── Test 4: New accounts in the current year ──────────────────────────────────

def test_04_new_accounts_are_flagged():
    """Scenario 04 — current year adds Marketing Expense and Vehicle
    Depreciation Expense, neither of which existed in the prior year.

    The analyzer should surface both in new_accounts and include them in the
    assumptions list.  Beginning RE is unchanged so no adjusting entry is
    required.
    """
    pl, ps, cl = scenario("04_new_accounts_added")
    result = run_analyze(pl, ps, cl)
    b = result.bridge

    assert result.status == "ok"
    assert len(b.new_accounts) >= 2, "At least 2 new RE-relevant accounts expected"

    new_names_lower = [a["account_name"].lower() for a in b.new_accounts]
    assert any("marketing" in n for n in new_names_lower), \
        "Marketing Expense should be flagged as a new account"
    assert any("vehicle" in n for n in new_names_lower), \
        "Vehicle Depreciation Expense should be flagged as a new account"

    assert abs(b.current_beg_adjustment) < 1, \
        "New accounts do not affect the beginning RE adjustment"
    assert any("new account" in a.lower() for a in b.assumptions), \
        "Assumptions list should mention the new accounts"


# ── Test 5: Accumulated deficit ───────────────────────────────────────────────

def test_05_accumulated_deficit_handled_correctly():
    """Scenario 05 — prior year net loss produces a deficit of −$30K.

    Schedule L line 25 correctly shows −$30K.  The current year GL opens at
    −$30K (correct basis).  After a profitable current year the projected
    ending RE should turn positive.  No adjusting entry is needed.
    """
    pl, ps, cl = scenario("05_accumulated_deficit")
    result = run_analyze(pl, ps, cl)
    b = result.bridge

    assert result.status == "ok"
    assert b.prior_book_ending_re == pytest.approx(-30_000), \
        "Prior year should end with a $30K deficit"
    assert b.prior_schedule_l_re == pytest.approx(-30_000)
    assert abs(b.prior_book_tax_diff) < 1, \
        "Deficit should match Schedule L — no book-tax gap"
    assert abs(b.current_beg_adjustment) < 1, \
        "Deficit basis carries forward correctly — no entry needed"
    assert b.current_projected_ending_re > 0, \
        "Entity should recover to positive RE in the current year"
    assert len(b.suggested_entries) == 0


# ── Test 6: Distributions reduce projected RE ────────────────────────────────

def test_06_distributions_correctly_reduce_projected_re():
    """Scenario 06 — prior year had $40K distributions; current year $30K.

    The prior year Schedule L ending RE ($180K) already reflects the
    distributions, so the current year GL starts at the correct basis.
    Projected ending RE must deduct the $30K current year distributions:
    $180K + $116K net income − $30K distributions = $266K.
    """
    pl, ps, cl = scenario("06_distributions")
    result = run_analyze(pl, ps, cl)
    b = result.bridge

    assert result.status == "ok"
    assert b.prior_distributions == pytest.approx(40_000)
    assert b.current_distributions == pytest.approx(30_000)
    assert abs(b.current_beg_adjustment) < 1, \
        "Beginning RE already matches Schedule L — no entry needed"
    assert b.current_projected_ending_re == pytest.approx(266_000)
    assert len(b.suggested_entries) == 0


# ── Test 7: Bookkeeper changeover — explicit column mapping ──────────────────

def test_07_bookkeeper_changeover_with_explicit_column_mapping():
    """Scenario 07 — the new bookkeeper's software exports completely different
    column names (gl_code, gl_description, classification, period_debits,
    period_credits).  When the caller supplies a ColumnMapping the analyzer
    normalises the ledger and produces a clean bridge with no errors.
    """
    pl, ps, cl = scenario("07_bookkeeper_changeover")
    col_map = ColumnMapping(
        account_number="gl_code",
        account_name="gl_description",
        account_type="classification",
        debit="period_debits",
        credit="period_credits",
    )
    result = run_analyze(pl, ps, cl, column_mapping=col_map)
    b = result.bridge

    assert result.status == "ok"
    assert result.errors == [], f"Unexpected errors: {result.errors}"
    assert abs(b.prior_book_tax_diff) < 1
    assert abs(b.current_beg_adjustment) < 1
    assert b.current_projected_ending_re == pytest.approx(336_000)


# ── Custom CSV data for tests 8–10 ───────────────────────────────────────────

# Test 8 — Schedule L shows MORE RE than the GL books
# (e.g. installment-sale income recognised on tax return, not yet booked)
_T8_PRIOR_LEDGER = textwrap.dedent("""\
    account_number,account_name,account_type,debit,credit
    1010,Cash,asset,50000,0
    2010,Accounts Payable,liability,0,10000
    3010,Common Stock,equity,0,5000
    3020,Retained Earnings,equity,0,40000
    4010,Service Revenue,revenue,0,100000
    5010,Operating Expenses,expense,90000,0
""")
# book ending RE = 40K + (100K − 90K) = 50K; SL line 25 end = 58K → gap = −8K (SL higher)
_T8_PRIOR_SL = textwrap.dedent("""\
    line_number,description,beginning_of_year,end_of_year
    1,Cash,30000,50000
    15,Total Assets,80000,85000
    16,Accounts Payable,8000,10000
    22b,Common Stock,5000,5000
    25,Retained Earnings Unappropriated,40000,58000
    28,Total Liabilities and Equity,80000,85000
""")
_T8_CURRENT_LEDGER = textwrap.dedent("""\
    account_number,account_name,account_type,debit,credit
    1010,Cash,asset,80000,0
    2010,Accounts Payable,liability,0,15000
    3010,Common Stock,equity,0,5000
    3020,Retained Earnings,equity,0,50000
    4010,Service Revenue,revenue,0,120000
    5010,Operating Expenses,expense,100000,0
""")
# current GL opens at 50K (wrong — should be 58K); net income = 20K
# adjustment = +8K (need to CREDIT RE to increase it to the SL basis)
# projected = 58K + 20K = 78K


def test_08_schedule_l_higher_than_book_requires_credit_entry():
    """Custom scenario — Schedule L records $8K more RE than the GL books.

    The current year incorrectly opens at the book basis ($50K) rather than
    the Schedule L basis ($58K).  The analyzer should suggest one journal entry
    that CREDITS Retained Earnings by $8,000 to increase the opening balance
    to the correct tax-basis figure.
    """
    result = run_analyze(_T8_PRIOR_LEDGER, _T8_PRIOR_SL, _T8_CURRENT_LEDGER)
    b = result.bridge

    assert result.status == "ok"
    assert b.prior_book_tax_diff == pytest.approx(-8_000), \
        "Book should be $8K below Schedule L"
    assert b.current_beg_adjustment == pytest.approx(8_000), \
        "Beginning RE must be increased by $8K"
    assert len(b.suggested_entries) == 1

    entry = b.suggested_entries[0]
    assert entry.amount == pytest.approx(8_000)
    assert "Retained Earnings" in entry.credit_account, \
        "Entry should credit Retained Earnings"
    assert entry.source == "deterministic"

    assert b.current_projected_ending_re == pytest.approx(78_000)


# Test 9 — Clean start + net loss in current year
_T9_PRIOR_LEDGER = textwrap.dedent("""\
    account_number,account_name,account_type,debit,credit
    1010,Cash,asset,100000,0
    2010,Accounts Payable,liability,0,20000
    3010,Common Stock,equity,0,5000
    3020,Retained Earnings,equity,0,80000
    4010,Service Revenue,revenue,0,180000
    5010,Salaries Expense,expense,100000,0
    5020,Other Expenses,expense,40000,0
""")
# prior net income = 180K − 140K = 40K; book ending RE = 120K; SL = 120K (clean)
_T9_PRIOR_SL = textwrap.dedent("""\
    line_number,description,beginning_of_year,end_of_year
    1,Cash,60000,100000
    15,Total Assets,185000,225000
    16,Accounts Payable,15000,20000
    22b,Common Stock,5000,5000
    25,Retained Earnings Unappropriated,80000,120000
    28,Total Liabilities and Equity,185000,225000
""")
_T9_CURRENT_LEDGER = textwrap.dedent("""\
    account_number,account_name,account_type,debit,credit
    1010,Cash,asset,85000,0
    2010,Accounts Payable,liability,0,25000
    3010,Common Stock,equity,0,5000
    3020,Retained Earnings,equity,0,120000
    4010,Service Revenue,revenue,0,100000
    5010,Salaries Expense,expense,110000,0
    5020,Other Expenses,expense,40000,0
""")
# current net income = 100K − 150K = −50K (net loss)
# beginning RE = 120K (correct) → no entry; projected = 120K − 50K = 70K


def test_09_net_loss_reduces_projected_re_no_entry_needed():
    """Custom scenario — entity reports a $50K net loss in the current year.

    The beginning RE carries forward correctly from the prior Schedule L, so
    no adjusting entry is required.  The projected ending RE should decrease
    from $120K to $70K, confirming that net losses are handled without errors.
    """
    result = run_analyze(_T9_PRIOR_LEDGER, _T9_PRIOR_SL, _T9_CURRENT_LEDGER)
    b = result.bridge

    assert result.status == "ok"
    assert abs(b.prior_book_tax_diff) < 1, "Prior year should be clean"
    assert abs(b.current_beg_adjustment) < 1, \
        "Beginning RE is already correct — no entry needed"
    assert len(b.suggested_entries) == 0
    assert b.current_net_income == pytest.approx(-50_000), \
        "Analyzer should detect the net loss"
    assert b.current_projected_ending_re == pytest.approx(70_000)


# Test 10 — Combination: book-tax gap ($15K) + current year distributions ($10K)
_T10_PRIOR_LEDGER = textwrap.dedent("""\
    account_number,account_name,account_type,debit,credit
    1010,Cash,asset,200000,0
    2010,Accounts Payable,liability,0,40000
    3010,Common Stock,equity,0,10000
    3020,Retained Earnings,equity,0,160000
    4010,Service Revenue,revenue,0,340000
    5010,Salaries Expense,expense,150000,0
    5020,Other Expenses,expense,100000,0
""")
# prior net income = 340K − 250K = 90K; book ending RE = 250K
# SL line 25 end = 235K → gap = +15K (e.g. Sec. 179 / bonus depreciation)
_T10_PRIOR_SL = textwrap.dedent("""\
    line_number,description,beginning_of_year,end_of_year
    1,Cash,120000,200000
    15,Total Assets,450000,510000
    16,Accounts Payable,30000,40000
    22b,Common Stock,10000,10000
    25,Retained Earnings Unappropriated,160000,235000
    28,Total Liabilities and Equity,450000,510000
""")
_T10_CURRENT_LEDGER = textwrap.dedent("""\
    account_number,account_name,account_type,debit,credit
    1010,Cash,asset,280000,0
    2010,Accounts Payable,liability,0,50000
    3010,Common Stock,equity,0,10000
    3020,Retained Earnings,equity,0,250000
    3030,Distributions Paid,equity,10000,0
    4010,Service Revenue,revenue,0,350000
    5010,Salaries Expense,expense,150000,0
    5020,Other Expenses,expense,150000,0
""")
# current: beg RE = 250K (wrong — should be 235K); net income = 50K; dist = 10K
# adjustment = −15K (debit RE); projected = 235K + 50K − 10K = 275K


def test_10_combination_book_tax_gap_and_distributions():
    """Custom scenario — prior year $15K book-tax gap (Sec. 179) combined with
    $10K current year distributions.

    The current year GL incorrectly opens at $250K (book basis) instead of
    $235K (Schedule L basis).  The analyzer should suggest one debit-RE entry
    for $15K AND correctly subtract the $10K distributions from the projected
    ending RE, yielding $275K.
    """
    result = run_analyze(_T10_PRIOR_LEDGER, _T10_PRIOR_SL, _T10_CURRENT_LEDGER)
    b = result.bridge

    assert result.status == "ok"
    assert b.prior_book_tax_diff == pytest.approx(15_000), \
        "Book should be $15K above Schedule L"
    assert b.current_beg_adjustment == pytest.approx(-15_000), \
        "Beginning RE must be reduced by $15K"
    assert len(b.suggested_entries) == 1

    entry = b.suggested_entries[0]
    assert entry.amount == pytest.approx(15_000)
    assert "Retained Earnings" in entry.debit_account

    assert b.current_distributions == pytest.approx(10_000), \
        "Distributions should be detected"
    assert b.current_projected_ending_re == pytest.approx(275_000)
