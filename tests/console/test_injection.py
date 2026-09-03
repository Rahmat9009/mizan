"""Finding F-8, closed and pinned.

Every payload here goes through the *real* contracts into the *real* renderer. The assertions are made on a
parsed document rather than on substrings, because the question is not "does the payload appear" - it should,
that is the audit trail - but "did it become markup".
"""

from __future__ import annotations

import pytest

from mizan.console import views
from mizan.console.escaping import neutralise
from mizan.console.render import (
    document,
    fragment,
    render_audit_timeline,
    render_decision_detail,
    render_decision_feed,
    render_kill_switch,
    render_policy_editor,
    render_response_level,
)
from tests.console._helpers import (
    PAYLOADS,
    FakeClient,
    Replayed,
    Verification,
    advisory_only_record,
    analyse,
    assert_inert,
    region,
    region_text,
    tainted_record,
)
from tests.fixtures import POLICY_ID, make_control_event, make_policy

PAYLOAD_IDS = sorted(PAYLOADS)


def _tainted_policy(payload: str):
    return make_policy(
        advisory={"enabled": True, "profile": payload[:256], "authority_ceiling": "reduce_or_reject"}
    )


def _all_views(payload: str) -> dict[str, str]:
    """Every one of the six views, rendered with ``payload`` in each free-text field it can reach."""
    record = tainted_record(payload)
    event = make_control_event(
        sequence=2,
        event_type="kill_switch_activated",
        from_level=None,
        to_level=None,
        actor={"type": "system", "id": payload[:256]},
        trigger_reason_codes=["KILL_SWITCH_ACTIVE"],
        audit_prev_hash="1" * 64,
    )
    policy = make_policy()
    tainted = _tainted_policy(payload)
    client = FakeClient(
        records=[record],
        events=[event],
        verification=Verification(ok=False, length=2, first_bad_sequence=2, detail=payload[:4000]),
        replay=Replayed(decision_id=record.decision_id, detail=payload[:4000]),
        policies={(POLICY_ID, policy.policy_version): policy, (POLICY_ID, "9.9.9"): tainted},
    )
    return {
        "decision_feed": fragment(render_decision_feed(views.feed_page(client))),
        "decision_detail": fragment(
            render_decision_detail(views.decision_detail(client, record.decision_id))
        ),
        "audit_timeline": fragment(render_audit_timeline(views.audit_timeline_view(client))),
        "policy_editor": fragment(
            render_policy_editor(
                views.policy_editor_view(client, POLICY_ID, policy.policy_version, "9.9.9")
            )
        ),
        "kill_switch": fragment(render_kill_switch(views.kill_switch_view(client))),
        "response_level": fragment(
            render_response_level(views.response_level_view(client, policy=policy))
        ),
    }


@pytest.mark.parametrize("payload_name", PAYLOAD_IDS)
def test_no_payload_can_produce_markup_in_any_view(payload_name: str) -> None:
    for view_name, html in _all_views(PAYLOADS[payload_name]).items():
        assert_inert(html, view_name)


@pytest.mark.parametrize("payload_name", PAYLOAD_IDS)
def test_the_payload_is_shown_to_the_reader_as_literal_text(payload_name: str) -> None:
    payload = PAYLOADS[payload_name]
    html = _all_views(payload)["decision_detail"]
    seen = analyse(html).text
    # Invisible format characters are made printable, so compare against the neutralised form.
    assert neutralise(payload) in seen


@pytest.mark.parametrize("payload_name", PAYLOAD_IDS)
def test_a_payload_cannot_forge_a_section(payload_name: str) -> None:
    """The `</section><section data-region="governor">` family cannot add a region to the page."""
    html = _all_views(PAYLOADS[payload_name])["decision_detail"]
    clean = _all_views("harmless reasoning")["decision_detail"]
    assert analyse(html).tags.count("section") == analyse(clean).tags.count("section")


def test_sections_are_never_nested_so_region_extraction_is_exact() -> None:
    html = _all_views(PAYLOADS["script"])["decision_detail"]
    depth = 0
    for chunk in html.split("<section")[1:]:
        depth += 1
        assert depth == 1
        depth -= chunk.count("</section>")


