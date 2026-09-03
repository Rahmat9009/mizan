"""The point of the lane: the signal is on the record and it decides nothing.

A volatility signal that a governance system merely *stores* is easy to believe harmless. This file
refuses to take that on trust and proves it three ways, over a battery of decisions with a mix of
verdicts (APPROVE, REDUCE and REJECT - a shadow proof over one repeated APPROVE would prove very
little, because the interesting case is a decision that was close to going the other way):

1. **The flag changes nothing.** The same inputs, the same provider, run with ``SIGNAL_SHADOW=1`` and
   with ``SIGNAL_SHADOW=0``: identical verdict sequence, identical verdict hashes, identical authorized
   quantities and identical reason codes. The only difference anywhere in the decision is the advisory
   ``reasoning`` text - and that difference is asserted to exist, so a test that passed because the
   signal was silently absent would fail here instead.
2. **The wrapper is transparent.** Wrapping a provider is indistinguishable, verdict-wise, from not
   wrapping it; and installing the wrapper over no provider at all is indistinguishable from having no
   provider at all. The signal cannot even change the outcome by being *present*.
3. **It survives replay.** A recorded decision carrying the signal in its advisory reasoning replays to
   an identical verdict and an identical ``verdict_hash`` - which is what makes the reading auditable
   rather than merely logged.

Why ``reasoning`` is the right field, restated as a test rather than as a claim: ``verdict_hash`` is
computed from the verdict, the reason codes, the authorized quantity, the authorized legs and the
evaluation id (``mizan.contracts.canonical.verdict_hash_for``), and invariant 17 forbids every module
in the enforcement path from reading ``reasoning`` at all. So the text is recorded, replayable, and
structurally incapable of reaching a check.
"""

from __future__ import annotations

import pytest

from mizan import advisory as advisory_module
from mizan import governor, risk
from mizan.advisory import OfflineAdvisoryProvider
from mizan.contracts.canonical import canonical_json
from mizan.replay import replay
from mizan.signal import SHADOW_ENV, VolSignalAdvisoryProvider, shadow_enabled
from tests.fixtures import make_decision_record
from tests.signal.conftest import decision_battery


def _run(battery, provider, *, timeout_seconds: int = 10):
    """Run the engine over the battery through the real advisory seam. Returns one row per decision."""
    rows = []
    for proposal, context, policy in battery:
        evaluation = risk.evaluate(proposal, context, policy)
        opinion = advisory_module.get_advisory(
            provider, proposal, evaluation, context, policy, timeout_seconds=timeout_seconds
        )
        decision = governor.govern(proposal, evaluation, policy, opinion, context=context)
        rows.append(
            {
                "verdict": decision.verdict,
                "verdict_hash": decision.verdict_hash,
                "authorized": decision.authorized.total_quantity,
                "reason_codes": sorted(str(getattr(c, "value", c)) for c in decision.reason_codes),
                "reasoning": (decision.llm_advisory.reasoning if decision.llm_advisory else ""),
                "decision": decision,
                "evaluation": evaluation,
                "proposal": proposal,
                "context": context,
                "policy": policy,
            }
        )
    return rows


def _verdict_view(rows):
    """Everything about the outcome except the advisory free text - the part that must never move."""
    return [
        {
            "verdict": row["verdict"],
            "verdict_hash": row["verdict_hash"],
            "authorized": row["authorized"],
            "reason_codes": row["reason_codes"],
        }
        for row in rows
    ]


@pytest.fixture
def battery():
    rows = decision_battery()
    assert len(rows) >= 5, "a shadow proof needs more than a couple of decisions"
    return rows


# ---------------------------------------------------------------------------------------------------
# The flag is off unless it is explicitly on
# ---------------------------------------------------------------------------------------------------
def test_the_shadow_flag_defaults_to_off(monkeypatch):
    monkeypatch.delenv(SHADOW_ENV, raising=False)
    assert shadow_enabled() is False
    for value in ("0", "false", "no", "off", "", "  ", "maybe", "2"):
        monkeypatch.setenv(SHADOW_ENV, value)
        assert shadow_enabled() is False, value
    for value in ("1", "true", "TRUE", "yes", "on", " on "):
        monkeypatch.setenv(SHADOW_ENV, value)
        assert shadow_enabled() is True, value


