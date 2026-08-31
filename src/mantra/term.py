"""Shared terminal primitives: width, size, raw mode."""

from __future__ import annotations

import os
import re
import sys
from contextlib import contextmanager

_ANSI_RE = re.compile(r"\033\[[0-9;?]*[ -/]*[@-~]")


def _char_width(ch: str) -> int:
    """Width of one char; CJK/emoji count as 2."""
    import unicodedata

    eaw = unicodedata.east_asian_width(ch)
    if eaw in ("W", "F"):
        return 2
    return 1


def visible_len(text: str) -> int:
    """Printed width ignoring escapes; wide chars count as 2."""
    return sum(_char_width(c) for c in _ANSI_RE.sub("", text))


def term_size() -> tuple[int, int]:
    """Visible size (cols, rows); Windows-aware."""
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
    """Raw mode for single-key input; no-op on Windows."""
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
    """True when stdin and stdout are terminals."""
    try:
        return bool(sys.stdin.isatty() and sys.stdout.isatty())
    except (ValueError, OSError):  # pragma: no cover - closed streams
        return False
