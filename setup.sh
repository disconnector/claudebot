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

# ── Step 3: install Claude Code CLI if not present ─────────────────────────
if ! command -v claude &>/dev/null; then
    info "Claude Code CLI not found — installing..."
    if ! command -v npm &>/dev/null; then
        info "Installing Node.js via nvm..."
        curl -fsSL https://raw.githubusercontent.com/nvm-sh/nvm/v0.39.7/install.sh | bash
        export NVM_DIR="$HOME/.nvm"
        [ -s "$NVM_DIR/nvm.sh" ] && source "$NVM_DIR/nvm.sh"
        nvm install --lts --default
    fi
    npm install -g @anthropic-ai/claude-code
    ok "Claude Code CLI installed: $(claude --version)"
else
    ok "Claude Code CLI already installed: $(claude --version)"
fi

# ── Step 4: enable systemd linger so services survive logout ───────────────
if command -v loginctl &>/dev/null; then
    sudo loginctl enable-linger "$(whoami)" 2>/dev/null \
        && ok "Systemd linger enabled — services persist after logout" \
        || warn "Could not enable linger — services may stop on logout"
fi

# ── Step 5: hand off to Claude Code for intelligent installation ────────────
info ""
info "Bootstrapping Claude Code installer agent..."
info "Claude will detect your environment and complete the setup automatically."
info ""

# Load .env so we have the API key for Claude
set -a
# shellcheck source=.env
source "$REPO_DIR/.env"
set +a

# Detect server IP for the final URL
SERVER_IP=$(hostname -I 2>/dev/null | awk '{print $1}' || echo "localhost")

INSTALL_PROMPT="You are an installation assistant for claudebot, a multi-agent AI system.

Repository: $REPO_DIR
Home directory: $HOME
OS: $(uname -s) $(uname -r)

Complete the installation by working through these steps in order:

1. DEPENDENCIES
   - Check if python3, pip3, sqlite3 are available; install missing ones via apt.
   - Install pip packages (use 'pip3 install --user --break-system-packages' on Ubuntu 24+,
     or plain 'pip3 install --user' on older systems):
     anthropic openai flask python-dotenv requests

2. AI-USAGE TRACKER
   - Read $REPO_DIR/ai-usage/tracker.py to find the init_db() function.
   - Initialize the DB: python3 -c \"import sys; sys.path.insert(0,'$REPO_DIR/ai-usage'); import tracker; tracker.init_db()\"
   - Verify $REPO_DIR/ai-usage/usage.db was created.

3. ENV FILES — copy .env to each component:
   cp $REPO_DIR/.env $REPO_DIR/claude-daemon/.env
   cp $REPO_DIR/.env $REPO_DIR/codex-daemon/.env
   cp $REPO_DIR/.env $REPO_DIR/web-chat/.env

4. MEMORY DIRS
   mkdir -p $REPO_DIR/claude-daemon/memory
   mkdir -p $REPO_DIR/codex-daemon/memory

5. BIN TOOL
   mkdir -p \$HOME/bin
   cp $REPO_DIR/bin/subcontract \$HOME/bin/subcontract
   chmod +x \$HOME/bin/subcontract
   Grep ~/.bashrc for '\$HOME/bin' or '~/bin' in PATH; if not found, append:
   export PATH=\"\$HOME/bin:\$PATH\"

6. SYSTEMD SERVICES — for each of the three services:
   Service files are in:
     $REPO_DIR/claude-daemon/claude-daemon.service
     $REPO_DIR/codex-daemon/codex-daemon.service
     $REPO_DIR/web-chat/claude-web-chat.service

   The service files reference '\$HOME/claudebot/' as the repo path.
   REPO_DIR is '$REPO_DIR'. If they differ, fix the paths.

   For each service:
   a. Copy to ~/.config/systemd/user/ (create dir if needed)
   b. If '$REPO_DIR' != '\$HOME/claudebot', run sed in-place on the installed copy:
      sed -i 's|\$HOME/claudebot|$REPO_DIR|g; s|%h/claudebot|$REPO_DIR|g' ~/.config/systemd/user/<service>.service
   c. systemctl --user daemon-reload
   d. systemctl --user enable <service>
   e. systemctl --user start <service>

7. VERIFY
   Check each service: systemctl --user is-active claude-daemon codex-daemon claude-web-chat
   If any are not active, check logs: journalctl --user -u <service> -n 30 --no-pager
   Diagnose and fix any failures before reporting.

8. REPORT
   Print a summary table: service name | status | PID
   Web chat URL: http://$SERVER_IP:5003

Work through every step. Diagnose and fix failures — do not skip steps. Do not ask for confirmation."

# Run Claude Code as the installer (non-interactive, uses Max subscription)
CLAUDECODE="" claude \
    --dangerously-skip-permissions \
    --no-session-persistence \
    --model sonnet \
    -p "$INSTALL_PROMPT" \
    2>&1 | tee -a "$LOG" || warn "Claude installer exited non-zero — check $LOG for details"

info ""
info "Setup complete. Check $LOG for full output."
info "Web chat: http://$SERVER_IP:5003"
