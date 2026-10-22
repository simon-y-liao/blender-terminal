"""
Blender Interactive Terminal — keep the terminal alive across file loads.

Opening, reverting or reloading a .blend (File > Open, File > Revert,
bpy.ops.wm.open_mainfile / read_homefile, e.g. from a script or an agent
running in the terminal) resets WindowManager properties and removes all
modal handlers. The shell and its scrollback live on in Python module
state, but the UI forgot about them: the terminal looked empty and
"Open Terminal" started a second, unrelated shell.

This module re-attaches the UI to the still-running session.
"""

import bpy
from bpy.app.handlers import persistent

from .log import get_logger

log = get_logger("core.persistence")


def _tag_text_editors() -> None:
    wm = bpy.context.window_manager
    for window in wm.windows:
        for area in window.screen.areas:
            if area.type == "TEXT_EDITOR":
                area.tag_redraw()


def _start_input_modal(window=None) -> None:
    wm = bpy.context.window_manager
    if wm.bat_input_active:
        return
    window = window or bpy.context.window or (wm.windows[0] if wm.windows else None)
    if window is None:
        return
    with bpy.context.temp_override(window=window, screen=window.screen):
        bpy.ops.bat.input_modal("INVOKE_DEFAULT")


def reattach_terminal(window=None) -> bool:
    """Reconnect the UI to the active session if its process is still running.

    Returns True if a running session was found and re-attached.
    """
    from ..terminal.manager import get_manager
    from ..ui.terminal_draw import register_draw_handler
    from .events import register_pump_timer
    from .ipc import ensure_ipc_timer

    manager = get_manager()
    session = manager.get_active() if manager else None
    if session is None or not session.is_alive():
        return False

    # Fonts loaded with blf.load() may not survive a file load
    from ..ui.terminal_draw import reset_font
    reset_font()

    wm = bpy.context.window_manager
    wm.bat_terminal_active = True
    wm.bat_session_id = session.session_id
    register_draw_handler()
    register_pump_timer()
    ensure_ipc_timer()
    try:
        _start_input_modal(window)
    except Exception as exc:
        log.error("Could not restart terminal input after file load: %s", exc)
    _tag_text_editors()
    log.info("Re-attached terminal session %s", session.session_id)
    return True


def _deferred_reattach():
    try:
        reattach_terminal()
    except Exception as exc:
        log.error("Terminal re-attach failed: %s", exc)
    return None  # run once


@persistent
def _on_load_post(*_args) -> None:
    # Wait until the new file's windows and screens are ready
    bpy.app.timers.register(_deferred_reattach, first_interval=0.2, persistent=True)


def register_handlers() -> None:
    if _on_load_post not in bpy.app.handlers.load_post:
        bpy.app.handlers.load_post.append(_on_load_post)


def unregister_handlers() -> None:
    if _on_load_post in bpy.app.handlers.load_post:
        bpy.app.handlers.load_post.remove(_on_load_post)
    if bpy.app.timers.is_registered(_deferred_reattach):
        bpy.app.timers.unregister(_deferred_reattach)
