#!/usr/bin/env python3
"""
Claude Persistent Daemon
========================
Maintains a single long-running conversation context accessible via Unix socket.
Multiple clients (terminal, Telegram) share the same Claude instance.

Protocol (newline-delimited JSON over Unix socket):
  Client → Daemon:  {"source": "terminal"|"telegram", "text": "...", "chat_id": null}
  Daemon → Client:  {"type": "text",        "content": "..."}
                    {"type": "tool_start",  "name": "...", "command": "..."}
                    {"type": "tool_result", "output": "..."}
                    {"type": "error",       "content": "..."}
                    {"type": "done"}
"""

import os
import sys
import json
import socket
import threading
import subprocess
import signal
import logging
import time
import queue
from datetime import datetime
from pathlib import Path

import anthropic
from dotenv import load_dotenv

load_dotenv(Path(__file__).parent / ".env")

import storage
import sys as _sys
_sys.path.insert(0, str(Path(__file__).parent.parent / "ai-usage"))
import tracker as _tracker

# ── Config ─────────────────────────────────────────────────────────────────────
SOCKET_PATH   = os.path.join(os.environ.get("CLAUDE_SOCKET_DIR", f"/run/user/{os.getuid()}"), "claude-daemon.sock")
API_KEY       = os.environ.get("ANTHROPIC_API_KEY", "")
MODEL         = "claude-sonnet-4-6"
MAX_TOKENS    = 16000
TOOL_TIMEOUT  = 300    # seconds for a single bash command
WATCHDOG_TICK = 10     # seconds between watchdog checks
PROCESSING_INTERVAL = 120  # seconds between "Processing..." messages
STUCK_TIMEOUT = 1200  # 20 min of silence → declare stuck

if not API_KEY:
    raise RuntimeError("ANTHROPIC_API_KEY environment variable is not set. Set it in .env or the environment.")

SENTINEL = "Mxyzptlk"

CONTEXT_KEEP    = 80   # messages to keep in full after trimming
CONTEXT_TRIM_AT = 100  # trigger trim when history exceeds this

LOG_PATH = Path(__file__).parent / "daemon.log"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler(LOG_PATH),
        logging.StreamHandler(sys.stdout),
    ]
)
log = logging.getLogger(__name__)

OWNER_NAME = os.environ.get("OWNER_NAME", "the system owner")

# ── System prompt ───────────────────────────────────────────────────────────────
SYSTEM_PROMPT = f"""You are Claude, running as a persistent daemon on a Linux server.

You are maintained as a single always-on process. {OWNER_NAME} connects via terminal \
(SSH/tmux) or Telegram. All inputs share this single conversation context — you are always the same \
instance, never reset between connections.

You have full, unrestricted access to the server: run any bash command, read/write any file, \
access the network. This is a trusted development environment.

Key infrastructure you can interact with:
- Proxmox/hypervisor server (configure YOUR_PROXMOX_IP in your environment)
- Additional VMs/containers as configured in your environment
- Cron jobs and scripts in your home directory
- All locally running services

Important rule — at the end of EVERY complete response (after all thinking and all tool calls \
are finished), output exactly this word on its own line:

{SENTINEL}

This sentinel lets automated systems detect when you have finished responding. \
Never output it mid-response, only at the very end."""

# ── Tools definition ────────────────────────────────────────────────────────────
TOOLS = [
    {
        "name": "bash",
        "description": (
            "Execute a bash command on the server with full system access. "
            "For long-running commands, prefer backgrounding with & and checking results. "
            "Timeout: 300 seconds."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "command": {"type": "string", "description": "Bash command to execute"},
            },
            "required": ["command"],
        },
    },
]


# ── Priority queue item ─────────────────────────────────────────────────────────
class WorkItem:
    """A pending message from a client."""
    def __init__(self, priority, text, source, conn, chat_id=None):
        self.priority = priority   # 0=terminal, 1=telegram
        self.text = text
        self.source = source
        self.conn = conn           # socket connection to reply on
        self.chat_id = chat_id

    def __lt__(self, other):
        return self.priority < other.priority


