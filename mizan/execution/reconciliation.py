"""Reconciliation: comparing what Mizan believes about an order with what the broker reports.

**This module reads. It never writes.** There is no cancel, no replace, no close and no re-submit here,
and there is no code path that reaches ``BrokerAdapter.submit_order``: the adapter Protocol exposes one
mutation and reconciliation does not use it (Hard Rule B4). Automated remediation is out of scope for
v1 precisely because a reconciler that can act is a second, unaudited execution path — one that runs on
a timer, with no authorization, no policy evaluation and no kill-switch check in front of it.

So the output is a *report*, and a human or the console decides what to do with it. Two discrepancies
matter most:

``MISSING_AT_BROKER``
    Mizan recorded a submission the broker has never heard of. Usually a broker that accepted the
    request and lost it, or a result recorded against the wrong account.

``UNEXPECTED_AT_BROKER``
    The broker holds an order for an authorization Mizan believes it BLOCKED. This is the serious one:
    a mutation exists that the gate says it never made. It is the shape a gate bypass would take, so it
    is reported as its own status rather than folded into a generic mismatch.

Every lookup goes through the idempotency key, which is derived from the tenant, the proposal and the
authorized legs — so the question "did this decision reach the venue?" has exactly one answer to look
for, and asking it twice is free.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from typing import TYPE_CHECKING, Literal

from pydantic import ConfigDict, Field

from mizan.contracts import (
    ContractModel,
    ExecutionResult,
    NonEmptyStr,
    Rfc3339,
    format_ts,
)
from mizan.contracts.errors import MizanError

if TYPE_CHECKING:  # pragma: no cover - typing only
    from mizan.adapters import BrokerAdapter, BrokerOrder

__all__ = [
    "DISCREPANCY_STATUSES",
    "ReconciliationItem",
    "ReconciliationReport",
    "ReconciliationStatus",
    "Reconciler",
]

ReconciliationStatus = Literal[
    "MATCHED",
    "STATUS_DIVERGED",
    "MISSING_AT_BROKER",
    "UNEXPECTED_AT_BROKER",
    "ABSENT_AS_EXPECTED",
    "BROKER_UNAVAILABLE",
    "NOT_APPLICABLE",
]

#: The statuses that need a human. ``MATCHED`` and ``ABSENT_AS_EXPECTED`` are agreement; the rest are not.
DISCREPANCY_STATUSES: frozenset[str] = frozenset(
    {"STATUS_DIVERGED", "MISSING_AT_BROKER", "UNEXPECTED_AT_BROKER", "BROKER_UNAVAILABLE"}
)

#: Statuses of an ExecutionResult that assert a broker order should exist.
_SUBMITTED_STATUSES = frozenset({"SUBMITTED", "RECONCILED_EXISTING"})
#: Statuses that assert nothing reached the broker. A FAILED submission is genuinely unknown, so it is
#: not in either set: an order may or may not exist and both answers are acceptable.
_NO_ORDER_STATUSES = frozenset({"BLOCKED", "WOULD_SUBMIT"})


class ReconciliationItem(ContractModel):
    """One authorization, as Mizan recorded it and as the broker answers for it."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    result_id: NonEmptyStr
    auth_id: NonEmptyStr
    client_order_id: NonEmptyStr | None
    mizan_status: NonEmptyStr
    broker_order_id: NonEmptyStr | None
    broker_status: NonEmptyStr | None
    status: ReconciliationStatus
    detail: str = Field(default="", max_length=4000)

    @property
    def is_discrepancy(self) -> bool:
        return self.status in DISCREPANCY_STATUSES


class ReconciliationReport(ContractModel):
    """What every reconciled authorization looked like at ``as_of``. Read-only, by construction."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    as_of: Rfc3339
    broker: NonEmptyStr
    items: list[ReconciliationItem] = Field(default_factory=list)

    @property
    def discrepancies(self) -> list[ReconciliationItem]:
        return [item for item in self.items if item.is_discrepancy]

    @property
    def clean(self) -> bool:
        """True when Mizan and the broker agree about every authorization examined."""
        return not self.discrepancies

    def by_status(self, status: str) -> list[ReconciliationItem]:
        return [item for item in self.items if item.status == status]


class Reconciler:
    """Reads a broker and reports how its view differs from Mizan's. It has no way to change either.

    The absence of a remediation method is the design. Anything this class could "fix" is a broker
    mutation, and every broker mutation in this system goes through :class:`~mizan.execution.
    ExecutionGate` with an authorization in front of it — including the ones that only put things back.
    """

    def __init__(self, broker: BrokerAdapter) -> None:
        self.broker = broker

    def reconcile(
        self, results: Sequence[ExecutionResult], *, as_of: datetime
    ) -> ReconciliationReport:
        """One read per result, in order. A broker failure is recorded, never raised over the rest."""
        return ReconciliationReport(
            as_of=format_ts(as_of),
            broker=self.broker.name,
            items=[self.reconcile_one(result) for result in results],
        )

    def reconcile_one(self, result: ExecutionResult) -> ReconciliationItem:
        client_order_id = result.client_order_id
        if client_order_id is None:
            return _item(
                result,
                status="NOT_APPLICABLE",
                detail="the result names no client order id, so there is nothing to look up",
            )
        try:
            order = self.broker.find_order(client_order_id)
        except MizanError as failure:
            # An unreachable broker is a discrepancy, not an agreement: "I could not ask" is never "fine".
            return _item(result, status="BROKER_UNAVAILABLE", detail=failure.code.value)
        return _classify(result, order)


def _classify(result: ExecutionResult, order: BrokerOrder | None) -> ReconciliationItem:
    expected = result.status in _SUBMITTED_STATUSES
    if order is None:
        if expected:
            return _item(
                result,
                status="MISSING_AT_BROKER",
                detail="Mizan recorded a submission the broker does not report",
            )
        return _item(result, status="ABSENT_AS_EXPECTED")
    if result.status in _NO_ORDER_STATUSES:
        # The gate says it never mutated anything, and the broker holds an order anyway.
        return _item(
            result,
            status="UNEXPECTED_AT_BROKER",
            broker_order_id=order.broker_order_id,
            broker_status=order.status,
            detail=f"the broker holds an order for an execution Mizan recorded as {result.status}",
        )
    if result.broker_order_id is not None and result.broker_order_id != order.broker_order_id:
        return _item(
            result,
            status="STATUS_DIVERGED",
            broker_order_id=order.broker_order_id,
            broker_status=order.status,
            detail="the broker order id differs from the one recorded at submission",
        )
    if result.broker_status is not None and result.broker_status != order.status:
        return _item(
            result,
            status="STATUS_DIVERGED",
            broker_order_id=order.broker_order_id,
            broker_status=order.status,
            detail="the broker reports a different order status than the one recorded",
        )
    return _item(
        result,
        status="MATCHED",
        broker_order_id=order.broker_order_id,
        broker_status=order.status,
    )


def _item(
    result: ExecutionResult,
    *,
    status: ReconciliationStatus,
    broker_order_id: str | None = None,
    broker_status: str | None = None,
    detail: str = "",
) -> ReconciliationItem:
    return ReconciliationItem(
        result_id=result.result_id,
        auth_id=result.auth_id,
        client_order_id=result.client_order_id,
        mizan_status=result.status,
        broker_order_id=broker_order_id,
        broker_status=broker_status,
        status=status,
        detail=detail,
    )
