"""View models: contracts in, plain data out. No HTML here, and no backend logic.

The information architecture is the financial decision, not the model trace. A decision detail is read as a
case file - what was proposed, what the rules said, what state the rules were applied to, what the governor
authorised, and what the broker did - and the model's opinion is one clearly fenced section inside it, never
the spine of the page.

Two structural rules make the escaping guarantee hold at this layer rather than only at the rendering layer:

* Every string an agent, a model or a broker authored is wrapped in :class:`Untrusted`. The renderer has one
  code path for that type and it always escapes, always fences and always badges.
* Every *enforcement* value is re-validated against a closed vocabulary before it is put in a view model:
  :func:`verdict` accepts only APPROVE/REDUCE/REJECT, :func:`reason_code` only a code in the frozen
  catalogue, :func:`amount` only a DecimalStr, :func:`sha` only 64 hex characters. Free text cannot reach an
  enforcement field even if a caller tries, because the field would raise rather than render.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

from mizan.console import client as client_api
from mizan.console.escaping import taint_flags
from mizan.contracts import (
    REASON_CODE_INFO,
    ControlEvent,
    DecisionRecord,
    Policy,
    ReasonCode,
    dec,
    dstr,
    parse_ts,
)
from mizan.contracts.canonical import DECIMAL_CONTEXT

__all__ = [
    "ADVISORY_BANNER",
    "ADVISORY_NOTE",
    "DE_ESCALATION_NOTE",
    "MAX_RESPONSE_LEVEL",
    "NOT_FOUND_MESSAGE",
    "PERFORMANCE_FIELDS",
    "Untrusted",
    "amount",
    "audit_timeline",
    "chain_status",
    "decision_detail",
    "decision_feed",
    "feed_page",
    "kill_switch_view",
    "policy_diff_rows",
    "policy_editor_view",
    "reason_code",
    "response_level_view",
    "sha",
    "timestamp",
    "untrusted",
    "verdict",
]

#: Shown on every advisory section, in the section itself, not in a tooltip.
ADVISORY_BANNER = "ADVISORY ONLY - NEVER ENFORCEMENT"

ADVISORY_NOTE = (
    "The advisory is a semantic opinion with no authority. Its ceiling is reduce_or_reject: it can concur, "
    "recommend a smaller quantity, or recommend rejection, and there is no value it can return that means "
    "approve or increase. Nothing in this section authorised anything. The enforcement sections above did."
)

#: One message for an id that does not exist and for an id belonging to another tenant (REQ-4, F-17).
NOT_FOUND_MESSAGE = "No decision with that id is visible to this tenant."

DE_ESCALATION_NOTE = (
    "Escalation is automatic: the engine may raise the response level on its own, and it takes effect on the "
    "next decision. De-escalation is not: lowering the level requires a named human actor, and the ledger "
    "refuses a downward change recorded by the system."
)

#: The graduated-response ladder is 0..5 (RiskContext.ResponseLevel).
MAX_RESPONSE_LEVEL = 5

#: Never rendered, and pinned by a test that walks every view model looking for them. Return and performance
#: figures are out of scope for a governance console: they invite the reading that the framework is a
#: strategy, and they are the fields most likely to be quoted back out of context.
PERFORMANCE_FIELDS = frozenset(
    {
        "daily_pnl",
        "daily_pnl_pct",
        "realized_expectancy",
        "expectancy",
        "realized_hit_rate",
        "claimed_confidence_mean",
        "calibration",
    }
)

_SHA_RE = re.compile(r"^[0-9a-f]{64}$")
_VERDICTS = ("APPROVE", "REDUCE", "REJECT")
_ADVISORY_RECOMMENDATIONS = ("CONCUR", "REDUCE", "REJECT")
_EXECUTION_STATUSES = ("SUBMITTED", "WOULD_SUBMIT", "BLOCKED", "FAILED", "RECONCILED_EXISTING")
_EVENT_TYPES = (
    "response_level_changed",
    "kill_switch_activated",
    "kill_switch_deactivated",
    "policy_activated",
)


@dataclass(frozen=True, slots=True)
class Untrusted:
    """A string an agent, a model, a tenant or a broker authored. The renderer escapes and fences it.

    ``flags`` records *why* the string looks adversarial (markup characters, HTML entities, invisible format
    characters, a homoglyph that would become markup under NFKC). It drives a badge; it never rewrites text.
    """

    text: str
    origin: str = "agent"
    flags: tuple[str, ...] = field(default_factory=tuple)

    @property
    def suspicious(self) -> bool:
        return bool(self.flags)


def untrusted(value: Any, origin: str = "agent") -> Untrusted | None:
    """Wrap a tainted string. ``None`` and the empty string stay ``None`` so views can omit the field."""
    if value is None:
        return None
    text = value if isinstance(value, str) else str(value)
    if text == "":
        return None
    return Untrusted(text=text, origin=origin, flags=taint_flags(text))


# --------------------------------------------------------------------------------------------------------
# Closed vocabularies. An enforcement field that is not one of these raises rather than rendering.
# --------------------------------------------------------------------------------------------------------


def _closed(value: Any, allowed: tuple[str, ...], what: str) -> str:
    text = value.value if isinstance(value, ReasonCode) else value
    if not isinstance(text, str) or text not in allowed:
        raise ValueError(f"{what} must be one of {allowed}, got {value!r}")
    return text


def verdict(value: Any) -> str:
    """APPROVE, REDUCE or REJECT. Free text cannot be rendered as a verdict."""
    return _closed(value, _VERDICTS, "verdict")


def advisory_recommendation(value: Any) -> str | None:
    """CONCUR, REDUCE or REJECT - the whole vocabulary an advisory has (Hard Rule E1)."""
    if value is None:
        return None
    return _closed(value, _ADVISORY_RECOMMENDATIONS, "advisory recommendation")


def execution_status(value: Any) -> str:
    """One of the five execution statuses."""
    return _closed(value, _EXECUTION_STATUSES, "execution status")


def reason_code(value: Any) -> dict[str, str]:
    """A reason code with its catalogue entry. Unknown codes raise; they are never shown as free text."""
    code = value.value if isinstance(value, ReasonCode) else value
    try:
        info = REASON_CODE_INFO[ReasonCode(code)]
    except (ValueError, KeyError) as exc:
        raise ValueError(f"unknown reason code {value!r}") from exc
    return {
        "code": info.code,
        "category": info.category,
        "severity": info.default_severity,
        "description": info.description,
        "check_id": info.check_id or "",
    }


def reason_codes(values: Any) -> list[dict[str, str]]:
    return [reason_code(value) for value in values or ()]


def amount(value: Any) -> str | None:
    """A normalised DecimalStr, or ``None``. Anything that is not a DecimalStr raises."""
    if value is None:
        return None
    return dstr(dec(value))


def sha(value: Any) -> str | None:
    """A 64-character lower-case hex hash, or ``None``."""
    if value is None:
        return None
    if not isinstance(value, str) or not _SHA_RE.fullmatch(value):
        raise ValueError(f"not a sha256 hex digest: {value!r}")
    return value


def short_sha(value: Any) -> str | None:
    digest = sha(value)
    return None if digest is None else f"{digest[:12]}..."


def timestamp(value: Any) -> str | None:
    """An RFC 3339 timestamp, echoed unchanged after being parsed. Anything else raises."""
    if value is None:
        return None
    parse_ts(value)
    return value


def _distance(threshold: Any, actual: Any) -> str | None:
    """``actual - threshold`` as a DecimalStr; ``None`` when either side is missing. No float, ever."""
    if threshold is None or actual is None:
        return None
    return dstr(DECIMAL_CONTEXT.subtract(dec(actual), dec(threshold)))


def _ratio_pct(value: Any) -> str | None:
    """A ratio rendered as a percentage string, in Decimal arithmetic."""
    if value is None:
        return None
    return dstr(DECIMAL_CONTEXT.multiply(dec(value), Decimal(100)))


# --------------------------------------------------------------------------------------------------------
# 1. Decision feed
# --------------------------------------------------------------------------------------------------------


def feed_row(record: DecisionRecord) -> dict[str, Any]:
    """One row of the decision feed."""
    advisory = record.llm_advisory
    execution = record.execution
    return {
        "sequence": record.sequence,
        "decision_id": record.decision_id,
        "decision_timestamp": timestamp(record.decision_timestamp),
        "verdict": verdict(record.verdict),
        "symbol": untrusted(record.proposal.symbol, "agent"),
        "agent_id": untrusted(record.agent_id, "agent"),
        "strategy": record.proposal.strategy,
        "intent": record.proposal.intent,
        "reason_codes": reason_codes(record.reason_codes),
        "policy_id": untrusted(record.policy.policy_id, "tenant"),
        "policy_version": record.policy.version,
        "policy_hash": sha(record.policy.hash),
        "policy_hash_short": short_sha(record.policy.hash),
        "original_quantity": amount(record.original.total_quantity),
        "authorized_quantity": amount(record.authorized.total_quantity),
        "advisory_present": advisory is not None and advisory.available,
        "advisory_recommendation": (
            advisory_recommendation(advisory.recommendation) if advisory is not None else None
        ),
        "execution_status": execution_status(execution.status) if execution is not None else None,
    }


def decision_feed(
    client: Any, *, limit: int = 50, before_sequence: int | None = None
) -> list[dict[str, Any]]:
    """Feed rows, newest first, strictly before ``before_sequence`` (REQ-4 cursor paging)."""
    records = client_api.list_decisions(client, limit=limit, before_sequence=before_sequence)
    return [feed_row(record) for record in records]


def feed_page(
    client: Any, *, limit: int = 50, before_sequence: int | None = None
) -> dict[str, Any]:
    """A page of the feed with its cursor. ``next_before_sequence`` is the last row's sequence."""
    rows = decision_feed(client, limit=limit, before_sequence=before_sequence)
    return {
        "view": "decision_feed",
        "rows": rows,
        "limit": limit,
        "before_sequence": before_sequence,
        "newest_first": True,
        "has_more": len(rows) == limit,
        "next_before_sequence": rows[-1]["sequence"] if len(rows) == limit else None,
    }


