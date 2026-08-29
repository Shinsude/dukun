"""Single-key line editor with an inline completion popup.

The console needs to react *while* you type - show matching files after an
``@`` and matching commands after a leading ``/``. ``input()`` cannot do
that: it only yields the finished line. This reads one key at a time.

Standard library only. Windows uses ``msvcrt.getwch()``; POSIX puts the
terminal in raw mode. When stdin is not a terminal the editor steps aside
and ``input()`` is used, so pipes, redirects and tests keep working.
"""

from __future__ import annotations

import os
import re
import sys
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any, Callable

KEY_UP = "key:up"
KEY_DOWN = "key:down"
KEY_LEFT = "key:left"
KEY_RIGHT = "key:right"
KEY_HOME = "key:home"
KEY_END = "key:end"
KEY_DELETE = "key:delete"
KEY_CTRL_G = "key:ctrl-g"
KEY_PAGE_UP = "key:page-up"
KEY_PAGE_DOWN = "key:page-down"

_WINDOWS_SPECIALS = {
    "H": KEY_UP,
    "P": KEY_DOWN,
    "K": KEY_LEFT,
    "M": KEY_RIGHT,
    "G": KEY_HOME,
    "O": KEY_END,
    "S": KEY_DELETE,
    "I": KEY_PAGE_UP,
    "Q": KEY_PAGE_DOWN,
}

_POSIX_SPECIALS = {
    "[A": KEY_UP,
    "[B": KEY_DOWN,
    "[C": KEY_RIGHT,
    "[D": KEY_LEFT,
    "[H": KEY_HOME,
    "[F": KEY_END,
    "[5": KEY_PAGE_UP,
    "[6": KEY_PAGE_DOWN,
}

HINT = "tab/enter completes · up/down selects · esc dismisses"

_ANSI_RE = re.compile(r"\033\[[0-9;?]*[ -/]*[@-~]")


def visible_len(text: str) -> int:
    """Width of ``text`` as printed, ignoring ANSI colour escapes.

    The console prompt is styled, so ``len(prompt)`` counts escape bytes
    that occupy no columns. Using it to place the cursor slides the caret
    to the right of where the text actually ends.
    """
    return len(_ANSI_RE.sub("", text))


@dataclass
class Completion:
    """What to show and what to insert for the token being typed."""

    items: list[str]  # text inserted on accept, e.g. "@src/app.py" or "/compact"
    start: int  # buffer index where the token begins
    end: int  # buffer index where the token ends (usually the cursor)
    labels: list[str] = field(default_factory=list)  # display text, defaults to items

    def label(self, index: int) -> str:
        return self.labels[index] if index < len(self.labels) else self.items[index]


