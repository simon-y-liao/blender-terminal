"""
Blender Interactive Terminal — bind MCP tools in a terminal to *this* Blender.

Both common Blender MCP add-ons listen on a fixed port (9876 by default):

* Blender Lab "MCP" extension (blender.org/lab/mcp-server). Its server
  `blender-mcp` (package blmcp) reads BLENDER_MCP_HOST / BLENDER_MCP_PORT.
  The port is an add-on preference shared by every Blender of that version,
  and the socket uses SO_REUSEADDR, which on Windows lets several Blender
  instances bind 9876 at the same time. Connections then land on an
  arbitrary instance.
* BlenderMCP by ahujasid (github.com/ahujasid/blender-mcp). Its server reads
  BLENDER_HOST / BLENDER_PORT. A second Blender skips auto-start when 9876
  is taken, so every client reaches the first Blender.

Either way an agent started in one Blender's terminal could edit another
Blender. When a terminal opens, give this Blender's MCP server a port no
other process is listening on, and export it under both naming schemes.
The add-on preferences are not modified.
"""

import socket
import sys
from typing import Optional, Tuple

import bpy

from ..core.log import get_logger

log = get_logger("integrations.blender_mcp")

DEFAULT_PORT = 9876
_PORT_RANGE = range(9876, 9900)


def _listening(port: int) -> bool:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(0.15)
    try:
        return s.connect_ex(("127.0.0.1", port)) == 0
    finally:
        s.close()


def _free_port(preferred: int) -> Optional[int]:
    for port in [preferred, *_PORT_RANGE]:
        if not _listening(port):
            return port
    return None


# ─── Blender Lab "MCP" extension ─────────────────────────────────────────────

def _find_lab_server():
    """Return (mcp_to_blender_server, execute_interactive) modules, or (None, None)."""
    for name, mod in list(sys.modules.items()):
        if (name.endswith(".mcp_to_blender_server")
                and all(hasattr(mod, a) for a in ("start", "stop", "is_running", "_state"))):
            pkg = name.rsplit(".", 1)[0]
            return mod, sys.modules.get(pkg + ".execute_interactive")
    return None, None


def _lab_instance_port() -> Optional[int]:
    server, interactive = _find_lab_server()
    if server is None:
        return None

    if not server.is_running():
        # Stopped on purpose (or online access disabled): leave it stopped,
        # but never let this terminal reach another Blender
        port = _free_port(DEFAULT_PORT)
        log.info("Blender Lab MCP server is not running; terminal bound to unused port %s", port)
        return port

    host, port = server._state.sock.getsockname()[:2]

    # Is another process (another Blender) listening on the same port?
    # Stop ours briefly and probe, then restart on that port or a free one.
    server.stop()
    try:
        new_port = port if not _listening(port) else _free_port(port + 1)
        if new_port is None:
            new_port = port
        server.start(host, new_port)
    except Exception as exc:
        log.error("Blender Lab MCP: restart on a free port failed: %s", exc)
        try:
            server.start(host, port)
            new_port = port
        except Exception:
            return _free_port(DEFAULT_PORT)

    if interactive is not None and not bpy.app.timers.is_registered(interactive.run):
        bpy.app.timers.register(interactive.run, first_interval=0.05, persistent=True)

    if new_port != port:
        log.info("Blender Lab MCP: port %d is shared with another Blender, "
                 "this Blender now serves on %d", port, new_port)
    return new_port


# ─── BlenderMCP (ahujasid) ───────────────────────────────────────────────────

def _ahujasid_instance_port() -> Optional[int]:
    scene = getattr(bpy.context, "scene", None)
    if scene is None or not hasattr(scene, "blendermcp_port"):
        return None

    server = getattr(bpy.types, "blendermcp_server", None)
    if server is not None and getattr(server, "running", False):
        return int(server.port)

    port = _free_port(int(scene.blendermcp_port))
    if port is None:
        return None
    if port != scene.blendermcp_port:
        scene.blendermcp_port = port

    if server is not None:
        # Stopped on purpose: do not restart, only make sure it cannot clash
        server.port = port
        return port

    # Auto-start was skipped (typically because another Blender owns 9876)
    try:
        bpy.ops.blendermcp.start_server()
    except Exception as exc:
        log.error("BlenderMCP: could not start server on port %d: %s", port, exc)
    return port


# ─── Public ──────────────────────────────────────────────────────────────────

def ensure_instance_port() -> Tuple[Optional[int], str]:
    """Return (port, source) for the MCP server that belongs to this Blender.

    source is "lab", "blendermcp", "none" (no add-on here, but something else
    listens on the default port, so an unused port is returned) or "" when
    nothing needs to be exported.
    """
    port = _lab_instance_port()
    if port is not None:
        return port, "lab"
    port = _ahujasid_instance_port()
    if port is not None:
        return port, "blendermcp"
    if _listening(DEFAULT_PORT):
        return _free_port(DEFAULT_PORT + 1), "none"
    return None, ""
