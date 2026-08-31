"""Terminal primitives shared by every interactive module.

Printable-width measurement and single-key input are needed by the line
editor, the menu, the compact layout and the console. They lived in four
copies before this module existed, which is how a width fix in one of
them silently failed to reach the other three.

The copies elsewhere re-export these rather than redefining them, so
existing importers keep working.
"""

from __future__ import annotations

import os
import re
import sys
from contextlib import contextmanager

_ANSI_RE = re.compile(r"\033\[[0-9;?]*[ -/]*[@-~]")


def _char_width(ch: str) -> int:
    """Display width of one character (CJK/emoji = 2).

    Centralised here so every module agrees on cursor placement; previously
    line_editor counted 2 for wide chars while compact/console counted 1,
    causing drift for paths containing CJK.
    """
    import unicodedata

    eaw = unicodedata.east_asian_width(ch)
    if eaw in ("W", "F"):
        return 2
    return 1


def visible_len(text: str) -> int:
    """Printed width of ``text``, ignoring colour escapes and counting wide chars.

    Prompts are styled, so ``len(prompt)`` counts escape bytes that
    occupy no columns. Using it to place the cursor slides the caret to
    the right of where the text actually ends. Wide characters (CJK,
    emoji) occupy two terminal columns and must be counted as such.
    """
    return sum(_char_width(c) for c in _ANSI_RE.sub("", text))


def term_size() -> tuple[int, int]:
    """Visible terminal size (cols, rows), Windows-aware.

    Windows Terminal can keep a console buffer larger than the visible
    window. Query the active console window rectangle first so
    resize/maximize transitions are reflected immediately. Falls back to
    shutil.get_terminal_size() on other platforms. Single source of truth
    for line_editor and compact so both see the same resize.
    """
    if sys.platform == "win32":
        try:
            import ctypes
            from ctypes import wintypes

            class _Coord(ctypes.Structure):
                _fields_ = [("X", wintypes.SHORT), ("Y", wintypes.SHORT)]

            class _Rect(ctypes.Structure):
                _fields_ = [
                    ("Left", wintypes.SHORT),
                    ("Top", wintypes.SHORT),
                    ("Right", wintypes.SHORT),
                    ("Bottom", wintypes.SHORT),
                ]

            class _ConsoleInfo(ctypes.Structure):
                _fields_ = [
                    ("Size", _Coord),
                    ("Cursor", _Coord),
                    ("Attributes", wintypes.WORD),
                    ("Window", _Rect),
                    ("MaximumWindowSize", _Coord),
                ]

            info = _ConsoleInfo()
            handle = ctypes.windll.kernel32.GetStdHandle(-11)
            if handle not in (0, -1):
                ok = ctypes.windll.kernel32.GetConsoleScreenBufferInfo(
                    handle, ctypes.byref(info)
                )
                if ok:
                    cols = max(1, info.Window.Right - info.Window.Left + 1)
                    rows = max(1, info.Window.Bottom - info.Window.Top + 1)
                    return cols, rows
        except Exception:
            pass
    try:
        import shutil

        s = shutil.get_terminal_size()
        return max(1, s.columns), max(1, s.lines)
    except Exception:
        return 80, 24


def ansi_strip(text: str) -> str:
    return _ANSI_RE.sub("", text)


@contextmanager
def raw_mode():
    """Single-key input for the duration of the block.

    A no-op on non-POSIX hosts, where the platform console API already
    reads one key without a mode change.
    """
    if os.name == "nt":
        yield
        return
    import termios
    import tty

    fd = sys.stdin.fileno()
    original = termios.tcgetattr(fd)
    try:
        tty.setraw(fd)
        yield
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, original)


def is_interactive() -> bool:
    """True when both stdin and stdout are a terminal."""
    try:
        return bool(sys.stdin.isatty() and sys.stdout.isatty())
    except (ValueError, OSError):  # pragma: no cover - closed streams
        return False
