"""Terminal user interface: a persistent frame with live session state.

The interface is three fixed regions and one scrolling one::

    row 1      identity      name · version            endpoint · model
    row 2      context       workspace · git           skills · goal
    rows 3..   conversation  scrolls; the only region that moves
    row R-1    prompt        > prefix + status (right-aligned)

The previous design had no header at all and a status line carrying only
the model and workspace. Everything a session is actually *doing* - which
approval mode is in force, how full the context is, what the goal is -
was either invisible or buried behind a slash command. Now it is on
screen permanently, because those are the things that change how the next
message should be read.

The frame is drawn with absolute cursor addressing and a terminal scroll
region, so the conversation scrolls inside it while the chrome stays put.
When the terminal is too small for that to be worth doing, the whole
frame is abandoned and output falls back to plain sequential lines.
"""

from __future__ import annotations

import logging
import re
import shutil
import sys
import threading
import unicodedata
from typing import Any

from mantra.term import visible_len

_log = logging.getLogger("mantra.tui")

_lock = threading.RLock()

# Below these the frame costs more room than it buys, so it is dropped
# and the console behaves like an ordinary line-oriented program.
MIN_COLS = 60
MIN_ROWS = 12
MIN_CONTENT_ROWS = 4

# Region offsets, counted from the top and the bottom respectively.
_HEADER_ROWS = 2
_CONTENT_TOP = 3

# Counted back from the last row: prompt, border, content.
_ROWS_FROM_BOTTOM = 2

_SEPARATOR = "─"

_MOUSE_ON = "\033[?1000h\033[?1006h"
_MOUSE_OFF = "\033[?1000l\033[?1006l"

# Approval mode -> (marker, style method). The mode is the one setting
# that silently changes what the agent may do, so it is the one thing on
# the status line that gets colour rather than dim text.
_MODE_STYLES = {
    "default": ("●", "green"),
    "auto": ("●", "yellow"),
    "yolo": ("▲", "red"),
    "plan": ("○", "cyan"),
}

_WELCOME_WIDTH = 72
_WELCOME_HEIGHT = 9
_welcome_rows: tuple[int, int] | None = None


def _display(value: object, fallback: str = "") -> str:
    """Render data-derived text as safe, single-line terminal text."""
    text = str(value if value is not None else fallback)
    text = text.replace("\n", " ").replace("\r", " ").replace("\x1b", "")
    return re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]", "", text)


def terminal_size() -> tuple[int, int]:
    """Columns and rows, with a floor for terminals that report nothing."""
    try:
        size = shutil.get_terminal_size()
        return max(1, size.columns), max(1, size.lines)
    except Exception:
        return 80, 24


def fits(cols: int, rows: int) -> bool:
    """Whether the terminal is big enough to be worth framing."""
    return cols >= MIN_COLS and rows >= MIN_ROWS


def _write(text: str) -> None:
    out = sys.stdout
    try:
        out.write(text)
    except UnicodeEncodeError:
        enc = getattr(out, "encoding", None) or "utf-8"
        out.write(text.encode(enc, errors="replace").decode(enc, errors="replace"))


def _style_fn(style: Any, name: str):
    fn = getattr(style, name, None)
    if callable(fn):
        return fn
    return lambda text: text


def _is_grapheme_continuation(ch: str) -> bool:
    """True if ch should not be counted as a new column on its own."""
    # Zero-width joiner, variation selectors, combining marks
    if ch in ("\u200d", "\ufe0e", "\ufe0f"):
        return True
    cat = unicodedata.category(ch)
    if cat in ("Mn", "Me", "Mc"):
        return True
    return False


def _shorten(text: str, width: int) -> str:
    if width <= 0:
        return ""
    if visible_len(text) <= width:
        return text
    if width == 1:
        return "…"
    # ANSI-aware truncation: walk the string counting visible columns,
    # copying escape sequences verbatim and cutting plain text at width-1.
    # Grapheme continuations (ZWJ, VS, combining) are kept with the base.
    ansi_re = re.compile(r"\033\[[0-9;?]*[ -/]*[@-~]")
    result = []
    vis = 0
    limit = width - 1
    i = 0
    while i < len(text):
        m = ansi_re.match(text, i)
        if m:
            result.append(m.group(0))
            i = m.end()
            continue
        ch = text[i]
        # Keep grapheme continuations together with base
        if _is_grapheme_continuation(ch) and result:
            result.append(ch)
            i += 1
            continue
        if vis + visible_len(ch) > limit:
            break
        result.append(ch)
        vis += visible_len(ch)
        i += 1
    result.append("…")
    return "".join(result)


