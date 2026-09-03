"""The kill switch and the graduated-response level, and the asymmetry the UI has to make obvious."""

from __future__ import annotations

from mizan.console import views
from mizan.console.render import fragment, render_kill_switch, render_response_level
from tests.console._helpers import FakeClient, analyse, assert_inert, region, region_text
from tests.fixtures import make_control_event, make_institutional_policy, make_policy


def _events(*events):
    return FakeClient(events=list(events))


def test_the_kill_switch_state_comes_from_the_tenant_chain() -> None:
    client = _events(
        make_control_event(
            sequence=1,
            event_type="kill_switch_activated",
            from_level=None,
            to_level=None,
            trigger_reason_codes=["KILL_SWITCH_ACTIVE"],
        )
    )
    view = views.kill_switch_view(client)
    assert view["engaged"] is True
    assert view["source"] == "control events"
    assert "ENGAGED" in view["state_label"]


def test_a_later_deactivation_wins() -> None:
    client = _events(
        make_control_event(
            sequence=1,
            event_type="kill_switch_activated",
            from_level=None,
            to_level=None,
            trigger_reason_codes=["KILL_SWITCH_ACTIVE"],
        ),
        make_control_event(
            sequence=2,
            event_type="kill_switch_deactivated",
            from_level=None,
            to_level=None,
            actor={"type": "human", "id": "risk.officer@example.test"},
            trigger_reason_codes=[],
            audit_prev_hash="1" * 64,
        ),
    )
    view = views.kill_switch_view(client)
    assert view["engaged"] is False
    assert view["last_event"]["actor_type"] == "human"


def test_an_unknown_kill_switch_state_is_shown_as_unknown_not_as_safe() -> None:
    view = views.kill_switch_view(FakeClient())
    assert view["engaged"] is None
    assert "UNKNOWN" in view["state_label"]
    html = fragment(render_kill_switch(view))
    assert analyse(html).attr_values("data-kill-switch") == ["unknown"]
    assert "verdict-APPROVE" not in region(html, "kill_switch")


def test_engaging_is_one_click_and_disengaging_demands_a_named_human() -> None:
    html = fragment(render_kill_switch(views.kill_switch_view(FakeClient())))
    assert_inert(html, "kill switch")
    found = analyse(region(html, "kill_switch"))
    assert found.tags.count("form") == 2
    assert found.attr_values("data-requires-human") == ["true"]
    assert "actor_id" in found.attr_values("name")
    text = region_text(html, "kill_switch")
    assert "Disengaging requires a named human actor" in text
    assert "refuses a kill_switch_deactivated" in text


def test_the_kill_switch_forms_post_to_a_relative_control_url_only() -> None:
    html = fragment(render_kill_switch(views.kill_switch_view(FakeClient())))
    assert analyse(html).attr_values("action") == [
        "/v1/control/kill-switch",
        "/v1/control/kill-switch",
    ]


def test_the_response_level_ladder_covers_zero_to_five_and_marks_the_active_one() -> None:
    client = _events(
        make_control_event(sequence=1, from_level=0, to_level=2, trigger_reason_codes=[])
    )
    view = views.response_level_view(client, policy=make_institutional_policy())
    assert view["level"] == 2
    assert [step["level"] for step in view["levels"]] == [0, 1, 2, 3, 4, 5]
    assert [step["active"] for step in view["levels"]] == [False, False, True, False, False, False]

    html = fragment(render_response_level(view))
    assert_inert(html, "response level")
    found = analyse(region(html, "response_level"))
    assert found.attr_values("data-active") == ["true"]


def test_escalation_is_automatic_and_de_escalation_needs_a_human() -> None:
    view = views.response_level_view(FakeClient(), policy=make_institutional_policy())
    assert view["escalation"] == "automatic"
    assert view["de_escalation"] == "requires a human"
    assert view["de_escalate"]["requires_human"] is True
    assert view["de_escalate"]["requires_actor_id"] is True

    text = region_text(fragment(render_response_level(view)), "response_level")
    assert "Escalation is automatic" in text
    assert "requires a named human actor" in text
    assert "Human actor id (required)" in text


def test_the_ladder_shows_each_level_trigger_and_size_multiplier() -> None:
    policy = make_institutional_policy()
    view = views.response_level_view(FakeClient(), policy=policy)
    assert view["ladder"], "the institutional policy configures a response ladder"
    assert {step["level"] for step in view["ladder"]} == {
        spec.level for spec in policy.response_ladder.levels
    }
    text = region_text(fragment(render_response_level(view)), "response_level")
    assert "size multiplier" in text
    assert "new risk allowed" in text


def test_a_policy_without_a_ladder_says_so_rather_than_showing_an_empty_table() -> None:
    view = views.response_level_view(FakeClient(), policy=make_policy())
    text = region_text(fragment(render_response_level(view)), "response_level")
    assert "No response ladder is configured" in text


def test_the_de_escalation_note_is_the_same_wording_everywhere() -> None:
    kill_switch = views.kill_switch_view(FakeClient())
    response_level = views.response_level_view(FakeClient(), policy=make_policy())
    assert kill_switch["asymmetry"] == views.DE_ESCALATION_NOTE
    assert response_level["asymmetry"] == views.DE_ESCALATION_NOTE
