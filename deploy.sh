#!/usr/bin/env bash
# deploy.sh — start / stop / restart / status the rolling_retained_earnings server
#
# Usage:
#   bash deploy.sh [command] [flags]
#
# Commands:
#   start    (default) Start the server
#   stop               Stop a running server
#   restart            Stop then start
#   status             Print whether the server is running
#
# Flags:
#   --dev              Development mode: auto-reload on file changes, 1 worker
#   --port  PORT       Override PORT from .env (default 8000)
#   --host  HOST       Override HOST from .env (default 0.0.0.0)
#   --workers N        Override WORKERS from .env (default 4)

set -euo pipefail

# ── Colours ───────────────────────────────────────────────────────────────────
RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; CYAN='\033[0;36m'; NC='\033[0m'
info()    { echo -e "${CYAN}[deploy]${NC} $*"; }
success() { echo -e "${GREEN}[deploy]${NC} $*"; }
warn()    { echo -e "${YELLOW}[deploy]${NC} $*"; }
die()     { echo -e "${RED}[deploy] ERROR:${NC} $*" >&2; exit 1; }

# ── Locate repo root ──────────────────────────────────────────────────────────
REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$REPO_DIR"

VENV_DIR="$REPO_DIR/.venv"
PID_FILE="$REPO_DIR/.server.pid"
LOG_DIR="$REPO_DIR/logs"
LOG_FILE="$LOG_DIR/server.log"

# ── Load .env if present ──────────────────────────────────────────────────────
[ -f "$REPO_DIR/.env" ] && set -a && source "$REPO_DIR/.env" && set +a

# ── Defaults (overridden by .env or flags) ────────────────────────────────────
PORT="${PORT:-8000}"
HOST="${HOST:-0.0.0.0}"
WORKERS="${WORKERS:-4}"
DEV_MODE=false

# ── Parse arguments ───────────────────────────────────────────────────────────
COMMAND="${1:-start}"
shift || true

while [[ $# -gt 0 ]]; do
    case "$1" in
        --dev)          DEV_MODE=true;         shift ;;
        --port)         PORT="$2";             shift 2 ;;
        --host)         HOST="$2";             shift 2 ;;
        --workers)      WORKERS="$2";          shift 2 ;;
        *) die "Unknown flag: $1. Run 'bash deploy.sh --help' for usage." ;;
    esac
done

# ── Helpers ───────────────────────────────────────────────────────────────────
check_venv() {
    [ -d "$VENV_DIR" ] || die "Virtual environment not found. Run 'bash setup.sh' first."
}

is_running() {
    [ -f "$PID_FILE" ] && kill -0 "$(cat "$PID_FILE")" 2>/dev/null
}

# ── Commands ──────────────────────────────────────────────────────────────────
cmd_status() {
    if is_running; then
        PID=$(cat "$PID_FILE")
        success "Server is running (PID $PID) on $HOST:$PORT"
    else
        warn "Server is not running."
        [ -f "$PID_FILE" ] && { warn "Stale PID file found — removing."; rm -f "$PID_FILE"; }
    fi
}

cmd_stop() {
    if is_running; then
        PID=$(cat "$PID_FILE")
        info "Stopping server (PID $PID) …"
        kill "$PID"
        # Wait up to 10 seconds for clean shutdown
        for i in $(seq 1 10); do
            kill -0 "$PID" 2>/dev/null || break
            sleep 1
        done
        if kill -0 "$PID" 2>/dev/null; then
            warn "Server did not stop cleanly — sending SIGKILL."
            kill -9 "$PID" 2>/dev/null || true
        fi
        rm -f "$PID_FILE"
        success "Server stopped."
    else
        warn "Server is not running — nothing to stop."
        rm -f "$PID_FILE"
    fi
}

cmd_start() {
    check_venv

    if is_running; then
        PID=$(cat "$PID_FILE")
        die "Server is already running (PID $PID). Use 'bash deploy.sh restart' to restart."
    fi

    mkdir -p "$LOG_DIR"

    if $DEV_MODE; then
        info "Starting in DEVELOPMENT mode (auto-reload, 1 worker) on $HOST:$PORT …"
        # Dev mode runs in the foreground so log output goes directly to terminal
        exec "$VENV_DIR/bin/uvicorn" src.api:app \
            --host "$HOST" \
            --port "$PORT" \
            --reload
    else
        info "Starting in PRODUCTION mode ($WORKERS workers) on $HOST:$PORT …"
        info "Logging to $LOG_FILE"
        "$VENV_DIR/bin/uvicorn" src.api:app \
            --host "$HOST" \
            --port "$PORT" \
            --workers "$WORKERS" \
            >> "$LOG_FILE" 2>&1 &
        echo $! > "$PID_FILE"
        # Give it a moment to confirm it started
        sleep 2
        if is_running; then
            success "Server started (PID $(cat "$PID_FILE"))."
            success "UI:       http://$HOST:$PORT"
            success "API docs: http://$HOST:$PORT/api/docs"
            success "Logs:     tail -f $LOG_FILE"
        else
            die "Server failed to start. Check $LOG_FILE for details."
        fi
    fi
}

cmd_restart() {
    cmd_stop
    sleep 1
    cmd_start
}

# ── Dispatch ──────────────────────────────────────────────────────────────────
case "$COMMAND" in
    start)   cmd_start   ;;
    stop)    cmd_stop    ;;
    restart) cmd_restart ;;
    status)  cmd_status  ;;
    --help|-h|help)
        grep '^#' "$0" | grep -v '!/usr/bin' | sed 's/^# \?//'
        ;;
    *) die "Unknown command: $COMMAND. Use start | stop | restart | status." ;;
esac