# ---------------------------------------------------------------------------------------------------
# 1. SIGNAL_SHADOW=1 and SIGNAL_SHADOW=0 produce the same verdicts
# ---------------------------------------------------------------------------------------------------
def test_the_shadow_signal_changes_no_verdict_and_no_verdict_hash(monkeypatch, battery, signal):
    provider = VolSignalAdvisoryProvider(signal, base=OfflineAdvisoryProvider())

    monkeypatch.setenv(SHADOW_ENV, "0")
    without = _run(battery, provider)

    monkeypatch.setenv(SHADOW_ENV, "1")
    with_signal = _run(battery, provider)

    assert len(with_signal) == len(without) == len(battery)
    assert _verdict_view(with_signal) == _verdict_view(without)
    assert [row["verdict"] for row in with_signal] == [row["verdict"] for row in without]
    assert [row["verdict_hash"] for row in with_signal] == [row["verdict_hash"] for row in without]

    # the battery has to be worth running: a single repeated verdict would prove nothing
    assert len({row["verdict"] for row in without}) >= 2, [row["verdict"] for row in without]

    # and the signal really was present in the shadow arm - otherwise this test passes vacuously
    assert any(signal.realized_vol_rank in row["reasoning"] for row in with_signal)
    assert all(signal.realized_vol_rank not in row["reasoning"] for row in without)
    for shadowed, plain in zip(with_signal, without, strict=True):
        assert shadowed["reasoning"] != plain["reasoning"]


def test_the_shadow_signal_changes_nothing_when_the_advisory_is_cutting_size_either(
    monkeypatch, battery, signal
):
    """The REDUCE row of the arbitration table, where an opinion *is* moving the quantity.

    The deterministic battery yields APPROVE and REJECT; a governor REDUCE only happens when the
    advisory layer cuts below the deterministic cap. That is precisely the case where an extra sentence
    of advisory text would do damage if it were ever read, so it gets its own run.
    """
    base = OfflineAdvisoryProvider(opinion=_reduce_to_one())
    provider = VolSignalAdvisoryProvider(signal, base=base)

    monkeypatch.setenv(SHADOW_ENV, "0")
    without = _run(battery, provider)
    monkeypatch.setenv(SHADOW_ENV, "1")
    with_signal = _run(battery, provider)

    assert "REDUCE" in {row["verdict"] for row in without}, [row["verdict"] for row in without]
    assert _verdict_view(with_signal) == _verdict_view(without)
    assert any(signal.realized_vol_rank in row["reasoning"] for row in with_signal)


def _reduce_to_one():
    from mizan.contracts import AdvisoryOpinion

    return AdvisoryOpinion(
        profile="shadow-proof-scripted",
        invoked=True,
        available=True,
        recommendation="REDUCE",
        recommended_quantity="1",
        reasoning="scripted downward opinion, so the REDUCE row of the table is exercised",
        authority_ceiling="reduce_or_reject",
        provider_ref=None,
        raw_hash=None,
    )


def test_the_only_difference_in_the_whole_decision_is_the_advisory_text(monkeypatch, battery, signal):
    """Canonical JSON of every decision, with decision_id and the advisory reasoning removed, is equal."""
    provider = VolSignalAdvisoryProvider(signal, base=OfflineAdvisoryProvider())

    monkeypatch.setenv(SHADOW_ENV, "0")
    without = _run(battery, provider)
    monkeypatch.setenv(SHADOW_ENV, "1")
    with_signal = _run(battery, provider)

    for shadowed, plain in zip(with_signal, without, strict=True):
        assert _stripped(shadowed["decision"]) == _stripped(plain["decision"])


def _stripped(decision) -> str:
    payload = decision.model_dump(mode="json")
    payload.pop("decision_id", None)
    if payload.get("llm_advisory"):
        payload["llm_advisory"].pop("reasoning", None)
    return canonical_json(payload)


# ---------------------------------------------------------------------------------------------------
# 2. The wrapper is verdict-transparent over whatever it wraps
# ---------------------------------------------------------------------------------------------------
def test_wrapping_a_provider_is_indistinguishable_from_not_wrapping_it(monkeypatch, battery, signal):
    monkeypatch.setenv(SHADOW_ENV, "1")
    base = OfflineAdvisoryProvider()
    assert _verdict_view(_run(battery, VolSignalAdvisoryProvider(signal, base=base))) == _verdict_view(
        _run(battery, base)
    )


def test_installing_the_shadow_over_no_provider_is_indistinguishable_from_no_provider(
    monkeypatch, battery, signal
):
    """The strongest form: the signal cannot move a verdict merely by existing in the loop."""
    monkeypatch.setenv(SHADOW_ENV, "1")
    shadowed = _run(battery, VolSignalAdvisoryProvider(signal, base=None))
    none_at_all = _run(battery, None)
    assert _verdict_view(shadowed) == _verdict_view(none_at_all)


