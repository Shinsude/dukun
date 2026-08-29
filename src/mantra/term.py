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


def visible_len(text: str) -> int:
    """Printed width of ``text``, ignoring colour escapes.

    Prompts are styled, so ``len(prompt)`` counts escape bytes that
    occupy no columns. Using it to place the cursor slides the caret to
    the right of where the text actually ends.
    """
    return len(_ANSI_RE.sub("", text))


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
