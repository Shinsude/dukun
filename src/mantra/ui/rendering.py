"""Terminal styling, spinner, and markdown-lite rendering.

Extracted from console.py to reduce the monolithic file size.
"""

from __future__ import annotations

import re
import sys
import threading
import time
from contextlib import contextmanager
from typing import Any

_WRITE_LOCK = threading.Lock()
SPINNER_FRAMES = ["⠋", "⠙", "⠹", "⠸", "⠼", "⠴"]


class Style:
    """ANSI escape helpers.  ``enabled=False`` strips every mark."""

    def __init__(self, enabled: bool = True) -> None:
        self.enabled = enabled

    def _m(self, code: str, text: str) -> str:
        if not self.enabled:
            return text
        return f"\033[{code}m{text}\033[0m"

    def bold(self, t: str) -> str:
        return self._m("1", t)

    def dim(self, t: str) -> str:
        return self._m("2", t)

    def strike(self, t: str) -> str:
        return self._m("9", t)

    def red(self, t: str) -> str:
        return self._m("31", t)

    def green(self, t: str) -> str:
        return self._m("32", t)

    def yellow(self, t: str) -> str:
        return self._m("33", t)

    def blue(self, t: str) -> str:
        return self._m("34", t)

    def magenta(self, t: str) -> str:
        return self._m("35", t)

    def cyan(self, t: str) -> str:
        return self._m("36", t)

    def bright_white(self, t: str) -> str:
        return self._m("97", t)

    def bright_yellow(self, t: str) -> str:
        return self._m("93", t)

    def bright_cyan(self, t: str) -> str:
        return self._m("96", t)

    def bright_magenta(self, t: str) -> str:
        return self._m("95", t)

    def grey(self, t: str) -> str:
        return self._m("37", t)

    def on_grey(self, t: str) -> str:
        return self._m("47;30", t)

    def neon_title(self, t: str) -> str:
        return self._m("1;96", t)

    def neon_label(self, t: str) -> str:
        return self._m("1;95", t)


class Spinner:
    """Background thread that redraws a line with an animated frame."""

    def __init__(self, style: Style, frame=None, layout=None) -> None:
        self.style = style
        self._thread = threading.Thread(target=self._spin, daemon=True)
        self._stop = threading.Event()
        self._paused = False
        self._lock = threading.Lock()
        self._frame = frame
        self._layout = layout
        self.label = "thinking"
        self.started_at = time.monotonic()

    def _render(self, frame_char: str) -> str:
        elapsed = int(time.monotonic() - self.started_at)
        if elapsed < 60:
            elapsed_str = f"{elapsed}s"
        else:
            elapsed_str = f"{elapsed // 60}m{elapsed % 60:02d}s"
        pulse = int(time.monotonic() * 4) % 6
        if pulse < 3:
            label_styled = self.style.dim(self.label)
        else:
            label_styled = self.style.grey(self.label)
        body = f"{self.style.cyan(frame_char)} {label_styled} {self.style.bright_yellow(elapsed_str)}"
        if self._layout is not None and self._layout.active:
            return body
        return "\r" + (self._frame(body) if self._frame else body + " ")

    def _spin(self) -> None:
        i = 0
        while not self._stop.wait(0.08):
            with self._lock:
                if not self._paused:
                    with _WRITE_LOCK:
                        if self._layout is not None and self._layout.active:
                            self._layout.draw_border_status(self._render(SPINNER_FRAMES[i % len(SPINNER_FRAMES)]))
                        else:
                            sys.stdout.write(self._render(SPINNER_FRAMES[i % len(SPINNER_FRAMES)]))
                            sys.stdout.flush()
            i += 1

    def start(self):
        if sys.stdout.isatty():
            self._thread.start()
        return self

    def stop(self, clear: bool = True):
        self._stop.set()
        if self._thread.is_alive():
            self._thread.join(timeout=1)
        if clear:
            self.clear_line()

    def clear_line(self):
        with self._lock:
            with _WRITE_LOCK:
                if self._layout is not None and self._layout.active:
                    self._layout.draw_border_status("")
                else:
                    sys.stdout.write("\r\033[K")
                sys.stdout.flush()

    @contextmanager
    def paused(self):
        """Suppress all spinner drawing while real output prints."""
        with self._lock:
            was = self._paused
            self._paused = True
            with _WRITE_LOCK:
                if self._layout is not None and self._layout.active:
                    self._layout.draw_border_status("")
                else:
                    sys.stdout.write("\r\033[K")
                sys.stdout.flush()
        try:
            yield
        finally:
            with self._lock:
                self._paused = was


def _inline_md(line: str, style: Style) -> str:
    """Inline markdown: code, bold, italic, strikethrough, links."""
    import re as _re
    # Order matters: process code spans first so their contents stay literal.
    parts = line.split("`")
    for i in range(0, len(parts)):
        if i % 2 == 1:
            parts[i] = style.bright_magenta(parts[i])
        else:
            segment = parts[i]
            # Links: [text](url) -> styled text
            segment = _re.sub(
                r'\[([^\]]+)\]\([^)]+\)',
                lambda m: style.bright_cyan(m.group(1)),
                segment,
            )
            # Bold: **text**
            segment = _re.sub(r'\*\*(.+?)\*\*', lambda m: style.bright_white(m.group(1)), segment)
            # Italic: *text*
            segment = _re.sub(r'(?<!\*)\*(?!\*)(.+?)(?<!\*)\*(?!\*)', lambda m: style.bright_magenta(m.group(1)), segment)
            # Strikethrough: ~~text~~
            segment = _re.sub(r'~~(.+?)~~', lambda m: style.strike(m.group(1)), segment)
            parts[i] = segment
    return "`".join(parts)


