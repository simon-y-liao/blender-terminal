"""
Blender Interactive Terminal — Windows PTY Backend (ConPTY via pywinpty)

This module handles the Windows ConPTY backend using the pywinpty library.
pywinpty wraps the Windows ConPTY API which was introduced in Windows 10 1809.

Status: MVP stub — shows a clear user-visible error on unsupported platforms.
Post-MVP: full pywinpty implementation.
"""

import sys
import threading
from typing import Callable, Dict, List, Optional

from .backend import TerminalBackend
from ..core.log import get_logger

log = get_logger("terminal.windows_pty")


class WindowsPTYBackend(TerminalBackend):
    """
    Windows ConPTY terminal backend using pywinpty.

    Requires:
      - Windows 10 version 1809 or later (ConPTY API)
      - pywinpty >= 2.0 bundled as a wheel

    On non-Windows platforms this class raises RuntimeError immediately.
    """

    def __init__(self, on_output: Callable[[bytes], None]) -> None:
        super().__init__(on_output)

        if sys.platform != "win32":
            raise RuntimeError(
                "WindowsPTYBackend is only available on Windows. "
                "Use UnixPTYBackend on macOS/Linux."
            )

        # Import pywinpty lazily so import errors are clear
        try:
            import winpty  # noqa: F401  (pywinpty provides the `winpty` module)
            self._winpty = winpty
        except ImportError as exc:
            raise ImportError(
                "pywinpty is required on Windows but could not be imported. "
                "Ensure pywinpty is bundled in the extension wheels/ directory.\n"
                f"Original error: {exc}"
            ) from exc

        self._pty: Optional[object] = None
        self._reader_thread: Optional[threading.Thread] = None

    # ─── TerminalBackend interface ─────────────────────────────────────────

    def start(
        self,
        cmd: List[str],
        cols: int,
        rows: int,
        env: Optional[Dict[str, str]] = None,
        cwd: Optional[str] = None,
    ) -> bool:
        try:
            import os
            import shutil
            import subprocess

            env = dict(env) if env else dict(os.environ)
            exe = shutil.which(cmd[0], path=env.get("PATH")) or cmd[0]
            cmdline = (" " + subprocess.list2cmdline(cmd[1:])) if len(cmd) > 1 else None
            env_str = "\0".join(f"{k}={v}" for k, v in env.items()) + "\0"

            self._pty = self._winpty.PTY(cols, rows)
            if cmdline:
                self._pty.spawn(exe, cmdline=cmdline, cwd=cwd or os.getcwd(), env=env_str)
            else:
                self._pty.spawn(exe, cwd=cwd or os.getcwd(), env=env_str)

            self._reader_thread = threading.Thread(
                target=self._reader_loop,
                name="BAT-reader-win",
                daemon=True,
            )
            self._reader_thread.start()

            log.info("Started Windows ConPTY process: %s", exe)
            return True

        except Exception as exc:
            log.error("Failed to start Windows ConPTY process %s: %s", cmd, exc)
            self.last_error = f"{type(exc).__name__}: {exc}"
            return False

    def write(self, data: bytes) -> None:
        if self._pty is None:
            return
        try:
            if isinstance(data, (bytes, bytearray)):
                data = bytes(data).decode("utf-8", errors="replace")
            self._pty.write(data)
        except Exception as exc:
            log.debug("ConPTY write error: %s", exc)

    def resize(self, cols: int, rows: int) -> None:
        if self._pty is None:
            return
        try:
            self._pty.set_size(cols, rows)
            log.debug("ConPTY resized to %d×%d", cols, rows)
        except Exception as exc:
            log.debug("ConPTY resize error: %s", exc)

    def kill(self) -> None:
        """Terminate the shell and everything started from it (e.g. claude/node)."""
        pty = self._pty
        if pty is None:
            return
        self._pty = None
        pid = None
        try:
            pid = pty.pid
        except Exception:
            pass
        if pid:
            try:
                import subprocess
                subprocess.run(
                    ["taskkill", "/PID", str(pid), "/T", "/F"],
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                    timeout=5,
                )
            except Exception as exc:
                log.debug("taskkill failed: %s", exc)
        try:
            pty.cancel_io()  # unblock the reader thread
        except Exception:
            pass

    def is_alive(self) -> bool:
        if self._pty is None:
            return False
        try:
            return self._pty.isalive()
        except Exception:
            return False

    @property
    def pid(self) -> Optional[int]:
        if self._pty is None:
            return None
        try:
            return self._pty.pid
        except Exception:
            return None

    # ─── Private helpers ───────────────────────────────────────────────────

    def _reader_loop(self) -> None:
        """Daemon reader thread: reads from ConPTY and delivers to callback."""
        log.debug("Windows reader thread started")
        while self._pty is not None:
            try:
                data = self._pty.read(blocking=True)
                if data:
                    if isinstance(data, str):
                        data = data.encode("utf-8", errors="replace")
                    self._on_output(data)
                elif self._pty is None or self._pty.iseof():
                    break
            except Exception:
                break
        log.debug("Windows reader thread exiting")
        self._on_output(b"")
