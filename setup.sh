#!/usr/bin/env bash
# claudebot setup — bootstraps a Claude Code instance to handle the rest
set -uo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LOG="$REPO_DIR/setup.log"

info()  { echo -e "\033[1;36m[setup]\033[0m $*" | tee -a "$LOG"; }
ok()    { echo -e "\033[1;32m[ok]\033[0m $*"    | tee -a "$LOG"; }
warn()  { echo -e "\033[1;33m[warn]\033[0m $*"  | tee -a "$LOG"; }
die()   { echo -e "\033[1;31m[error]\033[0m $*" | tee -a "$LOG"; exit 1; }

info "claudebot setup starting — $(date)"
info "Repo: $REPO_DIR"

# ── Step 1: ensure .env exists ─────────────────────────────────────────────
if [ ! -f "$REPO_DIR/.env" ]; then
    if [ -f "$REPO_DIR/.env.example" ]; then
        cp "$REPO_DIR/.env.example" "$REPO_DIR/.env"
        warn ".env created from .env.example — please fill in your API keys, then re-run setup.sh"
        warn "Required: ANTHROPIC_API_KEY (for claude-daemon)"
        warn "Required: OPENAI_API_KEY    (for codex-daemon)"
        exit 0
    else
        die ".env not found and no .env.example to copy from"
    fi
fi

# ── Step 2: source nvm if present (may have been installed previously) ──────
export NVM_DIR="$HOME/.nvm"
[ -s "$NVM_DIR/nvm.sh" ] && source "$NVM_DIR/nvm.sh"

# ── Step 3: install Python and the anthropic package (needed for installer) ─
if ! command -v python3 &>/dev/null; then
    info "Installing python3..."
    sudo apt-get install -y python3 python3-pip
fi

if ! python3 -c "import anthropic" &>/dev/null; then
    info "Installing anthropic Python package..."
    pip3 install --user --quiet --break-system-packages anthropic 2>/dev/null \
        || pip3 install --user --quiet anthropic
fi
ok "Python installer dependencies ready"

# ── Step 4: enable systemd linger so services survive logout ───────────────
if command -v loginctl &>/dev/null; then
    sudo loginctl enable-linger "$(whoami)" 2>/dev/null \
        && ok "Systemd linger enabled — services persist after logout" \
        || warn "Could not enable linger — services may stop on logout"
fi

# ── Step 5: hand off to the Python install agent ───────────────────────────
info ""
info "Bootstrapping install agent (uses ANTHROPIC_API_KEY — no Max subscription needed)..."
info ""

# Load .env so ANTHROPIC_API_KEY is in the environment
set -a
# shellcheck source=.env
source "$REPO_DIR/.env"
set +a

python3 "$REPO_DIR/install_agent.py" 2>&1 | tee -a "$LOG" \
    || warn "Install agent exited non-zero — check $LOG for details"

info ""
info "Setup complete. Check $LOG for full output."
info "Web chat: http://$SERVER_IP:5003"
