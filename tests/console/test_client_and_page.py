"""The client adapter (two spellings, one behaviour) and the page shell (two themes, no script)."""

from __future__ import annotations

import pytest

from mizan.console import client as console_client
from mizan.console import views
from mizan.console.render import THEMES, document, render_decision_feed
from mizan.contracts.errors import NotFound
from tests.console._helpers import FakeClient, assert_inert, tainted_record


class LedgerShaped:
    """A ``TenantLedger``-shaped object: ``list`` and ``get``, not ``list_decisions``/``get_decision``."""

    def __init__(self, records: list) -> None:
        self.records = records

    def list(self, *, limit: int = 50, before_sequence: int | None = None) -> list:
        rows = sorted(self.records, key=lambda record: record.sequence, reverse=True)
        if before_sequence is not None:
            rows = [row for row in rows if row.sequence < before_sequence]
        return rows[:limit]

    def get(self, decision_id: str):
        for record in self.records:
            if record.decision_id == decision_id:
                return record
        raise NotFound()


def test_the_ledger_spelling_of_the_read_api_works_unchanged() -> None:
    record = tainted_record("thesis")
    ledger = LedgerShaped([record])
    assert [row["sequence"] for row in views.decision_feed(ledger)] == [1]
    assert views.decision_detail(ledger, record.decision_id)["found"] is True


def test_not_found_is_collapsed_before_any_view_can_tell_the_two_cases_apart() -> None:
    record = tainted_record("thesis")
    ledger = LedgerShaped([record])
    assert console_client.get_decision(ledger, "01a00000-0000-7000-8000-000000000000") is None
    assert views.decision_detail(ledger, "01a00000-0000-7000-8000-000000000000")["found"] is False


def test_a_client_that_cannot_answer_returns_the_unavailable_sentinel_not_an_error() -> None:
    class Bare:
        pass

    assert console_client.read(Bare(), ("anything",)) is console_client.UNAVAILABLE
    assert bool(console_client.UNAVAILABLE) is False
    assert console_client.list_decisions(Bare()) == []
    assert console_client.chain_entries(Bare()) == []
    assert console_client.get_policy(Bare(), "p", "1.0.0") is None
    assert console_client.replay_decision(Bare(), "x") is console_client.UNAVAILABLE


def test_the_console_never_raises_when_a_view_has_nothing_to_show() -> None:
    class Bare:
        pass

    assert views.decision_feed(Bare()) == []
    assert views.audit_timeline(Bare()) == []
    assert views.chain_status(Bare())["available"] is False
    assert views.kill_switch_view(Bare())["engaged"] is None
    assert views.response_level_view(Bare())["level"] is None


@pytest.mark.parametrize("theme", sorted(THEMES))
def test_both_themes_render_a_complete_page_with_no_script(theme: str) -> None:
    client = FakeClient(records=[tainted_record("thesis")])
    body = render_decision_feed(views.feed_page(client))
    page = document("Mizan console", body, theme=theme)
    assert page.startswith("<!doctype html>")
    assert f'data-theme="{theme}"' in page
    assert page.endswith("</main></body></html>")
    assert "<script" not in page.lower()
    assert_inert(page, f"{theme} page")


def test_an_unknown_theme_is_refused() -> None:
    with pytest.raises(ValueError, match="unknown theme"):
        document("Mizan console", theme="neon")


def test_the_page_title_is_escaped_like_everything_else() -> None:
    page = document("<script>alert(1)</script>")
    assert "<script" not in page.lower()
    assert "&lt;script&gt;" in page


def test_the_stylesheet_carries_no_markup_character() -> None:
    for css in THEMES.values():
        assert "<" not in css and "&" not in css
