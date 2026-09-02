"""L3 — broker and framework adapters.

The :class:`BrokerAdapter` Protocol deliberately has no ``cancel_order``, ``replace_order``,
``close_position`` or ``close_all_positions``. Cancel/replace automation is out of scope for v1
(Hard Rule B4), and the way to guarantee that is to give the abstraction no vocabulary for it.

``AlpacaPaperBroker.from_environment`` proves paper mode before it constructs a client, so a
misconfigured environment fails before any network access (B1).

Layout:

``base``           the Protocols and the two wire types (``OrderRequest``, ``BrokerOrder``)
``mock``           a scriptable in-memory broker for tests and demos
``alpaca_paper``   the Alpaca PAPER adapter
``context``        ``BrokerContextProvider`` - where the engine's inputs are assembled (ADR-0006)
``tradingagents``  the W8 framework adapter
"""

from __future__ import annotations

from mizan.adapters.alpaca_paper import AlpacaPaperBroker
from mizan.adapters.base import (
    PAPER_HOST,
    BrokerAdapter,
    BrokerOrder,
    ContextProvider,
    OrderRequest,
)
from mizan.adapters.context import BrokerContextProvider
from mizan.adapters.mock import MockBroker

__all__ = [
    "PAPER_HOST",
    "AlpacaPaperBroker",
    "BrokerAdapter",
    "BrokerContextProvider",
    "BrokerOrder",
    "ContextProvider",
    "MockBroker",
    "OrderRequest",
]
