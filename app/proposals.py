"""The discriminated proposal union.

Kept in its own module so ``app.models`` never has to import the options
package: ``app.options.proposal`` depends on ``app.models`` for ``Side``, and a
union defined here keeps that dependency one-directional.

The discriminator is a callable rather than a plain field name for one specific
reason. Every ``proposals`` row written before options support was serialized
without an ``instrument_type`` key. A callable tag resolves an absent key to
``equity``, so all historical rows stay readable with no data migration and no
rewrite of stored JSON.
"""

from __future__ import annotations

from typing import Annotated, Any, Union

from pydantic import Discriminator, Tag, TypeAdapter

from app.models import InstrumentType, TradeProposal
from app.options.proposal import OptionTradeProposal


def instrument_tag(value: Any) -> str:
    """Resolve which member of the union a payload belongs to.

    An absent tag means the payload predates options support, which makes it an
    equity proposal.
    """

    if isinstance(value, dict):
        raw = value.get("instrument_type") or value.get(b"instrument_type")
    else:
        raw = getattr(value, "instrument_type", None)
    if raw is None:
        return InstrumentType.EQUITY.value
    return str(getattr(raw, "value", raw))


AnyTradeProposal = Annotated[
    Union[
        Annotated[TradeProposal, Tag(InstrumentType.EQUITY.value)],
        Annotated[OptionTradeProposal, Tag(InstrumentType.OPTION.value)],
    ],
    Discriminator(instrument_tag),
]

TRADE_PROPOSAL_ADAPTER: TypeAdapter[Any] = TypeAdapter(AnyTradeProposal)


def parse_trade_proposal(payload: Any) -> TradeProposal | OptionTradeProposal:
    """Validate a proposal payload into the correct member of the union."""

    return TRADE_PROPOSAL_ADAPTER.validate_python(payload)


def parse_trade_proposal_json(raw: str | bytes) -> TradeProposal | OptionTradeProposal:
    """Validate stored proposal JSON, including rows written before options."""

    return TRADE_PROPOSAL_ADAPTER.validate_json(raw)