# --------------------------------------------------------------------------------------------------------
# 2. Decision detail
# --------------------------------------------------------------------------------------------------------


def check_row(check: Any) -> dict[str, Any]:
    """One risk check: its threshold, the actual value, the distance between them, and its real outcome.

    REQ-10 (3): a check recorded ``passed=True, severity="info"`` was not evaluated - disabled, or without its
    policy section, or not implemented in this build. Rendering it as a pass would claim a control ran when it
    did not, so the outcome shown is NOT EVALUATED.
    """
    if check.severity == "info":
        outcome = "NOT EVALUATED"
    elif check.passed:
        outcome = "PASS"
    else:
        outcome = "FAIL"
    return {
        "check_id": check.check_id,
        "outcome": outcome,
        "passed": check.passed,
        "severity": check.severity,
        "reason_code": reason_code(check.reason_code) if check.reason_code else None,
        "threshold": amount(check.threshold),
        "actual": amount(check.actual),
        "distance": _distance(check.threshold, check.actual),
        "recommended_quantity": amount(check.recommended_quantity),
        "data_source": untrusted(check.data_source, "broker"),
        "snapshot_ts": timestamp(check.snapshot_ts),
        "detail": untrusted(check.detail, "engine"),
    }


def _path_state(state: Any) -> dict[str, Any] | None:
    if state is None:
        return None
    return {
        "as_of": timestamp(state.as_of),
        "peak_equity": amount(state.peak_equity),
        "current_drawdown_pct": _ratio_pct(state.current_drawdown_pct),
        "consecutive_losses": state.consecutive_losses,
        "days_under_water": state.days_under_water,
        "sample_size": state.sample_size,
    }


