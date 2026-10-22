"""
Blender Interactive Terminal — per-instance port for the BlenderMCP add-on.

The BlenderMCP add-on (github.com/ahujasid/blender-mcp) listens on a fixed
port (9876 by default). With two Blender instances open, the second one
skips auto-start because the port is taken, and every `blender-mcp` server
started from any terminal connects to 9876, i.e. to the *first* Blender.
An agent opened in the second Blender then silently edits the other one.

When a terminal opens, make sure *this* Blender's MCP server has its own
port and export it as BLENDER_PORT, which blender-mcp reads, so tools
started from this terminal talk to this Blender only.
"""

import socket
from typing import Optional

import bpy

from ..core.log import get_logger

log = get_logger("integrations.blender_mcp")

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


def ensure_instance_port() -> Optional[int]:
    """Return the BlenderMCP port that belongs to this Blender instance.

    Returns None when the add-on is not installed or no port is available.
    Never returns a port that another Blender is listening on.
    """
    scene = getattr(bpy.context, "scene", None)
    if scene is None:
        return None
    if not hasattr(scene, "blendermcp_port"):
        # Add-on not enabled in this Blender. If another Blender serves
        # BlenderMCP on the default port, a blender-mcp started here would
        # silently control *that* instance. Point it at an unused port so it
        # fails visibly instead.
        if _listening(9876):
            port = _free_port(9877)
            log.warning("BlenderMCP is not enabled in this Blender; "
                        "terminal bound to unused port %s", port)
            return port
        return None

    server = getattr(bpy.types, "blendermcp_server", None)

    # Already serving in this Blender: use it as is
    if server is not None and getattr(server, "running", False):
        return int(server.port)

    port = _free_port(int(scene.blendermcp_port))
    if port is None:
        log.warning("BlenderMCP: no free port in %s", _PORT_RANGE)
        return None

    if port != scene.blendermcp_port:
        scene.blendermcp_port = port

    if server is not None:
        # The user stopped the server on purpose: do not restart it, but make
        # sure tools from this terminal can never reach another Blender
        server.port = port
        log.info("BlenderMCP server is stopped; terminal bound to port %d", port)
        return port

    # Auto-start was skipped (typically because another Blender owns 9876)
    try:
        bpy.ops.blendermcp.start_server()
    except Exception as exc:
        log.error("BlenderMCP: could not start server on port %d: %s", port, exc)
        return port
    log.info("BlenderMCP server for this Blender started on port %d", port)
    return port