def test_a_signal_that_cannot_be_rendered_leaves_the_decision_exactly_as_it_was(
    monkeypatch, battery, signal
):
    """If the signal path fails, the loop must behave as if this package were not installed."""

    class Broken:
        def summary(self) -> str:
            raise RuntimeError("reading blew up")

    monkeypatch.setenv(SHADOW_ENV, "1")
    base = OfflineAdvisoryProvider()
    broken = VolSignalAdvisoryProvider(Broken(), base=base)  # type: ignore[arg-type]
    absent = VolSignalAdvisoryProvider(None, base=base)

    baseline = _run(battery, base)
    assert _verdict_view(_run(battery, broken)) == _verdict_view(baseline)
    assert _verdict_view(_run(battery, absent)) == _verdict_view(baseline)
    assert broken.annotations == 0, "a failed rendering must not be counted as an annotation"


def test_the_wrapper_never_upgrades_a_recommendation(monkeypatch, battery, signal):
    """Downward-only by construction: the wrapper copies the wrapped recommendation, it does not form one."""
    monkeypatch.setenv(SHADOW_ENV, "1")
    base = OfflineAdvisoryProvider()
    wrapper = VolSignalAdvisoryProvider(signal, base=base)
    for proposal, context, policy in battery:
        evaluation = risk.evaluate(proposal, context, policy)
        original = base.advise(proposal, evaluation, context, policy)
        wrapped = wrapper.advise(proposal, evaluation, context, policy)
        assert wrapped.recommendation == original.recommendation
        assert wrapped.recommended_quantity == original.recommended_quantity
        assert wrapped.available == original.available
        assert wrapped.invoked == original.invoked
        assert wrapped.authority_ceiling == original.authority_ceiling
        assert wrapped.reasoning != original.reasoning
        assert original.reasoning in wrapped.reasoning


# ---------------------------------------------------------------------------------------------------
# 3. A record carrying the signal replays to the same verdict
# ---------------------------------------------------------------------------------------------------
def test_a_record_carrying_the_signal_replays_to_an_identical_verdict(monkeypatch, battery, signal):
    monkeypatch.setenv(SHADOW_ENV, "1")
    provider = VolSignalAdvisoryProvider(signal, base=OfflineAdvisoryProvider())
    replayed = 0
    for row in _run(battery, provider):
        record = make_decision_record(
            proposal=row["proposal"],
            risk_context=row["context"],
            risk_evaluation=row["evaluation"],
            governor_decision=row["decision"],
            policy_snapshot=row["policy"],
        )
        assert signal.realized_vol_rank in record.llm_advisory.reasoning
        assert signal.regime in record.llm_advisory.reasoning

        result = replay(record)
        assert result.identical is True, result.detail
        assert result.replayed_verdict == record.verdict
        assert result.replayed_verdict_hash == record.governor_decision.verdict_hash
        replayed += 1
    assert replayed == len(battery)


def test_replaying_with_the_semantic_layer_switched_off_keeps_the_deterministic_part(
    monkeypatch, battery, signal
):
    """Addendum 1 section D: dropping the advisory can only authorize the same or more of the cap."""
    monkeypatch.setenv(SHADOW_ENV, "1")
    provider = VolSignalAdvisoryProvider(signal, base=OfflineAdvisoryProvider())
    for row in _run(battery, provider):
        record = make_decision_record(
            proposal=row["proposal"],
            risk_context=row["context"],
            risk_evaluation=row["evaluation"],
            governor_decision=row["decision"],
            policy_snapshot=row["policy"],
        )
        result = replay(record, advisory=None)
        assert result.mode == "counterfactual"
        assert result.replayed_evaluation.evaluation_id == record.risk_evaluation.evaluation_id


# ---------------------------------------------------------------------------------------------------
# The text itself
# ---------------------------------------------------------------------------------------------------
def test_the_advisory_text_says_what_the_number_is_and_what_it_is_not(signal):
    note = signal.summary()
    assert "realized_vol_rank=" in note
    assert "atr=" in note
    assert f"regime={signal.regime}" in note
    assert "not implied volatility" in note
    assert "no authority" in note


def test_the_annotated_reasoning_stays_within_the_contract_bound(monkeypatch, battery, signal):
    from mizan.contracts.trade_proposal import MAX_REASONING_CHARS

    monkeypatch.setenv(SHADOW_ENV, "1")
    provider = VolSignalAdvisoryProvider(signal, base=OfflineAdvisoryProvider())
    for row in _run(battery, provider):
        opinion = row["decision"].llm_advisory
        if opinion is not None:
            assert len(opinion.reasoning) <= MAX_REASONING_CHARS
