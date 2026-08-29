"""Compact TUI — minimal startup card + fixed bottom bar for MANTRA.

Startup card (centered, disappears on first turn):
  ┌──────────────────────────────┐
  │           MANTRA             │
  │       Spells Matter          │
  │           0.1.0              │
  └──────────────────────────────┘

Bottom:
  left  | MANTRA >  (gold)
  right model (reasoning) · workspace
"""

from __future__ import annotations

import re
import shutil
import sys
from typing import Any

_ANSI_RE = re.compile(r"\033\[[0-9;?]*[ -/]*[@-~]")

def _ansi(code: str, text: str, enabled: bool = True) -> str:
    return f"\033[{code}m{text}\033[0m" if enabled else text

def _white(t, e=True): return _ansi("97", t, e)
def _grey(t, e=True): return _ansi("37", t, e)
def _dim(t, e=True): return _ansi("90", t, e)
def _bold(t, e=True): return _ansi("1", t, e)
def _gold(t, e=True): return _ansi("93", t, e)

def _vis(text: str) -> int:
    return len(_ANSI_RE.sub("", text))

def _pad(text: str, width: int) -> str:
    diff = width - _vis(text)
    return text + (" " * max(0, diff))

COMPACT_WIDTH = 160
def _term_size() -> tuple[int, int]:
    try:
        s = shutil.get_terminal_size()
        return s.columns, s.lines
    except Exception:
        return 80, 24

def _version(session: Any) -> str:
    try:
        from mantra import __version__  # type: ignore
        return __version__
    except Exception:
        return "0.1.0"

def _shorten(text: str, width: int) -> str:
    if _vis(text) <= width:
        return text
    raw = _ANSI_RE.sub("", text)
    return raw[: max(0, width - 1)] + "…"

