"""
Blender Interactive Terminal — IPC Server Bridge

Provides a local HTTP server that allows external CLI tools (like `bpy-exec` used by subprocesses)
to send Python code to be executed safely on Blender's main thread.
"""

import io
import json
import os
import queue
import secrets
import stat
import sys
import tempfile
import threading
import traceback
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Optional

import bpy

from .log import get_logger

log = get_logger("core.ipc")


class ExecRequest:
    def __init__(self, code: str):
        self.code = code
        self.error: Optional[str] = None
        self.output: str = ""
        self.event = threading.Event()


_exec_queue: queue.Queue = queue.Queue()
_ipc_token: str = secrets.token_hex(32)


def get_ipc_token() -> str:
    return _ipc_token


def _process_queue() -> float:
    """Timer callback: pops code execution requests and runs them safely on the main thread."""
    while not _exec_queue.empty():
        req: ExecRequest = _exec_queue.get_nowait()

        old_stdout = sys.stdout
        old_stderr = sys.stderr
        sys.stdout = sys.stderr = buf = io.StringIO()

        try:
            # Execute with persistent globals so scripts can chain commands across multiple script calls
            if not hasattr(bpy, "_bat_ipc_globals"):
                bpy._bat_ipc_globals = {"bpy": bpy}  # type: ignore

            exec(req.code, bpy._bat_ipc_globals)  # type: ignore
        except Exception:
            req.error = traceback.format_exc()
        finally:
            sys.stdout = old_stdout
            sys.stderr = old_stderr
            req.output = buf.getvalue()
            req.event.set()

    return 0.05  # Run every 50ms


class IPCHandler(BaseHTTPRequestHandler):
    def do_POST(self):
        if self.path == "/exec":
            if not secrets.compare_digest(self.headers.get("X-BAT-Token", ""), _ipc_token):
                self.send_response(403)
                self.end_headers()
                return
            length = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(length).decode("utf-8")
            data = json.loads(body)
            code = data.get("code", "")

            req = ExecRequest(code)
            _exec_queue.put(req)
            req.event.wait()  # Block HTTP thread until Blender main thread finishes execution

            res = {"output": req.output, "error": req.error}
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps(res).encode("utf-8"))
        else:
            self.send_response(404)
            self.end_headers()

    def log_message(self, format, *args):
        # Suppress default HTTP logging to avoid spamming the console
        pass


_server: Optional[HTTPServer] = None
_server_thread: Optional[threading.Thread] = None
_ipc_port: int = 0


def ensure_ipc_timer() -> None:
    """(Re)register the main-thread executor. persistent=True keeps it alive
    across file loads; without it, bpy-exec calls hang after File > Open."""
    if not bpy.app.timers.is_registered(_process_queue):
        bpy.app.timers.register(_process_queue, first_interval=0.1, persistent=True)


def start_ipc_server() -> int:
    global _server, _server_thread, _ipc_port
    if _server is not None:
        ensure_ipc_timer()
        return _ipc_port

    _server = HTTPServer(("127.0.0.1", 0), IPCHandler)
    _ipc_port = _server.server_port

    _server_thread = threading.Thread(target=_server.serve_forever, daemon=True)
    _server_thread.start()

    ensure_ipc_timer()

    log.info("IPC Server started on port %d", _ipc_port)
    return _ipc_port


def stop_ipc_server() -> None:
    global _server, _ipc_port
    if _server:
        _server.shutdown()
        _server.server_close()
        _server = None
        _ipc_port = 0
    if bpy.app.timers.is_registered(_process_queue):
        bpy.app.timers.unregister(_process_queue)
    log.info("IPC Server stopped")


_BRIDGE_SCRIPT = """import sys, os, json, urllib.request

port = os.environ.get("BLENDER_IPC_PORT") or "%(port)s"
token = os.environ.get("BLENDER_IPC_TOKEN", "")
code = " ".join(sys.argv[1:]) if len(sys.argv) > 1 else sys.stdin.read()

req = urllib.request.Request(
    "http://127.0.0.1:" + port + "/exec",
    data=json.dumps({"code": code}).encode("utf-8"),
    headers={"Content-Type": "application/json", "X-BAT-Token": token},
    method="POST",
)
# Never route the local bridge through an HTTP(S)_PROXY
opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
try:
    with opener.open(req) as response:
        res = json.loads(response.read().decode())
except Exception as e:
    print("Blender IPC Error: %%s" %% e, file=sys.stderr)
    sys.exit(1)
if res.get("output"):
    print(res["output"], end="")
if res.get("error"):
    print(res["error"], file=sys.stderr)
    sys.exit(1)
"""


def generate_bpy_exec(port: int) -> str:
    """
    Generate the CLI bridge `bpy-exec` and return its directory (for PATH).

    One directory per Blender process: a shared directory with the port
    baked in meant a second Blender instance overwrote the first one's
    bridge, so `bpy-exec` in the first terminal ran code in the other
    Blender. The script also prefers BLENDER_IPC_PORT from the environment.
    Wrappers call Blender's bundled Python, so no system Python is needed.
    """
    bin_dir = os.path.join(tempfile.gettempdir(), f"bat_ipc_bin_{os.getpid()}")
    os.makedirs(bin_dir, exist_ok=True)

    py_path = os.path.join(bin_dir, "bpy_exec.py")
    with open(py_path, "w", encoding="utf-8") as f:
        f.write(_BRIDGE_SCRIPT % {"port": port})

    python = sys.executable or "python"

    # POSIX shells (macOS/Linux, Git Bash on Windows)
    sh_path = os.path.join(bin_dir, "bpy-exec")
    with open(sh_path, "w", encoding="utf-8", newline="\n") as f:
        f.write('#!/bin/sh\nexec "%s" "%s" "$@"\n' % (python.replace("\\", "/"), py_path.replace("\\", "/")))
    os.chmod(sh_path, os.stat(sh_path).st_mode | stat.S_IEXEC)

    # Windows cmd / PowerShell
    cmd_path = os.path.join(bin_dir, "bpy-exec.cmd")
    with open(cmd_path, "w", encoding="utf-8", newline="\r\n") as f:
        f.write('@echo off\n"%s" "%s" %%*\n' % (python, py_path))

    return bin_dir
