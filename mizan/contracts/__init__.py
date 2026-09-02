"""``mizan.contracts`` -- the frozen data contracts (L0). Re-exports every public name of its modules.

Source of truth for shapes: ``contracts/*.schema.json``. Source of truth for the derivation rules: ``contracts/CANONICAL.md``.
"""

from mizan.contracts import (
    canonical,
    control_event,
    decision_record,
    errors,
    execution_authorization,
    execution_result,
    governor_decision,
    policy,
    reason_codes,
    risk_context,
    risk_evaluation,
    trade_proposal,
    types,
)
from mizan.contracts._base import ContractModel, build_hashed
from mizan.contracts.canonical import *  # noqa: F403
from mizan.contracts.control_event import *  # noqa: F403
from mizan.contracts.decision_record import *  # noqa: F403
from mizan.contracts.errors import *  # noqa: F403
from mizan.contracts.execution_authorization import *  # noqa: F403
from mizan.contracts.execution_result import *  # noqa: F403
from mizan.contracts.governor_decision import *  # noqa: F403
from mizan.contracts.policy import *  # noqa: F403
from mizan.contracts.reason_codes import *  # noqa: F403
from mizan.contracts.risk_context import *  # noqa: F403
from mizan.contracts.risk_evaluation import *  # noqa: F403
from mizan.contracts.trade_proposal import *  # noqa: F403
from mizan.contracts.types import *  # noqa: F403

MODULES = (
    types,
    canonical,
    reason_codes,
    errors,
    trade_proposal,
    risk_context,
    policy,
    risk_evaluation,
    governor_decision,
    execution_authorization,
    execution_result,
    decision_record,
    control_event,
)

# The top-level contract objects, in object-chain order, with the JSON schema file each is described by.
TOP_LEVEL_CONTRACTS: dict[str, type[ContractModel]] = {
    "trade_proposal": trade_proposal.TradeProposal,
    "risk_context": risk_context.RiskContext,
    "policy": policy.Policy,
    "risk_evaluation": risk_evaluation.RiskEvaluation,
    "governor_decision": governor_decision.GovernorDecision,
    "execution_authorization": execution_authorization.ExecutionAuthorization,
    "execution_result": execution_result.ExecutionResult,
    "decision_record": decision_record.DecisionRecord,
    "control_event": control_event.ControlEvent,
}

__all__ = ["MODULES", "TOP_LEVEL_CONTRACTS", "ContractModel", "build_hashed"]
for _module in MODULES:
    __all__.extend(_module.__all__)
del _module
