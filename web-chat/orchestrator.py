"""
Multi-Agent Orchestrator v2
Routes messages between user, Claude daemon, and Codex daemon.
Handles @claude, @codex, @both prefixes.
Supports agent-to-agent delegation via shared message bus.
"""

import os
import socket
import json
import logging
import threading
import time
import re

logger = logging.getLogger("orchestrator")

CLAUDE_SOCKET = os.path.join(os.environ.get("CLAUDE_SOCKET_DIR", f"/run/user/{os.getuid()}"), "claude-daemon.sock")
CODEX_SOCKET = os.path.join(os.environ.get("CLAUDE_SOCKET_DIR", f"/run/user/{os.getuid()}"), "codex-daemon.sock")
CLAUDE_SENTINEL = "Mxyzptlk"

MAX_DELEGATION_DEPTH = 5


def parse_target(text):
    """
    Parse message to determine target agent(s).
    Returns (target, cleaned_text) where target is 'claude', 'codex', or 'both'.
    """
    stripped = text.strip()

    if re.match(r'^@(both|all)\b', stripped, re.IGNORECASE):
        cleaned = re.sub(r'^@(both|all)\s*', '', stripped, flags=re.IGNORECASE).strip()
        return 'both', cleaned or stripped

    if re.match(r'^@codex\b', stripped, re.IGNORECASE):
        cleaned = re.sub(r'^@codex\s*', '', stripped, flags=re.IGNORECASE).strip()
        return 'codex', cleaned or stripped

    if re.match(r'^@claude\b', stripped, re.IGNORECASE):
        cleaned = re.sub(r'^@claude\s*', '', stripped, flags=re.IGNORECASE).strip()
        return 'claude', cleaned or stripped

    # Default to Claude
    return 'claude', stripped


def send_to_agent(socket_path, text, source, send_event, agent_name):
    """
    Send a message to an agent daemon via Unix socket and stream responses.
    Returns the full response text and tool calls list.
    """
    full_response = []
    tool_calls = []

    try:
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.connect(socket_path)
        sock.settimeout(600)
    except (FileNotFoundError, ConnectionRefusedError, OSError) as e:
        err_msg = f"{agent_name} unavailable: {e}"
        send_event({"type": "error", "content": err_msg, "agent": agent_name})
        return err_msg, []

    req = json.dumps({"source": source, "text": text, "chat_id": 0}) + "\n"
    sock.sendall(req.encode())

    buf = b""
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
                except Exception:
                    continue

                mtype = msg.get("type")
                if mtype == "text":
                    content = msg.get("content", "")
                    if content:
                        full_response.append(content)
                        send_event({"type": "text", "content": content, "agent": agent_name})
                elif mtype == "tool_start":
                    tc = {"name": msg.get("name", "tool"), "command": msg.get("command", "")}
                    tool_calls.append(tc)
                    send_event({"type": "tool_start", "name": tc["name"],
                              "command": tc["command"], "agent": agent_name})
                elif mtype == "tool_result":
                    send_event({"type": "tool_result", "output": msg.get("output", ""),
                              "agent": agent_name})
                elif mtype == "processing":
                    send_event({"type": "processing", "content": msg.get("content", ""),
                              "agent": agent_name})
                elif mtype == "error":
                    send_event({"type": "error", "content": msg.get("content", ""),
                              "agent": agent_name})
                elif mtype == "done":
                    break
    except Exception as e:
        send_event({"type": "error", "content": str(e), "agent": agent_name})
    finally:
        try:
            sock.close()
        except:
            pass

    response_text = "".join(full_response)
    if agent_name == "claude":
        response_text = response_text.replace(CLAUDE_SENTINEL, "").rstrip()

    return response_text, tool_calls


def route_message(text, source, send_event_raw, save_callback=None):
    """
    Main routing function. Parses target, sends to agent(s),
    handles delegation, calls save_callback with results.
    """
    target, cleaned_text = parse_target(text)

    if target == 'both':
        results = {}
        done_count = [0]
        lock = threading.Lock()

        def run_agent(agent_name, sock_path):
            def agent_send(event):
                send_event_raw(event)
            resp, tools = send_to_agent(sock_path, cleaned_text, source, agent_send, agent_name)
            with lock:
                results[agent_name] = (resp, tools)
                done_count[0] += 1

            # Send per-agent done
            send_event_raw({"type": "agent_done", "agent": agent_name})

            if save_callback and resp:
                save_callback(agent_name, resp, tools)

        t_claude = threading.Thread(target=run_agent, args=("claude", CLAUDE_SOCKET))
        t_codex = threading.Thread(target=run_agent, args=("codex", CODEX_SOCKET))
        t_claude.start()
        t_codex.start()
        t_claude.join(timeout=620)
        t_codex.join(timeout=620)

        send_event_raw({"type": "done", "agent": "both"})

    elif target in ('claude', 'codex'):
        sock_path = CLAUDE_SOCKET if target == 'claude' else CODEX_SOCKET
        resp, tools = send_to_agent(sock_path, cleaned_text, source, send_event_raw, target)

        if save_callback and resp:
            save_callback(target, resp, tools)

        send_event_raw({"type": "done", "agent": target})
    else:
        send_event_raw({"type": "error", "content": f"Unknown target: {target}"})
        send_event_raw({"type": "done", "agent": "unknown"})
