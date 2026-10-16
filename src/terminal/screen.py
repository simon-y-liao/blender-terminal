"""
Blender Interactive Terminal — Terminal Screen (pyte wrapper + scrollback)

Wraps pyte.Screen and pyte.ByteStream to provide:
  - In-memory terminal emulation (ANSI, VT100/220/520, colors, cursor)
  - Scrollback buffer (configurable line limit)
  - Thread-safe snapshot access for the renderer

pyte is a pure-Python library bundled as a wheel.
"""

import threading
from dataclasses import dataclass, field
from typing import List, Optional, Tuple

from ..core.log import get_logger

log = get_logger("terminal.screen")

# ─── pyte color mapping ───────────────────────────────────────────────────────

# pyte returns color names like 'green', 'default', digit strings for 256-color
# We map to RGBA tuples (0.0–1.0) matching a dark terminal theme.
_DEFAULT_FG = (0.85, 0.85, 0.85, 1.0)   # light grey
_DEFAULT_BG = (0.0, 0.0, 0.0, 0.0)      # transparent (background rect drawn separately)
_REVERSE_FG = (0.10, 0.10, 0.10, 1.0)   # text colour for inverse cells with default background

# ANSI 16 colors — dark terminal palette
_ANSI_COLORS = {
    # Standard
    "black":         (0.07, 0.07, 0.07, 1.0),
    "red":           (0.80, 0.20, 0.20, 1.0),
    "green":         (0.30, 0.80, 0.30, 1.0),
    "brown":         (0.70, 0.50, 0.10, 1.0),
    "blue":          (0.20, 0.40, 0.90, 1.0),
    "magenta":       (0.75, 0.20, 0.75, 1.0),
    "cyan":          (0.20, 0.75, 0.75, 1.0),
    "white":         (0.80, 0.80, 0.80, 1.0),
    # Bright variants
    "brightblack":   (0.55, 0.55, 0.55, 1.0),
    "brightred":     (1.00, 0.35, 0.35, 1.0),
    "brightgreen":   (0.50, 1.00, 0.50, 1.0),
    "brightyellow":  (1.00, 1.00, 0.40, 1.0),
    "brightblue":    (0.40, 0.60, 1.00, 1.0),
    "brightmagenta": (1.00, 0.40, 1.00, 1.0),
    "brightcyan":    (0.40, 1.00, 1.00, 1.0),
    "brightwhite":   (1.00, 1.00, 1.00, 1.0),
    # pyte uses "yellow" for brown position
    "yellow":        (1.00, 1.00, 0.40, 1.0),
    "default":       _DEFAULT_FG,
}


def _patch_pyte_bright_colors() -> None:
    """pyte 0.8 maps bright colors (SGR 90-97/100-107) to the normal ones, so
    dark gray (90, used by PowerShell for parameters like --continue) became
    black and was invisible on a dark background. Keep them distinct."""
    try:
        import pyte.graphics as g
        names = ["brightblack", "brightred", "brightgreen", "brightyellow",
                 "brightblue", "brightmagenta", "brightcyan", "brightwhite"]
        for i, n in enumerate(names):
            g.FG_AIXTERM[90 + i] = n
            g.BG_AIXTERM[100 + i] = n
    except Exception:
        pass


_patch_pyte_bright_colors()


def resolve_color(color_spec, default: Tuple) -> Tuple:
    """
    Resolve a pyte color spec to an RGBA tuple.
    pyte color can be:
      - 'default'             → use default
      - 'green', 'red', etc.  → ANSI named color
      - an integer (0-255)    → 256-color index
      - a hex string '#rrggbb' (rare)
    """
    if color_spec == "default" or color_spec is None:
        return default
    if isinstance(color_spec, str):
        name = color_spec.lower()
        if name in _ANSI_COLORS:
            return _ANSI_COLORS[name]
        hexs = name[1:] if name.startswith("#") else name
        if len(hexs) == 6:
            try:
                r = int(hexs[0:2], 16) / 255.0
                g = int(hexs[2:4], 16) / 255.0
                b = int(hexs[4:6], 16) / 255.0
                return (r, g, b, 1.0)
            except ValueError:
                pass
    if isinstance(color_spec, int):
        return _resolve_256color(color_spec)
    return default


