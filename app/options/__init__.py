"""Options domain: OCC symbols, defined-risk structures, and fingerprinting.

Phase 1 is pure. Nothing in this package performs I/O, touches the database,
reads a clock, or calls a broker. Risk-engine integration, persistence, and
Alpaca submission are later phases.
"""

from app.options.fingerprint import (
    FINGERPRINT_VERSION,
    structure_fingerprint,
    structure_payload,
)
from app.options.money import (
    ABSOLUTE_MONEY_TOLERANCE,
    RELATIVE_MONEY_TOLERANCE,
    money_equal,
    to_decimal,
    to_money,
    to_ratio,
)
from app.options.occ import OccSymbol, OccSymbolError, parse_occ_symbol, strikes_equal
from app.options.proposal import (
    CREDIT_STRATEGIES,
    LEG_COUNTS,
    MAX_LEGS,
    InvalidOptionEconomics,
    OptionEconomics,
    OptionLeg,
    OptionStrategy,
    OptionTradeProposal,
    OptionType,
    ProfitBound,
    recompute_economics,
    risk_width_of,
    wing_width,
)
from app.options.risk import (
    FLAG_WEIGHTS,
    OptionMarketContext,
    OptionRiskCheck,
    OptionRiskEngine,
    OptionRiskFlag,
    OptionRiskPolicy,
    OptionRiskReport,
)

__all__ = [
    "ABSOLUTE_MONEY_TOLERANCE",
    "CREDIT_STRATEGIES",
    "FLAG_WEIGHTS",
    "FINGERPRINT_VERSION",
    "LEG_COUNTS",
    "MAX_LEGS",
    "InvalidOptionEconomics",
    "OccSymbol",
    "OccSymbolError",
    "OptionEconomics",
    "OptionLeg",
    "OptionMarketContext",
    "OptionRiskCheck",
    "OptionRiskEngine",
    "OptionRiskFlag",
    "OptionRiskPolicy",
    "OptionRiskReport",
    "OptionStrategy",
    "OptionTradeProposal",
    "OptionType",
    "ProfitBound",
    "RELATIVE_MONEY_TOLERANCE",
    "money_equal",
    "parse_occ_symbol",
    "recompute_economics",
    "risk_width_of",
    "strikes_equal",
    "structure_fingerprint",
    "structure_payload",
    "to_decimal",
    "to_money",
    "to_ratio",
    "wing_width",
]