def render_card(session: Any, width: int | None = None, enabled: bool = True) -> list[str]:
    """No container — just centered text, truly compact, fits any window."""
    cols, _ = _term_size()
    if width is None:
        width = max(30, cols - 8)
        if width > 80:
            width = 80
    ver = _version(session)
    inner = width
    lines = []
    for raw in [
        _bold(_white("M A N T R A", enabled), enabled),
        _dim("Spells Matter", enabled),
        _dim(ver, enabled),
    ]:
        vis = _vis(raw)
        pad = max(0, (inner - vis) // 2)
        content = " " * pad + raw
        lines.append(content)
    out: list[str] = []
    for r in lines:
        if _vis(r) > inner:
            r = _shorten(r, inner)
        out.append(_pad(r, inner))
    return out

def _safe_write(text: str) -> None:
    out = sys.stdout
    try:
        out.write(text)
    except UnicodeEncodeError:
        enc = getattr(out, "encoding", None) or "utf-8"
        out.write(text.encode(enc, errors="replace").decode(enc, errors="replace"))

class SplashBox:
    def __init__(self, width: int, left: int, top: int, lines: list[str]):
        self.width = width
        self.left = left
        self.top = top
        self.lines = lines
        self.height = len(lines)
    def draw(self) -> None:
        pad = " " * self.left
        for i, line in enumerate(self.lines):
            row = self.top + i
            _safe_write(f"\033[{row};1H\033[2K{pad}{line}")
    def clear(self) -> None:
        for i in range(self.top, self.top + self.height + 1):
            _safe_write(f"\033[{i};1H\033[2K")

_splash_box: SplashBox | None = None

def show_splash(session: Any, style: Any) -> int:
    if not sys.stdout.isatty():
        return 0
    global _splash_box
    enabled = getattr(style, "enabled", True)
    cols, rows = _term_size()
    width = max(30, cols - 8)
    if width > COMPACT_WIDTH:
        width = COMPACT_WIDTH
    if width % 2 == 1:
        width -= 1
    try:
        sys._mantra_card_width = width  # type: ignore
    except Exception:
        pass
    card = render_card(session, width, enabled=enabled)
    card_h = len(card)
    top_pad = max(1, (rows - card_h - 6) // 3)
    left_pad = max(0, (cols - width) // 2)
    top = top_pad + 1
    if _splash_box is not None:
        try:
            _splash_box.clear()
        except Exception:
            pass
    _splash_box = SplashBox(width, left_pad, top, card)
    _splash_box.draw()
    try:
        sys.stdout.write(f"\033[{top + card_h + 1};1H")
        sys.stdout.flush()
    except Exception:
        pass
    return top_pad + card_h

def hide_splash(session: Any, splash_rows: int) -> None:
    global _splash_box
    if not sys.stdout.isatty():
        return
    try:
        if _splash_box is not None:
            _splash_box.clear()
            _splash_box = None
        else:
            sys.stdout.write("\033[H")
            for i in range(1, splash_rows + 2):
                sys.stdout.write(f"\033[{i};1H\033[2K")
            sys.stdout.write("\033[H")
        sys.stdout.flush()
    except Exception:
        pass

def bottom_status(session: Any, enabled: bool = True) -> str:
    llm = session.config.get("llm", {}) if hasattr(session, "config") else {}
    model = llm.get("model", "?")
    reasoning = llm.get("reasoning_effort") or "off"
    ws = getattr(getattr(session, "sandbox", None), "root", "")
    if ws:
        ws_short = ws.replace("\\", "/").rstrip("/").split("/")[-1]
    else:
        ws_short = ""
    if ws_short:
        core = f"{model} ({reasoning}) · {ws_short}"
    else:
        core = f"{model} ({reasoning})"
    return _dim(core, enabled)

def draw_status(session: Any, style: Any) -> None:
    if not sys.stdout.isatty():
        return
    enabled = getattr(style, "enabled", True)
    cols, rows = _term_size()
    line = bottom_status(session, enabled)
    v = _vis(line)
    c = max(1, cols - v + 1)
    try:
        sys.stdout.write("\033[s")
        _safe_write(f"\033[{rows};{c}H\033[2K{line}")
        sep = _grey("─" * cols, enabled)
        _safe_write(f"\033[{rows - 3};1H\033[2K{sep}")
        sys.stdout.write("\033[u")
        sys.stdout.flush()
    except Exception:
        pass

class CompactLayout:
    def __init__(self) -> None:
        self.active = False
        self.dashboard_rows = 0
        self.prompt_row = 0
        self.content_top = 0
        self.content_bottom = 0
        self._cols = 0
        self._rows = 0
        self._splash_rows = 0
        self._splash_visible = True
    def setup(self, splash_rows: int, session: Any = None, style: Any = None) -> None:
        cols, rows = _term_size()
        self._cols = cols
        self._rows = rows
        self._splash_rows = splash_rows
        self._splash_visible = True
        self._session = session
        self._style = style
        self.dashboard_rows = splash_rows
        self.content_top = splash_rows + 2
        self.prompt_row = max(1, rows - 2)
        self.content_bottom = max(self.content_top, rows - 4)
        try:
            sys.stdout.write("\033[?1000h\033[?1006h")
            sys.stdout.flush()
        except Exception:
            pass
        if rows >= 8 and cols >= 30 and self.content_top <= self.content_bottom:
            self.active = True
            try:
                sys.stdout.write(f"\033[{self.content_top};{self.content_bottom}r")
                sys.stdout.write(f"\033[{self.content_top};1H")
                sys.stdout.flush()
            except Exception:
                pass
        else:
            self.active = False
    def hide_splash(self) -> None:
        if not self._splash_visible:
            return
        self._splash_visible = False
        hide_splash(None, self._splash_rows)
        self.dashboard_rows = 0
        self.content_top = 1
        if self.active:
            try:
                sys.stdout.write("\033[r")
                sys.stdout.write(f"\033[{self.content_top};{self.content_bottom}r")
                sys.stdout.write(f"\033[{self.content_top};1H")
                sys.stdout.flush()
            except Exception:
                pass
    def move_to_content(self) -> None:
        if self.active:
            sys.stdout.write(f"\033[{self.content_bottom};1H")
            sys.stdout.flush()
    def move_to_prompt(self) -> None:
        if self.active:
            try:
                cols, rows = _term_size()
                self.prompt_row = max(1, rows - 2)
                st = getattr(self, "_style", None)
                prompt = st.bright_yellow("| MANTRA > ") if st and getattr(st, "enabled", True) else "| MANTRA > "
                # Ensure status and separator are fresh
                draw_status(self._session, self._style)
                _safe_write(f"\033[{self.prompt_row};1H\033[2K{prompt}")
                sys.stdout.flush()
            except Exception:
                pass
    def move_to_dashboard(self) -> None:
        if self.active:
            sys.stdout.write("\033[1;1H")
            sys.stdout.flush()
    def cleanup(self) -> None:
        if self.active:
            try:
                sys.stdout.write("\033[r")
                sys.stdout.write("\033[?1000l\033[?1006l")
                sys.stdout.write(f"\033[{self._rows};1H\n")
                sys.stdout.flush()
            except Exception:
                pass
            self.active = False
    def check_resize(self) -> bool:
        cols, rows = _term_size()
        if cols == self._cols and rows == self._rows:
            return False
        if self.active:
            try:
                sys.stdout.write("\033[r")
                sys.stdout.flush()
            except Exception:
                pass
        if self._splash_visible and getattr(self, "_session", None) is not None:
            try:
                if _splash_box is not None:
                    _splash_box.clear()
                new_rows = show_splash(self._session, self._style)
                self._splash_rows = new_rows
                self.dashboard_rows = new_rows
            except Exception:
                pass
        self._cols, self._rows = cols, rows
        self.prompt_row = max(1, rows - 2)
        self.content_top = self._splash_rows + 2 if self._splash_visible else 1
        self.content_bottom = max(self.content_top, rows - 4)
        if rows >= 8 and cols >= 30 and self.content_top <= self.content_bottom:
            self.active = True
            try:
                sys.stdout.write(f"\033[{self.content_top};{self.content_bottom}r")
                sys.stdout.write(f"\033[{self.content_top};1H")
                sys.stdout.flush()
            except Exception:
                pass
        else:
            self.active = False
        try:
            if getattr(self, "_session", None) is not None:
                draw_status(self._session, self._style)
                st = getattr(self, "_style", None)
                prompt = st.bright_yellow("| MANTRA > ") if st and getattr(st, "enabled", True) else "| MANTRA > "
                sys.stdout.write(f"\033[{self.prompt_row};1H\033[2K{prompt}")
                sys.stdout.flush()
        except Exception:
            pass
        return True
