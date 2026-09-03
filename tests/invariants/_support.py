"""Shared helpers for the invariant suite.

Everything here goes *through* the public API in docs/API-SURFACE.md (+ Addendum 1). The test doubles
implement the section-3 Protocols (BrokerAdapter, ContextProvider, KillSwitch, AdvisoryProvider) verbatim so
that lanes can implement their modules freely; nothing in this file bypasses the gate, the ledger or the
contracts, and nothing here is a pytest fixture.
"""
from __future__ import annotations

import ast
import json
from collections.abc import Iterable, Sequence
from datetime import datetime
from decimal import Decimal
from enum import Enum
from pathlib import Path
from typing import Any

from mizan.contracts import (
    AdvisoryOpinion,
    MarketSnapshot,
    Policy,
    PortfolioSnapshot,
    ReasonCode,
    RiskContext,
    TradeProposal,
)
from mizan.contracts.canonical import uuid7
from mizan.contracts.types import dec, dstr, format_ts

from tests.fixtures import (
    FIXED_NOW,
    FIXED_NOW_STR,
    make_context,
    make_decision,
    make_evaluation,
    make_policy,
    make_proposal,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
MIZAN_DIR = REPO_ROOT / "mizan"
CONTRACTS_DIR = REPO_ROOT / "contracts"

# The sentinel every L0 stub carries: NotImplementedError("L<n> implements this in Sprint 2").
PENDING_MARKER = "implements this in Sprint"


# --------------------------------------------------------------------------------------------------
# Reason codes
# --------------------------------------------------------------------------------------------------
def code_str(code: Any) -> str:
    """Normalise a ReasonCode enum member (or plain string) to its string value."""
    if isinstance(code, Enum):
        return str(code.value)
    return str(code)


def codes(obj: Any) -> set[str]:
    """The set of reason-code strings carried by any object with a ``reason_codes`` list."""
    return {code_str(c) for c in obj.reason_codes}


def pick_code(*candidates: str) -> ReasonCode:
    """First ReasonCode member whose name is in ``candidates`` - loud failure if none exists."""
    available = {member.name for member in ReasonCode}
    for name in candidates:
        if name in available:
            return ReasonCode[name]
    raise AssertionError(
        f"none of {candidates} is in contracts/reason_codes.json; available: {sorted(available)}"
    )


def reduce_code() -> ReasonCode:
    """A catalogue code that legitimately accompanies a deterministic REDUCE."""
    return pick_code(
        "CAPITAL_THRESHOLD_EXCEEDED",
        "MAX_NOTIONAL_EXCEEDED",
        "ORDER_NOTIONAL_EXCEEDED",
        "MAX_QUANTITY_EXCEEDED",
        "POSITION_LIMIT_EXCEEDED",
        "BUYING_POWER_INSUFFICIENT",
    )


def reject_code() -> ReasonCode:
    """A catalogue code that legitimately accompanies a deterministic REJECT."""
    return pick_code(
        "RESTRICTED_SYMBOL",
        "SYMBOL_RESTRICTED",
        "RESTRICTED_STRATEGY",
        "STRATEGY_RESTRICTED",
        "MARKET_DATA_MISSING",
    )


# --------------------------------------------------------------------------------------------------
# Contract builders that keep the object chain consistent (proposal -> context -> evaluation -> ...)
# --------------------------------------------------------------------------------------------------
def linked_evaluation(
    proposal: TradeProposal,
    context: RiskContext,
    policy: Policy,
    *,
    verdict: str,
    recommended_quantity: str | None = None,
    reason_codes: Sequence[ReasonCode] = (),
    **overrides: Any,
):
    """A RiskEvaluation whose identifying fields point at the given proposal/context/policy."""
    original = dstr(proposal.total_quantity)
    if recommended_quantity is None:
        recommended_quantity = {"PASS": original, "REJECT": "0"}.get(verdict)
        if recommended_quantity is None:
            raise AssertionError("REDUCE needs an explicit recommended_quantity")
    if verdict != "PASS" and not reason_codes:
        reason_codes = (reject_code() if verdict == "REJECT" else reduce_code(),)
    fields: dict[str, Any] = dict(
        proposal_id=proposal.proposal_id,
        context_id=context.context_id,
        tenant_id=context.tenant_id,
        policy=policy.ref,
        evaluated_at=context.evaluated_at,
        verdict=verdict,
        reason_codes=sorted(set(reason_codes), key=code_str),
        original_quantity=original,
        recommended_quantity=recommended_quantity,
        data_complete=True,
    )
    fields.update(overrides)
    return make_evaluation(**fields)


def opinion(
    recommendation: str | None,
    quantity: str | None = None,
    *,
    available: bool = True,
    invoked: bool = True,
    reasoning: str = "",
) -> AdvisoryOpinion:
    """An AdvisoryOpinion built through the contract (so only CONCUR/REDUCE/REJECT can exist).

    PERMANENT GUARD (ledger/escalations.md, ESC-1). A REDUCE to zero is not a REDUCE - it is a REJECT,
    and the contract refuses that combination outright. ESC-1 happened because a hypothesis strategy
    generated ``advised=0``, handed it here, and the refusal surfaced as a bare ValidationError deep
    inside an unrelated assertion, where it read like a product defect rather than a generator bound.
    The bound was corrected in one test; this guard is the general fix, and it lives here because this
    is the single construction point every invariant uses. Any future caller - a new strategy, a new
    test, a widened bound - now fails HERE, naming the rule, instead of somewhere confusing.
    """
    if recommendation == "REDUCE" and (quantity is None or dec(quantity) <= 0):
        raise AssertionError(
            f"opinion('REDUCE', {quantity!r}) is not constructible: a REDUCE to a non-positive quantity "
            "is semantically a REJECT, and AdvisoryOpinion refuses it. Use opinion('REJECT') for that "
            "case, or generate advised quantities from 1 upward. See ledger/escalations.md ESC-1 - this "
            "guard exists so a generator-bound bug is reported as one."
        )
    return AdvisoryOpinion(
        profile="invariant-test",
        invoked=invoked,
        available=available,
        recommendation=recommendation,
        recommended_quantity=quantity,
        reasoning=reasoning,
        authority_ceiling="reduce_or_reject",
        provider_ref=None,
        raw_hash=None,
    )


def context_for(policy: Policy, **overrides: Any) -> RiskContext:
    """A RiskContext bound to ``policy`` (same tenant, same policy hash) plus top-level overrides."""
    fields: dict[str, Any] = dict(tenant_id=policy.tenant_id, policy=policy.ref)
    fields.update(overrides)
    return make_context(**fields)


def quantity_of(obj: Any) -> Decimal:
    """Decimal view of an ``authorized``/``scope``/``original`` total quantity."""
    return dec(obj.total_quantity)


def fixture_chain(*, tenant_id: str | None = None):
    """(proposal, policy, context, evaluation, decision) built ONLY from tests.fixtures builders, linked."""
    policy = make_policy(**({"tenant_id": tenant_id} if tenant_id else {}))
    context = make_context(tenant_id=policy.tenant_id, policy=policy.ref)
    proposal = make_proposal()
    evaluation = make_evaluation(
        proposal_id=proposal.proposal_id,
        context_id=context.context_id,
        tenant_id=policy.tenant_id,
        policy=policy.ref,
        evaluated_at=context.evaluated_at,
    )
    decision = make_decision(
        decision_id=uuid7(),
        proposal_id=proposal.proposal_id,
        evaluation_id=evaluation.evaluation_id,
        tenant_id=policy.tenant_id,
        agent_id=context.agent_id,
        policy=policy.ref,
        decision_timestamp=context.evaluated_at,
    )
    return proposal, policy, context, evaluation, decision


def append_fixture_record(tenant_ledger, *, tenant_id: str | None = None, recorded_at: datetime = FIXED_NOW):
    """Append one fixture-built decision to ``tenant_ledger`` through the public TenantLedger.append."""
    proposal, policy, context, evaluation, decision = fixture_chain(tenant_id=tenant_id)
    return tenant_ledger.append(
        proposal=proposal,
        risk_context=context,
        risk_evaluation=evaluation,
        governor_decision=decision,
        policy_snapshot=policy,
        recorded_at=recorded_at,
    )


def engine_chain(*, proposal=None, policy=None, context=None, advisory_opinion=None):
    """(proposal, policy, context, evaluation, decision) produced by the REAL engine (risk + governor)."""
    from mizan import governor, risk

    policy = policy or make_policy()
    context = context or context_for(policy)
    proposal = proposal or make_proposal()
    evaluation = risk.evaluate(proposal, context, policy)
    decision = governor.govern(proposal, evaluation, policy, advisory_opinion, context=context)
    return proposal, policy, context, evaluation, decision


def append_engine_record(tenant_ledger, *, recorded_at: datetime = FIXED_NOW, **chain_kwargs):
    """Append a decision produced by the real engine; returns (record, chain tuple)."""
    chain = engine_chain(**chain_kwargs)
    proposal, policy, context, evaluation, decision = chain
    record = tenant_ledger.append(
        proposal=proposal,
        risk_context=context,
        risk_evaluation=evaluation,
        governor_decision=decision,
        policy_snapshot=policy,
        recorded_at=recorded_at,
    )
    return record, chain


# --------------------------------------------------------------------------------------------------
# Protocol doubles (API-SURFACE section 3.4 / 3.8 / 3.9)
# --------------------------------------------------------------------------------------------------
class RecordingBroker:
    """A BrokerAdapter (section 3.9) serving scripted snapshots, logging every call, recording submissions.

    It has no cancel/replace/close methods, exactly like the Protocol (B4).
    """

    name = "recording-broker"
    environment = "paper"

    def __init__(
        self,
        *,
        portfolio_snapshot: PortfolioSnapshot,
        market_snapshot: MarketSnapshot,
        log: list[str] | None = None,
    ) -> None:
        self.portfolio_snapshot = portfolio_snapshot
        self.market_snapshot = market_snapshot
        self.log: list[str] = log if log is not None else []
        self.submitted: list[Any] = []
        self._orders: dict[str, Any] = {}

    @classmethod
    def from_context(cls, context: RiskContext, *, log: list[str] | None = None) -> "RecordingBroker":
        assert context.portfolio_snapshot is not None and context.market_snapshot is not None
        return cls(
            portfolio_snapshot=context.portfolio_snapshot, market_snapshot=context.market_snapshot, log=log
        )

    # reads ---------------------------------------------------------------------------------------
    def get_portfolio_snapshot(self, *, as_of: datetime) -> PortfolioSnapshot:
        self.log.append("broker.get_portfolio_snapshot")
        return self.portfolio_snapshot

    def get_market_snapshot(
        self, *, symbols: Sequence[str], occ_symbols: Sequence[str] = (), as_of: datetime
    ) -> MarketSnapshot:
        self.log.append("broker.get_market_snapshot")
        return self.market_snapshot

    def find_order(self, client_order_id: str):
        self.log.append("broker.find_order")
        return self._orders.get(client_order_id)

    def get_order(self, broker_order_id: str):
        self.log.append("broker.get_order")
        for order in self._orders.values():
            if order.broker_order_id == broker_order_id:
                return order
        raise KeyError(broker_order_id)

    # the one and only mutation -------------------------------------------------------------------
    def submit_order(self, request):
        from mizan.adapters import BrokerOrder

        self.log.append("broker.submit_order")
        self.submitted.append(request)
        order = BrokerOrder(
            broker_order_id=f"recording-{len(self.submitted)}",
            client_order_id=request.client_order_id,
            status="accepted",
            submitted_at=FIXED_NOW_STR,
        )
        self._orders[request.client_order_id] = order
        return order


READ_EVENTS = frozenset(
    {"broker.get_portfolio_snapshot", "broker.get_market_snapshot", "broker.find_order", "context.build"}
)


class ScriptedContextProvider:
    """A ContextProvider (section 3.9) that reads the broker's snapshots and can run a hook mid-read.

    ``on_build(context)`` runs after the broker reads and before the context is returned - i.e. strictly
    after the last broker read of the TOCTOU re-validation and before any mutation. ``context_overrides``
    are applied on top of the broker-derived fields (e.g. ``response_level=2``).
    """

    def __init__(
        self,
        broker: RecordingBroker,
        *,
        log: list[str] | None = None,
        on_build=None,
        context_overrides: dict[str, Any] | None = None,
    ) -> None:
        self._broker = broker
        self.log = log if log is not None else broker.log
        self.on_build = on_build
        self.context_overrides = dict(context_overrides or {})
        self.built: list[RiskContext] = []

    def build(
        self,
        *,
        tenant_id: str,
        agent_id: str,
        proposal: TradeProposal,
        policy: Policy,
        now: datetime,
        recent_orders=(),
    ) -> RiskContext:
        portfolio = self._broker.get_portfolio_snapshot(as_of=now)
        market = self._broker.get_market_snapshot(symbols=[proposal.symbol], as_of=now)
        fields: dict[str, Any] = dict(
            tenant_id=tenant_id,
            agent_id=agent_id,
            policy=policy.ref,
            evaluated_at=format_ts(now),
            market_snapshot=market,
            portfolio_snapshot=portfolio,
            recent_orders=list(recent_orders),
        )
        fields.update(self.context_overrides)
        context = make_context(**fields)
        self.built.append(context)
        self.log.append("context.build")
        if self.on_build is not None:
            self.on_build(context)
        return context


class EventKillSwitch:
    """A KillSwitch (section 3.8) whose every consultation is written to the shared event log."""

    def __init__(self, *, active: bool = False, log: list[str] | None = None) -> None:
        self.active = active
        self.log = log if log is not None else []
        self.calls = 0

    def is_active(self) -> bool:
        self.calls += 1
        self.log.append("kill_switch")
        return self.active


class ScriptedAdvisoryProvider:
    """An AdvisoryProvider (section 3.4) that returns whatever object it was given, or raises."""

    def __init__(self, result: Any = None, *, raises: BaseException | None = None) -> None:
        self.result = result
        self.raises = raises
        self.calls = 0

    def advise(self, proposal, evaluation, context, policy):
        self.calls += 1
        if self.raises is not None:
            raise self.raises
        return self.result


# --------------------------------------------------------------------------------------------------
# Static-analysis helpers (invariants 13, 15, 16, 17)
# --------------------------------------------------------------------------------------------------
def python_files(*subpackages: str) -> list[Path]:
    """Every .py file under mizan/<subpackage> (recursively), sorted. Fails loudly if one is absent."""
    files: list[Path] = []
    for sub in subpackages:
        root = MIZAN_DIR / sub if sub else MIZAN_DIR
        if not root.exists():
            raise AssertionError(f"expected package directory is missing: {root}")
        files.extend(sorted(p for p in root.rglob("*.py") if "__pycache__" not in p.parts))
    if not files:
        raise AssertionError(f"no python files found under {subpackages}")
    return files


def parse(path: Path) -> ast.Module:
    return ast.parse(path.read_text(encoding="utf-8"), filename=str(path))


def rel(path: Path) -> str:
    return path.relative_to(REPO_ROOT).as_posix()


def docstring_ids(tree: ast.AST) -> set[int]:
    """ids of Constant nodes that are docstrings (so prose is not mistaken for code)."""
    ids: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            body = node.body
            if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant):
                if isinstance(body[0].value.value, str):
                    ids.add(id(body[0].value))
    return ids