def _aggregate_state(state: Any) -> dict[str, Any] | None:
    if state is None:
        return None
    return {
        "as_of": timestamp(state.as_of),
        "gross_exposure": amount(state.gross_exposure),
        "net_exposure": amount(state.net_exposure),
        "exposure_pct_of_equity": _ratio_pct(state.exposure_pct_of_equity),
        "crowding_score": amount(state.crowding_score),
        "days_to_liquidate_book": amount(state.days_to_liquidate_book),
        "by_agent": [
            {"key": untrusted(key, "agent"), "value": amount(value)}
            for key, value in sorted(state.exposure_by_agent.items())
        ],
        "by_model_provider": [
            {"key": untrusted(key, "agent"), "value": amount(value)}
            for key, value in sorted(state.exposure_by_model_provider.items())
        ],
        "by_signal_source": [
            {"key": untrusted(key, "agent"), "value": amount(value)}
            for key, value in sorted(state.exposure_by_signal_source.items())
        ],
        "by_sector": [
            {"key": untrusted(key, "broker"), "value": amount(value)}
            for key, value in sorted(state.exposure_by_sector.items())
        ],
        "pending_intents": [
            {
                "agent_id": untrusted(intent.agent_id, "agent"),
                "symbol": untrusted(intent.symbol, "agent"),
                "direction": intent.direction,
                "notional": amount(intent.notional),
                "proposed_at": timestamp(intent.proposed_at),
                "model_provider": untrusted(intent.model_provider, "agent"),
            }
            for intent in state.pending_intents
        ],
    }