def _fit(text: str, width: int) -> str:
    """Centre ``text`` in exactly ``width`` visible columns.

    Padding has to be applied as a single left/right split after
    measuring; centring first and appending the remainder afterwards
    put the box's side borders out of line with its corners.
    """
    body = _shorten(text, width)
    spare = max(0, width - visible_len(body))
    left = spare // 2
    return " " * left + body + " " * (spare - left)


def _rule(cols: int) -> str:
    return _SEPARATOR * max(0, cols)


def _short_count(value: int) -> str:
    """1234 -> 1.2k. Status lines only ever need two figures."""
    if value < 1000:
        return str(value)
    if value < 10_000:
        return f"{value / 1000:.1f}k"
    return f"{round(value / 1000)}k"


def _version() -> str:
    try:
        from mantra import __version__  # type: ignore

        return __version__
    except Exception:
        return "0.1.0"


# --------------------------------------------------------------------- parts


def _identity(session: Any, style: Any) -> tuple[str, str]:
    """Left and right of the top header row."""
    bright_cyan = _style_fn(style, "bright_cyan")
    dim = _style_fn(style, "dim")
    bright_white = _style_fn(style, "bright_white")
    left = f"{bright_cyan('MANTRA')} {dim(_version())}"
    cfg = getattr(session, "config", None)
    if not isinstance(cfg, dict):
        cfg = {}
    llm = cfg.get("llm")
    if not isinstance(llm, dict):
        llm = {}
    model = _display(llm.get("model"), "?")
    effort = _display(llm.get("reasoning_effort"), "off")
    endpoint = ""
    try:
        endpoint = _display(session.endpoint_name or "")
    except Exception:
        endpoint = ""
    right_bits = [b for b in (endpoint, f"{bright_white(model)} {dim('(' + effort + ')')}") if b]
    return left, " · ".join(right_bits)


def _context(session: Any, style: Any) -> tuple[str, str]:
    """Left and right of the second header row."""
    workspace = getattr(getattr(session, "sandbox", None), "root", "") or ""
    if workspace:
        short = _display(workspace.replace("\\", "/").rstrip("/").split("/")[-1] or workspace)
    else:
        short = ""
    git = _display(getattr(session, "git_state", "") or "")
    dim = _style_fn(style, "dim")
    bright_green = _style_fn(style, "bright_green")
    left = f"{bright_green(short)}" + (f" {dim(git)}" if git else "")

    right_bits = []
    skills = getattr(session, "active_skills", None)
    if isinstance(skills, (list, tuple, set)):
        if skills:
            right_bits.append(dim(f"{len(skills)} skill" + ("" if len(skills) == 1 else "s")))
    goal = _display(getattr(session, "goal", "") or "")
    if goal:
        right_bits.append(dim("goal: " + goal))
    return left, "  ".join(right_bits)