def _resolve_256color(index: int) -> Tuple:
    """Map a 256-color index to RGBA."""
    if index < 0 or index > 255:
        return _DEFAULT_FG
    if index < 16:
        # Standard 16 colors — use our palette
        names = list(_ANSI_COLORS.keys())[:16]
        return list(_ANSI_COLORS.values())[index % 16]
    if index < 232:
        # 6×6×6 color cube
        index -= 16
        b = index % 6
        index //= 6
        g = index % 6
        r = index // 6
        return (r * 51 / 255.0, g * 51 / 255.0, b * 51 / 255.0, 1.0)
    # Grayscale
    v = (index - 232) * 10 + 8
    v /= 255.0
    return (v, v, v, 1.0)


# ─── Rendered line / character ────────────────────────────────────────────────

@dataclass
class RenderedChar:
    """A single rendered character cell."""
    char: str = " "
    fg: Tuple = field(default_factory=lambda: _DEFAULT_FG)
    bg: Tuple = field(default_factory=lambda: _DEFAULT_BG)
    bold: bool = False
    italics: bool = False
    underline: bool = False
    has_bg: bool = False      # True when the cell has a non-default background
    wide_tail: bool = False   # right half of a double-width (CJK) character


# A rendered line is a list of RenderedChar (length = screen width)
RenderedLine = List[RenderedChar]


# ─── TerminalScreen ───────────────────────────────────────────────────────────