def _advisory(record: DecisionRecord) -> dict[str, Any]:
    """The advisory section. Every field here is fenced; none of it is an enforcement value."""
    advisory = record.llm_advisory
    section: dict[str, Any] = {
        "authority": "advisory",
        "banner": ADVISORY_BANNER,
        "note": ADVISORY_NOTE,
        "enforced": False,
        "present": advisory is not None,
    }
    if advisory is None:
        section["status"] = "No advisory was attached to this decision."
        return section
    section.update(
        {
            "invoked": advisory.invoked,
            "available": advisory.available,
            "profile": untrusted(advisory.profile, "tenant"),
            "authority_ceiling": advisory.authority_ceiling,
            "recommendation": advisory_recommendation(advisory.recommendation),
            "recommended_quantity": amount(advisory.recommended_quantity),
            "reasoning": untrusted(advisory.reasoning, "model"),
            "provider_ref": untrusted(advisory.provider_ref, "model"),
            "raw_hash": sha(advisory.raw_hash),
            "status": (
                "The advisory ran and returned an opinion."
                if advisory.available
                else "The advisory was not available; the deterministic verdict stands alone."
            ),
        }
    )
    return section


def _authorization(record: DecisionRecord) -> dict[str, Any] | None:
    auth = record.authorization
    if auth is None:
        return None
    bound = auth.bound_state
    return {
        "auth_id": auth.auth_id,
        "issued_at": timestamp(auth.issued_at),
        "expires_at": timestamp(auth.expires_at),
        "ttl_seconds": auth.ttl_seconds,
        "single_use": auth.single_use,
        "environment": auth.environment,
        "idempotency_key": auth.idempotency_key,
        "authorization_hash": sha(auth.authorization_hash),
        "total_quantity": amount(auth.scope.total_quantity),
        "max_notional": amount(auth.scope.max_notional),
        "symbol": untrusted(auth.scope.symbol, "agent"),
        "intent": auth.scope.intent,
        "asset_class": auth.scope.asset_class,
        "legs": [
            {
                "leg_index": leg.leg_index,
                "side": leg.side,
                "symbol": untrusted(leg.symbol, "agent"),
                "occ_symbol": untrusted(leg.occ_symbol, "agent"),
                "quantity": amount(leg.quantity),
                "limit_price": amount(leg.limit_price),
                "order_type": leg.order_type,
            }
            for leg in auth.scope.legs
        ],
        "binding": {
            "policy_hash": sha(bound.policy_hash),
            "portfolio_snapshot_id": untrusted(bound.portfolio_snapshot_id, "broker"),
            "portfolio_state_hash": sha(bound.portfolio_state_hash),
            "market_snapshot_id": untrusted(bound.market_snapshot_id, "broker"),
            "response_level": bound.response_level,
            "path_state_hash": sha(bound.path_state_hash),
            "aggregate_state_hash": sha(bound.aggregate_state_hash),
            "note": (
                "The authorization is bound to these hashes. If the state behind any of them has moved, the "
                "execution gate refuses to act on it and the decision must be re-authorised."
            ),
        },
    }


def _execution(record: DecisionRecord) -> dict[str, Any] | None:
    result = record.execution
    if result is None:
        return None
    revalidation = result.revalidation
    return {
        "status": execution_status(result.status),
        "reason_codes": reason_codes(result.reason_codes),
        "broker": untrusted(result.broker.name, "broker"),
        "environment": result.broker.environment,
        "client_order_id": untrusted(result.client_order_id, "broker"),
        "broker_order_id": untrusted(result.broker_order_id, "broker"),
        "broker_status": untrusted(result.broker_status, "broker"),
        "message": untrusted(result.message, "broker"),
        "checked_at": timestamp(result.checked_at),
        "authorization_validated_at": timestamp(result.authorization_validated_at),
        "kill_switch_checked_at": timestamp(result.kill_switch_checked_at),
        "submitted_at": timestamp(result.submitted_at),
        "revalidation": {
            "performed": revalidation.performed,
            "supported": revalidation.supported,
            "state_changed": revalidation.state_changed,
            "fresh_recommended_quantity": amount(revalidation.fresh_recommended_quantity),
            "response_level_at_execution": revalidation.response_level_at_execution,
        },
        "fills": [
            {
                "filled_quantity": amount(fill.filled_quantity),
                "avg_price": amount(fill.avg_price),
                "filled_at": timestamp(fill.filled_at),
            }
            for fill in result.fills
        ],
    }


