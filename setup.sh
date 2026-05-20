#!/usr/bin/env bash
# setup.sh — first-time setup for rolling_retained_earnings
# Run once on the server after cloning the repo.
# Usage: bash setup.sh

set -euo pipefail

# ── Colours ───────────────────────────────────────────────────────────────────
RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; CYAN='\033[0;36m'; NC='\033[0m'
info()    { echo -e "${CYAN}[setup]${NC} $*"; }
success() { echo -e "${GREEN}[setup]${NC} $*"; }
warn()    { echo -e "${YELLOW}[setup]${NC} $*"; }
die()     { echo -e "${RED}[setup] ERROR:${NC} $*" >&2; exit 1; }

# ── Locate repo root (wherever this script lives) ─────────────────────────────
REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$REPO_DIR"
info "Working directory: $REPO_DIR"

# ── Check Python ──────────────────────────────────────────────────────────────
PYTHON=$(command -v python3 || command -v python || true)
[ -z "$PYTHON" ] && die "Python 3 not found. Install Python >= 3.9 and retry."

PY_VERSION=$("$PYTHON" -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')")
PY_MINOR=$("$PYTHON" -c "import sys; print(sys.version_info.minor)")
PY_MAJOR=$("$PYTHON" -c "import sys; print(sys.version_info.major)")

[ "$PY_MAJOR" -lt 3 ] && die "Python 3 required. Found Python $PY_VERSION."
[ "$PY_MINOR" -lt 9 ] && die "Python 3.9+ required. Found Python $PY_VERSION."
success "Python $PY_VERSION — OK"

# ── Create virtual environment ────────────────────────────────────────────────
VENV_DIR="$REPO_DIR/.venv"
if [ -d "$VENV_DIR" ]; then
    warn "Virtual environment already exists at .venv — skipping creation."
else
    info "Creating virtual environment at .venv …"
    "$PYTHON" -m venv "$VENV_DIR"
    success "Virtual environment created."
fi

# ── Install dependencies ──────────────────────────────────────────────────────
info "Installing dependencies …"
"$VENV_DIR/bin/pip" install --quiet --upgrade pip
"$VENV_DIR/bin/pip" install --quiet -r requirements.txt
success "Dependencies installed."

# ── Smoke test ────────────────────────────────────────────────────────────────
info "Running smoke test …"
"$VENV_DIR/bin/python" -c "
from src import roll
pl = open('scenarios/01_clean_rollover/ledger_2023.csv').read()
ps = open('scenarios/01_clean_rollover/schedule_l_2023.csv').read()
cl = open('scenarios/01_clean_rollover/ledger_2024.csv').read()
cs = open('scenarios/01_clean_rollover/schedule_l_2024.csv').read()
r  = roll(pl, ps, cl, cs)
assert r.status == 'ok', f'Unexpected status: {r.status}'
assert r.result.current_computed_ending_re == 336000.0
print('Smoke test passed — ending RE:', r.result.current_computed_ending_re)
"
success "Smoke test passed."

# ── Create .env if missing ────────────────────────────────────────────────────
if [ ! -f "$REPO_DIR/.env" ]; then
    info "Creating .env from .env.example …"
    cp "$REPO_DIR/.env.example" "$REPO_DIR/.env"
    success ".env created. Edit it to change HOST / PORT / WORKERS."
else
    warn ".env already exists — not overwritten."
fi

# ── Create logs directory ─────────────────────────────────────────────────────
mkdir -p "$REPO_DIR/logs"
success "Log directory ready at logs/."

# ── Install systemd service ───────────────────────────────────────────────────
SERVICE_FILE="/etc/systemd/system/rolling-re.service"
UNIT_FILE="$REPO_DIR/rolling-re.service"

if [ -f "$UNIT_FILE" ]; then
    if command -v systemctl &>/dev/null; then
        if [ "$EUID" -eq 0 ]; then
            info "Installing systemd service …"
            cp "$UNIT_FILE" "$SERVICE_FILE"
            systemctl daemon-reload
            systemctl enable rolling-re
            success "Systemd service installed and enabled."
            echo ""
            echo -e "  Start now:  ${CYAN}systemctl start rolling-re${NC}"
            echo -e "  Logs:       ${CYAN}journalctl -u rolling-re -f${NC}"
        else
            warn "Not running as root — skipping systemd install."
            warn "To install the service, run:"
            warn "  sudo cp $UNIT_FILE $SERVICE_FILE"
            warn "  sudo systemctl daemon-reload && sudo systemctl enable rolling-re"
        fi
    else
        warn "systemd not found — skipping service install."
    fi
fi

# ── Done ──────────────────────────────────────────────────────────────────────
echo ""
success "Setup complete."
echo ""
echo -e "  Run the server:   ${CYAN}bash deploy.sh start${NC}"
echo -e "  Stop the server:  ${CYAN}bash deploy.sh stop${NC}"
echo -e "  Dev mode:         ${CYAN}bash deploy.sh start --dev${NC}"
echo ""
