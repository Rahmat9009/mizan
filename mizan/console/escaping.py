"""The console's only route from an untrusted string to rendered markup.

Security finding F-8 is why this module exists. In the legacy console, LLM-authored ``reasoning`` reached
``st.markdown(..., unsafe_allow_html=True)`` verbatim, so a prompt-injected advisory response controlled the
markup of the operator UI. The fix here is structural rather than disciplinary: nothing in this module emits a
caller-supplied string without escaping it, there is no ``unsafe`` parameter to reach for, :func:`el` refuses
the tags that can execute (``script``, ``style``, ``iframe``, ...), it refuses every ``on*`` event-handler
attribute and the ``style`` attribute, and URL-bearing attributes are filtered by :func:`safe_url`.

Three rules that look like details and are not:

* **Neutralise, then escape. Never normalise.** NFKC normalisation turns a fullwidth ``<`` into a real ``<``,
  so normalising tainted text *manufactures* markup out of a homoglyph payload. Text is escaped exactly as
  received; NFKC is used only to *flag* such a string (:func:`taint_flags`), never to rewrite it.
* **Escape ``&`` first.** ``&lt;script&gt;`` arriving as data must render as those literal characters, so it
  leaves here as ``&amp;lt;script&amp;gt;``. Escaping ``&`` last would un-escape the payload.
* **Bidi and zero-width controls are made visible, not dropped.** ``U+202E`` reverses displayed text, which is
  one way a free-text field is made to look like a verdict. Format characters are replaced by a printable
  ``[U+202E]`` token so the reader can see that something was there.
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any

__all__ = [
    "BLOCKED_URL",
    "FORBIDDEN_ATTRS",
    "FORBIDDEN_TAGS",
    "SAFE_URL_SCHEMES",
    "URL_ATTRS",
    "VOID_TAGS",
    "Element",
    "Node",
    "attr_value",
    "el",
    "escape_attr",
    "escape_text",
    "neutralise",
    "render",
    "render_all",
    "safe_url",
    "taint_flags",
]

#: Where a URL we refuse to trust is sent. Visible in the DOM, inert in every browser.
BLOCKED_URL = "about:blank#blocked-by-mizan-console"

#: Allowed URL schemes. Everything else - ``javascript:``, ``data:``, ``vbscript:``, ``file:`` - is blocked.
SAFE_URL_SCHEMES = frozenset({"http", "https", "mailto"})

# ``&`` is in both tables and both are applied in a single pass, so no replacement can be re-escaped.
_TEXT_ESCAPES = {"&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#x27;"}
_ATTR_ESCAPES = {**_TEXT_ESCAPES, "`": "&#x60;"}
_TEXT_RE = re.compile("[" + "".join(re.escape(ch) for ch in _TEXT_ESCAPES) + "]")
_ATTR_RE = re.compile("[" + "".join(re.escape(ch) for ch in _ATTR_ESCAPES) + "]")

# C0/C1 controls (newline and tab survive), plus the zero-width, bidi and invisible-format characters that
# let tainted text reorder or hide itself on screen. Built from code points so this source file stays ASCII.
_FORMAT_CODEPOINTS: tuple[int, ...] = (
    tuple(range(0x00, 0x09))
    + (0x0B, 0x0C)
    + tuple(range(0x0E, 0x20))
    + tuple(range(0x7F, 0xA0))
    + (0xAD,)
    + tuple(range(0x200B, 0x2010))
    + tuple(range(0x202A, 0x202F))
    + tuple(range(0x2060, 0x2065))
    + tuple(range(0x2066, 0x206A))
    + (0xFEFF,)
)
_FORMAT_RE = re.compile("[" + "".join(re.escape(chr(code)) for code in _FORMAT_CODEPOINTS) + "]")
_ENTITY_RE = re.compile(r"&(?:#[0-9]{1,7}|#[xX][0-9a-fA-F]{1,6}|[a-zA-Z][a-zA-Z0-9]{1,31});")
_MARKUP_CHARS = "<>&\"'"

_TAG_RE = re.compile(r"^[a-z][a-z0-9-]*$")
_ATTR_NAME_RE = re.compile(r"^[a-z][a-z0-9-]*$")

#: Tags the console will never emit. ``el("script", ...)`` raises rather than escaping something.
FORBIDDEN_TAGS = frozenset(
    {
        "script",
        "style",
        "iframe",
        "object",
        "embed",
        "applet",
        "base",
        "link",
        "meta",
        "template",
        "noscript",
        "svg",
        "math",
        "frame",
        "frameset",
        "portal",
    }
)
VOID_TAGS = frozenset({"br", "hr", "img", "input", "source", "col", "wbr", "track", "area"})
#: Attributes whose value is a URL: every one is filtered by :func:`safe_url`.
URL_ATTRS = frozenset({"href", "src", "action", "formaction", "cite", "poster", "data", "ping", "srcset"})
#: Attributes never allowed, whatever their value. Any ``on*`` name is refused by prefix as well.
FORBIDDEN_ATTRS = frozenset({"style", "srcdoc", "http-equiv", "background"})


def neutralise(value: str) -> str:
    """Return ``value`` with line endings normalised and every invisible format character made printable.

    This is the only transformation applied to tainted text, and it can never *introduce* a markup character:
    every replacement has the form ``[U+XXXX]``.
    """
    text = value.replace("\r\n", "\n").replace("\r", "\n")
    return _FORMAT_RE.sub(lambda match: f"[U+{ord(match.group()):04X}]", text)


def escape_text(value: Any) -> str:
    """Escape ``value`` for an HTML text node. ``None`` becomes an em dash; other types are stringified."""
    if value is None:
        return "—"
    if value is True or value is False:
        return "true" if value else "false"
    text = neutralise(value if isinstance(value, str) else str(value))
    return _TEXT_RE.sub(lambda match: _TEXT_ESCAPES[match.group()], text)


def escape_attr(value: Any) -> str:
    """Escape ``value`` for a double-quoted attribute (backtick included, for lenient attribute parsers)."""
    if value is None:
        return ""
    text = neutralise(value if isinstance(value, str) else str(value))
    return _ATTR_RE.sub(lambda match: _ATTR_ESCAPES[match.group()], text)


def taint_flags(value: str) -> tuple[str, ...]:
    """Describe *why* a string looks adversarial, so the UI can badge it. Never used to rewrite the string."""
    if not isinstance(value, str) or not value:
        return ()
    flags: list[str] = []
    if _FORMAT_RE.search(value):
        flags.append("control-characters")
    if any(ch in value for ch in _MARKUP_CHARS):
        flags.append("markup-characters")
    if _ENTITY_RE.search(value):
        flags.append("html-entities")
    normalised = unicodedata.normalize("NFKC", value)
    if normalised != value and any(ch in normalised and ch not in value for ch in _MARKUP_CHARS):
        flags.append("normalises-to-markup")
    if not value.isascii() and "normalises-to-markup" not in flags:
        flags.append("non-ascii")
    return tuple(flags)


_URL_STRIP = "".join(chr(code) for code in range(0x21)) + "\x7f"


def safe_url(value: Any) -> str:
    r"""Return ``value`` if it is an http(s)/mailto or same-origin relative URL, else :data:`BLOCKED_URL`.

    Whitespace and control characters are removed before the scheme is read, because ``java\tscript:alert(1)``
    is a working URL in more than one browser. Protocol-relative ``//host`` URLs are refused too: they inherit
    whatever scheme the page was served over.
    """
    if not isinstance(value, str):
        return BLOCKED_URL
    stripped = "".join(ch for ch in value if ch not in _URL_STRIP)
    stripped = _FORMAT_RE.sub("", stripped)
    if not stripped or stripped.startswith("//"):
        return BLOCKED_URL
    head = re.split(r"[/?#]", stripped, maxsplit=1)[0]
    if ":" in head and head.split(":", 1)[0].lower() not in SAFE_URL_SCHEMES:
        return BLOCKED_URL
    return stripped


@dataclass(frozen=True, slots=True)
class Element:
    """An element of the console's output tree. Built by :func:`el`, serialised by :func:`render`."""

    tag: str
    attrs: tuple[tuple[str, str], ...]
    children: tuple[Any, ...]