def _decision_replay(client: Any, decision_id: str) -> dict[str, Any] | None:
    """The decision-replay panel. ``detail`` is surfaced verbatim (REQ-4): it announces engine drift."""
    result = client_api.replay_decision(client, decision_id)
    if isinstance(result, client_api.Unavailable) or result is None:
        return None
    recorded_verdict = getattr(result, "original_verdict", None)
    replayed_verdict = getattr(result, "replayed_verdict", None)
    if recorded_verdict is None or replayed_verdict is None:
        return None
    return {
        "decision_id": getattr(result, "decision_id", decision_id),
        "mode": getattr(result, "mode", ""),
        "identical": getattr(result, "identical", None),
        "original_verdict": verdict(recorded_verdict),
        "replayed_verdict": verdict(replayed_verdict),
        "original_verdict_hash": sha(getattr(result, "original_verdict_hash", None)),
        "replayed_verdict_hash": sha(getattr(result, "replayed_verdict_hash", None)),
        "engine_version_matches": getattr(result, "engine_version_matches", True),
        "recorded_engine_version": untrusted(getattr(result, "recorded_engine_version", ""), "engine"),
        "running_engine_version": untrusted(getattr(result, "running_engine_version", ""), "engine"),
        "detail": untrusted(getattr(result, "detail", ""), "engine"),
        "detail_is_verbatim": True,
    }


def decision_detail(client: Any, decision_id: str) -> dict[str, Any]:
    """One decision as a case file, or the not-found state.

    An unknown id and another tenant's id produce the identical result, with no echo of the requested id, so
    the two cannot be told apart from the page either (REQ-4, F-17).
    """
    record = client_api.get_decision(client, decision_id)
    if record is None:
        return {"view": "decision_detail", "found": False, "message": NOT_FOUND_MESSAGE}

    evaluation = record.risk_evaluation
    governor = record.governor_decision
    return {
        "view": "decision_detail",
        "found": True,
        "header": {
            "decision_id": record.decision_id,
            "sequence": record.sequence,
            "verdict": verdict(record.verdict),
            "decision_timestamp": timestamp(record.decision_timestamp),
            "recorded_at": timestamp(record.recorded_at),
            "agent_id": untrusted(record.agent_id, "agent"),
            "symbol": untrusted(record.proposal.symbol, "agent"),
            "strategy": record.proposal.strategy,
            "intent": record.proposal.intent,
            "asset_class": record.proposal.asset_class,
            "engine_version": untrusted(record.engine_version, "engine"),
            "audit_hash": sha(record.audit_hash),
            "audit_prev_hash": sha(record.audit_prev_hash),
        },
        "policy": {
            "policy_id": untrusted(record.policy.policy_id, "tenant"),
            "version": record.policy.version,
            "hash": sha(record.policy.hash),
            "hash_short": short_sha(record.policy.hash),
            "snapshot_hash": sha(record.policy_snapshot.policy_hash),
            "enabled_checks": list(record.policy_snapshot.enabled_checks),
        },
        "proposal": {
            "proposal_id": sha(record.proposal_id),
            "created_at": timestamp(record.proposal.created_at),
            "expires_at": timestamp(record.proposal.expires_at),
            "confidence": amount(record.proposal.confidence),
            "signal_sources": [untrusted(source, "agent") for source in record.proposal.signal_sources],
            "invalidation": (
                {
                    "level": amount(record.proposal.invalidation.level),
                    "direction": record.proposal.invalidation.direction,
                    "target": amount(record.proposal.invalidation.target),
                }
                if record.proposal.invalidation is not None
                else None
            ),
            "reasoning": untrusted(record.proposal.reasoning, "agent"),
            "reasoning_note": (
                "Agent free text. Audit-only: it is excluded from proposal_id and never reaches enforcement."
            ),
            "legs": [
                {
                    "leg_index": leg.leg_index,
                    "side": leg.side,
                    "quantity": amount(leg.quantity),
                    "limit_price": amount(leg.limit_price),
                    "order_type": leg.order_type,
                    "contract_type": leg.contract_type,
                    "strike": amount(leg.strike),
                    "expiry": leg.expiry,
                }
                for leg in record.proposal.legs
            ],
            "model": {
                "provider": untrusted(record.proposal.model.provider, "agent"),
                "model": untrusted(record.proposal.model.model, "agent"),
                "version": untrusted(record.proposal.model.version, "agent"),
                "prompt_hash": sha(record.proposal.model.prompt_hash),
            },
        },
        "risk": {
            "authority": "enforcement",
            "verdict": evaluation.verdict,
            "evaluation_id": sha(evaluation.evaluation_id),
            "data_complete": evaluation.data_complete,
            "original_quantity": amount(evaluation.original_quantity),
            "recommended_quantity": amount(evaluation.recommended_quantity),
            "checks": [check_row(check) for check in evaluation.checks],
            "note": (
                "An info result means the check did not run - disabled, missing its policy section, or not "
                "implemented in this build. It is not evidence that a control passed."
            ),
        },
        "path_state": _path_state(record.risk_context.path_state),
        "aggregate_state": _aggregate_state(record.risk_context.aggregate_state),
        "response_level": record.risk_context.response_level,
        "governor": {
            "authority": "enforcement",
            "verdict": verdict(governor.verdict),
            "reason_codes": reason_codes(governor.reason_codes),
            "verdict_hash": sha(governor.verdict_hash),
            "original_quantity": amount(governor.original.total_quantity),
            "original_notional": amount(governor.original.total_notional),
            "authorized_quantity": amount(governor.authorized.total_quantity),
            "authorized_notional": amount(governor.authorized.total_notional),
            "reductions": [
                {
                    "source": reduction.source,
                    "from_quantity": amount(reduction.from_quantity),
                    "to_quantity": amount(reduction.to_quantity),
                    "reason_code": reason_code(reduction.reason_code),
                }
                for reduction in governor.authorized.reductions
            ],
        },
        "advisory": _advisory(record),
        "authorization": _authorization(record),
        "execution": _execution(record),
        "decision_replay": _decision_replay(client, decision_id),
        "suppressed_note": (
            "Return and performance figures are deliberately not shown anywhere in this console."
        ),
    }