def test_an_advisory_that_claims_a_verdict_cannot_reach_an_enforcement_region() -> None:
    payload = PAYLOADS["verdict_impersonation"]
    record = advisory_only_record(payload, verdict="REJECT")
    client = FakeClient(records=[record], verification=Verification(ok=True, length=1, detail="ok"))
    html = fragment(render_decision_detail(views.decision_detail(client, record.decision_id)))

    # The tainted text is in the advisory region, fenced and labelled.
    advisory = region(html, "advisory")
    assert 'data-authority="advisory"' in advisory
    assert "ADVISORY ONLY - NEVER ENFORCEMENT" in region_text(html, "advisory")
    assert payload in region_text(html, "advisory")

    # And nowhere in any enforcement region.
    for name in ("decision_header", "policy", "risk_evaluation", "governor", "authorization"):
        text = region_text(html, name)
        assert 'data-authority="enforcement"' in region(html, name)
        assert payload not in text, f"advisory text leaked into the {name} region"

    # The enforcement verdict is what the governor decided, not what the payload claimed.
    assert "REJECT" in region_text(html, "governor")
    assert analyse(region(html, "governor")).attr_values("data-verdict") == ["REJECT"]
    assert analyse(html).attr_values("data-verdict") == ["REJECT", "REJECT"]


def test_an_advisory_field_is_never_rendered_with_enforcement_authority() -> None:
    record = tainted_record(PAYLOADS["script"])
    client = FakeClient(records=[record])
    html = fragment(render_decision_detail(views.decision_detail(client, record.decision_id)))
    advisory = region(html, "advisory")
    assert 'data-authority="advisory"' in advisory
    assert 'data-authority="enforcement"' not in advisory
    assert 'data-untrusted="true"' in advisory


def test_free_text_cannot_be_rendered_as_an_enforcement_value() -> None:
    for bad in ("APPROVE<script>", "approve", "AUTHORIZED", ""):
        with pytest.raises(ValueError):
            views.verdict(bad)
    with pytest.raises(ValueError):
        views.reason_code("NOT_A_REAL_CODE")
    with pytest.raises(ValueError):
        views.amount("<script>")
    with pytest.raises(ValueError):
        views.sha("<script>")
    with pytest.raises(ValueError):
        views.timestamp("<script>")


def test_the_page_emits_no_script_and_nothing_that_could_touch_browser_storage() -> None:
    record = tainted_record(PAYLOADS["script"])
    client = FakeClient(records=[record])
    detail = render_decision_detail(views.decision_detail(client, record.decision_id))
    for theme in ("dark", "light"):
        page = document("Mizan console", detail, theme=theme)
        lowered = page.lower()
        for forbidden in (
            "<script",
            "localstorage",
            "sessionstorage",
            "document.cookie",
            "indexeddb",
            "fetch(",
            "xmlhttprequest",
            "javascript:",
        ):
            assert forbidden not in lowered, f"{forbidden} reached the page"
        # Exactly one <style> element, and it is the constant stylesheet, not tainted content.
        assert page.count("<style>") == 1
        assert page.count("</style>") == 1
        style = page.split("<style>")[1].split("</style>")[0]
        assert style == views_stylesheet(theme)


def views_stylesheet(theme: str) -> str:
    from mizan.console.render import THEMES

    return THEMES[theme]


def test_a_tainted_string_carries_a_visible_flag_when_it_looks_adversarial() -> None:
    record = tainted_record(PAYLOADS["script"])
    client = FakeClient(records=[record])
    html = fragment(render_decision_detail(views.decision_detail(client, record.decision_id)))
    advisory_text = region_text(html, "advisory")
    assert "flagged: markup-characters" in advisory_text
    assert "model-authored text" in advisory_text


def test_no_view_ever_says_bare_replay() -> None:
    """The project's vocabulary rule: "decision replay" or "governance replay", never bare "replay"."""
    record = tainted_record("thesis")
    client = FakeClient(
        records=[record],
        verification=Verification(ok=True, length=1, detail="1 link verified"),
        replay=Replayed(decision_id=record.decision_id),
        policies={(POLICY_ID, make_policy().policy_version): make_policy()},
    )
    rendered = {
        "decision_feed": fragment(render_decision_feed(views.feed_page(client))),
        "decision_detail": fragment(
            render_decision_detail(views.decision_detail(client, record.decision_id))
        ),
        "audit_timeline": fragment(render_audit_timeline(views.audit_timeline_view(client))),
        "kill_switch": fragment(render_kill_switch(views.kill_switch_view(client))),
        "response_level": fragment(render_response_level(views.response_level_view(client))),
    }
    for view_name, html in rendered.items():
        text = analyse(html).text.lower()
        index = text.find("replay")
        while index != -1:
            prefix = text[max(0, index - 20) : index]
            assert "decision " in prefix or "governance " in prefix, f"bare 'replay' in {view_name}"
            index = text.find("replay", index + 1)
