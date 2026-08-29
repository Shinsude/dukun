"""Network tools: fetching a URL and reading it as text.

The harness has no third-party dependencies, so this uses ``urllib``
and ``html.parser`` rather than requests and BeautifulSoup. That costs
some fidelity - the extractor is a tag stripper, not a layout engine -
but a coding agent only needs the readable text of a page, and the
stdlib version cannot drift out of date or fail to install.
"""

from __future__ import annotations

import gzip
import ipaddress
import re
import zlib
from html.parser import HTMLParser
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import Request, urlopen

from mantra.interfaces.sandbox import Sandbox
from mantra.interfaces.tool import Tool

# A response bigger than this is almost certainly not something worth
# putting in a context window. Capped before decoding, so an enormous
# page cannot exhaust memory on the way to being truncated.
_MAX_BYTES = 2_000_000
_MAX_INFLATED = 4_000_000

_BLOCKED_HOSTS = frozenset(
    {
        "localhost",
        "metadata.google.internal",
        "metadata.google",
        "instance-data",
    }
)

# Long enough for a slow documentation host, short enough that a hung
# socket does not stall a turn for a minute.
_TIMEOUT = 15

_DEFAULT_MAX_CHARS = 12000

_USER_AGENT = "MANTRA/1.0 (coding harness; +https://github.com/mantra)"

_TEXTUAL = ("text/", "application/json", "application/xml", "application/javascript")


class _TextExtractor(HTMLParser):
    """Collect the visible text of a document.

    Not a renderer: it drops the contents of tags that never reach the
    screen and turns block-level tags into newlines so paragraphs do not
    run together. Good enough to read documentation; not good enough to
    reconstruct a table's layout, which is why the tool says so.
    """

    _SKIP = {"script", "style", "noscript", "template", "svg", "head", "iframe"}
    _BREAK = {
        "p", "div", "br", "li", "tr", "section", "article", "header",
        "footer", "nav", "table", "ul", "ol", "dl", "blockquote", "pre",
        "h1", "h2", "h3", "h4", "h5", "h6",
    }

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self._depth = 0

    def handle_starttag(self, tag: str, attrs: Any) -> None:
        if tag in self._SKIP:
            self._depth += 1
        elif tag in self._BREAK:
            self.parts.append("\n")

    def handle_startendtag(self, tag: str, attrs: Any) -> None:
        if tag in self._BREAK:
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in self._SKIP:
            # An unmatched close tag would otherwise leave the depth
            # stuck above zero and blank out the rest of the page.
            self._depth = max(0, self._depth - 1)
        elif tag in self._BREAK:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        if not self._depth:
            self.parts.append(data)

    def text(self) -> str:
        joined = "".join(self.parts)
        joined = joined.replace("\r\n", "\n").replace("\r", "\n")
        # Collapse runs of blanks and spaces: stripped HTML is mostly
        # indentation, and leaving it in wastes most of the budget.
        joined = re.sub(r"[ \t\f\v]+", " ", joined)
        joined = re.sub(r" *\n *", "\n", joined)
        joined = re.sub(r"\n{3,}", "\n\n", joined)
        return joined.strip()


def html_to_text(html: str) -> str:
    """Readable text from an HTML document."""
    extractor = _TextExtractor()
    try:
        extractor.feed(html)
        extractor.close()
    except Exception:  # pragma: no cover - malformed markup
        # html.parser is strict about nothing, but a half-downloaded
        # document can still trip it. Partial text beats an exception.
        pass
    return extractor.text()


def _decode(raw: bytes, encoding: str | None, note: list[str]) -> str:
    for candidate in (encoding, "utf-8", "latin-1"):
        if not candidate:
            continue
        try:
            return raw.decode(candidate)
        except (UnicodeDecodeError, LookupError):
            continue
    note.append("(charset could not be determined; decoded as latin-1)")
    return raw.decode("latin-1", errors="replace")


def _inflate(raw: bytes, content_encoding: str) -> bytes:
    """Undo transport compression. urllib does not do this for you."""
    enc = (content_encoding or "").lower()
    try:
        if "gzip" in enc:
            return gzip.decompress(raw)
        if "deflate" in enc:
            try:
                return zlib.decompress(raw)
            except zlib.error:
                # Servers that say deflate but send the raw stream.
                return zlib.decompress(raw, -zlib.MAX_WBITS)
    except (OSError, zlib.error):
        return raw
    return raw


def _is_private_hostname(hostname: str | None) -> bool:
    if not hostname:
        return False
    host = hostname.lower().strip().rstrip(".")
    if host in _BLOCKED_HOSTS:
        return True
    if host == "0.0.0.0" or host == "::1":
        return True
    # literal IP checks without DNS
    try:
        ip = ipaddress.ip_address(host)
        return ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved or ip.is_multicast
    except ValueError:
        pass
    # metadata service IP as literal
    if host in ("169.254.169.254", "169.254.169.253", "fd00::", "fe80::"):
        return True
    # private range hostnames that are IP-like
    if host.startswith("10.") or host.startswith("192.168."):
        return True
    if host.startswith("172."):
        try:
            second = int(host.split(".")[1])
            if 16 <= second <= 31:
                return True
        except (IndexError, ValueError):
            pass
    return False


