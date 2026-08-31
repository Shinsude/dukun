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


# Re-export shared primitives so every module agrees on width and size.
# Previously this file counted wide chars (2 cols) while console/compact
# counted 1, causing cursor drift for CJK paths. Centralised in term.py.
from mantra.term import term_size, visible_len  # noqa: F401

_ANSI_RE = re.compile(r"\033\[[0-9;?]*[ -/]*[@-~]")


# Key constants for special keys that don't map to a single character.
KEY_LEFT = "key:left"
KEY_RIGHT = "key:right"
KEY_UP = "key:up"
KEY_DOWN = "key:down"
KEY_DELETE = "key:delete"
KEY_HOME = "key:home"
KEY_END = "key:end"
KEY_PAGE_UP = "key:page-up"
KEY_PAGE_DOWN = "key:page-down"
KEY_RESIZE = "key:resize"

_WINDOWS_SPECIALS: dict[str, str] = {
    "H": KEY_UP,
    "P": KEY_DOWN,
    "K": KEY_LEFT,
    "M": KEY_RIGHT,
    "S": KEY_PAGE_DOWN,
    "I": KEY_PAGE_UP,
    "G": KEY_HOME,
    "O": KEY_END,
    "R": KEY_DELETE,
    # VT sequences sent by some terminals for arrows/insert/delete.
    "A": KEY_UP,
    "B": KEY_DOWN,
    "C": KEY_RIGHT,
    "D": KEY_LEFT,
}

_POSIX_SPECIALS: dict[str, str] = {
    "A": KEY_UP,
    "B": KEY_DOWN,
    "C": KEY_RIGHT,
    "D": KEY_LEFT,
    "H": KEY_HOME,
    "F": KEY_END,
    "5~": KEY_PAGE_UP,
    "6~": KEY_PAGE_DOWN,
    "2~": KEY_DELETE,
    "3~": KEY_DELETE,
}

HINT = ""


@dataclass
class Completion:
    """A set of completion candidates with their source span."""

    start: int
    end: int
    items: list[str] = field(default_factory=list)
    labels: list[str] | None = None

    def label(self, index: int) -> str:
        if self.labels and 0 <= index < len(self.labels):
            return self.labels[index]
        return self.items[index] if 0 <= index < len(self.items) else ""


