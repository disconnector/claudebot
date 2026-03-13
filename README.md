# claudebot

A multi-agent AI system for a Linux server. Two AI daemons run in parallel — **Claude** (Anthropic) and **Codex** (OpenAI) — accessible through a unified web chat interface with a draggable split layout.

## Architecture

```
┌─────────────────────────────────────────────────────┐
│  Web Chat  (Flask, port 5003)                        │
│  Left: conversation  Right: live tool output         │
│  Routing: (default)→Claude  @codex→Codex  @both→all │
└──────────┬──────────────────────┬────────────────────┘
           │ Unix socket          │ Unix socket
    ┌──────▼──────┐        ┌──────▼──────┐
    │claude-daemon│        │codex-daemon │
    │Anthropic API│        │OpenAI API   │
    │+ bash tool  │        │+ bash tool  │
    └─────────────┘        └─────────────┘
           │
    ┌──────▼──────┐
    │ ai-usage    │  Token usage tracking (SQLite)
    │ tracker.py  │  Visible at /usage in web chat
    └─────────────┘
```

**Additional components:**
- `observer/` — watches daemon logs, forwards events to web chat
- `bin/subcontract` — dispatch tasks to Claude Code subprocesses (parallel workers)

## Requirements

- Linux (Ubuntu 22.04+ recommended), systemd with user services
- Python 3.10+
- Anthropic API key (drives both the installer agent and claude-daemon)
- OpenAI API key (codex-daemon)

## Quick Start

```bash
git clone https://github.com/disconnector/claudebot
cd claudebot
./setup.sh
```

The first run of `setup.sh` will:
1. Create `.env` from `.env.example` — **fill in your API keys**, then re-run
2. Install the `anthropic` Python package if not present (needed for the installer)
3. Hand off to a self-contained Python install agent (`install_agent.py`) that uses your `ANTHROPIC_API_KEY` directly — **no Claude Max subscription required**:
   - installs Python dependencies
   - copies `.env` to each component directory
   - installs and starts systemd user services
   - creates memory directories
   - installs the `subcontract` tool

## Directory Structure

```
claudebot/
├── setup.sh                    # Smart installer (bootstraps Claude Code)
├── .env.example                # Environment template
├── claude-daemon/              # Anthropic Claude persistent daemon
│   ├── daemon.py               # Main daemon (Unix socket server, tool loop)
│   ├── client.py               # CLI client: cc "your message"
│   ├── backup.py               # Conversation backup to JSON
│   ├── storage.py              # SQLite message persistence
│   └── claude-daemon.service   # systemd user service
├── codex-daemon/               # OpenAI Codex persistent daemon
│   ├── daemon.py               # Main daemon (mirrors claude-daemon)
│   └── codex-daemon.service    # systemd user service
├── web-chat/                   # Multi-agent web interface
│   ├── app.py                  # Flask app (SSE streaming, split layout)
│   ├── orchestrator.py         # Message router (Claude/Codex/both)
│   ├── agent_bus.py            # SSE event bus
│   ├── agent_send.py           # CLI sender for agent-to-agent messaging
│   └── claude-web-chat.service # systemd user service
├── ai-usage/                   # Token usage tracker
│   └── tracker.py              # Log/query API usage (SQLite)
├── observer/                   # Daemon activity monitor
│   ├── observer.py             # Watches logs, pushes events to web chat
│   └── claude-observer.service # systemd user service
└── bin/
    └── subcontract             # Dispatch tasks to Claude Code subprocesses
```

## Web Chat

Open `http://your-server:5003` in a browser.

**Message routing:**
- Plain message → Claude (default)
- `@codex your message` → Codex
- `@claude your message` → Claude (explicit)
- `@both your message` → both agents in parallel

**Usage page:** `/usage` — token costs per model over time

## subcontract Tool

Dispatch work to Claude Code subprocesses (uses your Max subscription, no API cost for Haiku):

```bash
# Single task
subcontract -t "explain this function" -f src/foo.py

# Parallel workers
subcontract --parallel \
  -t "check app.py for race conditions" -f app.py -l "app" \
  -- \
  -t "check db.py for SQL injection" -f db.py -l "db"
```

Default model: `haiku`. Options: `haiku`, `sonnet`, `opus`.

## Daemons

Each daemon persists conversation history across connections, has access to a `bash` tool for executing commands on the host, and maintains memory files loaded into its system prompt.

**Aliases (add to ~/.bashrc):**
```bash
alias cc='python3 /path/to/claudebot/claude-daemon/client.py'
```

**Restart services:**
```bash
systemctl --user restart claude-daemon codex-daemon claude-web-chat
```

**View logs:**
```bash
journalctl --user -u claude-daemon -f
```

## Notes

- Daemons communicate via Unix sockets in `/run/user/$UID/`
- Memory files live in `<daemon>/memory/*.md` — add `.md` files to give persistent context
- Context is trimmed automatically when conversation history grows large
- The `CLAUDE_SOCKET_DIR` environment variable overrides the default socket path
