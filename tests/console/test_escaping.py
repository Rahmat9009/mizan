"""The escaping primitives, tested against the payloads finding F-8 was written about."""

from __future__ import annotations

import pytest

from mizan.console.escaping import (
    BLOCKED_URL,
    el,
    escape_attr,
    escape_text,
    neutralise,
    render,
    safe_url,
    taint_flags,
)
from tests.console._helpers import PAYLOADS, analyse


def test_every_markup_character_is_escaped_in_a_text_node() -> None:
    assert escape_text("<>&\"'") == "&lt;&gt;&amp;&quot;&#x27;"


def test_ampersand_is_escaped_first_so_an_entity_payload_stays_literal() -> None:
    # A payload that arrives already entity-encoded must not be handed back to the browser decodable.
    assert escape_text("&lt;script&gt;") == "&amp;lt;script&amp;gt;"
    assert analyse(escape_text("&lt;script&gt;")).text == "&lt;script&gt;"
    assert analyse(escape_text("&lt;script&gt;")).tags == []


def test_a_quote_cannot_break_out_of_an_attribute() -> None:
    html = render(el("div", "x", title=PAYLOADS["attribute_break"]))
    found = analyse(html)
    assert found.tags == ["div"]
    assert not found.event_handler_attrs
    assert found.attr_values("title") == ['" onmouseover="alert(1)" x="']


def test_bidi_and_zero_width_controls_are_made_visible_rather_than_dropped() -> None:
    reversed_text = neutralise(PAYLOADS["bidi_override"])
    assert "[U+202E]" in reversed_text and "[U+202C]" in reversed_text
    assert neutralise("AP" + chr(0x200B) + "PROVE") == "AP[U+200B]PROVE"


def test_a_homoglyph_payload_is_flagged_and_never_normalised_into_markup() -> None:
    payload = PAYLOADS["homoglyph"]
    assert "normalises-to-markup" in taint_flags(payload)
    rendered = escape_text(payload)
    # NFKC would have turned the fullwidth brackets into real ones. Escaping never normalises, so it does not.
    assert "<" not in rendered and ">" not in rendered
    assert analyse(rendered).tags == []


@pytest.mark.parametrize(
    "url",
    [
        "javascript:alert(1)",
        "JavaScript:alert(1)",
        "java\tscript:alert(1)",
        "java\nscript:alert(1)",
        " javascript:alert(1)",
        "vbscript:msgbox(1)",
        "data:text/html;base64,PHNjcmlwdD4=",
        "file:///etc/passwd",
        "//evil.example/x",
    ],
)
def test_dangerous_urls_are_blocked(url: str) -> None:
    assert safe_url(url) == BLOCKED_URL


@pytest.mark.parametrize(
    "url",
    ["https://example.test/x", "http://example.test", "mailto:ops@example.test", "/v1/decisions", "#top"],
)
def test_safe_urls_survive(url: str) -> None:
    assert safe_url(url) == url


def test_url_attributes_are_filtered_at_the_element_boundary() -> None:
    html = render(el("a", "click", href="javascript:alert(1)"))
    assert html == f'<a href="{BLOCKED_URL}">click</a>'


@pytest.mark.parametrize("tag", ["script", "style", "iframe", "object", "embed", "svg", "base", "link"])
def test_executable_tags_cannot_be_built_at_all(tag: str) -> None:
    with pytest.raises(ValueError, match="never emitted"):
        el(tag)


@pytest.mark.parametrize("attribute", ["onclick", "onerror", "onload", "style", "srcdoc"])
def test_dangerous_attributes_cannot_be_built_at_all(attribute: str) -> None:
    with pytest.raises(ValueError, match="never emitted"):
        el("div", **{attribute: "x"})


def test_a_string_child_is_always_escaped_and_there_is_no_raw_html_sink() -> None:
    for payload in PAYLOADS.values():
        html = render(el("div", payload))
        found = analyse(html)
        assert found.tags == ["div"], f"{payload!r} produced elements {found.tags}"
        assert not found.event_handler_attrs


def test_render_refuses_anything_that_is_not_an_element_or_a_string() -> None:
    with pytest.raises(TypeError):
        render(42)  # type: ignore[arg-type]


def test_escape_attr_also_escapes_the_backtick() -> None:
    assert escape_attr("a`b") == "a&#x60;b"


def test_none_renders_as_a_dash_not_as_the_word_none() -> None:
    assert escape_text(None) == "—"