def imported_modules(tree: ast.Module) -> list[tuple[str, int]]:
    """(module name as written, line) for every import statement in the tree."""
    found: list[tuple[str, int]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                found.append((alias.name, node.lineno))
        elif isinstance(node, ast.ImportFrom):
            if node.module is not None and node.level == 0:
                found.append((node.module, node.lineno))
    return found


def offenders_message(title: str, offenders: Iterable[str]) -> str:
    lines = list(offenders)
    return f"{title} ({len(lines)} offender(s)):\n  " + "\n  ".join(lines)


def read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def as_decimal(value: str | None) -> Decimal:
    if value is None:
        raise AssertionError("expected a DecimalStr, got None")
    return dec(value)
# ---- appended to tests/invariants/_support.py ----

def path_and_aggregate_policy(**overrides: Any) -> Policy:
    """The default policy plus the path and aggregate sections, so invariants 19-22 vary ONE thing.

    ``make_policy`` has neither section and ``make_institutional_policy`` has both but also demands an
    invalidation level the default proposal does not carry - so it rejects for reasons unrelated to path
    or aggregate state. This composes the two: a policy the default proposal passes cleanly, which also
    has a drawdown ladder and a book limit to exercise.
    """
    from tests.fixtures import make_institutional_policy, make_policy

    institutional = make_institutional_policy().model_dump(mode="json")
    payload = make_policy().model_dump(mode="json")
    payload["path"] = institutional["path"]
    payload["aggregate"] = institutional["aggregate"]
    payload.pop("policy_hash", None)
    payload.update(overrides)
    return Policy.build(**payload)


def empty_book(**overrides: Any):
    """An aggregate state holding nothing, so the aggregate layer has no reason to object."""
    from tests.fixtures import make_aggregate_state

    base = dict(
        gross_exposure="0",
        net_exposure="0",
        exposure_pct_of_equity="0",
        exposure_by_agent={},
        exposure_by_model_provider={},
        exposure_by_signal_source={},
        exposure_by_sector={},
        pending_intents=[],
        crowding_score="0",
    )
    base.update(overrides)
    return make_aggregate_state(**base)


def full_book(equity: Any, limit: Any, **overrides: Any):
    """An aggregate state sitting exactly at the policy's book limit."""
    from tests.fixtures import make_aggregate_state

    at_limit = dstr(dec(equity) * dec(limit))
    base = dict(gross_exposure=at_limit, net_exposure=at_limit, exposure_pct_of_equity=dstr(dec(limit)))
    base.update(overrides)
    return make_aggregate_state(**base)


def unstressed_context(policy: Policy, **overrides: Any) -> RiskContext:
    """A context with path and aggregate state present but benign - the baseline for 19-22."""
    from tests.fixtures import make_path_state

    fields: dict[str, Any] = dict(
        path_state=make_path_state(current_drawdown_pct="0", consecutive_losses=0, days_under_water=0),
        aggregate_state=empty_book(),
    )
    fields.update(overrides)
    return context_for(policy, **fields)


# ---------------------------------------------------------------------------------------------------
# Check-catalogue battery (invariants 25 and 26). Shared so both assert against ONE definition of
# "every check, driven hard" - two batteries would drift and the weaker one would silently win.
# ---------------------------------------------------------------------------------------------------
EVIDENCE_FIELDS = ("threshold", "actual", "data_source", "snapshot_ts")


def has_evidence(check) -> bool:
    if any(getattr(check, field, None) is not None for field in EVIDENCE_FIELDS):
        return True
    return bool((check.detail or "").strip())


def rebuild_proposal(proposal: TradeProposal, **overrides) -> TradeProposal:
    """Rebuild a proposal with overrides. proposal_id and total_quantity are derived, never passed."""
    payload = proposal.model_dump(mode="json")
    payload.pop("proposal_id", None)
    payload.pop("total_quantity", None)
    payload.update(overrides)
    return TradeProposal.build(**payload)


def policy_with(**dotted) -> Policy:
    payload = path_and_aggregate_policy().model_dump(mode="json")
    payload.pop("policy_hash", None)
    for path, value in dotted.items():
        cursor = payload
        *parents, leaf = path.split(".")
        for segment in parents:
            cursor = cursor[segment]
        cursor[leaf] = value
    return Policy.build(**payload)


def iron_condor():
    """A genuine four-leg structure. Needed because the contract enforces per-strategy leg counts, so a
    multi-leg proposal cannot be faked by duplicating a single-leg one."""
    from tests.fixtures import make_option_proposal

    leg = make_option_proposal().model_dump(mode="json")["legs"][0]
    return rebuild_proposal(
        make_option_proposal(),
        strategy="iron_condor",
        legs=[
            {**leg, "leg_index": 0, "side": "sell", "contract_type": "put", "strike": "170"},
            {**leg, "leg_index": 1, "side": "buy", "contract_type": "put", "strike": "165"},
            {**leg, "leg_index": 2, "side": "sell", "contract_type": "call", "strike": "190"},
            {**leg, "leg_index": 3, "side": "buy", "contract_type": "call", "strike": "195"},
        ],
    )


def check_battery():
    """(proposal, context, policy) triples shaped to break as many checks as possible."""
    import itertools

    from tests.fixtures import (
        make_agent_state,
        make_calendar,
        make_institutional_context,
        make_institutional_policy,
        make_option_proposal,
        make_path_state,
        make_portfolio_snapshot,
        make_proposal,
    )

    triples = []
    for policy in (make_institutional_policy(), path_and_aggregate_policy()):
        base = context_for(policy)
        equity = base.portfolio_snapshot.equity
        limit = policy.aggregate.max_portfolio_exposure_pct if policy.aggregate else "0.6"
        contexts = (
            base,
            context_for(policy, market_snapshot=None),
            context_for(policy, portfolio_snapshot=None),
            context_for(policy, path_state=None),
            context_for(policy, aggregate_state=None),
            context_for(
                policy,
                path_state=make_path_state(
                    current_drawdown_pct="0.45", consecutive_losses=12, days_under_water=400
                ),
            ),
            context_for(policy, aggregate_state=full_book(equity, limit)),
            context_for(policy, aggregate_state=empty_book()),
            context_for(
                policy,
                portfolio_snapshot=make_portfolio_snapshot(buying_power="1", equity="10", cash="1"),
            ),
            context_for(policy, response_level=5),
            context_for(
                policy,
                calendar=make_calendar(
                    session="closed", is_holiday_or_half_day=True, minutes_to_close=0
                ),
            ),
            context_for(
                policy,
                agent_state=make_agent_state(
                    daily_notional_used="99999999", daily_order_count=9999, open_positions=999
                ),
            ),
            make_institutional_context(),
        )
        proposals = (make_proposal(), make_option_proposal(), iron_condor())
        triples.extend((p, c, policy) for c, p in itertools.product(contexts, proposals))
    return triples