def _check_url_allowed(url: str) -> str | None:
    try:
        parsed = urlparse(url)
    except ValueError:
        return f"fetch failed: malformed URL {url!r}"
    if _is_private_hostname(parsed.hostname):
        return f"fetch failed: blocked private or internal host {parsed.hostname!r}"
    return None


class WebFetchTool(Tool):
    """Fetch a URL and return its readable text.

    Returns an error *string* rather than raising, because a failure here
    is information the agent can act on - follow a different link, fix
    the URL, tell the operator the host is down. Raising would end the
    turn with a traceback instead.
    """

    name = "web_fetch"
    description = (
        "Fetch a web page over HTTP(S) and return its readable text. "
        "HTML tags, scripts and styles are stripped. Use it to read "
        "documentation, release notes, issue threads and API references. "
        "Table and page layout is not preserved."
    )
    parameters: dict[str, Any] = {
        "type": "object",
        "properties": {
            "url": {
                "type": "string",
                "description": "Absolute http:// or https:// URL to fetch",
            },
            "max_chars": {
                "type": "integer",
                "description": (
                    "Truncate the extracted text after this many characters "
                    f"(default {_DEFAULT_MAX_CHARS})"
                ),
            },
        },
        "required": ["url"],
    }

    timeout = _TIMEOUT

    def execute(
        self,
        sandbox: Sandbox,
        url: str,
        max_chars: int = _DEFAULT_MAX_CHARS,
    ) -> str:
        note: list[str] = []
        url = (url or "").strip()
        if not url:
            return "fetch failed: no URL given"

        scheme = urlparse(url).scheme.lower()
        if scheme not in ("http", "https"):
            return (
                f"fetch failed: unsupported scheme '{scheme or 'none'}' - "
                "only http and https can be fetched"
            )
        blocked = _check_url_allowed(url)
        if blocked:
            return blocked

        try:
            budget = max(0, min(int(max_chars), 200_000))
        except (TypeError, ValueError):
            budget = _DEFAULT_MAX_CHARS
        if budget == 0:
            budget = _DEFAULT_MAX_CHARS

        try:
            request = Request(url, headers={"User-Agent": _USER_AGENT})
            with urlopen(request, timeout=self.timeout) as response:
                raw = response.read(_MAX_BYTES + 1)
                final_url = response.geturl()
                content_type = response.headers.get("Content-Type", "")
                encoding = response.headers.get_content_charset()
                compressed = response.headers.get("Content-Encoding", "")
                status = getattr(response, "status", None) or response.getcode()
        except HTTPError as exc:
            # The body of an error response often says which header was
            # wrong, so surface the status and let the agent carry on.
            return f"fetch failed: HTTP {exc.code} {exc.reason} for {url}"
        except URLError as exc:
            return f"fetch failed: {exc.reason} for {url}"
        except (TimeoutError, OSError) as exc:
            return f"fetch failed: {exc} for {url}"

        # block redirects to private hosts
        blocked_final = _check_url_allowed(final_url)
        if blocked_final:
            return blocked_final

        if len(raw) > _MAX_BYTES:
            raw = raw[:_MAX_BYTES]
            note.append(f"(response truncated at {_MAX_BYTES} bytes)")

        raw = _inflate(raw, compressed)
        if len(raw) > _MAX_INFLATED:
            raw = raw[:_MAX_INFLATED]
            note.append(f"(decompressed response truncated at {_MAX_INFLATED} bytes)")
        charset_note: list[str] = []
        body = _decode(raw, encoding if _looks_textual(content_type) else None, charset_note)
        note.extend(charset_note)

        if _looks_textual(content_type) and "html" in content_type.lower():
            body = html_to_text(body)

        body = body.strip()
        if not body:
            return f"fetched {final_url} (HTTP {status}) but it had no text content"

        if len(body) > budget:
            body = body[:budget].rstrip() + "\n... [truncated]"
            note.append(f"(text truncated at {budget} chars)")

        header = f"{final_url} (HTTP {status})"
        if note:
            header += " " + " ".join(note)
        return f"{header}\n\n{body}"


def _looks_textual(content_type: str) -> bool:
    """False for types that are certainly not prose (images, archives)."""
    head = (content_type or "").split(";")[0].strip().lower()
    if not head:
        # No declared type: assume text rather than refusing to read it.
        return True
    return head.startswith(_TEXTUAL) or head.endswith("+xml") or head.endswith("+json")