# --------------------------------------------------------------------------------------------------------
# 3. Audit timeline
# --------------------------------------------------------------------------------------------------------


def _control_event_row(event: ControlEvent) -> dict[str, Any]:
    event_type = _closed(event.event_type, _EVENT_TYPES, "control event type")
    direction = None
    human_required = False
    if event_type == "response_level_changed":
        direction = "escalation" if (event.to_level or 0) > (event.from_level or 0) else "de-escalation"
        human_required = direction == "de-escalation"
    elif event_type == "kill_switch_deactivated":
        human_required = True
    return {
        "kind": "control_event",
        "sequence": event.sequence,
        "id": event.event_id,
        "event_type": event_type,
        "occurred_at": timestamp(event.occurred_at),
        "recorded_at": timestamp(event.recorded_at),
        "from_level": event.from_level,
        "to_level": event.to_level,
        "direction": direction,
        "human_required": human_required,
        "actor_type": _closed(event.actor.type, ("system", "human"), "actor type"),
        "actor_id": untrusted(event.actor.id, "tenant"),
        "reason_codes": reason_codes(event.trigger_reason_codes),
        "audit_prev_hash": sha(event.audit_prev_hash),
        "audit_hash": sha(event.audit_hash),
    }


def _decision_row(record: DecisionRecord) -> dict[str, Any]:
    return {
        "kind": "decision",
        "sequence": record.sequence,
        "id": record.decision_id,
        "verdict": verdict(record.verdict),
        "symbol": untrusted(record.proposal.symbol, "agent"),
        "agent_id": untrusted(record.agent_id, "agent"),
        "occurred_at": timestamp(record.decision_timestamp),
        "recorded_at": timestamp(record.recorded_at),
        "reason_codes": reason_codes(record.reason_codes),
        "audit_prev_hash": sha(record.audit_prev_hash),
        "audit_hash": sha(record.audit_hash),
    }


def _entry_row(entry: Any) -> dict[str, Any]:
    return _control_event_row(entry) if isinstance(entry, ControlEvent) else _decision_row(entry)


def chain_status(client: Any) -> dict[str, Any]:
    """Chain verification, phrased so a broken link is named rather than merely reported."""
    verification = client_api.verify_chain(client)
    if isinstance(verification, client_api.Unavailable) or verification is None:
        return {
            "available": False,
            "ok": None,
            "headline": "Chain verification is not available from this client.",
            "length": None,
            "first_bad_sequence": None,
            "detail": None,
        }
    ok = bool(getattr(verification, "ok", False))
    length = getattr(verification, "length", 0)
    first_bad = getattr(verification, "first_bad_sequence", None)
    detail = getattr(verification, "detail", "") or ""
    if ok and first_bad is None:
        links = "link" if length == 1 else "links"
        headline = f"Chain verified: {length} {links}, every hash re-derived."
    elif first_bad is not None:
        headline = (
            f"Chain verification FAILED. Link {first_bad} is the first that does not verify; "
            f"links 1 to {max(first_bad - 1, 0)} verify."
        )
    else:
        headline = f"Chain verification FAILED across {length} links; no single link was identified."
    return {
        "available": True,
        "ok": ok,
        "length": length,
        "first_bad_sequence": first_bad,
        "headline": headline,
        "detail": untrusted(detail, "engine"),
    }