class TerminalScreen:
    """
    Wraps pyte.Screen + pyte.ByteStream.

    Thread safety:
      - feed() may be called from the MAIN THREAD (timer callback).
      - get_display_lines() is called from the DRAW HANDLER on the main thread.
      - Both happen on the main thread so no locking is required here.
        (The OutputQueue handles the thread-safe handoff from reader thread.)
    """

    def __init__(self, cols: int = 220, rows: int = 50, max_scrollback: int = 2000) -> None:
        self._cols = cols
        self._rows = rows
        self._max_scrollback = max_scrollback
        self._scroll_offset: int = 0  # lines scrolled up from bottom
        self._trackpad_scroll_accum: float = 0.0
        self._lock = threading.Lock()

        # Text selection (col, row). (row is 0 at top of current display, NOT absolute history)
        self.selection_start: Optional[Tuple[int, int]] = None
        self.selection_end: Optional[Tuple[int, int]] = None

        # Callback that sends terminal replies (cursor position, device
        # attributes) back to the program; set by TerminalSession.
        self.response_handler = None

        self._init_pyte()

    @property
    def cols(self) -> int:
        return self._cols

    @property
    def rows(self) -> int:
        return self._rows

    def _init_pyte(self) -> None:
        import pyte
        self._screen = pyte.HistoryScreen(self._cols, self._rows, history=self._max_scrollback)
        self._pushed_lines = 0
        screen = self._screen
        orig_index = screen.index

        self._alt_saved = None  # (buffer copy, cursor) while the alternate screen is active

        def _counting_index(*a, **kw):
            top = screen.history.top
            last = top[-1] if top else None
            orig_index(*a, **kw)
            if top and top[-1] is not last:
                if self._alt_saved is not None:
                    top.pop()  # full-screen apps must not pollute scrollback
                else:
                    self._pushed_lines += 1

        _ALT_MODES = {47, 1047, 1049}
        orig_set_mode = screen.set_mode
        orig_reset_mode = screen.reset_mode

        def _set_mode(*modes, **kw):
            if kw.get("private") and _ALT_MODES.intersection(modes) and self._alt_saved is None:
                import copy
                saved_buffer = {y: row.copy() for y, row in screen.buffer.items()}
                self._alt_saved = (saved_buffer, copy.copy(screen.cursor))
                self._scroll_offset = 0
                screen.erase_in_display(2)
                modes = tuple(m for m in modes if m not in _ALT_MODES)
                if not modes:
                    return
            orig_set_mode(*modes, **kw)

        def _reset_mode(*modes, **kw):
            if kw.get("private") and _ALT_MODES.intersection(modes) and self._alt_saved is not None:
                saved_buffer, saved_cursor = self._alt_saved
                self._alt_saved = None
                screen.buffer.clear()
                screen.buffer.update(saved_buffer)
                screen.cursor = saved_cursor
                screen.dirty.update(range(screen.lines))
                modes = tuple(m for m in modes if m not in _ALT_MODES)
                if not modes:
                    return
            orig_reset_mode(*modes, **kw)

        screen.set_mode = _set_mode
        screen.reset_mode = _reset_mode

        # Create the parser last: pyte binds handler methods when the stream attaches
        self._stream = pyte.ByteStream(self._screen)

        screen.index = _counting_index

        def _reply(data):
            handler = self.response_handler
            if handler and data:
                try:
                    handler(data.encode("utf-8") if isinstance(data, str) else data)
                except Exception as exc:
                    log.debug("terminal reply failed: %s", exc)

        screen.write_process_input = _reply
        log.debug("pyte history screen initialized (%d×%d)", self._cols, self._rows)

    def feed(self, data: bytes) -> None:
        """Feed raw PTY bytes into pyte. Called on the main thread."""
        if not data:
            return
        self._pushed_lines = 0
        try:
            self._stream.feed(data)
        except Exception as exc:
            log.debug("pyte feed error: %s", exc)
        # Keep the view still while the user is reading scrollback
        if self._scroll_offset > 0 and self._pushed_lines:
            top_len = len(self._screen.history.top)
            self._scroll_offset = min(self._scroll_offset + self._pushed_lines, top_len)

    def resize(self, cols: int, rows: int) -> None:
        """Resize the pyte screen. Called when the Blender area resizes."""
        if cols == self._cols and rows == self._rows:
            return

        old_rows = self._rows
        diff = old_rows - rows

        if getattr(self._screen, "history", None) is not None:
            if diff > 0:
                # Shrinking: push top lines to history
                for y in range(diff):
                    if y in self._screen.buffer:
                        self._screen.history.top.append(self._screen.buffer[y].copy())
            elif diff < 0:
                # Expanding: pull lines from history
                pull_count = min(-diff, len(self._screen.history.top))
                if pull_count > 0:
                    for y in range(old_rows - 1, -1, -1):
                        if y in self._screen.buffer:
                            self._screen.buffer[y + pull_count] = self._screen.buffer.pop(y)
                    for y in range(pull_count - 1, -1, -1):
                        self._screen.buffer[y] = self._screen.history.top.pop()
                    self._screen.cursor.y += pull_count

        self._cols = cols
        self._rows = rows
        try:
            self._screen.resize(rows, cols)
            log.debug("pyte screen resized to %d×%d", cols, rows)
        except Exception as exc:
            log.debug("pyte resize error: %s", exc)

    def _pyte_char_to_rendered(self, char) -> RenderedChar:
        """Convert a pyte.screens.Char to our RenderedChar."""
        fg = resolve_color(char.fg, _DEFAULT_FG)
        # Windows/ConPTY often paints the console's black background explicitly;
        # like Windows Terminal, treat palette black as "no background"
        bg_default = char.bg in (None, "default", "black", "000000", "0c0c0c")
        bg = _DEFAULT_BG if bg_default else resolve_color(char.bg, _DEFAULT_BG)
        if char.bold and char.fg in (None, "default"):
            # Brighten default foreground for bold text
            fg = tuple(min(1.0, c * 1.3) if i < 3 else c for i, c in enumerate(fg))
        has_bg = not bg_default
        if getattr(char, "reverse", False):
            # Inverse video: swap foreground and background
            new_bg = fg
            new_fg = bg if has_bg else _REVERSE_FG
            fg, bg, has_bg = new_fg, new_bg, True
        return RenderedChar(
            char=char.data if char.data else "",
            fg=fg,
            bg=bg,
            bold=char.bold,
            italics=char.italics,
            underline=bool(getattr(char, "underscore", False)),
            has_bg=has_bg,
            wide_tail=(char.data == ""),
        )

    def get_display_lines(self) -> List[RenderedLine]:
        """
        Return the lines currently visible on screen, accounting for
        scroll offset. Also returns scrollback context above the screen.
        """
        top = list(self._screen.history.top) if getattr(self._screen, "history", None) else []
        
        # Build the full raw lines array
        raw_lines = top + [self._screen.buffer[r] for r in range(self._rows)]
        
        # Slice the visible window
        start_idx = len(raw_lines) - self._rows - self._scroll_offset
        start_idx = max(0, start_idx)
        end_idx = start_idx + self._rows
        
        visible_raw = raw_lines[start_idx:end_idx]
        
        # Parse into RenderedLine
        screen_lines: List[RenderedLine] = []
        for raw_line in visible_raw:
            line: RenderedLine = []
            for col_idx in range(self._cols):
                char = raw_line.get(col_idx)
                if char is None:
                    line.append(RenderedChar())
                else:
                    line.append(self._pyte_char_to_rendered(char))
            screen_lines.append(line)
            
        return screen_lines

    @property
    def cursor_row(self) -> int:
        return self._screen.cursor.y

    @property
    def cursor_col(self) -> int:
        return self._screen.cursor.x

    def _private_mode(self, n: int) -> bool:
        try:
            return (n << 5) in self._screen.mode
        except Exception:
            return False

    @property
    def mouse_reporting(self) -> bool:
        """True when the program asked for mouse events (ESC[?1000h / 1002h / 1003h)."""
        return any(self._private_mode(n) for n in (1000, 1002, 1003))

    @property
    def mouse_sgr(self) -> bool:
        return self._private_mode(1006)

    @property
    def in_alt_screen(self) -> bool:
        return self._alt_saved is not None

    @property
    def bracketed_paste(self) -> bool:
        """True when the program enabled bracketed paste (ESC[?2004h)."""
        try:
            return (2004 << 5) in self._screen.mode
        except Exception:
            return False

    @property
    def cursor_hidden(self) -> bool:
        """True when the running program hid the cursor (ESC[?25l), e.g. TUIs like Claude Code."""
        try:
            return bool(self._screen.cursor.hidden)
        except Exception:
            return False

    @property
    def cols(self) -> int:
        return self._cols

    @property
    def rows(self) -> int:
        return self._rows

    def scroll_up(self, lines: int = 3) -> None:
        self.selection_start = self.selection_end = None
        if self._alt_saved is not None:
            return
        top_len = len(self._screen.history.top) if getattr(self._screen, "history", None) else 0
        self._scroll_offset = min(self._scroll_offset + lines, top_len)

    def scroll_down(self, lines: int = 3) -> None:
        self.selection_start = self.selection_end = None
        self._scroll_offset = max(0, self._scroll_offset - lines)

    def scroll_to_bottom(self) -> None:
        self._scroll_offset = 0
        self._trackpad_scroll_accum = 0.0

    def trackpad_scroll(self, delta: float) -> None:
        """
        Smooth trackpad scroll accumulation.
        delta: Y pixel delta. Positive means cursor moved up (pushing page up).
        """
        sensitivity = 20.0
        self._trackpad_scroll_accum += delta
        
        lines = int(self._trackpad_scroll_accum / sensitivity)
        if lines != 0:
            self._trackpad_scroll_accum -= lines * sensitivity
            if lines > 0:
                self.scroll_down(lines)
            else:
                self.scroll_up(-lines)

    @property
    def is_scrolled(self) -> bool:
        return self._scroll_offset > 0

    @property
    def scroll_offset(self) -> int:
        return self._scroll_offset

    def clear(self) -> None:
        """Clear the screen and scrollback."""
        self._scroll_offset = 0
        self.clear_selection()
        self._init_pyte()

    def get_selection_text(self) -> str:
        """Returns the text currently selected by the user."""
        if not self.selection_start or not self.selection_end:
            return ""

        c1, r1 = self.selection_start
        c2, r2 = self.selection_end

        # Normalize so (c1,r1) is before (c2,r2)
        if r1 > r2 or (r1 == r2 and c1 > c2):
            c1, r1, c2, r2 = c2, r2, c1, r1

        lines = self.get_display_lines()
        
        # Clamp rows
        r1 = max(0, min(r1, len(lines) - 1))
        r2 = max(0, min(r2, len(lines) - 1))
        
        selected_text = []
        for r in range(r1, r2 + 1):
            line = lines[r]
            start_col = c1 if r == r1 else 0
            end_col = c2 + 1 if r == r2 else len(line)
            
            # Clamp columns
            start_col = max(0, min(start_col, len(line)))
            end_col = max(0, min(end_col, len(line)))
            
            line_str = "".join(char.char for char in line[start_col:end_col])
            # Only right-strip if it's the end of a line being fully copied
            if r != r2:
                line_str = line_str.rstrip()
            selected_text.append(line_str)

        return "\n".join(selected_text)

    def clear_selection(self) -> None:
        self.selection_start = None
        self.selection_end = None
