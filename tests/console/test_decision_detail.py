"""The decision detail: the whole reasoning chain, and the not-found state that reveals nothing."""

from __future__ import annotations

from typing import Any

from mizan.console import views
from mizan.console.render import fragment, render_decision_detail
from tests.console._helpers import (
    FakeClient,
    Replayed,
    advisory_only_record,
    analyse,
    assert_inert,
    region,
    region_text,
    tainted_record,
)
from tests.fixtures import (
    TENANT_B,
    make_checks,
    make_context,
    make_decision,
    make_decision_record,
    make_evaluation,
    make_institutional_context,
    make_institutional_policy,
    make_policy,
    make_proposal,
)


def _detail_html(record: Any, **client_kwargs: Any) -> str:
    client = FakeClient(records=[record], **client_kwargs)
    return fragment(render_decision_detail(views.decision_detail(client, record.decision_id)))


def _record_for_tenant(tenant_id: str) -> Any:
    """A complete, valid record belonging to ``tenant_id``."""
    policy = make_policy(tenant_id=tenant_id)
    proposal = make_proposal()
    context = make_context(policy=policy, tenant_id=tenant_id)
    evaluation = make_evaluation(proposal=proposal, context=context, policy_snapshot=policy)
    decision = make_decision(proposal=proposal, evaluation=evaluation)
    return make_decision_record(
        policy_snapshot=policy,
        proposal=proposal,
        risk_context=context,
        risk_evaluation=evaluation,
        governor_decision=decision,
    )


def test_the_case_file_carries_every_link_of_the_chain() -> None:
    record = tainted_record("thesis")
    html = _detail_html(record, replay=Replayed(decision_id=record.decision_id))
    assert_inert(html, "decision detail")
    for name in (
        "decision_header",
        "policy",
        "risk_evaluation",
        "state",
        "governor",
        "advisory",
        "authorization",
        "execution",
        "decision_replay",
        "agent_reasoning",
    ):
        assert f'data-region="{name}"' in html, f"the {name} section is missing"


def test_the_policy_version_and_hash_are_both_shown() -> None:
    record = tainted_record("thesis")
    detail = views.decision_detail(FakeClient(records=[record]), record.decision_id)
    assert detail["policy"]["version"] == record.policy.version
    assert detail["policy"]["hash"] == record.policy.hash
    assert record.policy.hash in region_text(_detail_html(record), "policy")


def test_every_check_is_listed_with_its_threshold_actual_and_distance() -> None:
    record = tainted_record("thesis")
    detail = views.decision_detail(FakeClient(records=[record]), record.decision_id)
    checks = detail["risk"]["checks"]
    assert {row["check_id"] for row in checks} == {
        check.check_id for check in record.risk_evaluation.checks
    }
    for row in checks:
        assert set(row) >= {"threshold", "actual", "distance", "outcome", "severity"}


def test_the_distance_is_exact_decimal_arithmetic() -> None:
    policy = make_policy()
    proposal = make_proposal()
    context = make_context(policy=policy)
    checks = make_checks(
        policy,
        options_delta_limit={"threshold": "500.10", "actual": "840.25", "detail": "projected delta"},
    )
    evaluation = make_evaluation(
        proposal=proposal, context=context, policy_snapshot=policy, checks=checks
    )
    decision = make_decision(proposal=proposal, evaluation=evaluation)
    record = make_decision_record(
        policy_snapshot=policy,
        proposal=proposal,
        risk_context=context,
        risk_evaluation=evaluation,
        governor_decision=decision,
    )
    detail = views.decision_detail(FakeClient(records=[record]), record.decision_id)
    row = next(r for r in detail["risk"]["checks"] if r["check_id"] == "options_delta_limit")
    assert row["distance"] == "340.15"
    assert isinstance(row["distance"], str)


def test_a_check_without_a_threshold_does_not_get_an_invented_distance() -> None:
    record = tainted_record("thesis")
    detail = views.decision_detail(FakeClient(records=[record]), record.decision_id)
    row = next(r for r in detail["risk"]["checks"] if r["check_id"] == "restricted_symbol")
    assert row["threshold"] is None and row["distance"] is None


def test_an_info_check_is_reported_as_not_evaluated_never_as_a_pass() -> None:
    record = tainted_record("thesis")
    detail = views.decision_detail(FakeClient(records=[record]), record.decision_id)
    info_rows = [row for row in detail["risk"]["checks"] if row["severity"] == "info"]
    assert info_rows, "the default policy leaves checks unevaluated; the fixture should surface them"
    assert all(row["outcome"] == "NOT EVALUATED" for row in info_rows)
    text = region_text(_detail_html(record), "risk_evaluation")
    assert "NOT EVALUATED" in text
    assert "It is not evidence that a control passed." in text


