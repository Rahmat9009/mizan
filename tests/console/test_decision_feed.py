"""The decision feed: newest first, cursor-paged, and financial rather than model-shaped."""

from __future__ import annotations

from mizan.console import views
from mizan.console.render import fragment, render_decision_feed
from tests.console._helpers import FakeClient, analyse, assert_inert, region_text, tainted_record


def _records(count: int) -> list:
    return [tainted_record("thesis text", sequence=index + 1) for index in range(count)]


def test_the_feed_is_newest_first() -> None:
    client = FakeClient(records=_records(5))
    rows = views.decision_feed(client)
    assert [row["sequence"] for row in rows] == [5, 4, 3, 2, 1]


def test_cursor_paging_uses_before_sequence_and_stops_at_the_end_of_the_chain() -> None:
    client = FakeClient(records=_records(5))

    first = views.feed_page(client, limit=2)
    assert [row["sequence"] for row in first["rows"]] == [5, 4]
    assert first["has_more"] is True
    assert first["next_before_sequence"] == 4

    second = views.feed_page(client, limit=2, before_sequence=first["next_before_sequence"])
    assert [row["sequence"] for row in second["rows"]] == [3, 2]

    last = views.feed_page(client, limit=2, before_sequence=second["next_before_sequence"])
    assert [row["sequence"] for row in last["rows"]] == [1]
    assert last["has_more"] is False
    assert last["next_before_sequence"] is None


def test_the_cursor_is_passed_to_the_client_verbatim() -> None:
    client = FakeClient(records=_records(3))
    views.decision_feed(client, limit=7, before_sequence=3)
    assert ("list_decisions", (7, 3)) in client.calls


def test_a_row_carries_the_facts_an_operator_scans_for() -> None:
    record = tainted_record("thesis text")
    client = FakeClient(records=[record])
    row = views.decision_feed(client)[0]
    assert row["verdict"] == record.verdict
    assert row["symbol"].text == record.proposal.symbol
    assert row["policy_version"] == record.policy.version
    assert row["policy_hash"] == record.policy.hash
    assert row["decision_timestamp"] == record.decision_timestamp
    assert row["original_quantity"] == record.original.total_quantity
    assert row["authorized_quantity"] == record.authorized.total_quantity


def test_a_rejected_row_shows_its_reason_codes() -> None:
    record = tainted_record("thesis text", verdict="REJECT")
    client = FakeClient(records=[record])
    row = views.decision_feed(client)[0]
    codes = [code["code"] for code in row["reason_codes"]]
    assert "RESTRICTED_SYMBOL" in codes
    html = fragment(render_decision_feed(views.feed_page(client)))
    assert "RESTRICTED_SYMBOL" in region_text(html, "decision_feed")


def test_the_feed_renders_the_verdict_from_the_closed_vocabulary_only() -> None:
    client = FakeClient(records=_records(3))
    html = fragment(render_decision_feed(views.feed_page(client)))
    assert_inert(html, "decision feed")
    assert set(analyse(html).attr_values("data-verdict")) <= {"APPROVE", "REDUCE", "REJECT"}


def test_an_empty_feed_says_so_rather_than_rendering_an_empty_table() -> None:
    html = fragment(render_decision_feed(views.feed_page(FakeClient())))
    assert "No decisions are visible to this tenant." in region_text(html, "decision_feed")
    assert analyse(html).tags.count("table") == 0


def test_the_feed_never_shows_a_return_or_performance_figure() -> None:
    client = FakeClient(records=_records(3))
    html = fragment(render_decision_feed(views.feed_page(client)))
    lowered = html.lower()
    for banned in ("pnl", "p&amp;l", "return", "expectancy", "hit rate", "win rate"):
        assert banned not in lowered
