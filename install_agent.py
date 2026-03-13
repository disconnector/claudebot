#!/usr/bin/env python3
"""
claudebot install agent — agentic installer using Anthropic API directly.
Requires only ANTHROPIC_API_KEY in the environment (no Claude Max subscription).
"""

import os
import sys
import json
import subprocess
from pathlib import Path

try:
    import anthropic
except ImportError:
    print("[install_agent] ERROR: anthropic package not found.")
    print("[install_agent] Run: pip3 install --user anthropic")
    sys.exit(1)

REPO_DIR  = Path(__file__).parent.resolve()
HOME      = Path.home()
MODEL     = "claude-sonnet-4-5"
MAX_TURNS = 40


def bash(command: str) -> str:
    try:
        result = subprocess.run(
            ["bash", "-c", command],
            capture_output=True, text=True, timeout=120, cwd=str(HOME)
        )
        out = result.stdout
        if result.stderr:
            out += ("\n" if out else "") + result.stderr
        if not out:
            out = f"(exit code {result.returncode})"
        return out[:20000]
    except subprocess.TimeoutExpired:
        return "ERROR: command timed out after 120s"
    except Exception as e:
        return f"ERROR: {e}"


TOOLS = [{
    "name": "bash",
    "description": "Execute a bash command on the local system.",
    "input_schema": {
        "type": "object",
        "properties": {"command": {"type": "string"}},
        "required": ["command"]
    }
}]

SERVER_IP = bash("hostname -I | awk '{print $1}'").strip() or "localhost"

PROMPT = f"""You are an installation assistant for claudebot, a multi-agent AI system.

Repository: {REPO_DIR}
Home: {HOME}
OS: {bash('uname -s -r').strip()}

Complete the installation by working through these steps:

1. DEPENDENCIES
   - Check python3, pip3, sqlite3; install missing via apt if needed.
   - Install pip packages (use --break-system-packages on Ubuntu 24+):
     anthropic openai flask python-dotenv requests

2. AI-USAGE TRACKER
   - Read {REPO_DIR}/ai-usage/tracker.py to find init_db().
   - Run: python3 -c "import sys; sys.path.insert(0,'{REPO_DIR}/ai-usage'); import tracker; tracker.init_db()"
   - Verify {REPO_DIR}/ai-usage/usage.db was created.

3. ENV FILES — copy .env to each component:
   cp {REPO_DIR}/.env {REPO_DIR}/claude-daemon/.env
   cp {REPO_DIR}/.env {REPO_DIR}/codex-daemon/.env
   cp {REPO_DIR}/.env {REPO_DIR}/web-chat/.env

4. MEMORY DIRS
   mkdir -p {REPO_DIR}/claude-daemon/memory
   mkdir -p {REPO_DIR}/codex-daemon/memory

5. BIN TOOL
   mkdir -p {HOME}/bin
   cp {REPO_DIR}/bin/subcontract {HOME}/bin/subcontract
   chmod +x {HOME}/bin/subcontract
   Add $HOME/bin to PATH in ~/.bashrc if not already there.

6. SYSTEMD SERVICES — for each of the three .service files:
     {REPO_DIR}/claude-daemon/claude-daemon.service
     {REPO_DIR}/codex-daemon/codex-daemon.service
     {REPO_DIR}/web-chat/claude-web-chat.service

   a. mkdir -p ~/.config/systemd/user
   b. Copy the .service file there.
   c. If '{REPO_DIR}' != '{HOME}/claudebot', fix paths in the installed copy:
      sed -i 's|%h/claudebot|{REPO_DIR}|g' ~/.config/systemd/user/<name>.service
   d. systemctl --user daemon-reload
   e. systemctl --user enable <service>
   f. systemctl --user start <service>

7. VERIFY
   systemctl --user is-active claude-daemon codex-daemon claude-web-chat
   If any are not active, check logs and fix before reporting.

8. REPORT — print a summary table and this URL: http://{SERVER_IP}:5003

Work through every step. Fix failures. Do not ask for confirmation."""


def run():
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        print("[install_agent] ERROR: ANTHROPIC_API_KEY not set.")
        sys.exit(1)

    client = anthropic.Anthropic(api_key=api_key)
    messages = [{"role": "user", "content": PROMPT}]

    for turn in range(MAX_TURNS):
        response = client.messages.create(
            model=MODEL,
            max_tokens=4096,
            tools=TOOLS,
            messages=messages,
        )

        # Print any text
        for block in response.content:
            if hasattr(block, "text") and block.text:
                print(block.text, flush=True)

        if response.stop_reason == "end_turn":
            break

        if response.stop_reason == "tool_use":
            messages.append({"role": "assistant", "content": response.content})
            tool_results = []
            for block in response.content:
                if block.type == "tool_use" and block.name == "bash":
                    cmd = block.input.get("command", "")
                    print(f"\n$ {cmd}", flush=True)
                    output = bash(cmd)
                    print(output, flush=True)
                    tool_results.append({
                        "type": "tool_result",
                        "tool_use_id": block.id,
                        "content": output,
                    })
            messages.append({"role": "user", "content": tool_results})
        else:
            break


if __name__ == "__main__":
    run()
