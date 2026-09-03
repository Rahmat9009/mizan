"""The audit timeline: one chain per tenant, in sequence order, with the broken link named."""

from __future__ import annotations

from mizan.console import views
from mizan.console.render import fragment, render_audit_timeline
from tests.console._helpers import FakeClient, Verification, assert_inert, region_text, tainted_record
from tests.fixtures import make_control_event


def _chain() -> FakeClient:
    decisions = [tainted_record("thesis", sequence=seq) for seq in (1, 3)]
    events = [
        make_control_event(sequence=2, audit_prev_hash="1" * 64),
        make_control_event(
            sequence=4,
            from_level=1,
            to_level=0,
            actor={"type": "human", "id": "risk.officer@example.test"},
            audit_prev_hash="2" * 64,
        ),
    ]
    return FakeClient(
        records=decisions,
        events=events,
        verification=Verification(ok=True, length=4, detail="4 links verified"),
    )


def test_decisions_and_control_events_share_one_ordered_timeline() -> None:
    entries = views.audit_timeline(_chain())
    assert [entry["sequence"] for entry in entries] == [1, 2, 3, 4]
    assert [entry["kind"] for entry in entries] == [
        "decision",
        "control_event",
        "decision",
        "control_event",
    ]


def test_the_timeline_can_be_narrowed_to_one_decision() -> None:
    client = _chain()
    target = client.records[0]
    entries = views.audit_timeline(client, target.decision_id)
    assert [entry["id"] for entry in entries] == [target.decision_id]


def test_a_verified_chain_says_how_many_links_it_checked() -> None:
    status = views.chain_status(_chain())
    assert status["ok"] is True
    assert status["headline"] == "Chain verified: 4 links, every hash re-derived."
    html = fragment(render_audit_timeline(views.audit_timeline_view(_chain())))
    assert_inert(html, "audit timeline")
    assert "Chain verified: 4 links" in region_text(html, "audit_timeline")


def test_a_broken_chain_names_the_link_that_failed() -> None:
    client = _chain()
    client.verification = Verification(
        ok=False,
        length=4,
        first_bad_sequence=3,
        detail="record 3 content does not match its audit_hash (recomputed a1b2..., stored c3d4...)",
    )
    status = views.chain_status(client)
    assert status["first_bad_sequence"] == 3
    assert "Link 3 is the first that does not verify" in status["headline"]
    assert "links 1 to 2 verify" in status["headline"]

    entries = views.audit_timeline(client)
    by_sequence = {entry["sequence"]: entry["chain"] for entry in entries}
    assert by_sequence == {1: "UNVERIFIED", 2: "UNVERIFIED", 3: "BROKEN HERE", 4: "AFTER THE BREAK"}

    text = region_text(fragment(render_audit_timeline(views.audit_timeline_view(client))), "audit_timeline")
    assert "Link 3 is the first that does not verify" in text
    assert "BROKEN HERE" in text
    # The verifier's own words are shown, not paraphrased.
    assert "recomputed a1b2..., stored c3d4..." in text


def test_a_client_that_cannot_verify_says_so_rather_than_claiming_the_chain_is_fine() -> None:
    client = FakeClient(records=[tainted_record("thesis")], verification=None)
    status = views.chain_status(client)
    assert status["ok"] is None
    assert status["available"] is False
    assert "not available" in status["headline"]


def test_an_escalation_and_a_de_escalation_are_distinguished_with_their_actor() -> None:
    entries = views.audit_timeline(_chain())
    escalation = entries[1]
    de_escalation = entries[3]
    assert escalation["direction"] == "escalation"
    assert escalation["actor_type"] == "system"
    assert escalation["human_required"] is False
    assert de_escalation["direction"] == "de-escalation"
    assert de_escalation["actor_type"] == "human"
    assert de_escalation["human_required"] is True

    text = region_text(fragment(render_audit_timeline(views.audit_timeline_view(_chain()))), "audit_timeline")
    assert "[system]" in text and "[human]" in text


def test_the_timeline_falls_back_to_separate_listings_when_the_client_has_no_merged_chain() -> None:
    inner = _chain()

    class NoMergedChain:
        """A client that lists decisions and control events but cannot merge them itself."""

        list_decisions = inner.list_decisions
        list_control_events = inner.list_control_events
        verify_chain = inner.verify_chain

    entries = views.audit_timeline(NoMergedChain())
    assert [entry["sequence"] for entry in entries] == [1, 2, 3, 4]


def test_an_empty_chain_renders_a_statement_not_an_empty_table() -> None:
    client = FakeClient(verification=Verification(ok=True, length=0, detail="empty chain"))
    html = fragment(render_audit_timeline(views.audit_timeline_view(client)))
    assert "No chain entries are visible to this tenant." in region_text(html, "audit_timeline")
