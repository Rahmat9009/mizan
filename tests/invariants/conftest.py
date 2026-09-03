"""Terminal reporting for the invariant suite - no fixtures, by design.

This conftest defines NO fixtures: an implementer must satisfy every invariant through the public API in
docs/API-SURFACE.md, and nothing here can be overridden to bypass it. Its only job is the INVARIANT STATUS
section printed at the end of every run, which the Orchestrator reads at each checkpoint:

    PASS          every test in the invariant's file passed
    PENDING-IMPL  every failure is an L0 stub raising NotImplementedError("L<n> implements this in Sprint ...")
    BLOCKING      any other failure (a real violation, a spec-conformance bug, or a broken test)
    NOT-RUN       the file was not collected or its tests did not run (e.g. stopped by -x)
"""
from __future__ import annotations

import re
from collections import defaultdict

import pytest

PENDING_MARKER = "implements this in Sprint"

# (number, invariant, hard rule)
INVARIANTS: tuple[tuple[int, str, str], ...] = (
    (1, "llm_cannot_increase_order_size", "E1"),
    (2, "llm_cannot_overturn_hard_rejection", "E1"),
    (3, "missing_price_blocks", "E2"),
    (4, "missing_buying_power_blocks", "E2"),
    (5, "missing_portfolio_state_blocks", "E2"),
    (6, "expired_authorization_blocks", "E6"),
    (7, "kill_switch_blocks_at_mutation_boundary", "E4"),
    (8, "audit_record_cannot_be_modified", "A2"),
    (9, "audit_record_cannot_be_deleted", "A2"),
    (10, "hash_chain_verifies", "A1/A5"),
    (11, "replay_verdict_is_identical", "A1"),
    (12, "cross_tenant_access_is_impossible", "B3"),
    (13, "engine_operates_with_llm_offline", "E8"),
    (14, "toctou_revalidation_occurs", "E9/E5"),
    (15, "no_binary_float_in_decision_path", "A6"),
    (16, "no_live_trading_path_exists", "B1"),
    (17, "reasoning_field_never_reaches_enforcement", "E1/§0"),
    (18, "semantic_layer_disabled_produces_identical_verdict", "Addendum-1 §D"),
    (19, "aggregate_check_can_override_per_agent_pass", "Addendum-2/R-AGG"),
    (20, "size_scales_down_as_drawdown_deepens", "Addendum-2/R-ERG-2"),
    (21, "authorization_bound_to_state_hash", "Addendum-2/E6"),
    (22, "authorization_invalid_after_state_change", "Addendum-2/E6"),
    (23, "no_llm_in_deterministic_path", "Addendum-2/E8"),
    (24, "advisory_reduce_to_zero_is_rejected_as_invalid", "ESC-1/E1"),
    (25, "every_enabled_check_can_actually_fail", "ESC-4/E2"),
    (26, "check_passed_implies_evidence_present", "E2/ESC-4"),
)

_FILE_RE = re.compile(r"test_(\d{2})_[a-z_]+\.py")

# nodeid -> (invariant number, outcome, short reason); outcome in {"passed", "pending", "blocking", "skipped"}
_outcomes: dict[str, tuple[int, str, str]] = {}
# invariant number -> list of (nodeid, outcome, short reason) for collection errors
_collection_errors: dict[int, list[tuple[str, str, str]]] = defaultdict(list)


def _invariant_number(nodeid_or_path: str) -> int | None:
    match = _FILE_RE.search(nodeid_or_path.replace("\\", "/"))
    return int(match.group(1)) if match else None


def _exception_chain(exc: BaseException):
    seen: set[int] = set()
    stack = [exc]
    while stack:
        current = stack.pop()
        if current is None or id(current) in seen:
            continue
        seen.add(id(current))
        yield current
        if isinstance(current, BaseExceptionGroup):
            stack.extend(current.exceptions)
        stack.append(current.__cause__)
        stack.append(current.__context__)


