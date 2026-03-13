#!/usr/bin/env python3
"""
CLI tool for agents to send messages to each other.
Usage: python3 agent_send.py <target_agent> <message>
  target_agent: 'claude' or 'codex'

Streams the response to stdout. Used by agents during task collaboration.
"""

import os
import socket
import json
import sys

_socket_dir = os.environ.get("CLAUDE_SOCKET_DIR", f"/run/user/{os.getuid()}")

SOCKETS = {
    "claude": os.path.join(_socket_dir, "claude-daemon.sock"),
    "codex":  os.path.join(_socket_dir, "codex-daemon.sock"),
}

def send_to_agent(agent, message):
    sock_path = SOCKETS.get(agent)
    if not sock_path:
        print(f"Unknown agent: {agent}", file=sys.stderr)
        sys.exit(1)

    try:
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.connect(sock_path)
        sock.settimeout(300)
    except Exception as e:
        print(f"Cannot connect to {agent}: {e}", file=sys.stderr)
        sys.exit(1)

    req = json.dumps({"source": "agent", "text": message, "chat_id": 0}) + "\n"
    sock.sendall(req.encode())

    buf = b""
    full = ""
    while True:
        try:
            chunk = sock.recv(4096)
            if not chunk:
                break
            buf += chunk
            while b"\n" in buf:
                line, buf = buf.split(b"\n", 1)
                if not line.strip():
                    continue
                try:
                    msg = json.loads(line)
                    if msg.get("type") == "text":
                        full += msg.get("content", "")
                        print(msg.get("content", ""), end="", flush=True)
                    elif msg.get("type") == "tool_start":
                        cmd = msg.get("command", "")
                        print(f"\n[TOOL] $ {cmd}", file=sys.stderr)
                    elif msg.get("type") == "done":
                        print()
                        sock.close()
                        return full
                except json.JSONDecodeError:
                    pass
        except socket.timeout:
            break

    print()
    try:
        sock.close()
    except:
        pass
    return full

if __name__ == "__main__":
    if len(sys.argv) < 3:
        print("Usage: python3 agent_send.py <claude|codex> <message>", file=sys.stderr)
        sys.exit(1)

    agent = sys.argv[1].lower()
    message = " ".join(sys.argv[2:])
    send_to_agent(agent, message)