def _render_md_line(line: str, style: Style, ctx: Any = None) -> str:
    """Render a single markdown line to ANSI-styled text."""
    in_fence = getattr(ctx, "_in_code_fence", False)
    stripped = line.strip()

    if in_fence:
        if stripped.startswith("```"):
            if ctx is not None:
                ctx._in_code_fence = False
            return style.dim("│" + "─" * 4)
        return style.dim(line)
    if stripped.startswith("```"):
        if ctx is not None:
            ctx._in_code_fence = True
        return style.dim("│" + "─" * 4)

    # ── headings ─────────────────────────────────────────
    if stripped.startswith("#"):
        level = len(stripped) - len(stripped.lstrip("#"))
        heading = stripped.lstrip("# ").rstrip()
        if level == 1:
            return style.bold(style.bright_white(heading)) + chr(10) + style.dim(chr(0x2500) * 40)
        if level == 2:
            return style.bold(heading) + chr(10) + style.dim(chr(0x2500) * 40)
        return style.bold(heading)

    # ── horizontal rule ──────────────────────────────────
    if stripped in ("---", "***", "___") and len(stripped) >= 3:
        return style.dim(chr(0x2500) * 40)

    # ── blockquote ───────────────────────────────────────
    if stripped.startswith(">"):
        quote = stripped[1:].lstrip()
        return style.dim("│ ") + style.dim(quote)

    # ── unordered list ───────────────────────────────────
    m_list = re.match(r"^(\s*)[-*+]\s+(.*)", line)
    if m_list:
        indent, rest = m_list.group(1), m_list.group(2)
        return indent + style.bright_magenta("* ") + _inline_md(rest, style)

    # ── ordered list ─────────────────────────────────────
    m_ord = re.match(r"^(\s*)(\d+)[.)]\s+(.*)", line)
    if m_ord:
        indent, num, rest = m_ord.group(1), m_ord.group(2), m_ord.group(3)
        return indent + style.bright_magenta(num + ". ") + _inline_md(rest, style)

    # ── normal paragraph ─────────────────────────────────
    return _inline_md(line, style)


def render_markdown(text: str, style: Style) -> str:
    """Render Markdown to styled terminal output."""
    out_lines = []
    in_fence = False
    for line in text.split("\n"):
        stripped = line.strip()
        # Code fence toggle.
        if stripped.startswith("```"):
            in_fence = not in_fence
            out_lines.append(style.dim("│" + "─" * 4) if in_fence else style.dim("│" + "─" * 4))
            continue
        if in_fence:
            out_lines.append(style.dim(line))
            continue
        # Headings.
        if stripped.startswith("#"):
            level = len(stripped) - len(stripped.lstrip("#"))
            heading = stripped.lstrip("# ").rstrip()
            if level == 1:
                out_lines.append(style.bold(style.bright_white(heading)))
            else:
                out_lines.append(style.bold(heading))
            if level <= 2:
                out_lines.append(style.dim(chr(0x2500) * 40))
            continue
        # Horizontal rule.
        if stripped in ("---", "***", "___") and len(stripped) >= 3:
            out_lines.append(style.dim(chr(0x2500) * 40))
            continue
        # Blockquote.
        if stripped.startswith(">"):
            quote = stripped[1:].lstrip()
            out_lines.append(style.dim("│ ") + style.dim(quote))
            continue
        # Unordered list.
        m_list = re.match(r"^(\s*)[-*+]\s+(.*)", line)
        if m_list:
            indent, rest = m_list.group(1), m_list.group(2)
            out_lines.append(indent + style.bright_magenta("* ") + _inline_md(rest, style))
            continue
        # Ordered list.
        m_ord = re.match(r"^(\s*)(\d+)[.)]\s+(.*)", line)
        if m_ord:
            indent, num, rest = m_ord.group(1), m_ord.group(2), m_ord.group(3)
            out_lines.append(indent + style.bright_magenta(num + ". ") + _inline_md(rest, style))
            continue
        # Normal paragraph.
        out_lines.append(_inline_md(line, style))
    return "\n".join(out_lines)


class StreamingRenderer:
    """Buffers streamed tokens until line boundaries, applying markdown formatting."""

    def __init__(self, style: Style) -> None:
        self.style = style
        self._buf = ""
        self._in_code_fence = False

    def reset(self) -> None:
        self._buf = ""
        self._in_code_fence = False

    def render_piece(self, piece: str) -> str:
        self._buf += piece
        out = ""
        while "\n" in self._buf:
            line, self._buf = self._buf.split("\n", 1)
            out += _render_md_line(line, self.style, self) + "\n"
        return out

    def flush(self) -> str:
        if self._buf:
            result = _render_md_line(self._buf, self.style, self)
            self._buf = ""
            return result + "\n"
        return ""