class LineEditor:
    """Reads one line with a suggestion popup below it."""

    def __init__(
        self,
        style: Any,
        completer: Any = None,
        max_popup: int = 8,
        hint: str = HINT,
        on_ctrl_g: Callable[[], None] | None = None,
        on_submit: Callable[[int], None] | None = None,
        no_popup: bool = False,
        popup_above: bool = False,
        on_page_up: Callable[[], None] | None = None,
        on_page_down: Callable[[], None] | None = None,
    ) -> None:
        self.style = style
        self.completer = completer
        self.max_popup = max_popup
        self.hint = hint
        # Called with the width the finished line occupies, before the
        # newline is written - so a host drawing a frame can close the
        # row it owns while the caret is still on it.
        self.on_submit = on_submit
        # The Ctrl+G hook lets a host show a transient panel without
        # breaking out of the editor: the callback prints to stdout, the
        # next _draw repaints the prompt and popup from a clean state.
        self.on_ctrl_g = on_ctrl_g
        # PageUp/PageDown callbacks scroll the terminal scrollback buffer
        # by temporarily disabling the DECSTBM scroll region.
        self.on_page_up = on_page_up
        self.on_page_down = on_page_down
        # Set to a callable that restores the scroll region after scrolling.
        self._restore_region: Callable[[], None] | None = None
        self._region_cleared = False
        # Escape closes the popup for the rest of this line: it covers
        # several rows of scrollback, so once dismissed it stays dismissed
        # until Tab explicitly asks for suggestions again.
        self._dismissed = False
        self._last_token: str | None = None
        # When True, the suggestion popup is suppressed (used in
        # bottom-fixed prompt mode where the popup would scroll off-screen).
        self.no_popup = no_popup
        # When True, the popup draws ABOVE the prompt line (inside the
        # scroll region) instead of below it.  Used in bottom-fixed layout
        # where the prompt is on the last terminal row.
        self.popup_above = popup_above

    # ---- public API ------------------------------------------------------

    def read(self, prompt: str = "", skip_newline: bool = False) -> str:
        if not (sys.stdin.isatty() and sys.stdout.isatty()):
            return input(prompt)  # piped or captured: plain line input

        if self.completer is not None and hasattr(self.completer, "begin"):
            self.completer.begin()

        # The console puts a blank line before the prompt to separate
        # turns. That has to be written once and then forgotten: redrawing
        # it would emit its newline on every keystroke and walk the line
        # down the screen. Only the final line is part of the redraw.
        head, sep, prompt = prompt.rpartition("\n")
        if sep:
            # sep is the newline itself; a leading one yields an empty
            # head that must still be written, or the spacing is lost.
            sys.stdout.write(head + sep)
            sys.stdout.flush()

        buffer = ""
        cursor = 0
        popup: Completion | None = None
        selected = 0
        drawn = 0
        self._dismissed = False
        self._last_token = None

        try:
            with self._raw_mode():
                drawn = self._draw(prompt, buffer, cursor, popup, selected, drawn)
                while True:
                    # After scrolling, restore the scroll region before
                    # processing the next key so the layout is intact.
                    if self._region_cleared and self._restore_region is not None:
                        self._restore_region()
                        self._region_cleared = False
                    key = self._read_key()
                    if key in ("\r", "\n"):
                        if popup and popup.items and not self._dismissed:
                            chosen = popup.items[min(selected, len(popup.items) - 1)]
                            buffer = buffer[: popup.start] + chosen + buffer[popup.end :]
                            cursor = popup.start + len(chosen)
                            # Slash command at the start should execute immediately on Enter
                            if popup.start == 0 and chosen.startswith("/"):
                                if self.on_submit is not None:
                                    self.on_submit(visible_len(prompt) + len(buffer))
                                break
                            popup, selected = self._recompute(buffer, cursor, selected)
                            self._dismissed = True
                            popup = None
                            drawn = self._draw(prompt, buffer, cursor, popup, selected, drawn)
                            continue
                        if self.on_submit is not None:
                            self.on_submit(visible_len(prompt) + len(buffer))
                        break
                    if key == "\x03":  # ctrl+c
                        raise KeyboardInterrupt
                    if key == "\x04":  # ctrl+d
                        if not buffer:
                            raise EOFError
                        continue
                    if key == "\x07" and self.on_ctrl_g is not None:  # ctrl+g
                        # The hook is allowed to print above the prompt;
                        # the next _draw erases whatever it wrote and
                        # re-paints the prompt + popup from a clean state.
                        try:
                            self.on_ctrl_g()
                        except Exception:
                            pass
                        # Force a full repaint so any bytes the hook
                        # wrote below the prompt get cleared too.
                        drawn = self._draw(prompt, buffer, cursor, None, 0, drawn)
                        continue
                    if key in ("\x7f", "\b"):
                        if cursor > 0:
                            buffer = buffer[: cursor - 1] + buffer[cursor:]
                            cursor -= 1
                            self._last_token = None
                    elif key == KEY_LEFT:
                        cursor = max(0, cursor - 1)
                    elif key == KEY_RIGHT:
                        cursor = min(len(buffer), cursor + 1)
                    elif key == KEY_HOME:
                        cursor = 0
                    elif key == KEY_END:
                        cursor = len(buffer)
                    elif key == KEY_DELETE:
                        buffer = buffer[:cursor] + buffer[cursor + 1 :]
                    elif key == KEY_PAGE_UP:
                        if self.on_page_up is not None:
                            self.on_page_up()
                            self._region_cleared = True
                    elif key == KEY_PAGE_DOWN:
                        if self.on_page_down is not None:
                            self.on_page_down()
                            self._region_cleared = True
                    elif key == KEY_UP:
                        if popup:
                            selected = (selected - 1) % len(popup.items)
                    elif key == KEY_DOWN:
                        if popup:
                            selected = (selected + 1) % len(popup.items)
                    elif key == "\x1b":
                        if popup:
                            self._dismissed = True
                            popup = None
                    elif key == "\t":
                        if popup and popup.items:
                            chosen = popup.items[min(selected, len(popup.items) - 1)]
                            buffer = buffer[: popup.start] + chosen + buffer[popup.end :]
                            cursor = popup.start + len(chosen)
                        else:
                            # Nothing on screen, so Tab means "show me" rather
                            # than "accept". The next Tab accepts.
                            self._dismissed = False
                    elif len(key) == 1 and key >= " ":
                        buffer = buffer[:cursor] + key + buffer[cursor:]
                        cursor += 1

                    popup, selected = self._recompute(buffer, cursor, selected)
                    drawn = self._draw(prompt, buffer, cursor, popup, selected, drawn)
        finally:
            # Whatever the outcome - Enter, Ctrl+C, Ctrl+D - the
            # suggestion rows have to go. Otherwise the next command's
            # output overwrites only its own width and the tails of the
            # old rows stay on screen as garbage.
            self._finish(drawn, skip_newline=skip_newline)
        return buffer

    def _finish(self, drawn: int, skip_newline: bool = False) -> None:
        """Erase the popup rows and leave the caret on a clean line.

        The rows are painted *below* the prompt (or above in popup_above
        mode), so they outlive the line that produced them. Whatever prints
        next lands on those rows and replaces only as many characters as
        it is long, which is how you end up reading
        ``endpoint   https://...v1· esc dismisses``.

        When *skip_newline* is True the trailing newline is omitted —
        used in bottom-fixed prompt mode where the caret must stay on
        the prompt row so the layout can reposition it.
        """
        out = sys.stdout
        if drawn:
            if self.popup_above:
                # Popup is above: move up, clear each row, come back down.
                for _ in range(drawn):
                    out.write("\033[1A\r\033[K")
            else:
                for _ in range(drawn):
                    out.write("\033[1B\r\033[K")
                out.write(f"\033[{drawn}A")
        if not skip_newline:
            out.write("\n")
        out.flush()

    # ---- popup -----------------------------------------------------------

    def _recompute(self, buffer: str, cursor: int, selected: int):
        if self.completer is None or self._dismissed or self.no_popup:
            return None, 0
        completion = self.completer.complete(buffer, cursor)
        if completion is None or not completion.items:
            self._last_token = None
            return None, 0
        token = buffer[completion.start : completion.end]
        # Reset the highlight whenever the filter text changes.
        selected = 0 if token != self._last_token else selected
        self._last_token = token
        return completion, min(selected, len(completion.items) - 1)

    # ---- rendering -------------------------------------------------------

    def _draw(self, prompt, buffer, cursor, popup, selected, drawn) -> int:
        out = sys.stdout
        # Erase the prompt line, then the rows the previous repaint drew.
        # Move down first and clear after: clearing before moving erases
        # the current row and leaves the bottom popup row on screen.
        out.write("\r\033[K")
        if self.popup_above and drawn:
            # Popup was above: erase those rows first.
            for _ in range(drawn):
                out.write("\033[1A\r\033[K")
        else:
            for _ in range(drawn):
                out.write("\033[1B\r\033[K")
            for _ in range(drawn):
                out.write("\033[1A")
        out.write(prompt)
        out.write(buffer)

        rows: list[str] = []
        if popup:
            for index in range(min(self.max_popup, len(popup.items))):
                label = popup.label(index)
                if index == selected:
                    rows.append(f"  {self.style.cyan('> ' + label)}")
                else:
                    rows.append(f"    {self.style.dim(label)}")
            if len(popup.items) > self.max_popup:
                rows.append(self.style.dim(f"    ... {len(popup.items) - self.max_popup} more"))
            if self.hint:
                rows.append(self.style.dim("    " + self.hint))

        if rows and self.popup_above:
            # Draw popup ABOVE the prompt line (inside scroll region).
            n = len(rows)
            out.write(f"\033[{n}A")  # move up N rows from prompt
            for i, row in enumerate(rows):
                out.write("\r\033[2K" + row)  # clear line, write popup row
                if i < n - 1:
                    out.write("\n")  # move down (not last row)
            # Cursor is at the last popup row. Move down 1 to prompt row.
            out.write("\033[1B")
        elif rows:
            for row in rows:
                out.write("\n\033[K")
                out.write(row)
            out.write(f"\033[{len(rows)}A")

        out.write("\r")
        # Visible columns only - the prompt carries colour escapes that
        # take up no space on screen.
        remaining = visible_len(prompt) + cursor
        if remaining:
            out.write(f"\033[{remaining}C")
        out.flush()
        return len(rows)

    # ---- key input -------------------------------------------------------

    @contextmanager
    def _raw_mode(self):
        if os.name == "nt":
            # Enable VT mouse tracking on Windows so scroll wheel works.
            sys.stdout.write("\033[?1006h")  # SGR extended mouse mode
            sys.stdout.write("\033[?1003h")  # enable all mouse motion
            sys.stdout.flush()
            try:
                yield
            finally:
                sys.stdout.write("\033[?1003l")  # disable mouse motion
                sys.stdout.write("\033[?1006l")  # disable SGR mouse
                sys.stdout.flush()
            return
        import termios
        import tty

        fd = sys.stdin.fileno()
        original = termios.tcgetattr(fd)
        try:
            tty.setraw(fd)
            # Enable VT mouse tracking on POSIX so scroll wheel works.
            sys.stdout.write("\033[?1006h")
            sys.stdout.write("\033[?1003h")
            sys.stdout.flush()
            yield
        finally:
            sys.stdout.write("\033[?1003l")
            sys.stdout.write("\033[?1006l")
            sys.stdout.flush()
            termios.tcsetattr(fd, termios.TCSADRAIN, original)

    def _read_key(self) -> str:
        if os.name == "nt":
            import msvcrt

            char = msvcrt.getwch()
            if char in ("\x00", "\xe0"):
                return _WINDOWS_SPECIALS.get(msvcrt.getwch(), char)
            # Mouse reports in SGR mode: ESC [ < Cb ; Cx ; Cy M
            if char == "\x1b":
                if self._input_pending():
                    seq = sys.stdin.read(1)
                    if seq == "[":
                        # Could be mouse or a normal escape sequence.
                        buf = sys.stdin.read(1)
                        if buf == "<":
                            return self._read_sgr_mouse()
                        # Normal ESC [ sequence – read rest.
                        return self._finish_esc("[" + buf)
            return char

        char = sys.stdin.read(1)
        if char != "\x1b":
            return char
        if not self._input_pending():
            return char
        seq = sys.stdin.read(1)
        if seq == "[":
            buf = sys.stdin.read(1)
            if buf == "<":
                return self._read_sgr_mouse()
            # Keys like Delete, PageUp, PageDown send ESC [ N ~
            rest = buf + (sys.stdin.read(1) if buf in "356" else "")
            return _POSIX_SPECIALS.get(rest, char)
        return char

    def _read_sgr_mouse(self) -> str:
        """Read an SGR mouse report and return KEY_PAGE_UP / KEY_PAGE_DOWN
        for scroll wheel events, or ``'key:mouse'`` for other buttons.
        """
        buf = ""
        while True:
            ch = sys.stdin.read(1)
            buf += ch
            if ch in ("M", "m"):
                break
        # SGR format: <button;col;rowM  (M = press, m = release)
        parts = buf.rstrip("Mm").split(";")
        try:
            btn = int(parts[0])
        except (ValueError, IndexError):
            return ""
        # Button 64 = scroll up, 65 = scroll down.
        if btn == 64:
            return KEY_PAGE_UP
        if btn == 65:
            return KEY_PAGE_DOWN
        return ""

    def _finish_esc(self, prefix: str) -> str:
        """Finish reading an ESC [ sequence that wasn't mouse input."""
        # Consume the rest of the sequence (N ~, or letter).
        rest = prefix
        while True:
            ch = sys.stdin.read(1)
            rest += ch
            if ch.isalpha() or ch == "~":
                break
        # Keys like Delete, PageUp, PageDown send ESC [ N ~
        inner = rest[1:]  # strip leading [
        if inner.startswith("3") or inner.startswith("5") or inner.startswith("6"):
            return _POSIX_SPECIALS.get(inner[:2], "\x1b")
        return _POSIX_SPECIALS.get(inner[:2], "\x1b")

    def _input_pending(self) -> bool:
        """True when more bytes are already buffered on stdin."""
        try:
            import select
        except ImportError:  # pragma: no cover - POSIX always has select
            return True
        try:
            return bool(select.select([sys.stdin], [], [], 0)[0])
        except Exception:
            return True


def make_reader(editor: LineEditor) -> Callable[[str], str]:
    """Adapt the editor to the ``prompt -> line`` signature the REPL wants."""
    return editor.read