class LineEditor:
    """Single-key line editor with an inline completion popup."""

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
        on_resize: Callable[[], str | None] | None = None,
    ) -> None:
        self.style = style
        self.completer = completer
        self.max_popup = max_popup
        self.hint = hint
        self.on_submit = on_submit
        self.on_ctrl_g = on_ctrl_g
        self.on_page_up = on_page_up
        self.on_page_down = on_page_down
        self.on_resize = on_resize
        self._restore_region: Callable[[], None] | None = None
        self._region_cleared = False
        self._dismissed = False
        self._last_token: str | None = None
        self.no_popup = no_popup
        self.popup_above = popup_above

        # Called before each _draw to let the host restore content
        # after popup erasure.
        self.on_before_draw: Callable[[], None] | None = None

        # Absolute row for the prompt (when known). Used to compute
        # absolute rows for popup above, avoiding newlines in scroll region.
        self.fixed_row: int | None = None
        self._prev_fixed_row: int | None = None

        # Track terminal size for resize detection without side effects.
        self._term_cols: int | None = None
        self._term_rows: int | None = None

    # ── public API ────────────────────────────────────────────

    def read(self, prompt: str = "", skip_newline: bool = False) -> str:
        if not (sys.stdin.isatty() and sys.stdout.isatty()):
            return input(prompt)

        if self.completer is not None and hasattr(self.completer, "begin"):
            self.completer.begin()

        head, sep, prompt = prompt.rpartition("\n")
        if sep:
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
                    if self._region_cleared and self._restore_region is not None:
                        self._restore_region()
                        self._region_cleared = False
                    key = self._read_key()
                    if key == KEY_RESIZE:
                        # The host may have moved the fixed prompt to a new bottom row.
                        # A resize redraws the whole layout, so discard any popup-row
                        # bookkeeping tied to the old geometry before repainting.
                        if self.on_resize is not None:
                            try:
                                new_prompt = self.on_resize()
                                if isinstance(new_prompt, str):
                                    _head, _sep, prompt = new_prompt.rpartition("\n")
                            except Exception:
                                pass
                        drawn = 0
                        drawn = self._draw(prompt, buffer, cursor, popup, selected, drawn)
                        continue
                    if key in ("\r", "\n"):
                        if popup and popup.items and not self._dismissed:
                            chosen = popup.items[min(selected, len(popup.items) - 1)]
                            buffer = buffer[: popup.start] + chosen + buffer[popup.end :]
                            cursor = popup.start + len(chosen)
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
                    if key == "\x03":
                        raise KeyboardInterrupt
                    if key == "\x04":
                        if not buffer:
                            raise EOFError
                        continue
                    if key == "\x1b":
                        # Escape: dismiss popup if open, otherwise ignore.
                        if popup and popup.items:
                            self._dismissed = True
                            popup = None
                            drawn = self._draw(prompt, buffer, cursor, None, selected, drawn)
                        continue
                    if key == "\x07" and self.on_ctrl_g is not None:
                        try:
                            self.on_ctrl_g()
                        except Exception:
                            pass
                        drawn = self._draw(prompt, buffer, cursor, None, 0, drawn)
                        continue
                    if key in ("\x7f", "\b"):
                        if cursor > 0:
                            buffer = buffer[: cursor - 1] + buffer[cursor:]
                            cursor -= 1
                            self._last_token = None
                            popup, selected = self._recompute(buffer, cursor, selected)
                    elif key == KEY_LEFT:
                        cursor = max(0, cursor - 1)
                        popup, selected = self._recompute(buffer, cursor, selected)
                    elif key == KEY_RIGHT:
                        cursor = min(len(buffer), cursor + 1)
                        popup, selected = self._recompute(buffer, cursor, selected)
                    elif key == KEY_HOME:
                        cursor = 0
                        popup, selected = self._recompute(buffer, cursor, selected)
                    elif key == KEY_END:
                        cursor = len(buffer)
                        popup, selected = self._recompute(buffer, cursor, selected)
                    elif key == KEY_DELETE:
                        buffer = buffer[:cursor] + buffer[cursor + 1 :]
                        popup, selected = self._recompute(buffer, cursor, selected)
                    elif key == KEY_UP:
                        if popup and popup.items:
                            selected = max(0, selected - 1)
                        elif self.on_page_up is not None:
                            self.on_page_up()
                            self._region_cleared = True
                    elif key == KEY_DOWN:
                        if popup and popup.items:
                            selected = min(len(popup.items) - 1, selected + 1)
                        elif self.on_page_down is not None:
                            self.on_page_down()
                            self._region_cleared = True
                    elif key == KEY_PAGE_UP:
                        if self.on_page_up is not None:
                            self.on_page_up()
                            self._region_cleared = True
                    elif key == KEY_PAGE_DOWN:
                        if self.on_page_down is not None:
                            self.on_page_down()
                            self._region_cleared = True
                    elif key in ("\t",):
                        self._dismissed = False
                        popup, selected = self._recompute(buffer, cursor, selected)
                    elif len(key) == 1 and key.isprintable():
                        buffer = buffer[:cursor] + key + buffer[cursor:]
                        cursor += 1
                        self._last_token = None
                        popup, selected = self._recompute(buffer, cursor, selected)
                    else:
                        continue
                    drawn = self._draw(prompt, buffer, cursor, popup, selected, drawn)
        finally:
            self._finish(drawn, skip_newline=skip_newline)

        return buffer

    # ── completion ────────────────────────────────────────────

    def _recompute(self, buffer: str, cursor: int, selected: int):
        if self.completer is None or self._dismissed or self.no_popup:
            return None, 0
        completion = self.completer.complete(buffer, cursor)
        if completion is None or not completion.items:
            self._last_token = None
            return None, 0
        token = buffer[completion.start : completion.end]
        selected = 0 if token != self._last_token else selected
        self._last_token = token
        return completion, min(selected, len(completion.items) - 1)

    # ── rendering ─────────────────────────────────────────────

    def _draw(self, prompt, buffer, cursor, popup, selected, drawn) -> int:
        out = sys.stdout

        # Auto-compute fixed_row if not set externally.
        if self.popup_above and self.fixed_row is None:
            try:
                _, rows = term_size()
                self.fixed_row = rows
            except Exception:
                pass

        # If fixed_row moved (resize), clear old prompt row that is now inside content
        if self.fixed_row is not None and self._prev_fixed_row is not None and self.fixed_row != self._prev_fixed_row:
            out.write(f"\033[{self._prev_fixed_row};1H\033[2K")
            # Also clear old popup rows at their previous absolute positions
            if drawn > 0 and self.popup_above:
                for i in range(drawn):
                    # Old popup ended at old_fixed_row-2 (content_bottom), not -1
                    row = self._prev_fixed_row - i - 2
                    if row >= 1:
                        out.write(f"\033[{row};1H\033[2K")
        self._prev_fixed_row = self.fixed_row

        # Ensure cursor is on the prompt row before any clearing,
        # preventing a flash in the content area.
        if self.popup_above and self.fixed_row is not None:
            out.write(f"\033[{self.fixed_row};1H")

        # Wrap all drawing in synchronized output to prevent flicker.
        out.write("\033[?2026h")
        try:
            # Step 1: Clear old popup rows. Use absolute rows when a fixed
            # bottom prompt is active (no newlines inside scroll region), else
            # use relative moves. Always clear the prompt line first.
            # Popup occupies rows fixed_row-2 .. fixed_row-n-1 (content_bottom
            # is fixed_row-2, border is fixed_row-1, prompt is fixed_row).
            if drawn > 0 and self.fixed_row is not None and self.popup_above:
                for i in range(drawn):
                    row = self.fixed_row - i - 2
                    if row >= 1:
                        out.write(f"\033[{row};1H\033[2K")
                out.write("\r\033[K")
            else:
                out.write("\r\033[K")
                if self.popup_above and drawn:
                    for _ in range(drawn):
                        out.write("\033[1A\r\033[K")
                elif drawn:
                    for _ in range(drawn):
                        out.write("\033[1B\r\033[K")
                    for _ in range(drawn):
                        out.write("\033[1A")

            # Step 2: Restore content rows that were corrupted by popup.
            if drawn > 0 and self.on_before_draw is not None:
                try:
                    self.on_before_draw(drawn)
                except Exception:
                    pass

            # Step 3: Write the prompt (contains absolute positioning).
            out.write(prompt)

            # Step 4: Clip buffer to prevent bottom-row wrapping.
            try:
                cols, _ = term_size()
            except Exception:
                cols = 80

            pvis = visible_len(prompt)
            space = max(0, cols - pvis)

            if space <= 0:
                shown = ""
                cpos = 0
            elif len(buffer) <= space:
                shown = buffer
                cpos = cursor
            else:
                start = max(0, cursor - space + 1)
                shown = buffer[start : start + space]
                cpos = cursor - start

            out.write(shown)

            # Step 5: Draw popup above the prompt.
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
                n = len(rows)
                if self.fixed_row is not None:
                    # Clamp popup so it never overlaps info border (row 2) or
                    # border row (fixed_row-1). Available = fixed_row-4
                    # (content height). Uses fixed_row-2 as content_bottom.
                    max_popup_rows = max(0, self.fixed_row - 4)
                    # Also respect max_popup already limited by repl to content height
                    max_popup_rows = min(max_popup_rows, self.max_popup) if self.max_popup else max_popup_rows
                    if n > max_popup_rows:
                        rows = rows[-max_popup_rows:]
                        n = len(rows)
                    # Anchor popup to content_bottom (fixed_row-2), not border
                    for i, row_text in enumerate(rows):
                        row = self.fixed_row - 2 - n + 1 + i
                        if row >= 1:
                            out.write(f"\033[{row};1H\033[2K{row_text}")
                    out.write(f"\033[{self.fixed_row};1H")   # absolute: move cursor to prompt row
                else:
                    out.write(f"\033[{n}A")
                    for i, row_text in enumerate(rows):
                        out.write("\r\033[2K" + row_text)
                        if i < n - 1:
                            out.write("\n")
                    out.write("\033[1B")

            elif rows:
                for row in rows:
                    out.write("\n\033[K")
                    out.write(row)
                out.write(f"\033[{len(rows)}A")

            # Step 6: Position cursor at the typed text position.
            out.write("\r")
            remaining = pvis + cpos
            if remaining:
                out.write(f"\033[{remaining}C")

        finally:
            out.write("\033[?2026l")
            out.flush()

        return len(rows)

    def _finish(self, drawn: int, skip_newline: bool = False) -> None:
        out = sys.stdout

        if drawn:
            if self.popup_above and self.fixed_row is not None:
                for i in range(drawn):
                    row = self.fixed_row - i - 2
                    if row >= 1:
                        out.write(f"\033[{row};1H\033[2K")
                out.write(f"\033[{self.fixed_row};1H")
            elif self.popup_above:
                for _ in range(drawn):
                    out.write("\033[1A\r\033[K")
                if drawn:
                    out.write(f"\033[{drawn}B")
                out.write("\r")
            else:
                for _ in range(drawn):
                    out.write("\033[1B\r\033[K")
                out.write(f"\033[{drawn}A")
                out.write("\r")

        # Restore host content after popup dismissal.
        if self.popup_above and self.on_before_draw is not None and drawn > 0:
            try:
                self.on_before_draw(drawn)
            except Exception:
                pass

        if not skip_newline:
            out.write("\n")

        out.flush()

    # ── key input ─────────────────────────────────────────────

    @contextmanager
    def _raw_mode(self):
        if os.name == "nt":
            try:
                yield
            finally:
                pass
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

    def _read_key(self) -> str:
        if os.name == "nt":
            import msvcrt
            import time

            while not msvcrt.kbhit():
                try:
                    cols, rows = term_size()
                    if (self._term_cols is None or self._term_rows is None):
                        self._term_cols, self._term_rows = cols, rows
                    elif cols != self._term_cols or rows != self._term_rows:
                        self._term_cols, self._term_rows = cols, rows
                        return KEY_RESIZE
                except Exception:
                    pass
                time.sleep(0.05)

            char = msvcrt.getwch()
            if char in ("\x00", "\xe0"):
                return _WINDOWS_SPECIALS.get(msvcrt.getwch(), char)
            if char == "\x1b":
                if self._input_pending():
                    seq = sys.stdin.read(1)
                    if seq == "[":
                        buf = sys.stdin.read(1)
                        if buf == "<":
                            return "\x1b"  # mouse event ignored
                        return self._finish_esc("[" + buf)
            return char

        import select
        while True:
            try:
                ready, _, _ = select.select([sys.stdin], [], [], 0.05)
            except (OSError, ValueError):
                ready = [sys.stdin]

            try:
                cols, rows = term_size()
                if (self._term_cols is None or self._term_rows is None):
                    self._term_cols, self._term_rows = cols, rows
                elif cols != self._term_cols or rows != self._term_rows:
                    self._term_cols, self._term_rows = cols, rows
                    return KEY_RESIZE
            except Exception:
                pass

            if ready:
                break

        char = sys.stdin.read(1)
        if char != "\x1b":
            return char
        if not self._input_pending():
            return char
        seq = sys.stdin.read(1)
        if seq == "[":
            buf = sys.stdin.read(1)
            if buf == "<":
                return "\x1b"  # mouse event ignored
            rest = buf + (sys.stdin.read(1) if buf in "356" else "")
            return _POSIX_SPECIALS.get(rest, char)
        return char

    def _finish_esc(self, prefix: str) -> str:
        rest = prefix
        while True:
            ch = sys.stdin.read(1)
            rest += ch
            if ch.isalpha() or ch == "~":
                break
        inner = rest[1:]
        if inner.startswith("3") or inner.startswith("5") or inner.startswith("6"):
            return _POSIX_SPECIALS.get(inner[:2], "\x1b")
        return _POSIX_SPECIALS.get(inner[:2], "\x1b")

    def _input_pending(self) -> bool:
        if os.name == "nt":
            try:
                import msvcrt
                return msvcrt.kbhit()
            except Exception:
                return False
        try:
            import select
        except ImportError:
            return True
        try:
            return bool(select.select([sys.stdin], [], [], 0)[0])
        except Exception:
            return True


def make_reader(editor: LineEditor) -> Callable[[str], str]:
    """Adapt the editor to the ``prompt -> line`` signature the REPL wants."""
    return editor.read