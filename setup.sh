#!/usr/bin/env bash
# claudebot setup — bootstraps a Claude Code instance to handle the rest
set -euo pipefail

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

# ── Step 2: install Claude Code CLI if not present ─────────────────────────
if ! command -v claude &>/dev/null; then
    info "Claude Code CLI not found — installing..."
    if ! command -v npm &>/dev/null; then
        info "Installing Node.js via nvm..."
        curl -fsSL https://raw.githubusercontent.com/nvm-sh/nvm/v0.39.7/install.sh | bash
        export NVM_DIR="$HOME/.nvm"
        # shellcheck source=/dev/null
        [ -s "$NVM_DIR/nvm.sh" ] && source "$NVM_DIR/nvm.sh"
        nvm install --lts --default
    fi
    npm install -g @anthropic-ai/claude-code
    ok "Claude Code CLI installed: $(claude --version)"
else
    ok "Claude Code CLI already installed: $(claude --version)"
fi

# ── Step 3: hand off to Claude Code for intelligent installation ────────────
info ""
info "Bootstrapping Claude Code installer agent..."
info "Claude will detect your environment and complete the setup automatically."
info ""

# Load .env so we have the API key
set -a
# shellcheck source=.env
source "$REPO_DIR/.env"
set +a

INSTALL_PROMPT="You are an installation assistant for claudebot, a multi-agent AI system.

You are running inside the claudebot repository at: $REPO_DIR
The target user's home directory is: $HOME
The operating system is: $(uname -s) $(uname -r)

Your job is to complete the claudebot installation by:

1. DEPENDENCIES: Install required Python packages system-wide or in a venv:
   - pip packages: anthropic openai flask python-dotenv requests
   - Check if python3, pip3, sqlite3 are available; install if not

2. AI-USAGE TRACKER: Create $REPO_DIR/ai-usage/usage.db (SQLite) by running:
   cd $REPO_DIR/ai-usage && python3 tracker.py --init  (if that flag exists)
   Or just create the DB by importing tracker and calling any init function.

3. SYSTEMD SERVICES: Install systemd user services for:
   - claude-daemon: $REPO_DIR/claude-daemon/claude-daemon.service
   - codex-daemon: $REPO_DIR/codex-daemon/codex-daemon.service
   - web-chat: $REPO_DIR/web-chat/claude-web-chat.service
   Steps: copy .service file to ~/.config/systemd/user/, systemctl --user daemon-reload,
   systemctl --user enable <service>, systemctl --user start <service>

4. ENV FILES: Each daemon expects a .env in its directory. Copy $REPO_DIR/.env to:
   - $REPO_DIR/claude-daemon/.env
   - $REPO_DIR/codex-daemon/.env
   - $REPO_DIR/web-chat/.env

5. BIN TOOL: Install the subcontract tool:
   cp $REPO_DIR/bin/subcontract ~/bin/subcontract
   chmod +x ~/bin/subcontract
   Ensure ~/bin is in PATH (add to ~/.bashrc if not)

6. MEMORY DIRS: Create memory directories if they don't exist:
   mkdir -p $REPO_DIR/claude-daemon/memory
   mkdir -p $REPO_DIR/codex-daemon/memory

7. VERIFY: After installing, check each service is running:
   systemctl --user status claude-daemon codex-daemon claude-web-chat

8. REPORT: Print a summary of what was installed, what is running, and any issues.
   Include the web chat URL: http://localhost:5003

Work through these steps systematically. If anything fails, diagnose and fix it.
Be thorough but don't ask for confirmation — just do it."

# Run Claude Code as the installer (non-interactive, uses Max subscription)
CLAUDECODE="" claude \
    --dangerously-skip-permissions \
    --no-session-persistence \
    --model sonnet \
    -p "$INSTALL_PROMPT" \
    2>&1 | tee -a "$LOG"

echo ""
# Enable linger so user services survive logout
sudo loginctl enable-linger "$(whoami)" 2>/dev/null && ok "Systemd linger enabled — services persist after logout"

info "Setup complete. Check $LOG for full output."
info "Web chat should be available at: http://localhost:5003"
