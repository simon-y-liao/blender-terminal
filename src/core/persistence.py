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


def _auto_open_enabled() -> bool:
    try:
        from ..blender.preferences import get_prefs
        prefs = get_prefs(bpy.context)
        return bool(prefs and prefs.auto_open)
    except Exception:
        return False


def _main_window():
    wm = bpy.context.window_manager
    return bpy.context.window or (wm.windows[0] if wm.windows else None)


def _find_or_make_text_area(window):
    """Return a Text Editor area in this window, splitting one off the
    largest 3D Viewport (right-hand 35%) if there is none."""
    screen = window.screen
    for area in screen.areas:
        if area.type == "TEXT_EDITOR":
            return area

    candidates = [a for a in screen.areas if a.type == "VIEW_3D"] or list(screen.areas)
    if not candidates:
        return None
    target = max(candidates, key=lambda a: a.width * a.height)
    region = next((r for r in target.regions if r.type == "WINDOW"), None)
    before = set(a.as_pointer() for a in screen.areas)
    with bpy.context.temp_override(window=window, screen=screen, area=target, region=region):
        bpy.ops.screen.area_split(direction="VERTICAL", factor=0.65)
    new_areas = [a for a in screen.areas if a.as_pointer() not in before]
    if not new_areas:
        return None
    # Of the two halves, use the right-hand one for the terminal
    pair = [target, new_areas[0]]
    right = max(pair, key=lambda a: a.x)
    right.type = "TEXT_EDITOR"
    return right


def show_terminal(open_new: bool) -> bool:
    """Make sure the terminal is visible: reattach to a running session or,
    if open_new, start one. Creates a Text Editor area when needed."""
    window = _main_window()
    if window is None:
        return False
    area = _find_or_make_text_area(window)
    if area is None:
        return False

    if reattach_terminal(window):
        area.tag_redraw()
        return True
    if not open_new:
        return False

    region = next((r for r in area.regions if r.type == "WINDOW"), None)
    with bpy.context.temp_override(window=window, screen=window.screen, area=area, region=region):
        bpy.ops.bat.open_terminal("INVOKE_DEFAULT")
    return True


def _deferred_reattach():
    try:
        if _auto_open_enabled() and not bpy.app.background:
            show_terminal(open_new=True)
        else:
            reattach_terminal()
    except Exception as exc:
        log.error("Terminal re-attach failed: %s", exc)
    return None  # run once


def schedule_auto_open(delay: float = 1.0) -> None:
    """Called at add-on registration (Blender startup)."""
    if bpy.app.background or not _auto_open_enabled():
        return
    if not bpy.app.timers.is_registered(_deferred_reattach):
        bpy.app.timers.register(_deferred_reattach, first_interval=delay, persistent=True)


@persistent
def _on_load_post(*_args) -> None:
    # Wait until the new file's windows and screens are ready
    if not bpy.app.timers.is_registered(_deferred_reattach):
        bpy.app.timers.register(_deferred_reattach, first_interval=0.2, persistent=True)


def register_handlers() -> None:
    if _on_load_post not in bpy.app.handlers.load_post:
        bpy.app.handlers.load_post.append(_on_load_post)


def unregister_handlers() -> None:
    if _on_load_post in bpy.app.handlers.load_post:
        bpy.app.handlers.load_post.remove(_on_load_post)
    if bpy.app.timers.is_registered(_deferred_reattach):
        bpy.app.timers.unregister(_deferred_reattach)
