#!/usr/bin/env python3
"""
Claude Daemon Terminal Client
==============================
Interactive REPL that connects to the daemon's Unix socket.
Displays streamed responses with tool call formatting.

Usage:
  python3 client.py
  # or via alias: claude-connect
"""

import sys
import os
import json
import socket
import readline
import threading
from pathlib import Path

SOCKET_PATH = os.path.join(os.environ.get("CLAUDE_SOCKET_DIR", f"/run/user/{os.getuid()}"), "claude-daemon.sock")
SENTINEL    = "Mxyzptlk"

# ── ANSI colours ────────────────────────────────────────────────────────────────
RESET   = "\033[0m"
BOLD    = "\033[1m"
DIM     = "\033[2m"
CYAN    = "\033[36m"
GREEN   = "\033[32m"
YELLOW  = "\033[33m"
RED     = "\033[31m"
MAGENTA = "\033[35m"
BLUE    = "\033[34m"

def c(color, text):
    return f"{color}{text}{RESET}"

# ── Readline history ─────────────────────────────────────────────────────────────
HISTORY_FILE = Path.home() / ".claude_daemon_history"
try:
    readline.read_history_file(HISTORY_FILE)
except FileNotFoundError:
    pass
readline.set_history_length(500)


def send_message(text: str, source: str = "terminal") -> bool:
    """Send a message to the daemon and stream the response. Returns True on success."""
    try:
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.connect(SOCKET_PATH)
    except FileNotFoundError:
        print(c(RED, "✗ Daemon not running. Start it with: systemctl --user start claude-daemon"))
        return False
    except ConnectionRefusedError:
        print(c(RED, "✗ Daemon socket exists but refused connection."))
        return False

    request = json.dumps({"source": source, "text": text}) + "\n"
    sock.sendall(request.encode())

    # Stream response
    buf = b""
    in_tool = False
    response_started = False

    try:
        while True:
            chunk = sock.recv(4096)
            if not chunk:
                break
            buf += chunk

            while b"\n" in buf:
                line, buf = buf.split(b"\n", 1)
                if not line.strip():
                    continue

                try:
                    msg = json.loads(line.decode())
                except json.JSONDecodeError:
                    continue

                mtype = msg.get("type")

                if mtype == "text":
                    text_chunk = msg["content"]
                    # Strip sentinel from display
                    if SENTINEL in text_chunk:
                        text_chunk = text_chunk.replace(SENTINEL, "").rstrip()
                    if text_chunk:
                        if not response_started:
                            print(c(BOLD + CYAN, "\nClaude: "), end="", flush=True)
                            response_started = True
                        print(text_chunk, end="", flush=True)

                elif mtype == "tool_start":
                    if in_tool is False and response_started:
                        print()  # newline after any preceding text
                    name    = msg.get("name", "tool")
                    command = msg.get("command", "")
                    print(c(DIM, f"\n  ┌─ {name}"), flush=True)
                    print(c(DIM, f"  │ $ {command}"), flush=True)
                    in_tool = True

                elif mtype == "tool_result":
                    output = msg.get("output", "")
                    lines  = output.splitlines()
                    for l in lines[:20]:
                        print(c(DIM, f"  │   {l}"), flush=True)
                    if len(lines) > 20:
                        print(c(DIM, f"  │   ... ({len(lines)-20} more lines)"), flush=True)
                    print(c(DIM, "  └─"), flush=True)
                    in_tool = False

                elif mtype == "processing":
                    print(c(YELLOW, f"\n  {msg['content']}"), end="", flush=True)

                elif mtype == "error":
                    print(c(RED, f"\n✗ {msg['content']}"), flush=True)

                elif mtype == "done":
                    if response_started:
                        print()  # final newline
                    sock.close()
                    return True

    except KeyboardInterrupt:
        print(c(YELLOW, "\n[interrupted]"))
        sock.close()
        return True
    finally:
        sock.close()

    return True


def check_daemon() -> bool:
    """Quick check if daemon is reachable."""
    return Path(SOCKET_PATH).exists()


def main():
    print(c(BOLD, "Claude Persistent Daemon"))
    print(c(DIM, f"  Socket: {SOCKET_PATH}"))

    if not check_daemon():
        print(c(YELLOW, "  Daemon not detected. Attempting to start..."))
        os.system("systemctl --user start claude-daemon 2>/dev/null || true")
        import time; time.sleep(2)
        if not check_daemon():
            print(c(RED, "  Could not start daemon. Run: systemctl --user start claude-daemon"))
            sys.exit(1)

    print(c(DIM, "  Type your message. Ctrl+C to interrupt, Ctrl+D to exit.\n"))

    while True:
        try:
            user_input = input(c(BOLD + GREEN, "You: ")).strip()
        except EOFError:
            print(c(DIM, "\nGoodbye."))
            break
        except KeyboardInterrupt:
            print()
            continue

        if not user_input:
            continue

        readline.write_history_file(HISTORY_FILE)
        send_message(user_input, source="terminal")


if __name__ == "__main__":
    main()