def audit_timeline(client: Any, decision_id: str | None = None) -> list[dict[str, Any]]:
    """Decisions and control events in sequence order.

    With ``decision_id`` the timeline is narrowed to that decision's own link; without it the whole chain is
    listed. Entries carry the chain verification outcome, and the failing link is marked as such.
    """
    entries = client_api.chain_entries(client)
    if not entries:
        decisions = client_api.list_decisions(client, limit=200)
        events = client_api.list_control_events(client, limit=200)
        entries = [*decisions, *events]
    rows = sorted((_entry_row(entry) for entry in entries), key=lambda row: row["sequence"])
    if decision_id is not None:
        rows = [row for row in rows if row["kind"] == "decision" and row["id"] == decision_id]
    status = chain_status(client)
    first_bad = status["first_bad_sequence"]
    for row in rows:
        if first_bad is not None and row["sequence"] == first_bad:
            row["chain"] = "BROKEN HERE"
        elif first_bad is not None and row["sequence"] > first_bad:
            row["chain"] = "AFTER THE BREAK"
        elif status["ok"]:
            row["chain"] = "VERIFIED"
        else:
            row["chain"] = "UNVERIFIED"
    return rows


def audit_timeline_view(client: Any, decision_id: str | None = None) -> dict[str, Any]:
    """The timeline plus its chain verification banner."""
    return {
        "view": "audit_timeline",
        "scope": "decision" if decision_id else "tenant chain",
        "entries": audit_timeline(client, decision_id),
        "status": chain_status(client),
    }


# --------------------------------------------------------------------------------------------------------
# 4. Policy editor with diff
# --------------------------------------------------------------------------------------------------------


def _flatten(value: Any, prefix: str = "") -> dict[str, Any]:
    out: dict[str, Any] = {}
    if isinstance(value, dict):
        for key, item in value.items():
            out.update(_flatten(item, f"{prefix}.{key}" if prefix else str(key)))
    elif isinstance(value, list):
        if not value:
            out[prefix] = "[]"
        for index, item in enumerate(value):
            out.update(_flatten(item, f"{prefix}[{index}]"))
    else:
        out[prefix] = value
    return out


def _scalar(value: Any) -> str:
    if value is None:
        return "null"
    if value is True or value is False:
        return "true" if value else "false"
    return str(value)


def policy_diff_rows(
    old: Policy, new: Policy, *, include_unchanged: bool = False
) -> list[dict[str, Any]]:
    """Field-level diff of two policy versions, sorted by field path."""
    left = _flatten(old.model_dump(mode="json"))
    right = _flatten(new.model_dump(mode="json"))
    rows: list[dict[str, Any]] = []
    for path in sorted(set(left) | set(right)):
        before = left.get(path)
        after = right.get(path)
        if path not in right:
            change = "removed"
        elif path not in left:
            change = "added"
        elif before == after:
            change = "unchanged"
        else:
            change = "changed"
        if change == "unchanged" and not include_unchanged:
            continue
        rows.append(
            {
                "path": path,
                "change": change,
                "old": _scalar(before) if path in left else None,
                "new": _scalar(after) if path in right else None,
            }
        )
    return rows


def policy_diff_view(
    client: Any,
    policy_id: str,
    old_version: str,
    new_version: str,
    *,
    old: Policy | None = None,
    new: Policy | None = None,
) -> list[dict[str, Any]]:
    """Field-level policy diff rows. ``old``/``new`` may be supplied directly instead of fetched."""
    left = old if old is not None else client_api.get_policy(client, policy_id, old_version)
    right = new if new is not None else client_api.get_policy(client, policy_id, new_version)
    if left is None or right is None:
        return []
    return policy_diff_rows(left, right)


