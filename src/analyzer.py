"""Autonomous retained-earnings bridge analysis with optional LLM narrative.

Given the prior year ledger + Schedule L and the current year ledger, this
module determines what adjustments are needed to bring the current GL into
agreement with where the prior year Schedule L left off — with no user input.

If ANTHROPIC_API_KEY is set, an LLM call produces a plain-English CPA workpaper
narrative and can suggest additional adjustment types. If the key is absent the
deterministic analysis is returned on its own.
"""
from __future__ import annotations

import json
import os
from typing import Any, Dict, List, Optional

import pandas as pd

from src.engine import (
    _beginning_re,
    _distributions,
    _fmt,
    _matches,
    _net_income,
    _new_accounts,
    _DIST_PATTERNS,
    _RE_PATTERNS,
    _schedule_l_re,
)
from src.models import AnalysisResponse, BridgeAnalysis, SuggestedEntry

_SYSTEM_PROMPT = (
    "You are a CPA assistant specializing in corporate tax return preparation "
    "and retained earnings reconciliation. You produce concise, accurate workpaper "
    "narratives that explain book-to-tax differences in plain English."
)


# ── Provider dispatch ─────────────────────────────────────────────────────────

def _strip_fences(text: str) -> str:
    text = text.strip()
    if text.startswith("```"):
        text = text.split("```")[1]
        if text.startswith("json"):
            text = text[4:]
    return text.strip()


async def _call_anthropic(prompt: str) -> dict:
    api_key = os.getenv("ANTHROPIC_API_KEY")
    if not api_key:
        raise ValueError("ANTHROPIC_API_KEY is not set")
    import anthropic
    client = anthropic.AsyncAnthropic(api_key=api_key)
    response = await client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=1800,
        system=[{"type": "text", "text": _SYSTEM_PROMPT, "cache_control": {"type": "ephemeral"}}],
        messages=[{"role": "user", "content": prompt}],
    )
    return json.loads(_strip_fences(response.content[0].text))


async def _call_gemini(prompt: str) -> dict:
    api_key = os.getenv("GEMINI_API_KEY")
    if not api_key:
        raise ValueError("GEMINI_API_KEY is not set")
    from google import genai
    from google.genai import types
    client = genai.Client(api_key=api_key)
    response = await client.aio.models.generate_content(
        model="gemini-2.5-flash",
        contents=prompt,
        config=types.GenerateContentConfig(
            system_instruction=_SYSTEM_PROMPT,
            max_output_tokens=4096,
            # Disable thinking: structured-output tasks don't benefit from it
            # and it consumes most of the token budget before the actual response.
            thinking_config=types.ThinkingConfig(thinking_budget=0),
        ),
    )
    return json.loads(_strip_fences(response.text))


async def _call_llm(prompt: str) -> dict:
    provider = os.getenv("LLM_PROVIDER", "anthropic").lower().strip()
    if provider == "anthropic":
        return await _call_anthropic(prompt)
    if provider == "gemini":
        return await _call_gemini(prompt)
    raise ValueError(f"Unknown LLM_PROVIDER '{provider}' — use 'anthropic' or 'gemini'")


# ── Balance-sheet helpers ─────────────────────────────────────────────────────

def _total_assets(ledger: pd.DataFrame) -> float:
    a  = ledger[ledger["account_type"] == "asset"]
    ca = ledger[ledger["account_type"] == "contra_asset"]
    return float(
        (a["debit"].sum() - a["credit"].sum()) -
        (ca["credit"].sum() - ca["debit"].sum())
    )


def _total_liabilities(ledger: pd.DataFrame) -> float:
    li = ledger[ledger["account_type"] == "liability"]
    return float(li["credit"].sum() - li["debit"].sum())