# ── Daemon ──────────────────────────────────────────────────────────────────────
class ClaudeDaemon:
    def __init__(self):
        storage.init_db()
        self.client = anthropic.Anthropic(api_key=API_KEY)
        self.messages: list[dict] = []
        self.work_queue: queue.PriorityQueue = queue.PriorityQueue()
        self.processing = False
        self.last_output_time: float = 0.0
        self._shutdown = False
        self._active_conn = None  # socket conn currently being served
        self._active_source = None

        # Load persisted conversation
        self.messages = storage.load_messages()
        log.info(f"Loaded {len(self.messages)} messages from storage.")

    # ── Socket server ───────────────────────────────────────────────────────────
    def run(self):
        signal.signal(signal.SIGTERM, self._handle_sigterm)
        signal.signal(signal.SIGINT, self._handle_sigterm)

        # Start worker thread
        worker = threading.Thread(target=self._worker_loop, daemon=True)
        worker.start()

        # Start watchdog thread
        watchdog = threading.Thread(target=self._watchdog_loop, daemon=True)
        watchdog.start()

        self._run_socket_server()

    def _run_socket_server(self):
        sock_path = SOCKET_PATH
        if os.path.exists(sock_path):
            os.unlink(sock_path)

        server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        server.bind(sock_path)
        os.chmod(sock_path, 0o600)
        server.listen(5)
        server.settimeout(1.0)
        log.info(f"Listening on {sock_path}")

        while not self._shutdown:
            try:
                conn, _ = server.accept()
                t = threading.Thread(target=self._handle_client, args=(conn,), daemon=True)
                t.start()
            except socket.timeout:
                continue
            except Exception as e:
                if not self._shutdown:
                    log.error(f"Accept error: {e}")

        server.close()
        if os.path.exists(sock_path):
            os.unlink(sock_path)
        log.info("Socket server shut down.")

    def _handle_client(self, conn: socket.socket):
        """Read one request from a client, enqueue it, then hold connection open for response."""
        try:
            data = b""
            while b"\n" not in data:
                chunk = conn.recv(4096)
                if not chunk:
                    return
                data += chunk
            request = json.loads(data.split(b"\n")[0].decode())
        except Exception as e:
            log.warning(f"Bad client request: {e}")
            conn.close()
            return

        text   = request.get("text", "").strip()
        source = request.get("source", "terminal")
        chat_id = request.get("chat_id")
        priority = 0 if source == "terminal" else 1

        if not text:
            self._send(conn, {"type": "error", "content": "Empty message."})
            self._send(conn, {"type": "done"})
            conn.close()
            return

        item = WorkItem(priority, text, source, conn, chat_id)
        self.work_queue.put((priority, item))
        log.info(f"Queued [{source}] message (priority={priority}): {text[:80]}")

    # ── Worker loop ─────────────────────────────────────────────────────────────
    def _worker_loop(self):
        while not self._shutdown:
            try:
                _, item = self.work_queue.get(timeout=1.0)
            except queue.Empty:
                continue

            self.processing = True
            self.last_output_time = time.time()
            self._active_conn = item.conn
            self._active_source = item.source

            try:
                self._process_message(item)
            except Exception as e:
                log.exception(f"Error processing message: {e}")
                self._send(item.conn, {"type": "error", "content": str(e)})
                self._send(item.conn, {"type": "done"})
            finally:
                self.processing = False
                self._active_conn = None
                self._active_source = None
                try:
                    item.conn.close()
                except Exception:
                    pass

    def _process_message(self, item: WorkItem):
        conn = item.conn

        # Add user message
        self.messages.append({"role": "user", "content": item.text})
        storage.save_message("user", item.text, item.source)

        while True:
            response_text, stop_reason, content_blocks = self._call_api(conn)
            self.last_output_time = time.time()

            if stop_reason == "tool_use":
                # Execute all tools in the response
                assistant_content = content_blocks
                self.messages.append({"role": "assistant", "content": assistant_content})

                tool_results = []
                for block in content_blocks:
                    if block.type == "tool_use":
                        output = self._run_tool(block.name, block.input, conn)
                        tool_results.append({
                            "type": "tool_result",
                            "tool_use_id": block.id,
                            "content": output,
                        })
                        self.last_output_time = time.time()

                self.messages.append({"role": "user", "content": tool_results})

            else:
                # Final text response
                self.messages.append({"role": "assistant", "content": response_text})
                storage.save_message("assistant", response_text, item.source)
                self._trim_context()

                # Strip sentinel from stored text but it was already streamed
                self._send(conn, {"type": "done"})
                break

    def _trim_context(self):
        """If history is too long, summarise the oldest messages and replace them."""
        if len(self.messages) <= CONTEXT_TRIM_AT:
            return

        to_summarise = self.messages[:-CONTEXT_KEEP]
        keep         = self.messages[-CONTEXT_KEEP:]

        # Build a plain-text digest of the dropped messages
        lines = []
        for m in to_summarise:
            role = m.get("role", "?")
            content = m.get("content", "")
            if isinstance(content, list):
                # Tool call blocks — just note the tool names/outputs briefly
                parts = []
                for block in content:
                    if isinstance(block, dict):
                        if block.get("type") == "tool_result":
                            parts.append(f"[tool_result: {str(block.get('content',''))[:120]}]")
                    elif hasattr(block, "type"):
                        if block.type == "tool_use":
                            parts.append(f"[tool_use: {block.name}({str(block.input)[:80]})]")
                        elif hasattr(block, "text"):
                            parts.append(block.text[:200])
                content = " ".join(parts)
            elif not isinstance(content, str):
                content = str(content)
            lines.append(f"{role}: {content[:300]}")

        summary_text = (
            f"[Context summary — {len(to_summarise)} older messages trimmed]\n"
            + "\n".join(lines)
        )

        self.messages = [{"role": "user", "content": summary_text},
                         {"role": "assistant", "content": "Understood, I have the summary of our earlier conversation."},
                         *keep]
        log.info(f"Context trimmed: {len(to_summarise)} messages → summary. Keeping {len(keep)} recent.")

    def _call_api(self, conn: socket.socket):
        """Stream a response from the API. Returns (full_text, stop_reason, content_blocks)."""
        full_text = ""
        content_blocks = []

        with self.client.messages.stream(
            model=MODEL,
            max_tokens=MAX_TOKENS,
            system=SYSTEM_PROMPT,
            messages=self.messages,
            tools=TOOLS,
        ) as stream:
            for event in stream:
                event_type = type(event).__name__

                if event_type == "RawContentBlockDeltaEvent":
                    delta = event.delta
                    if hasattr(delta, "text"):
                        self._send(conn, {"type": "text", "content": delta.text})
                        full_text += delta.text
                        self.last_output_time = time.time()

            final = stream.get_final_message()

        # Log token usage
        try:
            u = final.usage
            _tracker.log_usage(
                agent="claude", model=MODEL,
                source=getattr(self, "_active_source", None) or "unknown",
                input_tokens=u.input_tokens,
                output_tokens=u.output_tokens,
            )
        except Exception:
            pass

        return full_text, final.stop_reason, final.content

    def _run_tool(self, name: str, input_data: dict, conn: socket.socket) -> str:
        if name == "bash":
            command = input_data.get("command", "")
            self._send(conn, {"type": "tool_start", "name": "bash", "command": command})
            output = self._exec_bash(command)
            self._send(conn, {"type": "tool_result", "output": output})
            return output
        return f"Unknown tool: {name}"

    def _exec_bash(self, command: str) -> str:
        # How long bash can stay in do_wait (waiting for a child) with no new
        # output before we assume it's blocked on a background service and kill
        # it early.  Background children survive and are reparented to systemd.
        BG_IDLE_SECS = 8

        import selectors, os as _os, signal as _signal

        try:
            proc = subprocess.Popen(
                command,
                shell=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                stdin=subprocess.DEVNULL,
                executable="/bin/bash",
                start_new_session=True,   # new process group so we can killpg
            )
        except Exception as e:
            return f"[ERROR] {e}"

        def _kill_group():
            """Kill the entire process group so background children release the pipe."""
            try:
                _os.killpg(_os.getpgid(proc.pid), _signal.SIGKILL)
            except ProcessLookupError:
                pass
            try:
                proc.kill()
            except ProcessLookupError:
                pass

        chunks: list[bytes] = []
        total_bytes = 0
        MAX_OUTPUT = 512 * 1024   # 512 KB — prevent pipe-flooding
        last_output = time.time()
        deadline = time.time() + TOOL_TIMEOUT
        killed_for_bg = False

        raw_fd = proc.stdout.fileno()
        sel = selectors.DefaultSelector()
        sel.register(raw_fd, selectors.EVENT_READ)
        try:
            while True:
                if time.time() >= deadline:
                    _kill_group()
                    break

                ready = sel.select(timeout=1.0)
                if ready:
                    # Use os.read on the raw fd to avoid BufferedReader blocking
                    # even after sel.select reports readability.
                    try:
                        data = _os.read(raw_fd, 4096)
                    except OSError:
                        data = b""
                    if not data:   # EOF — bash exited and closed the pipe
                        break
                    chunks.append(data)
                    total_bytes += len(data)
                    last_output = time.time()
                    if total_bytes > MAX_OUTPUT:
                        chunks.append(b"\n[output truncated -- exceeded 512 KB limit]")
                        _kill_group()
                        break
                else:
                    # No output this tick — check if bash already exited
                    if proc.poll() is not None:
                        break

                    # Check whether bash is stuck in do_wait (waiting for a
                    # child to exit) with no recent output.  This happens when
                    # bash has finished its script but is waiting for a
                    # background process (e.g. a server) to exit before closing
                    # its stdout pipe.  Killing bash lets background children
                    # continue under systemd while we unblock immediately.
                    idle = time.time() - last_output
                    if idle >= BG_IDLE_SECS:
                        try:
                            wchan = Path(f"/proc/{proc.pid}/wchan").read_text().strip()
                            if wchan.startswith("do_wait"):
                                killed_for_bg = True
                                _kill_group()
                                break
                        except Exception:
                            pass
        finally:
            sel.close()

        # Wait for bash to fully exit; hard-kill if it somehow lingers
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()

        # Drain any bytes still in the pipe after bash exits
        try:
            remaining = _os.read(raw_fd, 65536)
            if remaining:
                chunks.append(remaining)
        except OSError:
            pass

        out = b"".join(chunks).decode(errors="replace").strip() or "(no output)"
        if killed_for_bg:
            out += "\n[daemon: bash killed after idle wait — background service kept running]"
        return out

    # ── Watchdog ────────────────────────────────────────────────────────────────
    def _watchdog_loop(self):
        last_processing_alert = 0.0

        while not self._shutdown:
            time.sleep(WATCHDOG_TICK)

            if not self.processing:
                last_processing_alert = 0.0
                continue

            now = time.time()
            silence = now - self.last_output_time
            since_last_alert = now - last_processing_alert

            if silence > STUCK_TIMEOUT:
                log.warning("Claude appears stuck (20 min silence). Aborting current task.")
                if self._active_conn:
                    self._send(self._active_conn, {
                        "type": "error",
                        "content": "Task aborted: no output for 20 minutes."
                    })
                    self._send(self._active_conn, {"type": "done"})
                self.processing = False

            elif since_last_alert >= PROCESSING_INTERVAL:
                log.info("Watchdog: sending Processing... alert")
                if self._active_conn:
                    self._send(self._active_conn, {
                        "type": "processing",
                        "content": "Still working..."
                    })
                last_processing_alert = now

    # ── Helpers ─────────────────────────────────────────────────────────────────
    @staticmethod
    def _send(conn: socket.socket, obj: dict):
        try:
            conn.sendall((json.dumps(obj) + "\n").encode())
        except Exception:
            pass

    def _handle_sigterm(self, signum, frame):
        log.info(f"Received signal {signum}, shutting down.")
        self._shutdown = True


# ── Entry point ─────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    daemon = ClaudeDaemon()
    daemon.run()