def policy_editor_view(
    client: Any,
    policy_id: str,
    old_version: str,
    new_version: str,
    *,
    old: Policy | None = None,
    new: Policy | None = None,
) -> dict[str, Any]:
    """The policy editor: two versions side by side, each with its hash, and the field-level diff."""
    left = old if old is not None else client_api.get_policy(client, policy_id, old_version)
    right = new if new is not None else client_api.get_policy(client, policy_id, new_version)
    if left is None or right is None:
        return {
            "view": "policy_editor",
            "available": False,
            "policy_id": untrusted(policy_id, "tenant"),
            "message": "One of the two policy versions is not visible to this tenant.",
        }
    rows = policy_diff_rows(left, right)
    return {
        "view": "policy_editor",
        "available": True,
        "policy_id": untrusted(left.policy_id, "tenant"),
        "old": {"version": left.policy_version, "hash": sha(left.policy_hash)},
        "new": {"version": right.policy_version, "hash": sha(right.policy_hash)},
        "rows": rows,
        "changed_count": sum(1 for row in rows if row["change"] != "unchanged"),
        "note": (
            "Editing here proposes a version. The hash is recomputed from the content when the policy is "
            "loaded, a policy that enables a check this build does not implement is refused at load, and "
            "activation is recorded in the tenant chain as a policy_activated control event."
        ),
    }


# --------------------------------------------------------------------------------------------------------
# 5. Kill switch and the graduated-response level
# --------------------------------------------------------------------------------------------------------


def _latest(events: list[ControlEvent], types: tuple[str, ...]) -> ControlEvent | None:
    matching = [event for event in events if event.event_type in types]
    return max(matching, key=lambda event: event.sequence) if matching else None


def kill_switch_view(client: Any) -> dict[str, Any]:
    """Kill-switch state, derived from the tenant chain, with both controls and their asymmetry."""
    events = client_api.list_control_events(client, limit=200)
    latest = _latest(events, ("kill_switch_activated", "kill_switch_deactivated"))
    reported = client_api.read(client, ("kill_switch_engaged", "kill_switch_state"))
    if not isinstance(reported, client_api.Unavailable) and reported is not None:
        engaged: bool | None = bool(reported)
        source = "client"
    elif latest is not None:
        engaged = latest.event_type == "kill_switch_activated"
        source = "control events"
    else:
        engaged = None
        source = "unknown"
    return {
        "view": "kill_switch",
        "engaged": engaged,
        "source": source,
        "state_label": (
            "ENGAGED - no order is submitted"
            if engaged
            else ("DISENGAGED" if engaged is False else "UNKNOWN - state not reported by this client")
        ),
        "last_event": _control_event_row(latest) if latest is not None else None,
        "engage": {
            "label": "Engage the kill switch",
            "action": "/v1/control/kill-switch",
            "requires_human": False,
            "note": "Engaging is immediate and may also be done automatically by the engine.",
        },
        "disengage": {
            "label": "Disengage the kill switch",
            "action": "/v1/control/kill-switch",
            "requires_human": True,
            "note": (
                "Disengaging requires a named human actor. The ledger refuses a kill_switch_deactivated "
                "event whose actor is the system."
            ),
        },
        "asymmetry": DE_ESCALATION_NOTE,
    }


def response_level_view(client: Any, *, policy: Policy | None = None) -> dict[str, Any]:
    """The graduated-response level 0..5, its ladder, and the escalation/de-escalation asymmetry."""
    events = client_api.list_control_events(client, limit=200)
    latest = _latest(events, ("response_level_changed",))
    reported = client_api.read(client, ("response_level",))
    if not isinstance(reported, client_api.Unavailable) and reported is not None:
        level: int | None = int(reported)
    elif latest is not None:
        level = latest.to_level
    else:
        records = client_api.list_decisions(client, limit=1)
        level = records[0].risk_context.response_level if records else None
        if policy is None and records:
            policy = records[0].policy_snapshot
    ladder = []
    if policy is not None and policy.response_ladder is not None:
        for spec in policy.response_ladder.levels:
            ladder.append(
                {
                    "level": spec.level,
                    "size_multiplier": amount(spec.size_multiplier),
                    "new_risk_allowed": spec.new_risk_allowed,
                    "daily_loss_pct": _ratio_pct(spec.trigger.daily_loss_pct),
                    "drawdown_pct": _ratio_pct(spec.trigger.drawdown_pct),
                }
            )
    return {
        "view": "response_level",
        "level": level,
        "max_level": MAX_RESPONSE_LEVEL,
        "levels": [
            {"level": index, "active": level == index} for index in range(MAX_RESPONSE_LEVEL + 1)
        ],
        "ladder": ladder,
        "escalation": "automatic",
        "de_escalation": "requires a human",
        "asymmetry": DE_ESCALATION_NOTE,
        "de_escalate": {
            "label": "Lower the response level",
            "action": "/v1/control/response-level",
            "requires_human": True,
            "requires_actor_id": True,
        },
        "last_change": _control_event_row(latest) if latest is not None else None,
    }