def render_header(session: Any, style: Any, cols: int) -> list[str]:
    """The two identity rows, trimmed to fit the terminal."""
    rows = []
    for left, right in (_identity(session, style), _context(session, style)):
        left = _shorten(left, max(0, cols // 2))
        # Right gets whatever is left, but never negative
        right_width = max(0, cols - visible_len(left) - 1)
        right = _shorten(right, right_width)
        gap = cols - visible_len(left) - visible_len(right)
        if gap < 0:
            # Overfull (e.g., wide chars) — trim right further
            right = _shorten(right, max(0, right_width + gap))
            gap = cols - visible_len(left) - visible_len(right)
        gap = max(0, gap)
        # Ensure at least one space when both sides present, without exceeding cols
        if left and right and gap == 0 and cols > 0:
            right = _shorten(right, max(0, visible_len(right) - 1))
            gap = cols - visible_len(left) - visible_len(right)
            gap = max(0, gap)
        if left and right and gap == 0:
            gap = 1
            # If still over, last resort: allow one column overflow is better than wrapping
            # but prefer trimming left
            if visible_len(left) + gap + visible_len(right) > cols:
                left = _shorten(left, max(0, visible_len(left) - 1))
        rows.append(left + " " * gap + right)
    return rows


def render_status(session: Any, style: Any, cols: int) -> str:
    """One line of live session state."""
    dim = _style_fn(style, "dim")
    bright_white = _style_fn(style, "bright_white")
    bright_cyan = _style_fn(style, "bright_cyan")
    
    # Get model name from session config
    model_name = ""
    cfg = getattr(session, "config", None)
    if isinstance(cfg, dict):
        llm = cfg.get("llm")
        if isinstance(llm, dict):
            model_name = _display(llm.get("model"), "")
    
    # Get reasoning effort
    effort = ""
    if isinstance(cfg, dict):
        llm = cfg.get("llm")
        if isinstance(llm, dict):
            effort = _display(llm.get("reasoning_effort"), "")
    
    # Build model display like Grok: "model-name (effort)"
    model_display = ""
    if model_name:
        if effort:
            model_display = f"{bright_cyan(model_name)} {dim('(' + effort + ')')}"
        else:
            model_display = bright_cyan(model_name)
    
    # Get approval mode
    mode = getattr(getattr(session, "approvals", None), "mode", "default")
    if not isinstance(mode, str) or mode not in _MODE_STYLES:
        if isinstance(mode, str) and mode not in _MODE_STYLES:
            _log.debug("unknown approval mode %r, falling back to default", mode)
        mode = "default"
    marker, colour_name = _MODE_STYLES.get(mode, ("●", "green"))
    colour = _style_fn(style, colour_name)
    
    parts = []
    if model_display:
        parts.append(model_display)
    parts.append(f"{colour(marker)} {_display(mode, 'default')}")

    totals = getattr(session, "totals", None)
    if not isinstance(totals, dict):
        totals = {}
    try:
        turns = int(totals.get("turns", 0) or 0)
    except (TypeError, ValueError) as exc:
        _log.debug("totals.turns not numeric: %r", exc)
        turns = 0
    if turns:
        parts.append(dim(f"{turns} turn" + ("" if turns == 1 else "s")))

    try:
        tokens_in = int(totals.get("tokens_in", 0) or 0)
        cached = int(totals.get("cache_hit", 0) or 0)
    except (TypeError, ValueError) as exc:
        _log.debug("totals token counts not numeric: %r", exc)
        tokens_in = cached = 0
    try:
        ctx_val = getattr(getattr(session, "context", None), "tokens", 0)
        context = int(ctx_val() if callable(ctx_val) else ctx_val or 0)
    except Exception as exc:
        _log.debug("context.tokens conversion failed: %r", exc)
        context = 0
    if context:
        parts.append(dim(f"{_short_count(int(context))} ctx"))
    if tokens_in and cached:
        try:
            rate = max(0, min(100, int(cached * 100 / tokens_in)))
        except Exception as exc:
            _log.debug("cache rate calc failed: %r", exc)
            rate = 0
        parts.append(dim(f"{rate}% cached"))

    # Tool call count, time, and errors from current turn
    tool_count = getattr(session, "_tool_call_count", 0)
    tool_total = getattr(session, "_tool_total_time", 0.0)
    tool_ok = getattr(session, "_tool_success_count", 0)
    tool_fail = getattr(session, "_tool_fail_count", 0)
    if tool_count:
        # Format total time prominently
        if tool_total < 0.001:
            time_str = "0ms"
        elif tool_total < 1:
            time_str = f"{int(tool_total * 1000)}ms"
        elif tool_total < 60:
            time_str = f"{tool_total:.1f}s"
        else:
            minutes = int(tool_total) // 60
            seconds = tool_total % 60
            time_str = f"{minutes}m{seconds:02d}s"
        # Build tool status: count · time · errors
        tool_str = f"{tool_count} tool"
        time_part = _style_fn(style, "bright_white")(time_str)
        # Color based on errors
        if tool_fail == 0:
            rate_str = f"{tool_str} {time_part}"
            rate_str = _style_fn(style, "green")(rate_str)
        else:
            err_str = _style_fn(style, "red")(f"{tool_fail} err")
            rate_str = f"{tool_str} {time_part} {err_str}"
            rate_str = _style_fn(style, "yellow")(tool_str) + " " + time_part + " " + err_str
        parts.append(rate_str)

    # Token usage breakdown with cost
    tokens_in = getattr(session, "_tokens_input", 0)
    tokens_out = getattr(session, "_tokens_output", 0)
    tokens_cached = getattr(session, "_tokens_cached", 0)
    tokens_cost = getattr(session, "_tokens_cost", 0.0)
    if tokens_in or tokens_out:
        # Format: in/out with cache percentage
        cache_pct = int(tokens_cached * 100 / tokens_in) if tokens_in > 0 else 0
        token_str = f"{_short_count(tokens_in)}↑ {_short_count(tokens_out)}↓"
        if cache_pct > 0:
            token_str += f" {cache_pct}%♻"
        # Add cost estimate
        if tokens_cost > 0:
            if tokens_cost < 0.01:
                cost_str = f"${tokens_cost * 100:.1f}¢"
            else:
                cost_str = f"${tokens_cost:.2f}"
            token_str += f" {cost_str}"
        parts.append(_style_fn(style, "cyan")(token_str))
    else:
        # Fallback: show streaming tokens
        try:
            streamed = int(getattr(session, "_stream_tokens", 0) or 0)
        except (TypeError, ValueError) as exc:
            _log.debug("stream_tokens not numeric: %r", exc)
            streamed = 0
        if streamed:
            parts.append(_style_fn(style, "bright_yellow")(f"~{_short_count(streamed)} tok"))

    return _shorten("  ".join(parts), cols)


def render_card(session: Any = None, style: Any = None, width: int | None = None) -> list[str]:
    """The startup card: clean text."""
    bold = _style_fn(style, "bold")
    bright_cyan = _style_fn(style, "bright_cyan")
    dim = _style_fn(style, "dim")
    
    lines = [
        "",
        f"  {bold(bright_cyan('M A N T R A'))}",
        f"  {dim('Spells Matter')}",
        f"  {dim(_version())}",
        "",
    ]
    return lines


# -------------------------------------------------------------------- layout


class Layout:
    """The fixed frame: reserves rows, sets the scroll region, redraws."""

    def __init__(self) -> None:
        self.active = False
        self.content_top = 1
        self.content_bottom = 1
        self.border_row = 1
        self.prompt_row = 1
        self._cols = 0
        self._rows = 0
        self._session: Any = None
        self._style: Any = None
        self._splash_visible = False
        self._mouse_on = False

    # ---- lifecycle ---------------------------------------------------

    def setup(self, session: Any = None, style: Any = None) -> bool:
        """Reserve the frame. True when the terminal was big enough."""
        with _lock:
            self._session = session
            self._style = style
            cols, rows = terminal_size()
            # Enter alternate screen buffer and clear it
            _write("\033[?1049h")
            _write("\033[2J\033[H")
            sys.stdout.flush()
            self._resize_locked(cols, rows)
            if not self.active:
                return False
            self.draw_chrome_locked()
            return True

    def cleanup(self) -> None:
        """Release the scroll region and mouse capture, whatever happened."""
        with _lock:
            try:
                _write("\033[r")
            except Exception as exc:
                _log.debug("cleanup reset region failed: %r", exc)
            self._disable_mouse_locked()
            try:
                _write("\033[0 q")  # Reset cursor to default
                if self._rows > 0:
                    _write(f"\033[{self._rows};1H\n")
                # Leave alternate screen buffer
                _write("\033[?1049l")
                sys.stdout.flush()
            except Exception as exc:
                _log.debug("cleanup flush failed: %r", exc)
            self.active = False
            self._splash_visible = False

    # ---- geometry ----------------------------------------------------

    def _resize(self, cols: int, rows: int) -> None:
        self._resize_locked(cols, rows)

    def _resize_locked(self, cols: int, rows: int) -> None:
        self._cols, self._rows = cols, rows
        self.content_top = _CONTENT_TOP
        self.prompt_row = max(1, rows - 1)
        self.border_row = max(1, rows - 2)
        self.content_bottom = max(_CONTENT_TOP, rows - _ROWS_FROM_BOTTOM - 1)
        content_rows = self.content_bottom - self.content_top + 1
        self.active = fits(cols, rows) and content_rows >= MIN_CONTENT_ROWS
        if self.active:
            self._apply_region_locked()

    def _apply_region(self) -> None:
        with _lock:
            self._apply_region_locked()

    def _apply_region_locked(self) -> None:
        try:
            _write(f"\033[{self.content_top};{self.content_bottom}r")
            # Move cursor inside scroll region, NOT to the prompt row
            _write(f"\033[{self.content_bottom};1H")
            sys.stdout.flush()
        except Exception as exc:
            _log.debug("apply region failed: %r", exc)

    def check_resize(self) -> bool:
        """Redraw after a terminal size change. True when it changed."""
        cols, rows = terminal_size()
        if cols == self._cols and rows == self._rows:
            return False
        with _lock:
            # Save OLD positions BEFORE recalculating
            old_border = self.border_row
            old_prompt = self.prompt_row
            was_active = self.active
            try:
                _write("\033[r")
                sys.stdout.flush()
            except Exception as exc:
                _log.debug("check_resize reset region failed: %r", exc)
            self._resize_locked(cols, rows)
            if not self.active:
                if was_active:
                    self._disable_mouse_locked()
                    self._clear_locked()
                    self._splash_visible = False
                return True
            # Clear old prompt row and new bottom row to prevent duplication
            try:
                _write(f"\033[{old_prompt};1H\033[2K")
                _write(f"\033[{rows};1H\033[2K")
                sys.stdout.flush()
            except Exception as exc:
                _log.debug("clear prompt rows failed: %r", exc)
            # Full clear — wipe entire screen so no ghosts remain
            self._clear_locked()
            # Restore scroll region (clear wiped it)
            if self.active:
                self._apply_region_locked()
            self.draw_chrome_locked()
            return True

    def _clear(self) -> None:
        with _lock:
            self._clear_locked()

    def _clear_locked(self) -> None:
        try:
            _write("\033[2J\033[H")
            sys.stdout.flush()
        except Exception as exc:
            _log.debug("clear failed: %r", exc)

    # ---- chrome ------------------------------------------------------

    def draw_chrome(self) -> None:
        """Header, rules and status - everything except the content."""
        with _lock:
            self.draw_chrome_locked()

    def draw_chrome_locked(self) -> None:
        """Header, border and status - caller must hold _lock."""
        if not self.active or self._session is None:
            return
        style = self._style
        cols = self._cols
        dim = _style_fn(style, "dim")
        try:
            _write("\033[r")  # address the whole screen first
            for index, line in enumerate(render_header(self._session, style, cols)):
                _write(f"\033[{index + 1};1H\033[2K{line}")
            # Draw subtle border line above prompt
            bright_cyan = _style_fn(style, "bright_cyan")
            _write(f"\033[{self.border_row};1H\033[2K{bright_cyan(_rule(cols))}")
            sys.stdout.flush()
        except Exception as exc:
            _log.debug("draw_chrome failed: %r", exc)
        finally:
            self._apply_region_locked()

    def draw_border_status(self, text: str = "") -> None:
        """Show tool call status in the border line."""
        if not self.active or self._session is None:
            return
        with _lock:
            style = self._style
            cols = self._cols
            dim = _style_fn(style, "dim")
            bright_cyan = _style_fn(style, "bright_cyan")
            try:
                _write("\033[r")  # address whole screen
                if text:
                    # Show tool status in the border line
                    truncated = _shorten(text, max(0, cols - 4))
                    border = f" {bright_cyan(truncated)} "
                    # Pad to fill the line
                    pad = max(0, cols - visible_len(border))
                    left_pad = pad // 2
                    right_pad = pad - left_pad
                    _write(f"\033[{self.border_row};1H\033[2K{dim('─' * left_pad)}{border}{dim('─' * right_pad)}")
                else:
                    # Restore normal border
                    _write(f"\033[{self.border_row};1H\033[2K{bright_cyan(_rule(cols))}")
                sys.stdout.flush()
            except Exception as exc:
                _log.debug("draw_border_status failed: %r", exc)
            finally:
                self._apply_region_locked()

    # ---- splash ------------------------------------------------------

    def show_splash(self) -> int:
        """Draw the startup card inside the content area."""
        if not self.active:
            return 0
        card = render_card(self._session, self._style, width=self._cols - 4 if self._cols else None)
        with _lock:
            try:
                _write("\033[r")
                # Center vertically in content area
                available_rows = self.content_bottom - self.content_top + 1
                vertical_pad = max(0, (available_rows - len(card)) // 2)
                for offset, line in enumerate(card):
                    row = self.content_top + vertical_pad + offset
                    if row > self.content_bottom:
                        break
                    pad = max(0, (self._cols - visible_len(line)) // 2)
                    _write(f"\033[{row};1H\033[2K{' ' * pad}{line}")
                _write(f"\033[{min(self.content_bottom, self.content_top + vertical_pad + len(card) + 1)};1H")
                sys.stdout.flush()
            except Exception as exc:
                _log.debug("show_splash failed: %r", exc)
            finally:
                self._apply_region_locked()
        self._splash_visible = True
        return len(card)

    def hide_splash(self) -> None:
        with _lock:
            if not self._splash_visible:
                return
            self._splash_visible = False
            if not self.active:
                return
            try:
                for row in range(self.content_top, self.content_bottom + 1):
                    _write(f"\033[{row};1H\033[2K")
                _write(f"\033[{self.content_top};1H")
                sys.stdout.flush()
            except Exception as exc:
                _log.debug("hide_splash failed: %r", exc)

    # ---- cursor ------------------------------------------------------

    def move_to_content(self) -> None:
        if self.active:
            with _lock:
                # Move to bottom of content area - scroll region scrolls upward when full
                _write(f"\033[{self.content_bottom};1H")
                sys.stdout.flush()

    def move_to_header(self) -> None:
        if self.active:
            with _lock:
                _write("\033[1;1H")
                sys.stdout.flush()

    # Kept under the old name so existing callers read either way.
    move_to_dashboard = move_to_header

    # ---- mouse -------------------------------------------------------
    # Mouse reporting is intentionally NOT enabled by the frame itself.
    # Menus enable it only while a selectable list is visible
    # (see core/menu.py), so native selection and wheel scrolling work
    # in the conversation area. These helpers remain for explicit
    # opt-in callers.

    def _enable_mouse(self) -> None:
        with _lock:
            self._enable_mouse_locked()

    def _enable_mouse_locked(self) -> None:
        if self._mouse_on:
            return
        try:
            _write(_MOUSE_ON)
            sys.stdout.flush()
            self._mouse_on = True
        except Exception as exc:
            _log.debug("enable mouse failed: %r", exc)

    def _disable_mouse(self) -> None:
        with _lock:
            self._disable_mouse_locked()

    def _disable_mouse_locked(self) -> None:
        if not self._mouse_on:
            return
        try:
            _write(_MOUSE_OFF)
            sys.stdout.flush()
        except Exception as exc:
            _log.debug("disable mouse failed: %r", exc)
        self._mouse_on = False


# ------------------------------------------------------------------ helpers


def show_splash(session: Any, style: Any) -> int:
    """Draw the startup card. Returns how many rows it took."""
    layout = getattr(session, "layout", None)
    if layout is not None and layout.active:
        return layout.show_splash()
    return 0


def render_welcome(session: Any, style: Any, cols: int, rows: int) -> list[str]:
    """Create the MANTRA welcome panel used by native scrollback mode."""
    dim = _style_fn(style, "dim")
    bright_cyan = _style_fn(style, "bright_cyan")
    bold = _style_fn(style, "bold")
    
    lines = [
        "",
        f"  {bold(bright_cyan('M A N T R A'))}",
        f"  {dim('Spells Matter')}",
        f"  {dim(_version())}",
        "",
    ]
    return lines


def show_welcome(session: Any, style: Any) -> None:
    """Draw a centered welcome panel without taking over scrollback."""
    if not sys.stdout.isatty():
        return
    cols, rows = terminal_size()
    lines = render_welcome(session, style, cols, rows)
    top = max(2, (rows - len(lines)) // 3)
    with _lock:
        try:
            _write("\033[s")
            for offset, line in enumerate(lines):
                row = top + offset
                _write(f"\033[{row};1H\033[2K{line}")
            _write(f"\033[{top + len(lines) + 1};1H")
            _write("\033[u")
            sys.stdout.flush()
        except Exception as exc:
            _log.debug("welcome draw failed: %r", exc)
