"""Structure fingerprinting.

The Governor may reduce the number of contracts. It may not change anything
else. A fingerprint makes that enforceable rather than merely intended: a
SHA-256 digest over every field that defines *what* is being traded, with
quantity deliberately excluded so an approved reduction does not invalidate the
authorization it is attached to.

Changing a leg, a strike, an expiry, a side, an option type, a ratio, the
strategy, or the underlying all produce a different digest. Reordering the legs
does not — leg order carries no meaning, so the digest is computed over a
canonically sorted structure.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

from app.models import TradeProposal
from app.options.proposal import OptionTradeProposal

FINGERPRINT_VERSION = "v1"


def structure_fingerprint(proposal: OptionTradeProposal | TradeProposal) -> str:
    """Return a stable digest of a proposal's immutable trade structure."""

    return "sha256:" + hashlib.sha256(
        json.dumps(
            structure_payload(proposal),
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        ).encode("utf-8")
    ).hexdigest()


def structure_payload(proposal: OptionTradeProposal | TradeProposal) -> dict[str, Any]:
    """The canonical structure a fingerprint is taken over.

    Exposed separately so an audit event can record exactly what was bound,
    not just the digest of it.
    """

    if isinstance(proposal, OptionTradeProposal):
        return {
            "version": FINGERPRINT_VERSION,
            "instrument_type": "option",
            "underlying": proposal.underlying,
            "strategy": proposal.strategy.value,
            "expiry": proposal.expiry.isoformat(),
            "contract_multiplier": proposal.contract_multiplier,
            "legs": sorted(
                (
                    {
                        "option_symbol": leg.option_symbol,
                        "side": leg.side.value,
                        "option_type": leg.option_type.value,
                        "strike": f"{leg.strike:.4f}",
                        "expiry": leg.expiry.isoformat(),
                        "ratio": leg.ratio,
                        "position_effect": leg.position_effect,
                    }
                    for leg in proposal.legs
                ),
                key=lambda leg: (leg["option_symbol"], leg["side"]),
            ),
        }

    return {
        "version": FINGERPRINT_VERSION,
        "instrument_type": "equity",
        "symbol": proposal.symbol,
        "side": proposal.side.value,
    }
