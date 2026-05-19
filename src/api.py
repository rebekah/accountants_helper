"""FastAPI application.

Two equivalent roll endpoints:
  POST /api/roll        — multipart file upload (browser UI, curl, direct API callers)
  POST /api/roll/json   — JSON body with CSV strings (AI agents, software-to-software)

Both return the same RollResponse schema.
"""
from __future__ import annotations

import json
import pathlib

from typing import List, Optional
from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse

from src.models import (
    AccountTypeMapping,
    ColumnMapping,
    DetectSchemaRequest,
    PreflightRequest,
    PreflightResponse,
    RequiredInput,
    RollJsonRequest,
    RollResponse,
    SchemaDetection,
)
from src.reader import detect_schema, normalize_ledger, normalize_schedule_l
from src.engine import roll_retained_earnings, _new_accounts

SCENARIOS_DIR = pathlib.Path(__file__).parent.parent / "scenarios"
UI_DIR = pathlib.Path(__file__).parent.parent / "ui"

app = FastAPI(
    title="Rolling Retained Earnings",
    description=(
        "Roll retained earnings from one tax year to the next, with automatic "
        "detection of book-tax differences, opening balance mismatches, new accounts, "
        "accumulated deficits, distributions, and bookkeeper-changeover schema changes."
    ),
    version="0.1.0",
    docs_url="/api/docs",
    redoc_url="/api/redoc",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


# ── Utility ───────────────────────────────────────────────────────────────────

async def _read(file: UploadFile) -> str:
    raw = await file.read()
    return raw.decode("utf-8-sig")  # strip Windows BOM


async def _process(
    prior_ledger_content: str,
    prior_sl_content: str,
    current_ledger_content: str,
    current_sl_content: str,
    prior_year: int,
    current_year: int,
    column_mapping: Optional[ColumnMapping],
    account_type_mapping: Optional[AccountTypeMapping],
    resolutions: Optional[dict] = None,
) -> RollResponse:

    all_warnings: List[str] = []

    # Auto-detect schema mismatch on current-year ledger
    if column_mapping is None:
        detection = detect_schema(current_ledger_content)
        if not detection.is_canonical:
            return RollResponse(
                status="requires_input",
                required_inputs=[
                    RequiredInput(
                        field="column_mapping",
                        label="Current-year ledger has a non-standard schema",
                        description=(
                            f"Expected columns: {detection.canonical_columns}. "
                            f"Found: {detection.detected_columns}. "
                            f"Suggested mapping: {detection.suggested_mapping}. "
                            "Resubmit with column_mapping_json set."
                        ),
                    )
                ],
                next_step=(
                    "POST the current-year ledger CSV to /api/detect-schema to get a "
                    "suggested column mapping, then resubmit with column_mapping_json."
                ),
            )

    try:
        prior_ledger, pw = normalize_ledger(
            prior_ledger_content, account_type_mapping=account_type_mapping
        )
        all_warnings.extend(pw)
    except ValueError as e:
        return RollResponse(status="error", errors=[f"Prior-year ledger: {e}"])

    try:
        current_ledger, cw = normalize_ledger(
            current_ledger_content, column_mapping, account_type_mapping
        )
        all_warnings.extend(cw)
    except ValueError as e:
        return RollResponse(status="error", errors=[f"Current-year ledger: {e}"])

    try:
        prior_sl = normalize_schedule_l(prior_sl_content)
    except ValueError as e:
        return RollResponse(status="error", errors=[f"Prior-year Schedule L: {e}"])

    try:
        current_sl = normalize_schedule_l(current_sl_content)
    except ValueError as e:
        return RollResponse(status="error", errors=[f"Current-year Schedule L: {e}"])

    return roll_retained_earnings(
        prior_ledger, prior_sl,
        current_ledger, current_sl,
        prior_year, current_year,
        reader_warnings=all_warnings,
        resolutions=resolutions,
    )


# ── Endpoints ─────────────────────────────────────────────────────────────────

@app.get("/health", tags=["meta"])
def health():
    """Liveness check."""
    return {"status": "ok"}


@app.get("/api/scenarios", tags=["scenarios"])
def list_scenarios():
    """List the bundled example scenarios with their edge-case labels."""
    if not SCENARIOS_DIR.exists():
        return {"scenarios": []}

    scenarios = []
    for d in sorted(SCENARIOS_DIR.iterdir()):
        if not d.is_dir():
            continue
        notes_path = d / "_notes.txt"
        title = d.name
        if notes_path.exists():
            first_line = notes_path.read_text().splitlines()[0]
            title = first_line.strip().lstrip("#= ").strip()
        scenarios.append({
            "name": d.name,
            "title": title,
            "files": [f.name for f in sorted(d.iterdir()) if f.is_file()],
        })
    return {"scenarios": scenarios}


@app.get("/api/scenarios/{name}", tags=["scenarios"])
def get_scenario(name: str):
    """Return the CSV contents and notes for a named example scenario.

    Use this to pre-fill the roll endpoint for demos or testing.
    """
    path = SCENARIOS_DIR / name
    if not path.exists() or not path.is_dir():
        raise HTTPException(status_code=404, detail=f"Scenario '{name}' not found.")

    result: dict[str, str] = {}
    for fname in (
        "ledger_2023.csv", "schedule_l_2023.csv",
        "ledger_2024.csv", "schedule_l_2024.csv",
        "_notes.txt",
    ):
        fpath = path / fname
        if fpath.exists():
            key = fname.replace(".", "_").replace("-", "_")
            result[key] = fpath.read_text()
    return result


@app.post("/api/preflight", response_model=PreflightResponse, tags=["preflight"])
def preflight(request: PreflightRequest):
    """Detect schema and identify new accounts before running the full rollover.

    Call this after all four files are loaded. Returns the auto-detected
    column_mapping (or null if the schema is canonical) and new_accounts so the
    UI can present the account-mapping review step before the roll is submitted.
    """
    col_map: Optional[ColumnMapping] = None

    detection = detect_schema(request.current_ledger_csv)
    if not detection.is_canonical and detection.suggested_mapping:
        col_map = ColumnMapping(**detection.suggested_mapping)

    try:
        prior_ledger, _ = normalize_ledger(
            request.prior_ledger_csv,
            account_type_mapping=request.account_type_mapping,
        )
    except ValueError as e:
        return PreflightResponse(error=f"Prior-year ledger: {e}")

    try:
        current_ledger, _ = normalize_ledger(
            request.current_ledger_csv,
            col_map,
            request.account_type_mapping,
        )
    except ValueError as e:
        return PreflightResponse(error=f"Current-year ledger: {e}")

    return PreflightResponse(
        new_accounts=_new_accounts(prior_ledger, current_ledger),
        column_mapping=col_map,
    )


@app.post("/api/detect-schema", response_model=SchemaDetection, tags=["schema"])
def detect_schema_endpoint(request: DetectSchemaRequest):
    """Inspect a ledger CSV and suggest a ColumnMapping.

    Use this before calling /api/roll when you suspect the ledger uses
    non-canonical column names (e.g. after a bookkeeper changeover).
    """
    return detect_schema(request.csv_content)


@app.post("/api/roll", response_model=RollResponse, tags=["roll"])
async def roll_multipart(
    prior_ledger: UploadFile = File(..., description="Prior-year trial balance CSV"),
    prior_schedule_l: UploadFile = File(..., description="Prior-year Schedule L CSV"),
    current_ledger: UploadFile = File(..., description="Current-year trial balance CSV"),
    current_schedule_l: UploadFile = File(..., description="Current-year Schedule L CSV"),
    prior_year: int = Form(2023),
    current_year: int = Form(2024),
    column_mapping_json: Optional[str] = Form(
        None,
        description="JSON ColumnMapping for non-canonical current-year ledger schemas",
    ),
    account_type_mapping_json: Optional[str] = Form(
        None,
        description="JSON AccountTypeMapping override",
    ),
):
    """Roll retained earnings — multipart file upload.

    Designed for browser UIs, curl, and direct HTTP API callers.
    """
    col_map = (
        ColumnMapping(**json.loads(column_mapping_json))
        if column_mapping_json
        else None
    )
    type_map = (
        AccountTypeMapping(**json.loads(account_type_mapping_json))
        if account_type_mapping_json
        else None
    )
    return await _process(
        await _read(prior_ledger),
        await _read(prior_schedule_l),
        await _read(current_ledger),
        await _read(current_schedule_l),
        prior_year, current_year,
        col_map, type_map,
    )


@app.post("/api/roll/json", response_model=RollResponse, tags=["roll"])
async def roll_json(request: RollJsonRequest):
    """Roll retained earnings — JSON body with CSV strings.

    Preferred for AI agents and software-to-software calls where constructing
    a multipart form is inconvenient. Pass raw CSV text in the request body.
    """
    return await _process(
        request.prior_ledger_csv,
        request.prior_schedule_l_csv,
        request.current_ledger_csv,
        request.current_schedule_l_csv,
        request.prior_year,
        request.current_year,
        request.column_mapping,
        request.account_type_mapping,
        resolutions=request.resolutions,
    )


# ── UI ────────────────────────────────────────────────────────────────────────

@app.get("/", response_class=HTMLResponse, include_in_schema=False)
async def ui():
    html = UI_DIR / "index.html"
    return HTMLResponse(html.read_text() if html.exists() else "<h1>UI not built</h1>")
