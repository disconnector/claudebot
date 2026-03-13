"""
Codex Daemon — persistent OpenAI agent with tool execution
Mirrors claude-daemon architecture: Unix socket server, tool loop, streaming
"""

import os
import sys
import json
import socket
import subprocess
import threading
import logging
import time
import signal
from pathlib import Path
from datetime import datetime

# Load environment
from dotenv import load_dotenv
load_dotenv(Path(__file__).parent / ".env")

from openai import OpenAI
sys.path.insert(0, str(Path(__file__).parent.parent / "ai-usage"))
import tracker as _tracker

SOCKET_PATH = os.path.join(os.environ.get("CLAUDE_SOCKET_DIR", f"/run/user/{os.getuid()}"), "codex-daemon.sock")
LOG_PATH = Path(__file__).parent / "daemon.log"
MEMORY_DIR = Path(__file__).parent / "memory"
MODEL = os.environ.get("CODEX_MODEL", "o3")
MAX_TOOL_LOOPS  = 25
TOOL_TIMEOUT    = 300
CONTEXT_KEEP    = 80
CONTEXT_TRIM_AT = 100

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    handlers=[
        logging.FileHandler(LOG_PATH),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger("codex-daemon")

client = OpenAI(api_key=os.environ["OPENAI_API_KEY"])

# ── System prompt ──

def build_system_prompt():
    """Build system prompt, incorporating memory files if they exist."""
    base = """You are Codex, an AI coding agent running as a persistent daemon on a Linux server called YOUR_SERVER_NAME (YOUR_SERVER_IP).

You have full, unrestricted access to the server via the bash tool. You can run any command, read/write any file, access the network.

You are one of two AI agents on this system:
- **You (Codex)** — powered by OpenAI, specialist in code generation, debugging, and systems work
- **Claude** — powered by Anthropic Claude, the primary assistant daemon

You and Claude can communicate with each other through a shared message bus. When a user prefixes a message with @codex, it comes to you. When they say @claude, it goes to Claude. Either of you can delegate tasks to the other via @claude or @codex prefixes in your responses.

YOUR_USER is the system owner. Be direct, technical, and efficient. Show your work.

Your home directory for memory and identity: ~/codex-daemon/memory/
Your conversation history persists across connections but resets on daemon restart."""

    # Load any memory files
    memory_parts = []
    if MEMORY_DIR.exists():
        for f in sorted(MEMORY_DIR.glob("*.md")):
            try:
                content = f.read_text().strip()
                if content:
                    memory_parts.append(f"## {f.stem}\n{content}")
            except Exception:
                pass

    if memory_parts:
        base += "\n\n# Memory Files\n\n" + "\n\n".join(memory_parts)

    return base


# ── Tools ──

TOOLS = [{
    "type": "function",
    "function": {
        "name": "bash",
        "description": "Execute a bash command on the server. Full system access, 300s timeout.",
        "parameters": {
            "type": "object",
            "properties": {
                "command": {
                    "type": "string",
                    "description": "The bash command to execute"
                }
            },
            "required": ["command"]
        }
    }
}]


def execute_bash(command: str) -> str:
    """Execute a bash command and return stdout+stderr."""
    logger.info(f"TOOL bash: {command[:200]}")
    try:
        result = subprocess.run(
            ["bash", "-c", command],
            capture_output=True, text=True,
            timeout=TOOL_TIMEOUT,
            cwd=str(Path.home())
        )
        output = ""
        if result.stdout:
            output += result.stdout
        if result.stderr:
            output += ("\n" if output else "") + result.stderr
        if not output:
            output = f"(exit code {result.returncode})"
        # Truncate very long output
        if len(output) > 50000:
            output = output[:25000] + "\n\n... [truncated] ...\n\n" + output[-25000:]
        return output
    except subprocess.TimeoutExpired:
        return f"ERROR: Command timed out after {TOOL_TIMEOUT}s"
    except Exception as e:
        return f"ERROR: {e}"


# ── Conversation state ──

conversation_lock = threading.Lock()
messages = []  # OpenAI message format


def reset_conversation():
    global messages
    messages = []


def _trim_context():
    """If history exceeds CONTEXT_TRIM_AT, summarise old messages and keep recent ones."""
    global messages
    if len(messages) <= CONTEXT_TRIM_AT:
        return

    to_summarise = messages[:-CONTEXT_KEEP]
    keep         = messages[-CONTEXT_KEEP:]

    lines = []
    for m in to_summarise:
        role    = m.get("role", "?")
        content = m.get("content") or ""
        if isinstance(content, list):
            content = " ".join(
                p.get("text", "") if isinstance(p, dict) else str(p)
                for p in content
            )[:300]
        lines.append(f"{role}: {str(content)[:300]}")

    summary = (
        f"[Context summary — {len(to_summarise)} older messages trimmed]\n"
        + "\n".join(lines)
    )

    messages = [
        {"role": "user",      "content": summary},
        {"role": "assistant", "content": "Understood, I have the summary of our earlier conversation."},
        *keep,
    ]
    logger.info(f"Context trimmed: {len(to_summarise)} messages → summary. Keeping {len(keep)} recent.")


# ── Handle a single request ──

def handle_request(text: str, source: str, send_event):
    """Process a user message through the OpenAI API with tool loop."""
    global messages

    with conversation_lock:
        messages.append({"role": "user", "content": text})

        system_prompt = build_system_prompt()
        api_messages = [{"role": "system", "content": system_prompt}] + messages

        tool_loops = 0
        while tool_loops < MAX_TOOL_LOOPS:
            tool_loops += 1
            logger.info(f"API call #{tool_loops}, model={MODEL}, msgs={len(api_messages)}")

            try:
                # Stream the response
                full_content = ""
                tool_calls_acc = {}  # id -> {name, arguments}
                finish_reason = None
                usage_snapshot = None

                stream = client.chat.completions.create(
                    model=MODEL,
                    messages=api_messages,
                    tools=TOOLS,
                    max_completion_tokens=16000,
                    stream=True,
                    stream_options={"include_usage": True},
                )

                for chunk in stream:
                    # Usage comes in the final chunk (choices=[])
                    if hasattr(chunk, "usage") and chunk.usage:
                        usage_snapshot = chunk.usage
                    choice = chunk.choices[0] if chunk.choices else None
                    if not choice:
                        continue

                    delta = choice.delta
                    finish_reason = choice.finish_reason or finish_reason

                    # Text content
                    if delta and delta.content:
                        full_content += delta.content
                        send_event({"type": "text", "content": delta.content})

                    # Tool calls
                    if delta and delta.tool_calls:
                        for tc_delta in delta.tool_calls:
                            idx = tc_delta.index
                            if idx not in tool_calls_acc:
                                tool_calls_acc[idx] = {
                                    "id": tc_delta.id or "",
                                    "name": "",
                                    "arguments": ""
                                }
                            if tc_delta.id:
                                tool_calls_acc[idx]["id"] = tc_delta.id
                            if tc_delta.function:
                                if tc_delta.function.name:
                                    tool_calls_acc[idx]["name"] = tc_delta.function.name
                                if tc_delta.function.arguments:
                                    tool_calls_acc[idx]["arguments"] += tc_delta.function.arguments

                # Log token usage
                if usage_snapshot:
                    try:
                        _tracker.log_usage(
                            agent="codex", model=MODEL, source=source,
                            input_tokens=usage_snapshot.prompt_tokens,
                            output_tokens=usage_snapshot.completion_tokens,
                        )
                    except Exception:
                        pass

                # If we got text content, save it
                if full_content:
                    assistant_msg = {"role": "assistant", "content": full_content}
                    if tool_calls_acc:
                        assistant_msg["tool_calls"] = [
                            {
                                "id": tc["id"],
                                "type": "function",
                                "function": {"name": tc["name"], "arguments": tc["arguments"]}
                            }
                            for tc in tool_calls_acc.values()
                        ]
                    messages.append(assistant_msg)
                    api_messages.append(assistant_msg)
                elif tool_calls_acc:
                    # Tool calls without text
                    assistant_msg = {
                        "role": "assistant",
                        "content": None,
                        "tool_calls": [
                            {
                                "id": tc["id"],
                                "type": "function",
                                "function": {"name": tc["name"], "arguments": tc["arguments"]}
                            }
                            for tc in tool_calls_acc.values()
                        ]
                    }
                    messages.append(assistant_msg)
                    api_messages.append(assistant_msg)

                # Execute tool calls if any
                if tool_calls_acc and finish_reason == "tool_calls":
                    for idx in sorted(tool_calls_acc.keys()):
                        tc = tool_calls_acc[idx]
                        try:
                            args = json.loads(tc["arguments"])
                        except json.JSONDecodeError:
                            args = {"command": tc["arguments"]}

                        cmd = args.get("command", "")
                        send_event({
                            "type": "tool_start",
                            "name": tc["name"],
                            "command": cmd
                        })

                        result = execute_bash(cmd)

                        send_event({
                            "type": "tool_result",
                            "output": result[:2000]
                        })

                        tool_msg = {
                            "role": "tool",
                            "tool_call_id": tc["id"],
                            "content": result
                        }
                        messages.append(tool_msg)
                        api_messages.append(tool_msg)

                    # Continue the loop for the model to process tool results
                    continue
                else:
                    # No tool calls or finished — we're done
                    break

            except Exception as e:
                logger.error(f"API error: {e}")
                send_event({"type": "error", "content": f"API error: {e}"})
                break

        # Trim conversation if it gets too long
        _trim_context()

        send_event({"type": "done"})


# ── Socket server ──

def handle_client(conn):
    """Handle a single client connection."""
    try:
        conn.settimeout(600)
        buf = b""
        while True:
            chunk = conn.recv(4096)
            if not chunk:
                break
            buf += chunk
            if b"\n" in buf:
                break

        if not buf.strip():
            conn.close()
            return

        line = buf.split(b"\n")[0]
        req = json.loads(line.decode())
        text = req.get("text", "").strip()
        source = req.get("source", "web")

        if not text:
            conn.close()
            return

        logger.info(f"Request from {source}: {text[:100]}")

        def send_event(event):
            try:
                conn.sendall((json.dumps(event) + "\n").encode())
            except (BrokenPipeError, OSError):
                pass

        send_event({"type": "processing", "content": "thinking..."})
        handle_request(text, source, send_event)

    except Exception as e:
        logger.error(f"Client handler error: {e}")
        try:
            conn.sendall((json.dumps({"type": "error", "content": str(e)}) + "\n").encode())
            conn.sendall((json.dumps({"type": "done"}) + "\n").encode())
        except:
            pass
    finally:
        try:
            conn.close()
        except:
            pass


def cleanup_socket():
    try:
        os.unlink(SOCKET_PATH)
    except FileNotFoundError:
        pass


def main():
    cleanup_socket()

    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    server.bind(SOCKET_PATH)
    server.listen(5)
    os.chmod(SOCKET_PATH, 0o770)

    logger.info(f"Codex daemon started on {SOCKET_PATH} (model: {MODEL})")

    def shutdown(sig, frame):
        logger.info("Shutting down...")
        cleanup_socket()
        sys.exit(0)

    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)

    while True:
        try:
            conn, _ = server.accept()
            t = threading.Thread(target=handle_client, args=(conn,), daemon=True)
            t.start()
        except Exception as e:
            logger.error(f"Accept error: {e}")


if __name__ == "__main__":
    main()
