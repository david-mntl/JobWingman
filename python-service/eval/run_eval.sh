#!/usr/bin/env bash
# ============================================================================
# JobWingman — Eval runner
#
# Runs the LLM scoring eval against the fixture dataset and generates a
# markdown report in eval/test_results/.
#
# Environment detection:
#   - Inside the dev container (/.dockerenv exists OR $DEVCONTAINER is set):
#     runs Python directly — same behaviour as before.
#   - On the host machine: delegates to docker compose using
#     docker-compose.eval.yml so no local Python install is needed.
#
# Usage:
#   ./eval/run_eval.sh                           # full mode (score + judge)
#   ./eval/run_eval.sh --no-judge                # score assertions only (fast)
#   ./eval/run_eval.sh --fixture f004            # single fixture, full mode
#   ./eval/run_eval.sh --fixture f004 --no-judge # single fixture, score only
#
# Flags are passed through directly to run_eval.py — any flag supported by
# the Python script is also supported here.
# ============================================================================

set -euo pipefail

# ---------------------------------------------------------------------------
# Colour helpers
# ---------------------------------------------------------------------------
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[0;33m'
CYAN='\033[0;36m'
BOLD='\033[1m'
DIM='\033[2m'
RESET='\033[0m'

info()    { echo -e "${DIM}$*${RESET}"; }
success() { echo -e "${GREEN}$*${RESET}"; }
warn()    { echo -e "${YELLOW}$*${RESET}"; }
error()   { echo -e "${RED}$*${RESET}" >&2; }
header()  { echo -e "${BOLD}${CYAN}$*${RESET}"; }

# ---------------------------------------------------------------------------
# Resolve the python-service root (works from any working directory)
# ---------------------------------------------------------------------------
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SERVICE_ROOT="$(dirname "$SCRIPT_DIR")"
PROJECT_ROOT="$(dirname "$SERVICE_ROOT")"

# ---------------------------------------------------------------------------
# Detect environment: container vs host
#
# /.dockerenv is created by Docker in every container. $DEVCONTAINER is set
# by VS Code's devcontainer feature. Either one means we're inside a
# container and can run Python directly.
# ---------------------------------------------------------------------------
_inside_container() {
    [[ -f "/.dockerenv" ]] || [[ -n "${DEVCONTAINER:-}" ]]
}

# ---------------------------------------------------------------------------
# Verify the fixtures file exists before spending any LLM tokens
# ---------------------------------------------------------------------------
FIXTURES="$SCRIPT_DIR/fixtures/jobs.json"
if [[ ! -f "$FIXTURES" ]]; then
    error "ERROR: Fixtures file not found at $FIXTURES"
    exit 1
fi

# ---------------------------------------------------------------------------
# Determine mode label for the header banner
# ---------------------------------------------------------------------------
MODE_LABEL="full (score + judge)"
for arg in "$@"; do
    if [[ "$arg" == "--no-judge" ]]; then
        MODE_LABEL="score only (no judge)"
        break
    fi
done

FIXTURE_LABEL=""
NEXT_IS_ID=false
for arg in "$@"; do
    if $NEXT_IS_ID; then
        FIXTURE_LABEL="  fixture: $arg"
        NEXT_IS_ID=false
    fi
    if [[ "$arg" == "--fixture" ]]; then
        NEXT_IS_ID=true
    fi
done

echo ""
header "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
header "  JobWingman Eval Runner"
info   "  mode: $MODE_LABEL$FIXTURE_LABEL"
header "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo ""

# ---------------------------------------------------------------------------
# Run the eval — container path or Docker path
# ---------------------------------------------------------------------------
if _inside_container; then
    # --- Inside dev container: run Python directly ---

    # Locate Python — prefer a virtual environment if one exists
    if [[ -f "$SERVICE_ROOT/.venv/bin/python" ]]; then
        PYTHON="$SERVICE_ROOT/.venv/bin/python"
    elif [[ -f "$SERVICE_ROOT/venv/bin/python" ]]; then
        PYTHON="$SERVICE_ROOT/venv/bin/python"
    elif command -v python3 &>/dev/null; then
        PYTHON="python3"
    elif command -v python &>/dev/null; then
        PYTHON="python"
    else
        error "ERROR: No Python interpreter found."
        error "Install Python 3.11+ or create a virtual environment at $SERVICE_ROOT/.venv"
        exit 1
    fi

    info "Using Python: $PYTHON (inside container)"

    # Ensure test_results subdirectories exist
    mkdir -p "$SCRIPT_DIR/test_results/full"
    mkdir -p "$SCRIPT_DIR/test_results/score"
    mkdir -p "$SCRIPT_DIR/test_results/single"

    cd "$SERVICE_ROOT"
    "$PYTHON" eval/run_eval.py "$@"

else
    # --- On host machine: delegate to Docker ---
    info "Running via Docker (host machine detected)"

    COMPOSE_FILE="$PROJECT_ROOT/docker-compose.eval.yml"
    if [[ ! -f "$COMPOSE_FILE" ]]; then
        error "ERROR: docker-compose.eval.yml not found at $COMPOSE_FILE"
        exit 1
    fi

    # Ensure test_results subdirectories exist on the host so the bind
    # mount doesn't create them as root-owned directories
    mkdir -p "$SCRIPT_DIR/test_results/full"
    mkdir -p "$SCRIPT_DIR/test_results/score"
    mkdir -p "$SCRIPT_DIR/test_results/single"

    cd "$PROJECT_ROOT"
    docker compose -f docker-compose.eval.yml run --rm eval-runner "$@"
fi