#: What a renderable child may be: an element, a string (escaped on render), or ``None`` (dropped).
Node = Element | str | None


def attr_value(name: str, value: Any) -> str | None:
    """The stored form of one attribute, or ``None`` when the attribute is dropped entirely."""
    if value is None or value is False:
        return None
    if value is True:
        return ""
    text = value if isinstance(value, str) else str(value)
    return safe_url(text) if name in URL_ATTRS else text


def _attr_name(raw: str) -> str:
    name = raw.rstrip("_").replace("_", "-").lower()
    if not _ATTR_NAME_RE.fullmatch(name):
        raise ValueError(f"unsafe attribute name: {raw!r}")
    if name in FORBIDDEN_ATTRS or name.startswith("on"):
        raise ValueError(f"attribute {name!r} is never emitted by the console")
    return name


def _flatten(children: Iterable[Any]) -> tuple[Any, ...]:
    out: list[Any] = []
    for child in children:
        if child is None:
            continue
        if isinstance(child, Element | str):
            out.append(child)
        elif isinstance(child, Mapping):
            raise TypeError("a mapping is not a renderable child; build elements from it first")
        elif isinstance(child, Iterable):
            out.extend(_flatten(child))
        else:
            out.append(str(child))
    return tuple(out)


def el(tag: str, *children: Any, **attrs: Any) -> Element:
    """Build an element. ``class_`` and ``data_region`` become ``class`` and ``data-region``.

    Raises ``ValueError`` for a forbidden tag or attribute; there is deliberately no escape hatch.
    """
    name = tag.lower()
    if not _TAG_RE.fullmatch(name):
        raise ValueError(f"unsafe tag name: {tag!r}")
    if name in FORBIDDEN_TAGS:
        raise ValueError(f"<{name}> is never emitted by the console")
    pairs: list[tuple[str, str]] = []
    for raw, value in attrs.items():
        attribute = _attr_name(raw)
        stored = attr_value(attribute, value)
        if stored is not None:
            pairs.append((attribute, stored))
    kids = _flatten(children)
    if name in VOID_TAGS and kids:
        raise ValueError(f"<{name}> is a void element and cannot have children")
    return Element(tag=name, attrs=tuple(pairs), children=kids)


def render(node: Node) -> str:
    """Serialise a node. A ``str`` child is escaped; there is no branch that emits one unescaped."""
    if node is None:
        return ""
    if isinstance(node, str):
        return escape_text(node)
    if not isinstance(node, Element):
        raise TypeError(f"cannot render {type(node).__name__}")
    attrs = "".join(
        f" {name}" if value == "" else f' {name}="{escape_attr(value)}"' for name, value in node.attrs
    )
    if node.tag in VOID_TAGS:
        return f"<{node.tag}{attrs}>"
    inner = "".join(render(child) for child in node.children)
    return f"<{node.tag}{attrs}>{inner}</{node.tag}>"


def render_all(nodes: Iterable[Node]) -> str:
    """Serialise a sequence of nodes."""
    return "".join(render(node) for node in nodes)
