"""CSV reading and normalisation.

Every ledger ends up as a DataFrame with exactly these columns:
  account_number, account_name, account_type, debit, credit

account_type values are normalised to one of:
  asset | contra_asset | liability | equity | revenue | expense

Every Schedule L ends up with:
  line_number, description, beginning_of_year, end_of_year
"""
from __future__ import annotations

import difflib
import io

import pandas as pd

from typing import Dict, List, Optional, Tuple
from src.models import AccountTypeMapping, ColumnMapping, SchemaDetection

CANONICAL_LEDGER_COLS = {"account_number", "account_name", "account_type", "debit", "credit"}
CANONICAL_SL_COLS = {"line_number", "description", "beginning_of_year", "end_of_year"}


def detect_schema(content: str) -> SchemaDetection:
    """Inspect a ledger CSV and report whether it matches the canonical schema.

    When it does not, returns fuzzy-matched suggestions so the caller can build
    a ColumnMapping without manual inspection.
    """
    df = pd.read_csv(io.StringIO(content), nrows=0)
    actual = list(df.columns)
    actual_lower = [c.lower().strip() for c in actual]

    is_canonical = CANONICAL_LEDGER_COLS.issubset(set(actual_lower))

    suggested: dict[str, str] = {}
    for canonical in CANONICAL_LEDGER_COLS:
        # Try exact match first (case-insensitive)
        for orig, low in zip(actual, actual_lower):
            if low == canonical:
                suggested[canonical] = orig
                break
        else:
            # Fuzzy fallback
            matches = difflib.get_close_matches(
                canonical, actual_lower, n=1, cutoff=0.5
            )
            if matches:
                orig = actual[actual_lower.index(matches[0])]
                suggested[canonical] = orig

    missing = [c for c in CANONICAL_LEDGER_COLS if c not in suggested]

    return SchemaDetection(
        is_canonical=is_canonical,
        detected_columns=actual,
        suggested_mapping=suggested,
        missing_canonical=missing,
    )


def _build_type_lookup(mapping: AccountTypeMapping) -> Dict[str, str]:
    lookup: dict[str, str] = {}
    for canonical, variants in mapping.model_dump().items():
        for v in variants:
            lookup[v.strip().lower()] = canonical
    return lookup


def normalize_ledger(
    content: str,
    column_mapping: Optional[ColumnMapping] = None,
    account_type_mapping: Optional[AccountTypeMapping] = None,
) -> Tuple[pd.DataFrame, List[str]]:
    """Read and normalise a ledger CSV.

    Returns (df, warnings).  df has the canonical five columns plus
    'account_type_raw' for debugging.  Raises ValueError if required columns
    are still missing after applying the mapping.
    """
    warnings: list[str] = []
    df = pd.read_csv(io.StringIO(content), dtype=str)

    cm = column_mapping or ColumnMapping()

    # Build rename map from mapping → canonical name
    rename: dict[str, str] = {}
    for canonical in ("account_number", "account_name", "account_type", "debit", "credit"):
        src = getattr(cm, canonical)
        if src and src in df.columns and src != canonical:
            rename[src] = canonical
    df = df.rename(columns=rename)

    # Handle ending_balance fallback (signed net column, no separate dr/cr)
    if cm.ending_balance and cm.ending_balance in df.columns:
        if "debit" not in df.columns or "credit" not in df.columns:
            eb = pd.to_numeric(df[cm.ending_balance], errors="coerce").fillna(0)
            df["debit"] = eb.clip(lower=0)
            df["credit"] = (-eb).clip(lower=0)
            warnings.append(
                f"Column '{cm.ending_balance}' used as signed ending balance "
                "(positive → debit, negative → credit). Verify contra-asset rows."
            )

    for col in ("account_number", "account_name", "account_type", "debit", "credit"):
        if col not in df.columns:
            raise ValueError(
                f"Required column '{col}' not found after applying column mapping. "
                f"Columns present: {list(df.columns)}"
            )

    df["debit"] = pd.to_numeric(df["debit"], errors="coerce").fillna(0)
    df["credit"] = pd.to_numeric(df["credit"], errors="coerce").fillna(0)
    df["account_number"] = df["account_number"].astype(str).str.strip()
    df["account_name"] = df["account_name"].astype(str).str.strip()

    # Normalise account types
    atm = account_type_mapping or AccountTypeMapping()
    lookup = _build_type_lookup(atm)

    df["account_type_raw"] = df["account_type"].astype(str).str.strip()
    df["account_type"] = df["account_type_raw"].apply(
        lambda t: lookup.get(t.lower(), None)
    )

    unrecognized = (
        df.loc[df["account_type"].isna(), "account_type_raw"].unique().tolist()
    )
    if unrecognized:
        warnings.append(
            f"Unrecognized account type(s): {unrecognized}. "
            "These accounts are excluded from RE calculation. "
            "Supply an account_type_mapping to handle them."
        )
        df["account_type"] = df["account_type"].fillna("_unknown")

    return (
        df[["account_number", "account_name", "account_type", "debit", "credit"]],
        warnings,
    )


def normalize_schedule_l(content: str) -> pd.DataFrame:
    """Read and normalise a Schedule L CSV to canonical columns."""
    df = pd.read_csv(io.StringIO(content), dtype=str)
    df.columns = [c.strip().lower().replace(" ", "_") for c in df.columns]

    for col in ("line_number", "description", "beginning_of_year", "end_of_year"):
        if col not in df.columns:
            raise ValueError(f"Schedule L missing required column: '{col}'")

    df["line_number"] = df["line_number"].astype(str).str.strip().str.lower()
    df["description"] = df["description"].astype(str).str.strip()
    df["beginning_of_year"] = pd.to_numeric(
        df["beginning_of_year"], errors="coerce"
    ).fillna(0)
    df["end_of_year"] = pd.to_numeric(df["end_of_year"], errors="coerce").fillna(0)

    return df[["line_number", "description", "beginning_of_year", "end_of_year"]]