def _is_pending(exc: BaseException | None, text: str) -> bool:
    if exc is not None:
        for e in _exception_chain(exc):
            if isinstance(e, NotImplementedError) and PENDING_MARKER in str(e):
                return True
        return False
    # Fallback for serialized reports (no live exception object): look at the rendered traceback.
    return "NotImplementedError" in text and PENDING_MARKER in text


def _short_reason(exc: BaseException | None, text: str) -> str:
    if exc is not None:
        first = next(iter(_exception_chain(exc)))
        msg = str(first).strip().splitlines()[0] if str(first).strip() else ""
        return f"{type(first).__name__}: {msg}"[:160]
    lines = [ln for ln in text.strip().splitlines() if ln.strip()]
    return (lines[-1] if lines else "failure")[:160]


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_makereport(item, call):
    outcome = yield
    report = outcome.get_result()
    number = _invariant_number(report.nodeid)
    if number is None:
        return
    excinfo = call.excinfo
    exc = excinfo.value if excinfo is not None else None
    text = str(report.longrepr) if report.longrepr is not None else ""
    if report.when == "call":
        if report.passed:
            _outcomes.setdefault(report.nodeid, (number, "passed", ""))
        elif report.skipped:
            _outcomes[report.nodeid] = (number, "skipped", _short_reason(exc, text))
        else:
            status = "pending" if _is_pending(exc, text) else "blocking"
            _outcomes[report.nodeid] = (number, status, _short_reason(exc, text))
    elif report.failed:  # setup/teardown failures
        status = "pending" if _is_pending(exc, text) else "blocking"
        _outcomes[report.nodeid] = (number, status, _short_reason(exc, text))
    elif report.skipped and report.when == "setup":
        _outcomes[report.nodeid] = (number, "skipped", _short_reason(exc, text))


def pytest_collectreport(report):
    if not report.failed:
        return
    number = _invariant_number(str(report.nodeid) or str(getattr(report, "fspath", "")))
    if number is None:
        return
    text = str(report.longrepr)
    status = "pending" if _is_pending(None, text) else "blocking"
    _collection_errors[number].append((report.nodeid, status, _short_reason(None, text)))


def _status_for(number: int) -> tuple[str, str]:
    rows = [v for v in _outcomes.values() if v[0] == number]
    errors = _collection_errors.get(number, [])
    if not rows and not errors:
        return "NOT-RUN", "no tests collected or executed"
    blocking = [r for r in rows if r[1] == "blocking"] + [e for e in errors if e[1] == "blocking"]
    pending = [r for r in rows if r[1] == "pending"] + [e for e in errors if e[1] == "pending"]
    skipped = [r for r in rows if r[1] == "skipped"]
    passed = [r for r in rows if r[1] == "passed"]
    total = len(rows)
    if blocking:
        return "BLOCKING", f"{len(passed)}/{total} passed; first: {blocking[0][2]}"
    if pending:
        return "PENDING-IMPL", f"{len(passed)}/{total} passed; {len(pending)} awaiting: {pending[0][2]}"
    if skipped:
        return "BLOCKING", f"{len(skipped)} test(s) skipped - invariants may not be skipped"
    return "PASS", f"{len(passed)}/{total} passed"


def pytest_terminal_summary(terminalreporter, exitstatus, config):
    if not _outcomes and not _collection_errors:
        return
    tr = terminalreporter
    tr.write_sep("=", "INVARIANT STATUS")
    totals = defaultdict(int)
    for number, name, rule in INVARIANTS:
        status, detail = _status_for(number)
        totals[status] += 1
        tr.write_line(f"INV-{number:02d} {status:<13} {name:<52} [{rule}]  {detail}")
    tr.write_line(
        "INVARIANT TOTALS: "
        f"PASS={totals['PASS']} PENDING-IMPL={totals['PENDING-IMPL']} "
        f"BLOCKING={totals['BLOCKING']} NOT-RUN={totals['NOT-RUN']} (of {len(INVARIANTS)})"
    )