def _equity_non_re(ledger: pd.DataFrame) -> float:
    equity = ledger[ledger["account_type"] == "equity"]
    re_mask   = equity["account_name"].apply(lambda n: _matches(n, _RE_PATTERNS))
    dist_mask = equity["account_name"].apply(lambda n: _matches(n, _DIST_PATTERNS))
    other = equity[~re_mask & ~dist_mask]
    return float(other["credit"].sum() - other["debit"].sum())


# ── Prompt helpers ────────────────────────────────────────────────────────────

def _account_rows(ledger: pd.DataFrame) -> List[Dict[str, Any]]:
    rows = []
    for _, r in ledger.iterrows():
        net = float(r["credit"]) - float(r["debit"])
        rows.append({
            "number": r["account_number"],
            "name":   r["account_name"],
            "type":   r["account_type"],
            "net":    round(net, 2),
        })
    return rows


def _sl_rows(sl: pd.DataFrame) -> List[Dict[str, Any]]:
    return [
        {
            "line":        r["line_number"],
            "description": r["description"],
            "beg":         float(r["beginning_of_year"]),
            "end":         float(r["end_of_year"]),
        }
        for _, r in sl.iterrows()
    ]


def _build_user_prompt(
    *,
    prior_year: int,
    current_year: int,
    entity_name: Optional[str],
    prior_accounts: List[Dict],
    prior_sl_lines: List[Dict],
    p_beg_re: float,
    p_net_inc: float,
    p_dist: float,
    p_book_re: float,
    p_sl_re: float,
    p_diff: float,
    current_accounts: List[Dict],
    c_beg_re: float,
    c_expected_beg_re: float,
    c_beg_adjustment: float,
    c_net_inc: float,
    c_dist: float,
    c_projected_re: float,
    new_accounts: List[Dict],
    deterministic_entries: List[Dict],
    assumptions: List[str],
) -> str:
    entity = entity_name or "the entity"
    gap_dir = "book higher" if p_diff > 0 else ("Schedule L higher" if p_diff < 0 else "no difference")
    return f"""You are preparing a retained earnings reconciliation workpaper for {entity}.

## Prior Year ({prior_year}) General Ledger Accounts
{json.dumps(prior_accounts, indent=2)}

## Prior Year ({prior_year}) Schedule L Lines
{json.dumps(prior_sl_lines, indent=2)}

## Prior Year Computed Figures
- Beginning RE (GL): {_fmt(p_beg_re)}
- Net income (GL): {_fmt(p_net_inc)}
- Distributions (GL): {_fmt(p_dist)}
- Book ending RE: {_fmt(p_book_re)}
- Schedule L ending RE (line 25): {_fmt(p_sl_re)}
- Book-to-Schedule L difference: {_fmt(p_diff)} ({gap_dir})

## Current Year ({current_year}) General Ledger Accounts
{json.dumps(current_accounts, indent=2)}

## Current Year Computed Figures
- Actual beginning RE in GL: {_fmt(c_beg_re)}
- Expected beginning RE (= prior Schedule L ending): {_fmt(c_expected_beg_re)}
- Adjustment needed to beginning RE: {_fmt(c_beg_adjustment)}
- Net income (GL): {_fmt(c_net_inc)}
- Distributions (GL): {_fmt(c_dist)}
- Projected ending RE after adjustments: {_fmt(c_projected_re)}

## New Accounts in {current_year} Not Present in {prior_year}
{json.dumps(new_accounts, indent=2) if new_accounts else "None"}

## Deterministic Adjusting Entries Already Identified
{json.dumps(deterministic_entries, indent=2)}

## Assumptions the System Already Made
{json.dumps(assumptions, indent=2)}

---

Respond with a JSON object (no markdown fences) matching this exact shape:

{{
  "llm_narrative": "3-4 paragraph plain-English reconciliation memo. Cover: (1) what the prior-year book-to-Schedule L difference most likely represents — name specific IRS/GAAP adjustment types; (2) what adjustments are needed in the current-year GL and why; (3) any unusual items or risks the preparer should review.",
  "named_adjustment_types": ["list of likely adjustment type names, e.g. 'Sec. 179 timing difference', 'Meals & entertainment add-back (IRC §274)'"],
  "additional_entries": [
    {{
      "description": "Short entry description",
      "debit_account": "Account to debit",
      "credit_account": "Account to credit",
      "amount": 0.00,
      "rationale": "Why this entry is needed"
    }}
  ],
  "confidence": "high | medium | low",
  "confidence_reason": "One sentence explaining the confidence rating."
}}"""