def test_the_path_and_aggregate_snapshots_are_rendered_when_the_context_carries_them() -> None:
    policy = make_institutional_policy()
    context = make_institutional_context()
    proposal = make_proposal()
    evaluation = make_evaluation(proposal=proposal, context=context, policy_snapshot=policy)
    decision = make_decision(proposal=proposal, evaluation=evaluation)
    record = make_decision_record(
        policy_snapshot=policy,
        proposal=proposal,
        risk_context=context,
        risk_evaluation=evaluation,
        governor_decision=decision,
    )
    detail = views.decision_detail(FakeClient(records=[record]), record.decision_id)
    assert detail["path_state"] is not None
    assert detail["aggregate_state"] is not None
    text = region_text(_detail_html(record), "state")
    assert "Path state" in text
    assert "Aggregate state" in text
    assert "graduated-response level" in text


def test_the_advisory_is_marked_advisory_only_and_never_enforcement() -> None:
    record = advisory_only_record("The structure looks acceptable.", verdict="APPROVE")
    html = _detail_html(record)
    text = region_text(html, "advisory")
    assert 'data-authority="advisory"' in region(html, "advisory")
    assert views.ADVISORY_BANNER in text
    assert "no authority" in text
    assert "reduce_or_reject" in text


def test_the_authorization_and_its_binding_are_shown() -> None:
    record = tainted_record("thesis")
    text = region_text(_detail_html(record), "authorization")
    assert record.authorization.idempotency_key in text
    assert record.authorization.bound_state.policy_hash in text
    assert "the execution gate refuses to act on it" in text


def test_the_execution_result_and_its_revalidation_are_shown() -> None:
    record = tainted_record("thesis")
    text = region_text(_detail_html(record), "execution")
    assert record.execution.status in text
    assert "revalidation performed" in text
    assert "kill switch checked at" in text


def test_the_decision_replay_detail_is_surfaced_verbatim() -> None:
    record = tainted_record("thesis")
    detail_text = "Engine drift: recorded 0.1.0, running 0.2.0. Verdicts match, but not comparably."
    replayed = Replayed(
        decision_id=record.decision_id,
        detail=detail_text,
        engine_version_matches=False,
        recorded_engine_version="0.1.0",
        running_engine_version="0.2.0",
    )
    text = region_text(_detail_html(record, replay=replayed), "decision_replay")
    assert detail_text in text
    assert "Decision replay" in text


def test_the_word_replay_is_never_used_bare_in_user_facing_text() -> None:
    record = tainted_record("thesis")
    html = _detail_html(record, replay=Replayed(decision_id=record.decision_id))
    text = analyse(html).text.lower()
    index = text.find("replay")
    while index != -1:
        prefix = text[max(0, index - 20) : index]
        assert "decision " in prefix or "governance " in prefix, f"bare 'replay' at {index}"
        index = text.find("replay", index + 1)


def test_no_return_or_performance_figure_is_rendered() -> None:
    record = tainted_record("thesis")
    html = _detail_html(record).lower()
    for banned in ("daily_pnl", "realized_expectancy", "realized_hit_rate", "claimed_confidence_mean"):
        assert banned not in html


def test_an_unknown_id_and_another_tenants_id_render_the_identical_page() -> None:
    mine = _record_for_tenant("tenant-a")
    theirs = _record_for_tenant(TENANT_B)
    client = FakeClient(records=[mine])
    unknown_id = "01a00000-0000-7000-8000-000000000000"

    unknown = fragment(render_decision_detail(views.decision_detail(client, unknown_id)))
    other = fragment(render_decision_detail(views.decision_detail(client, theirs.decision_id)))

    assert unknown == other
    assert views.NOT_FOUND_MESSAGE in unknown
    assert theirs.decision_id not in other
    assert unknown_id not in unknown


def test_the_not_found_page_echoes_nothing_the_caller_supplied() -> None:
    probe = "01a00000-0000-7000-8000-00000000dead"
    html = fragment(render_decision_detail(views.decision_detail(FakeClient(), probe)))
    assert probe not in html
    assert_inert(html, "not found")


def _keys(value, found=None):
    found = set() if found is None else found
    if isinstance(value, dict):
        for key, item in value.items():
            found.add(key)
            _keys(item, found)
    elif isinstance(value, list):
        for item in value:
            _keys(item, found)
    return found


def test_no_view_model_carries_a_performance_field() -> None:
    record = tainted_record("thesis")
    client = FakeClient(records=[record], replay=Replayed(decision_id=record.decision_id))
    models = [
        views.decision_detail(client, record.decision_id),
        views.feed_page(client),
        views.audit_timeline_view(client),
        views.kill_switch_view(client),
        views.response_level_view(client),
    ]
    for model in models:
        leaked = _keys(model) & views.PERFORMANCE_FIELDS
        assert not leaked, f"a performance field reached a view model: {sorted(leaked)}"
