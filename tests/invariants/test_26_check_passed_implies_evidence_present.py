"""Invariant 26 - Hard Rule E2 applied to the checks themselves: a blocking PASS must carry its evidence.

E2 says missing state blocks. Everyone applied that to the *inputs* — a missing price, a missing
portfolio — and nobody applied it to the checks' own output. So a check could report
``passed=True, severity="blocking"`` while carrying no threshold, no observed value, no data source, no
timestamp and no detail. That record is indistinguishable from a check that never ran, and it is written
into a DecisionRecord whose entire purpose is to be believed later, by a regulator or a court.

This is the ESC-4 class seen from the other side. Invariant 25 asks "can this control ever fail?"; this
one asks "when it says it passed, did it actually look at anything?". A control can be alive and still
report a pass that proves nothing.

Pass criterion: across the shared check battery, no enabled check reporting ``passed=True`` at
``blocking`` severity has an empty evidence set. Evidence is any of ``threshold``, ``actual``,
``data_source`` or ``snapshot_ts``, or a non-empty ``detail`` saying what was checked.

Found by this invariant when it was written: ``mizan/risk/__init__.py`` fabricates exactly such a result
whenever a check function returns ``None`` — 17 of 36 checks have that path — and ``restricted_symbol``
and ``restricted_strategy`` took it on every non-restricted proposal, producing 148 evidence-free
blocking passes across the battery. Both now state what they checked against.
"""
from __future__ import annotations

from mizan import risk
from mizan.risk import IMPLEMENTED_CHECKS

from tests.fixtures import make_proposal
from tests.invariants._support import check_battery, context_for, has_evidence, path_and_aggregate_policy


def test_check_passed_implies_evidence_present():
    offenders: dict[str, int] = {}
    for proposal, context, policy in check_battery():
        for check in risk.evaluate(proposal, context, policy).checks:
            if not policy.is_check_enabled(check.check_id):
                continue
            if check.severity != "blocking" or not check.passed:
                continue
            if not has_evidence(check):
                offenders[check.check_id] = offenders.get(check.check_id, 0) + 1
    assert not offenders, (
        f"these blocking checks reported passed=True with no evidence at all: {sorted(offenders)} "
        f"(occurrences: {offenders}). A pass carrying no threshold, no observed value, no source, no "
        "timestamp and no detail is indistinguishable from a check that never ran. E2 applied to the "
        "checks themselves means absent evidence fails closed; it does not read as a pass. If a check "
        "is genuinely not applicable, say so in `detail` - that IS the evidence."
    )


def test_the_battery_actually_produced_blocking_passes():
    """Guard against vacuity: if the battery produced no blocking passes at all, the assertion above
    would hold for the wrong reason and would keep holding after a regression."""
    seen = 0
    for proposal, context, policy in check_battery():
        for check in risk.evaluate(proposal, context, policy).checks:
            if policy.is_check_enabled(check.check_id) and check.severity == "blocking" and check.passed:
                seen += 1
    assert seen > 100, f"only {seen} blocking passes were observed; the battery is not exercising checks"


def test_a_returned_none_is_never_reported_as_a_bare_blocking_pass():
    """The mechanism, pinned directly. A check function returning None means 'not applicable here'.
    The engine must not render that as a blocking pass with an empty evidence set, because the record
    cannot then distinguish 'checked and fine' from 'did not run'."""
    policy = path_and_aggregate_policy()
    evaluation = risk.evaluate(make_proposal(), context_for(policy), policy)
    bare = [
        c.check_id
        for c in evaluation.checks
        if c.check_id in IMPLEMENTED_CHECKS
        and policy.is_check_enabled(c.check_id)
        and c.severity == "blocking"
        and c.passed
        and not has_evidence(c)
    ]
    assert not bare, (
        f"{bare} produced a blocking pass with no evidence on the default happy path. Every check that "
        "returns None on its pass path must instead return ok(...) with what it compared against."
    )