# ── Main entry point ──────────────────────────────────────────────────────────

async def analyze(
    prior_ledger: pd.DataFrame,
    prior_sl: pd.DataFrame,
    current_ledger: pd.DataFrame,
    prior_year: int,
    current_year: int,
    entity_name: Optional[str] = None,
) -> AnalysisResponse:
    """Run the full autonomous bridge analysis.

    Deterministic math runs first; an LLM call is made only if
    ANTHROPIC_API_KEY is present in the environment.
    """
    errors: List[str] = []

    # ── Prior year bridge math ────────────────────────────────────────────────
    p_beg_re  = _beginning_re(prior_ledger)
    p_net_inc = _net_income(prior_ledger)
    p_dist    = _distributions(prior_ledger)
    p_book_re = p_beg_re + p_net_inc - p_dist
    p_sl_re   = _schedule_l_re(prior_sl, "end_of_year")
    p_diff    = p_book_re - p_sl_re   # positive = book higher than Schedule L

    # ── Current year starting position ───────────────────────────────────────
    c_beg_re          = _beginning_re(current_ledger)
    c_net_inc         = _net_income(current_ledger)
    c_dist            = _distributions(current_ledger)
    c_expected_beg_re = p_sl_re                        # the correct starting point
    c_beg_adjustment  = c_expected_beg_re - c_beg_re   # positive = need to credit RE
    c_projected_re    = c_expected_beg_re + c_net_inc - c_dist

    new_accts = _new_accounts(prior_ledger, current_ledger)

    # ── Build deterministic adjusting entries and assumptions ─────────────────
    assumptions: List[str] = []
    entries: List[SuggestedEntry] = []

    if abs(p_diff) < 1:
        assumptions.append(
            f"Prior year ({prior_year}) book RE matches Schedule L RE ({_fmt(p_sl_re)}) — "
            "no book-tax adjustment was needed."
        )
    else:
        direction = "higher" if p_diff > 0 else "lower"
        assumptions.append(
            f"Prior year ({prior_year}) book RE ({_fmt(p_book_re)}) is {_fmt(abs(p_diff))} "
            f"{direction} than Schedule L RE ({_fmt(p_sl_re)}). This gap typically represents "
            "tax adjustments (depreciation timing, M&E add-backs, etc.) that were not posted "
            "back to the GL. The LLM analysis will name the most likely causes."
        )

    if abs(c_beg_adjustment) < 1:
        assumptions.append(
            f"Current year ({current_year}) beginning RE in the GL ({_fmt(c_beg_re)}) already "
            f"matches the prior Schedule L ending RE ({_fmt(c_expected_beg_re)}). "
            "No beginning-balance adjustment is needed."
        )
    else:
        direction = "credit" if c_beg_adjustment > 0 else "debit"
        entries.append(SuggestedEntry(
            description=(
                f"{'Increase' if c_beg_adjustment > 0 else 'Decrease'} beginning retained "
                f"earnings to match prior year Schedule L"
            ),
            debit_account=(
                "Prior Year Adjustment (clearing)" if c_beg_adjustment > 0
                else "Retained Earnings"
            ),
            credit_account=(
                "Retained Earnings" if c_beg_adjustment > 0
                else "Prior Year Adjustment (clearing)"
            ),
            amount=round(abs(c_beg_adjustment), 2),
            rationale=(
                f"GL shows beginning RE of {_fmt(c_beg_re)}, but prior year Schedule L "
                f"ended at {_fmt(c_expected_beg_re)}. {direction.capitalize()} RE by "
                f"{_fmt(abs(c_beg_adjustment))} to bring the starting point into agreement."
            ),
            source="deterministic",
        ))
        assumptions.append(
            f"Current year ({current_year}) beginning RE in the GL ({_fmt(c_beg_re)}) does not "
            f"match the prior Schedule L ending RE ({_fmt(c_expected_beg_re)}). "
            f"Difference: {_fmt(c_beg_adjustment)}. A prior-period adjustment entry is suggested."
        )

    if new_accts:
        names = ", ".join(a["account_name"] for a in new_accts)
        assumptions.append(
            f"{len(new_accts)} new account(s) in {current_year} not present in {prior_year}: "
            f"{names}. Their balances are included in the projected ending RE. "
            "Verify their Schedule L classification."
        )

    assumptions.append(
        f"Projected ending RE of {_fmt(c_projected_re)} = prior Schedule L RE "
        f"({_fmt(c_expected_beg_re)}) + net income ({_fmt(c_net_inc)}) "
        f"− distributions ({_fmt(c_dist)})."
    )

    bridge = BridgeAnalysis(
        prior_year=prior_year,
        prior_beg_re=p_beg_re,
        prior_net_income=p_net_inc,
        prior_distributions=p_dist,
        prior_book_ending_re=p_book_re,
        prior_schedule_l_re=p_sl_re,
        prior_book_tax_diff=p_diff,
        current_year=current_year,
        current_beg_re_book=c_beg_re,
        current_expected_beg_re=c_expected_beg_re,
        current_beg_adjustment=c_beg_adjustment,
        current_net_income=c_net_inc,
        current_distributions=c_dist,
        current_projected_ending_re=c_projected_re,
        suggested_entries=entries,
        assumptions=assumptions,
        new_accounts=new_accts,
    )

    # ── LLM narrative (optional) ──────────────────────────────────────────────
    provider = os.getenv("LLM_PROVIDER", "anthropic").lower().strip()
    has_key = bool(
        os.getenv("ANTHROPIC_API_KEY") if provider == "anthropic"
        else os.getenv("GEMINI_API_KEY")
    )
    if has_key:
        try:
            prompt = _build_user_prompt(
                prior_year=prior_year,
                current_year=current_year,
                entity_name=entity_name,
                prior_accounts=_account_rows(prior_ledger),
                prior_sl_lines=_sl_rows(prior_sl),
                p_beg_re=p_beg_re,
                p_net_inc=p_net_inc,
                p_dist=p_dist,
                p_book_re=p_book_re,
                p_sl_re=p_sl_re,
                p_diff=p_diff,
                current_accounts=_account_rows(current_ledger),
                c_beg_re=c_beg_re,
                c_expected_beg_re=c_expected_beg_re,
                c_beg_adjustment=c_beg_adjustment,
                c_net_inc=c_net_inc,
                c_dist=c_dist,
                c_projected_re=c_projected_re,
                new_accounts=new_accts,
                deterministic_entries=[e.model_dump() for e in entries],
                assumptions=assumptions,
            )
            parsed = await _call_llm(prompt)

            bridge.llm_narrative          = parsed.get("llm_narrative")
            bridge.named_adjustment_types = parsed.get("named_adjustment_types", [])
            bridge.confidence             = parsed.get("confidence")

            for ae in parsed.get("additional_entries", []):
                try:
                    bridge.suggested_entries.append(SuggestedEntry(
                        description=ae.get("description", ""),
                        debit_account=ae.get("debit_account", ""),
                        credit_account=ae.get("credit_account", ""),
                        amount=float(ae.get("amount", 0)),
                        rationale=ae.get("rationale", ""),
                        source="llm",
                    ))
                except Exception:
                    pass

        except Exception as exc:
            errors.append(f"LLM narrative unavailable: {exc}")

    return AnalysisResponse(status="ok", bridge=bridge, errors=errors)
