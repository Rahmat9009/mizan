from __future__ import annotations

import os
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from app.models import Decision, GovernorDecision, MarketRiskSnapshot, RiskReport, TradeProposal
from app.execution.models import ExecutionAuthorization, ExecutionMode, ExecutionState


TRUE_VALUES = frozenset({"1", "true", "yes", "on"})
FALSE_VALUES = frozenset({"0", "false", "no", "off"})
SYMBOL_PATTERN = re.compile(r"^[A-Z][A-Z0-9.-]{0,15}$")


class ExecutionConfigurationError(RuntimeError):
    pass


class ExecutionBlocked(RuntimeError):
    def __init__(self, state: ExecutionState, message: str) -> None:
        super().__init__(message)
        self.state = state
        self.message = message


def _env_bool(name: str, *, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    normalized = raw.strip().casefold()
    if normalized in TRUE_VALUES:
        return True
    if normalized in FALSE_VALUES:
        return False
    raise ExecutionConfigurationError(f"{name} must be a true/false value.")


@dataclass(frozen=True)
class ExecutionConfig:
    paper: bool = True
    enabled: bool = False
    dry_run: bool = True
    kill_switch: bool = False
    max_decision_age_seconds: int = 120

    @property
    def mode(self) -> ExecutionMode:
        return ExecutionMode.PAPER_DRY_RUN if self.dry_run else ExecutionMode.PAPER

    @classmethod
    def from_environment(cls) -> "ExecutionConfig":
        raw_age = os.getenv("EXECUTION_MAX_DECISION_AGE_SECONDS", "120").strip()
        try:
            max_age = int(raw_age)
        except ValueError as exc:
            raise ExecutionConfigurationError(
                "EXECUTION_MAX_DECISION_AGE_SECONDS must be an integer."
            ) from exc
        if not 1 <= max_age <= 3600:
            raise ExecutionConfigurationError(
                "EXECUTION_MAX_DECISION_AGE_SECONDS must be between 1 and 3600."
            )
        return cls(
            paper=_env_bool("ALPACA_PAPER", default=True),
            enabled=_env_bool("ALPACA_EXECUTION_ENABLED", default=False),
            dry_run=_env_bool("ALPACA_EXECUTION_DRY_RUN", default=True),
            kill_switch=_env_bool("ALPACA_EXECUTION_KILL_SWITCH", default=False),
            max_decision_age_seconds=max_age,
        )


class ExecutionGate:
    def __init__(self, config: ExecutionConfig) -> None:
        self.config = config

    def authorize(
        self,
        proposal: TradeProposal | None,
        hard_risk: RiskReport | None,
        governor: GovernorDecision | None,
        market: MarketRiskSnapshot | None,
        *,
        now: datetime | None = None,
    ) -> ExecutionAuthorization:
        checked_now = now or datetime.now(timezone.utc)
        self._configuration_gate()
        if proposal is None or hard_risk is None or governor is None or market is None:
            raise ExecutionBlocked(
                ExecutionState.BLOCKED,
                "Proposal, hard risk, governor decision, and market risk are all required.",
            )
        if not SYMBOL_PATTERN.fullmatch(proposal.symbol):
            raise ExecutionBlocked(ExecutionState.BLOCKED, "Proposal symbol is malformed.")
        if market.symbol != proposal.symbol:
            raise ExecutionBlocked(ExecutionState.BLOCKED, "Market-risk symbol does not match proposal.")
        if hard_risk.proposal_id != proposal.proposal_id or hard_risk.symbol != proposal.symbol:
            raise ExecutionBlocked(ExecutionState.BLOCKED, "Hard-risk identity does not match proposal.")
        if hard_risk.original_quantity != proposal.quantity:
            raise ExecutionBlocked(ExecutionState.BLOCKED, "Hard-risk quantity does not match proposal.")
        if hard_risk.blocked:
            raise ExecutionBlocked(ExecutionState.BLOCKED, "Deterministic hard risk blocked execution.")
        if governor.proposal_id != proposal.proposal_id:
            raise ExecutionBlocked(ExecutionState.BLOCKED, "Governor proposal ID does not match.")
        if governor.symbol != proposal.symbol or governor.side != proposal.side:
            raise ExecutionBlocked(ExecutionState.BLOCKED, "Governor symbol or side does not match proposal.")
        if governor.original_quantity != proposal.quantity:
            raise ExecutionBlocked(ExecutionState.BLOCKED, "Governor original quantity does not match proposal.")
        if governor.decision == Decision.REJECT:
            raise ExecutionBlocked(ExecutionState.BLOCKED, "Governor rejected the proposal.")
        if governor.decision not in {Decision.APPROVE, Decision.REDUCE}:
            raise ExecutionBlocked(ExecutionState.BLOCKED, "Governor decision is not executable.")
        if governor.approved_quantity <= 0:
            raise ExecutionBlocked(ExecutionState.BLOCKED, "Governor approved quantity must be positive.")
        if governor.approved_quantity > proposal.quantity:
            raise ExecutionBlocked(ExecutionState.BLOCKED, "Governor quantity exceeds proposal quantity.")
        if governor.approved_quantity > hard_risk.recommended_quantity:
            raise ExecutionBlocked(ExecutionState.BLOCKED, "Governor quantity exceeds hard-risk cap.")

        self._validate_decision_age(governor.decided_at, checked_now)
        authorization = ExecutionAuthorization(
            proposal_id=proposal.proposal_id,
            symbol=proposal.symbol,
            side=proposal.side,
            original_quantity=proposal.quantity,
            approved_quantity=governor.approved_quantity,
            governor_decision=governor.decision,
            governor_decided_at=governor.decided_at,
            authorization_created_at=checked_now,
            risk_score=governor.risk_score,
        )
        self.validate_authorization(
            authorization,
            proposal,
            hard_risk,
            governor,
            market,
            now=checked_now,
        )
        return authorization

    def validate_authorization(
        self,
        authorization: ExecutionAuthorization,
        proposal: TradeProposal,
        hard_risk: RiskReport,
        governor: GovernorDecision,
        market: MarketRiskSnapshot,
        *,
        now: datetime | None = None,
    ) -> None:
        checked_now = now or datetime.now(timezone.utc)
        self._configuration_gate()
        if hard_risk.proposal_id != proposal.proposal_id or hard_risk.symbol != proposal.symbol:
            raise ExecutionBlocked(ExecutionState.BLOCKED, "Hard-risk identity does not match proposal.")
        if hard_risk.original_quantity != proposal.quantity:
            raise ExecutionBlocked(ExecutionState.BLOCKED, "Hard-risk quantity does not match proposal.")
        if governor.proposal_id != proposal.proposal_id:
            raise ExecutionBlocked(ExecutionState.BLOCKED, "Governor proposal ID does not match.")
        if governor.symbol != proposal.symbol or governor.side != proposal.side:
            raise ExecutionBlocked(ExecutionState.BLOCKED, "Governor symbol or side does not match proposal.")
        if governor.original_quantity != proposal.quantity:
            raise ExecutionBlocked(ExecutionState.BLOCKED, "Governor original quantity does not match proposal.")
        if governor.decision not in {Decision.APPROVE, Decision.REDUCE}:
            raise ExecutionBlocked(ExecutionState.BLOCKED, "Governor decision is not executable.")
        if governor.approved_quantity <= 0 or governor.approved_quantity > proposal.quantity:
            raise ExecutionBlocked(ExecutionState.BLOCKED, "Governor quantity is not executable.")
        if authorization.proposal_id != proposal.proposal_id:
            raise ExecutionBlocked(ExecutionState.BLOCKED, "Authorization proposal ID does not match.")
        if authorization.symbol != proposal.symbol or authorization.side != proposal.side:
            raise ExecutionBlocked(ExecutionState.BLOCKED, "Authorization symbol or side does not match.")
        if authorization.original_quantity != proposal.quantity:
            raise ExecutionBlocked(ExecutionState.BLOCKED, "Authorization original quantity does not match.")
        if authorization.approved_quantity != governor.approved_quantity:
            raise ExecutionBlocked(ExecutionState.BLOCKED, "Authorization quantity does not match Governor.")
        if authorization.governor_decision != governor.decision:
            raise ExecutionBlocked(ExecutionState.BLOCKED, "Authorization decision does not match Governor.")
        if authorization.governor_decided_at != governor.decided_at:
            raise ExecutionBlocked(ExecutionState.BLOCKED, "Authorization timestamp does not match Governor.")
        if authorization.risk_score != governor.risk_score:
            raise ExecutionBlocked(ExecutionState.BLOCKED, "Authorization risk score does not match Governor.")
        if hard_risk.blocked or authorization.approved_quantity > hard_risk.recommended_quantity:
            raise ExecutionBlocked(ExecutionState.BLOCKED, "Authorization violates deterministic hard risk.")
        if market.symbol != authorization.symbol:
            raise ExecutionBlocked(ExecutionState.BLOCKED, "Authorization market symbol does not match.")
        if authorization.authorization_created_at.tzinfo is None:
            raise ExecutionBlocked(ExecutionState.BLOCKED, "Authorization timestamp must be timezone-aware.")
        authorization_delay = (
            authorization.authorization_created_at.astimezone(timezone.utc)
            - governor.decided_at.astimezone(timezone.utc)
        ).total_seconds()
        if authorization_delay < -5:
            raise ExecutionBlocked(ExecutionState.BLOCKED, "Authorization predates Governor decision.")
        if authorization.authorization_created_at > checked_now + timedelta(seconds=5):
            raise ExecutionBlocked(ExecutionState.BLOCKED, "Authorization timestamp is in the future.")
        self._validate_decision_age(authorization.governor_decided_at, checked_now)

    def _configuration_gate(self) -> None:
        if not self.config.paper:
            raise ExecutionBlocked(
                ExecutionState.BLOCKED,
                "Live trading is unsupported; ALPACA_PAPER must be true.",
            )
        if self.config.kill_switch:
            raise ExecutionBlocked(
                ExecutionState.KILL_SWITCH_ACTIVE,
                "Execution kill switch is active.",
            )
        if not self.config.enabled:
            raise ExecutionBlocked(
                ExecutionState.DISABLED,
                "Alpaca paper execution is disabled.",
            )

    def _validate_decision_age(self, decided_at: datetime, now: datetime) -> None:
        if decided_at.tzinfo is None or now.tzinfo is None:
            raise ExecutionBlocked(ExecutionState.BLOCKED, "Execution timestamps must be timezone-aware.")
        age = (now.astimezone(timezone.utc) - decided_at.astimezone(timezone.utc)).total_seconds()
        if age < -5:
            raise ExecutionBlocked(ExecutionState.BLOCKED, "Governor decision timestamp is in the future.")
        if age > self.config.max_decision_age_seconds:
            raise ExecutionBlocked(
                ExecutionState.STALE_AUTHORIZATION,
                "Governor decision is too old; reauthorization is required.",
            )
