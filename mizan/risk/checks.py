"""One function per check id, dispatched through a registry so the ORDER is data, not control flow.

Each function has the same signature -- ``(proposal, context, policy) -> CheckResult | None`` -- and is
pure: it reads the proposal, the context's snapshots and the policy, and nothing else. ``None`` means
"nothing to report", which the engine records as a passing result.

Three rules shape every function here:

* **Missing state blocks** (Hard Rule E2 / R-RUIN-4). A check that cannot see the data it needs fails
  BLOCKING with the matching ``*_MISSING`` code. It never assumes zero and never quietly passes.
* **Valuation comes from the snapshots** (findings F-1/F-2). ``leg.limit_price`` is read by
  ``erroneous_order`` and nowhere else; every capital figure comes from a quote or an option mark.
* **The policy's severity decides the shape of a breach.** A breached limit configured ``blocking``
  rejects (``recommended_quantity`` "0"); configured ``warning`` it caps the order and the engine
  takes the minimum cap. A code whose catalogue severity is a warning (a size scaling, a haircut) can
  never be reported as blocking -- it is a reduction by construction, not a refusal.
"""

from __future__ import annotations

from collections.abc import Callable
from decimal import Decimal

from mizan.contracts import (
    REASON_CODE_INFO,
    CheckResult,
    Policy,
    ReasonCode,
    ResponseLevelSpec,
    RiskContext,
    Severity,
    TradeProposal,
    dec,
    dstr,
    parse_ts,
)
from mizan.risk.valuation import (
    BASIS_POINTS,
    HALF,
    ONE,
    ZERO,
    add,
    apply_multiplier,
    cap_from_budget,
    days_to_expiry,
    divide,
    equity_of,
    exposure_of,
    floor_units,
    greek_change,
    gross_exposure_of,
    leg_reference_price,
    minutes_of_day,
    minutes_of_hhmm,
    multiply,
    option_quote_for,
    portfolio_greek,
    quote_for,
    sector_exposure,
    sector_of,
    subtract,
    symbol_exposure,
)

CheckFunction = Callable[[TradeProposal, RiskContext, Policy], CheckResult | None]

#: Codes the catalogue itself marks as non-blocking: a reduction or a note, never a refusal.
NON_BLOCKING_CODES: frozenset[ReasonCode] = frozenset(
    code for code, info in REASON_CODE_INFO.items() if info.default_severity != "blocking"
)

#: The codes that mean "the engine could not see something it needed"; they clear ``data_complete``.
MISSING_DATA_CODES: frozenset[ReasonCode] = frozenset(
    {
        ReasonCode.MARKET_DATA_MISSING,
        ReasonCode.PRICE_MISSING,
        ReasonCode.GREEKS_MISSING,
        ReasonCode.PORTFOLIO_STATE_MISSING,
        ReasonCode.BUYING_POWER_MISSING,
        ReasonCode.SECTOR_DATA_MISSING,
        ReasonCode.PATH_STATE_MISSING,
        ReasonCode.AGGREGATE_STATE_MISSING,
        ReasonCode.AGENT_STATE_MISSING,
        ReasonCode.CALENDAR_MISSING,
        ReasonCode.LIQUIDITY_DATA_MISSING,
    }
)

#: Sessions in which an order may be placed at all; the minute windows narrow it further.
TRADEABLE_SESSIONS: frozenset[str] = frozenset({"open", "close"})
CLOSE_INTENT = "close"
OPEN_INTENT = "open"


# ---------------------------------------------------------------------------------------------------
# Result builders
# ---------------------------------------------------------------------------------------------------
def severity_for(policy: Policy, check_id: str, code: ReasonCode) -> Severity:
    """The policy's severity for the check, never stronger than the code's own catalogue severity."""
    if code in NON_BLOCKING_CODES:
        return "warning"
    return policy.check_config(check_id).severity


def _result(
    check_id: str,
    *,
    passed: bool,
    severity: Severity,
    code: ReasonCode | None = None,
    threshold: Decimal | str | None = None,
    actual: Decimal | str | None = None,
    cap: Decimal | None = None,
    source: str | None = None,
    snapshot_ts: str | None = None,
    detail: str = "",
) -> CheckResult:
    return CheckResult(
        check_id=check_id,
        passed=passed,
        severity=severity,
        reason_code=code,
        threshold=_as_str(threshold),
        actual=_as_str(actual),
        data_source=source,
        snapshot_ts=snapshot_ts,
        recommended_quantity=None if cap is None else dstr(cap),
        detail=detail[:4000],
    )


def _as_str(value: Decimal | str | None) -> str | None:
    if value is None:
        return None
    return value if isinstance(value, str) else dstr(value)


def ok(
    check_id: str,
    policy: Policy,
    *,
    threshold: Decimal | str | None = None,
    actual: Decimal | str | None = None,
    source: str | None = None,
    snapshot_ts: str | None = None,
    detail: str = "",
) -> CheckResult:
    """A passing result at the policy's configured severity."""
    return _result(
        check_id,
        passed=True,
        severity=policy.check_config(check_id).severity,
        threshold=threshold,
        actual=actual,
        source=source,
        snapshot_ts=snapshot_ts,
        detail=detail,
    )


def fail(
    check_id: str,
    policy: Policy,
    code: ReasonCode,
    *,
    threshold: Decimal | str | None = None,
    actual: Decimal | str | None = None,
    cap: Decimal | None = None,
    source: str | None = None,
    snapshot_ts: str | None = None,
    detail: str = "",
) -> CheckResult:
    """A breach. Blocking rejects (cap 0); warning caps the order at ``cap``."""
    severity = severity_for(policy, check_id, code)
    return _result(
        check_id,
        passed=False,
        severity=severity,
        code=code,
        threshold=threshold,
        actual=actual,
        cap=ZERO if severity == "blocking" else cap,
        source=source,
        snapshot_ts=snapshot_ts,
        detail=detail,
    )


def reduce_to(
    check_id: str,
    policy: Policy,
    code: ReasonCode,
    cap: Decimal,
    *,
    threshold: Decimal | str | None = None,
    actual: Decimal | str | None = None,
    source: str | None = None,
    snapshot_ts: str | None = None,
    detail: str = "",
) -> CheckResult:
    """A reduction that is not a refusal: always recorded as a warning carrying its cap."""
    return _result(
        check_id,
        passed=False,
        severity="warning",
        code=code,
        threshold=threshold,
        actual=actual,
        cap=cap,
        source=source,
        snapshot_ts=snapshot_ts,
        detail=detail,
    )


def missing(
    check_id: str,
    code: ReasonCode,
    detail: str,
    *,
    source: str | None = None,
    snapshot_ts: str | None = None,
) -> CheckResult:
    """Unknown risk is not safe: a blocking failure with a cap of zero, whatever the policy says."""
    return _result(
        check_id,
        passed=False,
        severity="blocking",
        code=code,
        cap=ZERO,
        source=source,
        snapshot_ts=snapshot_ts,
        detail=detail,
    )


def _market_ts(context: RiskContext) -> str | None:
    return None if context.market_snapshot is None else context.market_snapshot.as_of


def _market_source(context: RiskContext) -> str | None:
    return None if context.market_snapshot is None else context.market_snapshot.source


def _portfolio_ts(context: RiskContext) -> str | None:
    return None if context.portfolio_snapshot is None else context.portfolio_snapshot.as_of


def _portfolio_source(context: RiskContext) -> str | None:
    return None if context.portfolio_snapshot is None else context.portfolio_snapshot.source


def _pct(value: Decimal, total: Decimal) -> Decimal | None:
    return divide(value, total)


def is_risk_increasing(proposal: TradeProposal, context: RiskContext) -> bool:
    """``open`` always; ``adjust`` when it raises gross exposure; ``close`` never (Addendum 1 C)."""
    if proposal.intent == OPEN_INTENT:
        return True
    if proposal.intent == CLOSE_INTENT:
        return False
    return exposure_of(proposal, context).change > ZERO


# ---------------------------------------------------------------------------------------------------
# Always-on presence checks
# ---------------------------------------------------------------------------------------------------
def market_data_presence(proposal: TradeProposal, context: RiskContext, policy: Policy) -> CheckResult | None:
    market = context.market_snapshot
    if market is None:
        return missing(
            "market_data_presence", ReasonCode.MARKET_DATA_MISSING, "no market snapshot in the context"
        )
    if quote_for(market, proposal.symbol) is None:
        return missing(
            "market_data_presence",
            ReasonCode.PRICE_MISSING,
            f"no quote for {proposal.symbol} in snapshot {market.snapshot_id}",
            source=market.source,
            snapshot_ts=market.as_of,
        )
    for leg in proposal.legs:
        if leg.is_option and option_quote_for(market, leg.occ_symbol(proposal.symbol)) is None:
            return missing(
                "market_data_presence",
                ReasonCode.PRICE_MISSING,
                f"no option quote for leg {leg.leg_index} ({leg.occ_symbol(proposal.symbol)})",
                source=market.source,
                snapshot_ts=market.as_of,
            )
    return ok(
        "market_data_presence",
        policy,
        source=market.source,
        snapshot_ts=market.as_of,
        detail=f"quoted from snapshot {market.snapshot_id}",
    )


def portfolio_state_presence(
    proposal: TradeProposal, context: RiskContext, policy: Policy
) -> CheckResult | None:
    portfolio = context.portfolio_snapshot
    if portfolio is None:
        return missing(
            "portfolio_state_presence", ReasonCode.PORTFOLIO_STATE_MISSING, "no portfolio snapshot"
        )
    return ok(
        "portfolio_state_presence",
        policy,
        source=portfolio.source,
        snapshot_ts=portfolio.as_of,
        detail=f"portfolio snapshot {portfolio.snapshot_id}",
    )


def proposal_expiry(proposal: TradeProposal, context: RiskContext, policy: Policy) -> CheckResult | None:
    if parse_ts(proposal.expires_at) <= parse_ts(context.evaluated_at):
        return fail(
            "proposal_expiry",
            policy,
            ReasonCode.PROPOSAL_EXPIRED,
            snapshot_ts=context.evaluated_at,
            detail=f"proposal expired at {proposal.expires_at}; evaluated at {context.evaluated_at}",
        )
    return ok("proposal_expiry", policy, snapshot_ts=context.evaluated_at)


# ---------------------------------------------------------------------------------------------------
# Order-shape checks
# ---------------------------------------------------------------------------------------------------
def restricted_symbol(proposal: TradeProposal, context: RiskContext, policy: Policy) -> CheckResult | None:
    if proposal.symbol in policy.restricted.symbols:
        return fail(
            "restricted_symbol",
            policy,
            ReasonCode.RESTRICTED_SYMBOL,
            detail=f"{proposal.symbol} is on the tenant's restricted list",
        )
    # Returning None here would make _run fabricate a blocking PASS carrying no evidence at all -
    # a control reporting success without saying what it checked (ESC-4 class). Say what was checked.
    return ok(
        "restricted_symbol",
        policy,
        actual=Decimal(len(policy.restricted.symbols)),
        detail=f"{proposal.symbol} is not among the {len(policy.restricted.symbols)} restricted symbol(s)",
    )


def restricted_strategy(proposal: TradeProposal, context: RiskContext, policy: Policy) -> CheckResult | None:
    if proposal.strategy in policy.restricted.strategies:
        return fail(
            "restricted_strategy",
            policy,
            ReasonCode.RESTRICTED_STRATEGY,
            detail=f"strategy {proposal.strategy} is on the tenant's restricted list",
        )
    # See restricted_symbol: a blocking PASS must carry the evidence it passed on.
    return ok(
        "restricted_strategy",
        policy,
        actual=Decimal(len(policy.restricted.strategies)),
        detail=(
            f"strategy {proposal.strategy} is not among the "
            f"{len(policy.restricted.strategies)} restricted strategy(ies)"
        ),
    )


def leg_limit(proposal: TradeProposal, context: RiskContext, policy: Policy) -> CheckResult | None:
    count = len(proposal.legs)
    if count > policy.order.max_legs:
        return fail(
            "leg_limit",
            policy,
            ReasonCode.LEG_LIMIT_EXCEEDED,
            threshold=Decimal(policy.order.max_legs),
            actual=Decimal(count),
            detail=f"{count} legs exceeds max_legs {policy.order.max_legs}",
        )
    return ok("leg_limit", policy, threshold=Decimal(policy.order.max_legs), actual=Decimal(count))


def position_limit(proposal: TradeProposal, context: RiskContext, policy: Policy) -> CheckResult | None:
    limit = dec(policy.order.max_quantity)
    quantity = proposal.total_quantity
    if quantity > limit:
        return fail(
            "position_limit",
            policy,
            ReasonCode.POSITION_LIMIT_EXCEEDED,
            threshold=limit,
            actual=quantity,
            cap=floor_units(limit),
            detail=f"total quantity {dstr(quantity)} exceeds max_quantity {policy.order.max_quantity}",
        )
    return ok("position_limit", policy, threshold=limit, actual=quantity)


def capital_threshold(proposal: TradeProposal, context: RiskContext, policy: Policy) -> CheckResult | None:
    exposure = exposure_of(proposal, context)
    if not exposure.priced:
        return missing(
            "capital_threshold",
            exposure.missing_code or ReasonCode.PRICE_MISSING,
            "order notional cannot be valued without a market price",
        )
    limit = dec(policy.order.max_notional)
    if exposure.gross > limit:
        return fail(
            "capital_threshold",
            policy,
            ReasonCode.CAPITAL_THRESHOLD_EXCEEDED,
            threshold=limit,
            actual=exposure.gross,
            cap=cap_from_budget(limit, exposure.unit_gross),
            source=_market_source(context),
            snapshot_ts=_market_ts(context),
            detail=(
                f"order notional {dstr(exposure.gross)} at market exceeds max_notional "
                f"{policy.order.max_notional}"
            ),
        )
    return ok(
        "capital_threshold",
        policy,
        threshold=limit,
        actual=exposure.gross,
        source=_market_source(context),
        snapshot_ts=_market_ts(context),
    )


def erroneous_order(proposal: TradeProposal, context: RiskContext, policy: Policy) -> CheckResult | None:
    """The one check that reads ``limit_price`` -- comparing what was asked against what is quoted."""
    config = policy.check_config("erroneous_order")
    market = context.market_snapshot
    if market is None:
        return missing("erroneous_order", ReasonCode.MARKET_DATA_MISSING, "no market snapshot")
    if config.price_deviation_threshold is not None:
        threshold = dec(config.price_deviation_threshold)
        for leg in proposal.legs:
            asked = leg.limit_price
            if asked is None:
                continue
            reference = leg_reference_price(proposal, leg, context.market_snapshot)
            if reference is None:
                return missing(
                    "erroneous_order", ReasonCode.PRICE_MISSING, f"no market price for leg {leg.leg_index}"
                )
            deviation = _pct(abs(subtract(dec(asked), reference)), reference)
            if deviation is None:
                return missing(
                    "erroneous_order", ReasonCode.PRICE_MISSING, f"zero market price for leg {leg.leg_index}"
                )
            if deviation > threshold:
                return fail(
                    "erroneous_order",
                    policy,
                    ReasonCode.ERRONEOUS_PRICE_DEVIATION,
                    threshold=threshold,
                    actual=deviation,
                    source=market.source,
                    snapshot_ts=market.as_of,
                    detail=(
                        f"leg {leg.leg_index} limit {asked} deviates {dstr(deviation)} from the market "
                        f"price {dstr(reference)}"
                    ),
                )
    if config.quantity_deviation_threshold is not None:
        threshold = dec(config.quantity_deviation_threshold)
        largest = ZERO
        for order in context.recent_orders:
            if order.symbol == proposal.symbol:
                largest = max(largest, dec(order.total_quantity))
        if largest > ZERO:
            ratio = divide(proposal.total_quantity, largest)
            if ratio is not None and ratio > threshold:
                return fail(
                    "erroneous_order",
                    policy,
                    ReasonCode.ERRONEOUS_QUANTITY_DEVIATION,
                    threshold=threshold,
                    actual=ratio,
                    detail=(
                        f"quantity {dstr(proposal.total_quantity)} is {dstr(ratio)}x the largest recent "
                        f"order for {proposal.symbol} ({dstr(largest)})"
                    ),
                )
    return ok("erroneous_order", policy, source=market.source, snapshot_ts=market.as_of)


def duplicate_order(proposal: TradeProposal, context: RiskContext, policy: Policy) -> CheckResult | None:
    config = policy.check_config("duplicate_order")
    now = parse_ts(context.evaluated_at)
    side = proposal.legs[0].side
    quantity = proposal.total_quantity
    for order in context.recent_orders:
        if order.symbol != proposal.symbol:
            continue
        if config.window_seconds is not None:
            age = (now - parse_ts(order.submitted_at)).total_seconds()
            if age > config.window_seconds or age < 0:
                continue
        same_order = order.proposal_id == proposal.proposal_id
        same_shape = order.side == side and dec(order.total_quantity) == quantity
        if same_order or same_shape:
            return fail(
                "duplicate_order",
                policy,
                ReasonCode.DUPLICATE_ORDER,
                snapshot_ts=order.submitted_at,
                detail=(
                    f"a {order.side} order for {dstr(dec(order.total_quantity))} {order.symbol} was "
                    f"submitted at {order.submitted_at} (status {order.status})"
                ),
            )
    return ok("duplicate_order", policy, snapshot_ts=context.evaluated_at)


# ---------------------------------------------------------------------------------------------------
# Portfolio checks
# ---------------------------------------------------------------------------------------------------
def buying_power_sufficiency(
    proposal: TradeProposal, context: RiskContext, policy: Policy
) -> CheckResult | None:
    portfolio = context.portfolio_snapshot
    if portfolio is None:
        return missing(
            "buying_power_sufficiency", ReasonCode.PORTFOLIO_STATE_MISSING, "no portfolio snapshot"
        )
    exposure = exposure_of(proposal, context)
    if not exposure.priced:
        return missing(
            "buying_power_sufficiency",
            exposure.missing_code or ReasonCode.PRICE_MISSING,
            "the order cannot be valued without a market price",
        )
    if exposure.change <= ZERO:
        return ok(
            "buying_power_sufficiency",
            policy,
            source=portfolio.source,
            snapshot_ts=portfolio.as_of,
            detail="the order does not consume buying power",
        )
    if portfolio.buying_power is None:
        return missing(
            "buying_power_sufficiency",
            ReasonCode.BUYING_POWER_MISSING,
            "buying power is unknown; the order cannot be shown to be affordable",
            source=portfolio.source,
            snapshot_ts=portfolio.as_of,
        )
    available = dec(portfolio.buying_power)
    if exposure.change > available:
        return fail(
            "buying_power_sufficiency",
            policy,
            ReasonCode.INSUFFICIENT_BUYING_POWER,
            threshold=available,
            actual=exposure.change,
            cap=cap_from_budget(available, exposure.unit_gross),
            source=portfolio.source,
            snapshot_ts=portfolio.as_of,
            detail=(
                f"the order requires {dstr(exposure.change)} at market against buying power "
                f"{portfolio.buying_power}"
            ),
        )
    return ok(
        "buying_power_sufficiency",
        policy,
        threshold=available,
        actual=exposure.change,
        source=portfolio.source,
        snapshot_ts=portfolio.as_of,
    )


def buying_power_utilization(
    proposal: TradeProposal, context: RiskContext, policy: Policy
) -> CheckResult | None:
    portfolio = context.portfolio_snapshot
    if portfolio is None:
        return missing(
            "buying_power_utilization", ReasonCode.PORTFOLIO_STATE_MISSING, "no portfolio snapshot"
        )
    exposure = exposure_of(proposal, context)
    if not exposure.priced:
        return missing(
            "buying_power_utilization",
            exposure.missing_code or ReasonCode.PRICE_MISSING,
            "the order cannot be valued without a market price",
        )
    if exposure.change <= ZERO:
        return ok("buying_power_utilization", policy, detail="the order does not consume buying power")
    if portfolio.buying_power is None:
        return missing(
            "buying_power_utilization",
            ReasonCode.BUYING_POWER_MISSING,
            "buying power is unknown; utilization cannot be computed",
            source=portfolio.source,
            snapshot_ts=portfolio.as_of,
        )
    available = dec(portfolio.buying_power)
    limit = dec(policy.portfolio.max_buying_power_utilization)
    utilization = _pct(exposure.change, available)
    if utilization is None:
        return fail(
            "buying_power_utilization",
            policy,
            ReasonCode.BUYING_POWER_UTILIZATION_EXCEEDED,
            threshold=limit,
            source=portfolio.source,
            snapshot_ts=portfolio.as_of,
            detail="buying power is zero; any utilization exceeds the limit",
        )
    if utilization > limit:
        budget = multiply(available, limit)
        return fail(
            "buying_power_utilization",
            policy,
            ReasonCode.BUYING_POWER_UTILIZATION_EXCEEDED,
            threshold=limit,
            actual=utilization,
            cap=cap_from_budget(budget, exposure.unit_gross),
            source=portfolio.source,
            snapshot_ts=portfolio.as_of,
            detail=(
                f"the order would use {dstr(utilization)} of buying power against a limit of "
                f"{policy.portfolio.max_buying_power_utilization}"
            ),
        )
    return ok(
        "buying_power_utilization",
        policy,
        threshold=limit,
        actual=utilization,
        source=portfolio.source,
        snapshot_ts=portfolio.as_of,
    )


def concentration_limit(proposal: TradeProposal, context: RiskContext, policy: Policy) -> CheckResult | None:
    portfolio = context.portfolio_snapshot
    if portfolio is None:
        return missing(
            "concentration_limit", ReasonCode.PORTFOLIO_STATE_MISSING, "no portfolio snapshot in the context"
        )
    exposure = exposure_of(proposal, context)
    if not exposure.priced:
        return missing(
            "concentration_limit",
            exposure.missing_code or ReasonCode.PRICE_MISSING,
            "the order cannot be valued without a market price",
        )
    if not exposure.increases_risk:
        return ok("concentration_limit", policy, detail="the order does not increase symbol exposure")
    equity = dec(portfolio.equity)
    limit = dec(policy.portfolio.max_single_symbol_pct)
    held = symbol_exposure(portfolio, proposal.symbol)
    projected = add(held, exposure.change)
    share = _pct(projected, equity)
    if share is not None and share > limit:
        room = subtract(multiply(equity, limit), held)
        return fail(
            "concentration_limit",
            policy,
            ReasonCode.CONCENTRATION_LIMIT_EXCEEDED,
            threshold=limit,
            actual=share,
            cap=cap_from_budget(room, exposure.unit_gross),
            source=portfolio.source,
            snapshot_ts=portfolio.as_of,
            detail=(
                f"{proposal.symbol} would be {dstr(share)} of equity against a limit of "
                f"{policy.portfolio.max_single_symbol_pct}"
            ),
        )
    return ok(
        "concentration_limit",
        policy,
        threshold=limit,
        actual=share,
        source=portfolio.source,
        snapshot_ts=portfolio.as_of,
    )


def sector_concentration(proposal: TradeProposal, context: RiskContext, policy: Policy) -> CheckResult | None:
    limit_str = policy.portfolio.max_sector_concentration_pct
    if limit_str is None:
        return ok("sector_concentration", policy, detail="no sector limit configured")
    portfolio = context.portfolio_snapshot
    if portfolio is None:
        return missing(
            "sector_concentration", ReasonCode.PORTFOLIO_STATE_MISSING, "no portfolio snapshot in the context"
        )
    exposure = exposure_of(proposal, context)
    if not exposure.priced:
        return missing(
            "sector_concentration",
            exposure.missing_code or ReasonCode.PRICE_MISSING,
            "the order cannot be valued without a market price",
        )
    if not exposure.increases_risk:
        return ok("sector_concentration", policy, detail="the order does not increase sector exposure")
    sector = sector_of(context.market_snapshot, proposal.symbol)
    if sector is None:
        return missing(
            "sector_concentration",
            ReasonCode.SECTOR_DATA_MISSING,
            f"no sector is known for {proposal.symbol}",
            source=_market_source(context),
            snapshot_ts=_market_ts(context),
        )
    equity = dec(portfolio.equity)
    limit = dec(limit_str)
    held = sector_exposure(portfolio, context.market_snapshot, sector)
    share = _pct(add(held, exposure.change), equity)
    if share is not None and share > limit:
        room = subtract(multiply(equity, limit), held)
        return fail(
            "sector_concentration",
            policy,
            ReasonCode.SECTOR_CONCENTRATION_EXCEEDED,
            threshold=limit,
            actual=share,
            cap=cap_from_budget(room, exposure.unit_gross),
            source=portfolio.source,
            snapshot_ts=portfolio.as_of,
            detail=f"sector {sector} would be {dstr(share)} of equity against a limit of {limit_str}",
        )
    return ok(
        "sector_concentration",
        policy,
        threshold=limit,
        actual=share,
        source=portfolio.source,
        snapshot_ts=portfolio.as_of,
    )


def drawdown_limit(proposal: TradeProposal, context: RiskContext, policy: Policy) -> CheckResult | None:
    portfolio = context.portfolio_snapshot
    if portfolio is None:
        return missing("drawdown_limit", ReasonCode.PORTFOLIO_STATE_MISSING, "no portfolio snapshot")
    peak = portfolio.peak_equity
    if peak is None and context.path_state is not None:
        peak = context.path_state.peak_equity
    if peak is None:
        return missing(
            "drawdown_limit",
            ReasonCode.PORTFOLIO_STATE_MISSING,
            "peak equity is unknown; drawdown cannot be computed",
            source=portfolio.source,
            snapshot_ts=portfolio.as_of,
        )
    equity = dec(portfolio.equity)
    peak_equity = dec(peak)
    drawdown = _pct(subtract(peak_equity, equity), peak_equity) or ZERO
    if drawdown < ZERO:
        drawdown = ZERO
    limit = dec(policy.portfolio.max_drawdown_pct)
    if drawdown > limit:
        return fail(
            "drawdown_limit",
            policy,
            ReasonCode.DRAWDOWN_LIMIT_BREACHED,
            threshold=limit,
            actual=drawdown,
            source=portfolio.source,
            snapshot_ts=portfolio.as_of,
            detail=(
                f"drawdown {dstr(drawdown)} from peak equity {dstr(peak_equity)} exceeds "
                f"max_drawdown_pct {policy.portfolio.max_drawdown_pct}"
            ),
        )
    return ok(
        "drawdown_limit",
        policy,
        threshold=limit,
        actual=drawdown,
        source=portfolio.source,
        snapshot_ts=portfolio.as_of,
    )


# ---------------------------------------------------------------------------------------------------
# Options checks
# ---------------------------------------------------------------------------------------------------
def days_to_expiry_check(proposal: TradeProposal, context: RiskContext, policy: Policy) -> CheckResult | None:
    options = policy.options
    if options is None or proposal.asset_class != "equity_option":
        return ok("days_to_expiry", policy, detail="not an options proposal")
    for leg in proposal.legs:
        if leg.expiry is None:
            continue
        dte = days_to_expiry(context, leg.expiry)
        if dte < options.min_days_to_expiry:
            return fail(
                "days_to_expiry",
                policy,
                ReasonCode.DTE_BELOW_MINIMUM,
                threshold=Decimal(options.min_days_to_expiry),
                actual=Decimal(dte),
                snapshot_ts=context.evaluated_at,
                detail=f"leg {leg.leg_index} expires in {dte} days; minimum is {options.min_days_to_expiry}",
            )
        if dte > options.max_days_to_expiry:
            return fail(
                "days_to_expiry",
                policy,
                ReasonCode.DTE_ABOVE_MAXIMUM,
                threshold=Decimal(options.max_days_to_expiry),
                actual=Decimal(dte),
                snapshot_ts=context.evaluated_at,
                detail=f"leg {leg.leg_index} expires in {dte} days; maximum is {options.max_days_to_expiry}",
            )
    return ok("days_to_expiry", policy, snapshot_ts=context.evaluated_at)


def _greek_limit_check(
    check_id: str,
    greek: str,
    limit_str: str,
    code: ReasonCode,
    proposal: TradeProposal,
    context: RiskContext,
    policy: Policy,
) -> CheckResult:
    """Projected portfolio greek = current + this order's contribution, against an absolute limit."""
    if proposal.asset_class != "equity_option":
        return ok(check_id, policy, detail="not an options proposal")
    current = portfolio_greek(context.portfolio_snapshot, greek)
    if current is None:
        return missing(check_id, ReasonCode.GREEKS_MISSING, f"portfolio {greek} is unknown")
    change = greek_change(proposal, context, greek)
    if change is None:
        return missing(check_id, ReasonCode.GREEKS_MISSING, f"a leg carries no {greek}")
    projected = add(current, change)
    limit = dec(limit_str)
    if abs(projected) > limit:
        return fail(
            check_id,
            policy,
            code,
            threshold=limit,
            actual=projected,
            source=_market_source(context),
            snapshot_ts=_market_ts(context),
            detail=(
                f"projected portfolio {greek} {dstr(projected)} = {dstr(current)} held plus "
                f"{dstr(change)} from this order, against the limit {limit_str}"
            ),
        )
    return ok(
        check_id,
        policy,
        threshold=limit,
        actual=projected,
        source=_market_source(context),
        snapshot_ts=_market_ts(context),
    )


def options_delta_limit(proposal: TradeProposal, context: RiskContext, policy: Policy) -> CheckResult | None:
    if policy.options is None:
        return None
    return _greek_limit_check(
        "options_delta_limit",
        "delta",
        policy.options.max_portfolio_delta,
        ReasonCode.OPTIONS_DELTA_LIMIT_EXCEEDED,
        proposal,
        context,
        policy,
    )


def options_gamma_limit(proposal: TradeProposal, context: RiskContext, policy: Policy) -> CheckResult | None:
    if policy.options is None:
        return None
    return _greek_limit_check(
        "options_gamma_limit",
        "gamma",
        policy.options.max_portfolio_gamma,
        ReasonCode.OPTIONS_GAMMA_LIMIT_EXCEEDED,
        proposal,
        context,
        policy,
    )


def options_vega_limit(proposal: TradeProposal, context: RiskContext, policy: Policy) -> CheckResult | None:
    if policy.options is None:
        return None
    return _greek_limit_check(
        "options_vega_limit",
        "vega",
        policy.options.max_portfolio_vega,
        ReasonCode.OPTIONS_VEGA_LIMIT_EXCEEDED,
        proposal,
        context,
        policy,
    )


def _short_greek_check(
    check_id: str,
    greek: str,
    short_field: str,
    limit_str: str | None,
    code: ReasonCode,
    proposal: TradeProposal,
    context: RiskContext,
    policy: Policy,
) -> CheckResult:
    """Short gamma/vega is asymmetric risk (R-OPT-1): only the negative side of the book is capped."""
    if limit_str is None:
        return ok(check_id, policy, detail="no short limit configured")
    if proposal.asset_class != "equity_option":
        return ok(check_id, policy, detail="not an options proposal")
    change = greek_change(proposal, context, greek)
    if change is None:
        return missing(check_id, ReasonCode.GREEKS_MISSING, f"a leg carries no {greek}")
    held_short = portfolio_greek(context.portfolio_snapshot, short_field)
    if held_short is None:
        held = portfolio_greek(context.portfolio_snapshot, greek)
        if held is None:
            return missing(check_id, ReasonCode.GREEKS_MISSING, f"portfolio {greek} is unknown")
        held_short = held if held < ZERO else ZERO
    projected = add(held_short, change)
    short_side = -projected if projected < ZERO else ZERO
    limit = dec(limit_str)
    if short_side > limit:
        return fail(
            check_id,
            policy,
            code,
            threshold=limit,
            actual=short_side,
            source=_market_source(context),
            snapshot_ts=_market_ts(context),
            detail=f"projected short {greek} {dstr(short_side)} exceeds the limit {limit_str}",
        )
    return ok(
        check_id,
        policy,
        threshold=limit,
        actual=short_side,
        source=_market_source(context),
        snapshot_ts=_market_ts(context),
    )


def options_short_gamma_limit(
    proposal: TradeProposal, context: RiskContext, policy: Policy
) -> CheckResult | None:
    if policy.options is None:
        return None
    return _short_greek_check(
        "options_short_gamma_limit",
        "gamma",
        "short_gamma",
        policy.options.max_short_gamma,
        ReasonCode.OPTIONS_SHORT_GAMMA_LIMIT_EXCEEDED,
        proposal,
        context,
        policy,
    )


def options_short_vega_limit(
    proposal: TradeProposal, context: RiskContext, policy: Policy
) -> CheckResult | None:
    if policy.options is None:
        return None
    return _short_greek_check(
        "options_short_vega_limit",
        "vega",
        "short_vega",
        policy.options.max_short_vega,
        ReasonCode.OPTIONS_SHORT_VEGA_LIMIT_EXCEEDED,
        proposal,
        context,
        policy,
    )


# ---------------------------------------------------------------------------------------------------
# Graduated response (R-GRAD) and per-agent budgets
# ---------------------------------------------------------------------------------------------------
def response_level_gate(proposal: TradeProposal, context: RiskContext, policy: Policy) -> CheckResult | None:
    """Level 0 is a no-op. 1-3 scale or forbid new risk. 4-5 halt everything, closes included."""
    level = context.response_level
    if level == 0:
        return ok("response_level_gate", policy, actual=ZERO, detail="response level 0")
    if level >= 4:
        return fail(
            "response_level_gate",
            policy,
            ReasonCode.RESPONSE_LEVEL_HALT,
            actual=Decimal(level),
            threshold=Decimal(3),
            detail=f"response level {level}: trading is halted until a human de-escalates",
        )
    if proposal.intent == CLOSE_INTENT:
        return ok(
            "response_level_gate",
            policy,
            actual=Decimal(level),
            detail=f"response level {level}: closing a position is always permitted",
        )
    spec = _ladder_spec(policy, level)
    if spec is None:
        return fail(
            "response_level_gate",
            policy,
            ReasonCode.RESPONSE_LEVEL_RESTRICTS_NEW_RISK,
            actual=Decimal(level),
            detail=f"response level {level} has no ladder entry in this policy; new risk is refused",
        )
    multiplier = dec(spec.size_multiplier)
    if not is_risk_increasing(proposal, context):
        # Levels 1-3 restrain new risk. An order that does not add any is left alone.
        return ok("response_level_gate", policy, actual=Decimal(level), detail="the order does not add risk")
    if not spec.new_risk_allowed or multiplier == ZERO:
        return fail(
            "response_level_gate",
            policy,
            ReasonCode.RESPONSE_LEVEL_RESTRICTS_NEW_RISK,
            actual=Decimal(level),
            threshold=multiplier,
            detail=f"response level {level} forbids new risk",
        )
    if multiplier < ONE:
        return reduce_to(
            "response_level_gate",
            policy,
            ReasonCode.SIZE_REDUCED_TO_POLICY_CAP,
            apply_multiplier(proposal.total_quantity, multiplier),
            threshold=multiplier,
            actual=Decimal(level),
            detail=f"response level {level} scales new risk by {spec.size_multiplier}",
        )
    return ok("response_level_gate", policy, actual=Decimal(level), threshold=multiplier)


def _ladder_spec(policy: Policy, level: int) -> ResponseLevelSpec | None:
    if policy.response_ladder is None:
        return None
    chosen = None
    for spec in policy.response_ladder.levels:
        if spec.level <= level:
            chosen = spec
    return chosen


def agent_budget(proposal: TradeProposal, context: RiskContext, policy: Policy) -> CheckResult | None:
    budget = policy.agent_budgets.get(context.agent_id)
    if budget is None:
        return ok("agent_budget", policy, detail=f"no budget configured for {context.agent_id}")
    if budget.allowed_symbols is not None and proposal.symbol not in budget.allowed_symbols:
        return fail(
            "agent_budget",
            policy,
            ReasonCode.AGENT_SYMBOL_NOT_ALLOWED,
            detail=f"{context.agent_id} may not trade {proposal.symbol}",
        )
    if budget.active_hours_utc is not None:
        start = minutes_of_hhmm(budget.active_hours_utc[0])
        end = minutes_of_hhmm(budget.active_hours_utc[1])
        now = minutes_of_day(context)
        inside = start <= now < end if start < end else (now >= start or now < end)
        if not inside:
            return fail(
                "agent_budget",
                policy,
                ReasonCode.AGENT_OUTSIDE_ACTIVE_HOURS,
                snapshot_ts=context.evaluated_at,
                detail=(
                    f"{context.evaluated_at} is outside the agent's active hours "
                    f"{budget.active_hours_utc[0]}-{budget.active_hours_utc[1]} UTC"
                ),
            )
    needs_state = (
        budget.max_daily_notional is not None
        or budget.max_daily_orders is not None
        or budget.max_open_positions is not None
    )
    state = context.agent_state
    if needs_state and state is None:
        return missing(
            "agent_budget",
            ReasonCode.AGENT_STATE_MISSING,
            f"no agent state for {context.agent_id}; the budget cannot be checked",
        )
    if state is None:
        return ok("agent_budget", policy, detail="the agent's budget carries no stateful limit")
    if budget.max_daily_orders is not None and state.daily_order_count + 1 > budget.max_daily_orders:
        return fail(
            "agent_budget",
            policy,
            ReasonCode.AGENT_DAILY_ORDERS_EXCEEDED,
            threshold=Decimal(budget.max_daily_orders),
            actual=Decimal(state.daily_order_count + 1),
            snapshot_ts=state.as_of,
            detail=f"this would be order {state.daily_order_count + 1} of {budget.max_daily_orders} today",
        )
    if (
        budget.max_open_positions is not None
        and proposal.intent == OPEN_INTENT
        and state.open_positions + 1 > budget.max_open_positions
    ):
        return fail(
            "agent_budget",
            policy,
            ReasonCode.AGENT_OPEN_POSITIONS_EXCEEDED,
            threshold=Decimal(budget.max_open_positions),
            actual=Decimal(state.open_positions + 1),
            snapshot_ts=state.as_of,
            detail=f"the agent already holds {state.open_positions} of {budget.max_open_positions} positions",
        )
    if budget.max_daily_notional is not None:
        exposure = exposure_of(proposal, context)
        if not exposure.priced:
            return missing(
                "agent_budget",
                exposure.missing_code or ReasonCode.PRICE_MISSING,
                "the order cannot be valued against the agent's notional budget",
            )
        used = dec(state.daily_notional_used)
        limit = dec(budget.max_daily_notional)
        projected = add(used, exposure.gross)
        if projected > limit:
            return fail(
                "agent_budget",
                policy,
                ReasonCode.AGENT_DAILY_NOTIONAL_EXCEEDED,
                threshold=limit,
                actual=projected,
                cap=cap_from_budget(subtract(limit, used), exposure.unit_gross),
                snapshot_ts=state.as_of,
                detail=(
                    f"{dstr(used)} of {budget.max_daily_notional} used today; this order adds "
                    f"{dstr(exposure.gross)}"
                ),
            )
    return ok("agent_budget", policy, snapshot_ts=state.as_of)


# ---------------------------------------------------------------------------------------------------
# Trader-school checks (R-TRADE)
# ---------------------------------------------------------------------------------------------------
def invalidation_defined(proposal: TradeProposal, context: RiskContext, policy: Policy) -> CheckResult | None:
    trade = policy.trade
    if trade is None or not trade.require_invalidation:
        return ok("invalidation_defined", policy, detail="an invalidation level is not required")
    if proposal.intent == CLOSE_INTENT:
        return ok("invalidation_defined", policy, detail="closing a position needs no invalidation level")
    if proposal.invalidation is None:
        return fail(
            "invalidation_defined",
            policy,
            ReasonCode.INVALIDATION_MISSING,
            detail="the policy requires a stated invalidation level and the proposal carries none",
        )
    return ok("invalidation_defined", policy, actual=dec(proposal.invalidation.level))


def reward_risk(proposal: TradeProposal, context: RiskContext, policy: Policy) -> CheckResult | None:
    trade = policy.trade
    if trade is None or trade.min_reward_risk is None:
        return ok("reward_risk", policy, detail="no minimum reward:risk configured")
    if proposal.intent == CLOSE_INTENT:
        return ok("reward_risk", policy, detail="closing a position has no reward:risk to state")
    invalidation = proposal.invalidation
    if invalidation is None or invalidation.target is None:
        return fail(
            "reward_risk",
            policy,
            ReasonCode.INVALIDATION_MISSING,
            threshold=dec(trade.min_reward_risk),
            detail="reward:risk cannot be computed without an invalidation level and a target",
        )
    entry = _entry_price(proposal, context)
    if entry is None:
        return missing("reward_risk", ReasonCode.PRICE_MISSING, "no market price to measure reward:risk from")
    risk = abs(subtract(entry, dec(invalidation.level)))
    reward = abs(subtract(dec(invalidation.target), entry))
    ratio = divide(reward, risk)
    minimum = dec(trade.min_reward_risk)
    if ratio is None:
        return fail(
            "reward_risk",
            policy,
            ReasonCode.REWARD_RISK_BELOW_MINIMUM,
            threshold=minimum,
            detail="the invalidation level equals the entry price; risk per unit is zero",
        )
    if ratio < minimum:
        return fail(
            "reward_risk",
            policy,
            ReasonCode.REWARD_RISK_BELOW_MINIMUM,
            threshold=minimum,
            actual=ratio,
            source=_market_source(context),
            snapshot_ts=_market_ts(context),
            detail=(
                f"reward:risk {dstr(ratio)} (reward {dstr(reward)} against risk {dstr(risk)}) is below "
                f"the minimum {trade.min_reward_risk}"
            ),
        )
    return ok("reward_risk", policy, threshold=minimum, actual=ratio, snapshot_ts=_market_ts(context))


def _entry_price(proposal: TradeProposal, context: RiskContext) -> Decimal | None:
    """The underlying's market price -- the level an invalidation is measured against."""
    quote = quote_for(context.market_snapshot, proposal.symbol)
    return None if quote is None else dec(quote.price)


def risk_per_trade(proposal: TradeProposal, context: RiskContext, policy: Policy) -> CheckResult | None:
    """Capital at risk on this trade against the policy's per-trade budget (R-TRADE-2, R-KELLY-2)."""
    trade = policy.trade
    if trade is None or trade.max_risk_per_trade_pct is None:
        return ok("risk_per_trade", policy, detail="no per-trade risk budget configured")
    portfolio = context.portfolio_snapshot
    if portfolio is None:
        return missing("risk_per_trade", ReasonCode.PORTFOLIO_STATE_MISSING, "no portfolio snapshot")
    exposure = exposure_of(proposal, context)
    if not exposure.priced:
        return missing(
            "risk_per_trade",
            exposure.missing_code or ReasonCode.PRICE_MISSING,
            "capital at risk cannot be measured without a market price",
        )
    if not exposure.increases_risk:
        return ok("risk_per_trade", policy, detail="the order does not add risk")
    equity = dec(portfolio.equity)
    budget = multiply(equity, dec(trade.max_risk_per_trade_pct))
    haircut = _confidence_factor(proposal, policy)
    if haircut is not None:
        budget = multiply(budget, haircut)
    unit_risk = _risk_per_unit(proposal, context, exposure.unit_gross)
    at_risk = multiply(unit_risk, proposal.total_quantity)
    if at_risk > budget:
        return fail(
            "risk_per_trade",
            policy,
            ReasonCode.RISK_PER_TRADE_EXCEEDED,
            threshold=budget,
            actual=at_risk,
            cap=cap_from_budget(budget, unit_risk),
            source=portfolio.source,
            snapshot_ts=portfolio.as_of,
            detail=(
                f"capital at risk {dstr(at_risk)} exceeds the per-trade budget {dstr(budget)} "
                f"({trade.max_risk_per_trade_pct} of equity"
                + (f", haircut to {dstr(haircut)} of it by stated confidence" if haircut is not None else "")
                + ")"
            ),
        )
    return ok(
        "risk_per_trade",
        policy,
        threshold=budget,
        actual=at_risk,
        source=portfolio.source,
        snapshot_ts=portfolio.as_of,
    )


def _confidence_factor(proposal: TradeProposal, policy: Policy) -> Decimal | None:
    """A stated confidence can only SHRINK the budget: a claim is never authority (R-KELLY-2)."""
    trade = policy.trade
    if trade is None or proposal.confidence is None:
        return None
    haircut = dec(trade.confidence_haircut)
    if haircut == ZERO:
        return None
    factor = subtract(dec(proposal.confidence), haircut)
    if factor < ZERO:
        return ZERO
    return factor if factor < ONE else ONE


def _risk_per_unit(proposal: TradeProposal, context: RiskContext, unit_gross: Decimal) -> Decimal:
    """Loss per unit if the thesis is wrong: to the invalidation level when stated, else the whole unit."""
    invalidation = proposal.invalidation
    entry = _entry_price(proposal, context)
    if invalidation is None or entry is None or proposal.asset_class != "equity":
        return unit_gross
    distance = abs(subtract(entry, dec(invalidation.level)))
    return distance if distance < unit_gross else unit_gross


# ---------------------------------------------------------------------------------------------------
# Path dependence (R-ERG)
# ---------------------------------------------------------------------------------------------------
def drawdown_size_scaling(
    proposal: TradeProposal, context: RiskContext, policy: Policy
) -> CheckResult | None:
    path = policy.path
    if path is None:
        return None
    state = context.path_state
    if state is None:
        return missing(
            "drawdown_size_scaling",
            ReasonCode.PATH_STATE_MISSING,
            "path state is unknown; size cannot be scaled to the drawdown",
        )
    drawdown = dec(state.current_drawdown_pct)
    multiplier = ONE
    step_pct = None
    for step in path.size_scaling_by_drawdown:
        if drawdown >= dec(step.drawdown_pct):
            multiplier = dec(step.size_multiplier)
            step_pct = step.drawdown_pct
    if multiplier >= ONE:
        return ok("drawdown_size_scaling", policy, actual=drawdown, snapshot_ts=state.as_of)
    if not is_risk_increasing(proposal, context):
        return ok(
            "drawdown_size_scaling",
            policy,
            actual=drawdown,
            snapshot_ts=state.as_of,
            detail="the order does not add risk",
        )
    return reduce_to(
        "drawdown_size_scaling",
        policy,
        ReasonCode.SIZE_SCALED_BY_DRAWDOWN,
        apply_multiplier(proposal.total_quantity, multiplier),
        threshold=multiplier,
        actual=drawdown,
        snapshot_ts=state.as_of,
        detail=(
            f"drawdown {dstr(drawdown)} is at or past the {step_pct} step; "
            f"size scales by {dstr(multiplier)}"
        ),
    )


def consecutive_loss_review(
    proposal: TradeProposal, context: RiskContext, policy: Policy
) -> CheckResult | None:
    path = policy.path
    if path is None:
        return None
    state = context.path_state
    if state is None:
        return missing(
            "consecutive_loss_review", ReasonCode.PATH_STATE_MISSING, "path state is unknown"
        )
    if path.max_consecutive_losses_before_review is not None:
        limit = path.max_consecutive_losses_before_review
        if state.consecutive_losses >= limit:
            return fail(
                "consecutive_loss_review",
                policy,
                ReasonCode.CONSECUTIVE_LOSS_REVIEW,
                threshold=Decimal(limit),
                actual=Decimal(state.consecutive_losses),
                snapshot_ts=state.as_of,
                detail=(
                    f"{state.consecutive_losses} consecutive losses; "
                    f"a human review is required at {limit}"
                ),
            )
    if path.max_days_under_water is not None and state.days_under_water >= path.max_days_under_water:
        return fail(
            "consecutive_loss_review",
            policy,
            ReasonCode.DAYS_UNDER_WATER_EXCEEDED,
            threshold=Decimal(path.max_days_under_water),
            actual=Decimal(state.days_under_water),
            snapshot_ts=state.as_of,
            detail=(
                f"{state.days_under_water} days under water; the policy's limit is "
                f"{path.max_days_under_water}"
            ),
        )
    return ok("consecutive_loss_review", policy, snapshot_ts=state.as_of)


# ---------------------------------------------------------------------------------------------------
# Aggregate multi-agent exposure (R-AGG)
# ---------------------------------------------------------------------------------------------------
def aggregate_exposure(proposal: TradeProposal, context: RiskContext, policy: Policy) -> CheckResult | None:
    aggregate = policy.aggregate
    if aggregate is None:
        return None
    state = context.aggregate_state
    if state is None:
        return missing(
            "aggregate_exposure",
            ReasonCode.AGGREGATE_STATE_MISSING,
            "book-level exposure is unknown; this agent's share cannot be bounded",
        )
    equity = equity_of(context.portfolio_snapshot)
    if equity is None:
        return missing(
            "aggregate_exposure", ReasonCode.PORTFOLIO_STATE_MISSING, "no equity for the book measure"
        )
    exposure = exposure_of(proposal, context)
    if not exposure.priced:
        return missing(
            "aggregate_exposure",
            exposure.missing_code or ReasonCode.PRICE_MISSING,
            "the order cannot be valued against the book",
        )
    if not exposure.increases_risk:
        return ok("aggregate_exposure", policy, detail="the order does not increase book exposure")
    held = dec(state.gross_exposure)
    limit = dec(aggregate.max_portfolio_exposure_pct)
    share = _pct(add(held, exposure.change), equity)
    if share is not None and share > limit:
        room = subtract(multiply(equity, limit), held)
        return fail(
            "aggregate_exposure",
            policy,
            ReasonCode.AGGREGATE_EXPOSURE_EXCEEDED,
            threshold=limit,
            actual=share,
            cap=cap_from_budget(room, exposure.unit_gross),
            snapshot_ts=state.as_of,
            detail=(
                f"book exposure would be {dstr(share)} of equity against a limit of "
                f"{aggregate.max_portfolio_exposure_pct}"
            ),
        )
    return ok("aggregate_exposure", policy, threshold=limit, actual=share, snapshot_ts=state.as_of)


def correlated_intent(proposal: TradeProposal, context: RiskContext, policy: Policy) -> CheckResult | None:
    aggregate = policy.aggregate
    if aggregate is None:
        return None
    state = context.aggregate_state
    if state is None:
        return missing(
            "correlated_intent",
            ReasonCode.AGGREGATE_STATE_MISSING,
            "pending intents are unknown; crowding into one name cannot be detected",
        )
    direction = "long" if exposure_of(proposal, context).change >= ZERO else "short"
    now = parse_ts(context.evaluated_at)
    window = aggregate.correlated_intent_window_seconds
    agents = {context.agent_id}
    for intent in state.pending_intents:
        if intent.symbol != proposal.symbol or intent.direction != direction:
            continue
        age = (now - parse_ts(intent.proposed_at)).total_seconds()
        if 0 <= age <= window:
            agents.add(intent.agent_id)
    count = len(agents)
    limit = aggregate.max_correlated_intent_agents
    if count > limit:
        return fail(
            "correlated_intent",
            policy,
            ReasonCode.CORRELATED_INTENT_DETECTED,
            threshold=Decimal(limit),
            actual=Decimal(count),
            snapshot_ts=state.as_of,
            detail=(
                f"{count} agents are going {direction} {proposal.symbol} within {window}s; "
                f"the limit is {limit}"
            ),
        )
    return ok(
        "correlated_intent",
        policy,
        threshold=Decimal(limit),
        actual=Decimal(count),
        snapshot_ts=state.as_of,
    )


def _concentration_by_bucket(
    check_id: str,
    code: ReasonCode,
    limit_str: str | None,
    buckets: dict[str, str],
    keys: list[str],
    proposal: TradeProposal,
    context: RiskContext,
    policy: Policy,
    label: str,
) -> CheckResult:
    if limit_str is None:
        return ok(check_id, policy, detail=f"no {label} concentration limit configured")
    state = context.aggregate_state
    if state is None:
        return missing(check_id, ReasonCode.AGGREGATE_STATE_MISSING, f"{label} exposure is unknown")
    equity = equity_of(context.portfolio_snapshot)
    if equity is None:
        return missing(check_id, ReasonCode.PORTFOLIO_STATE_MISSING, "no equity to measure exposure against")
    exposure = exposure_of(proposal, context)
    if not exposure.priced:
        return missing(check_id, exposure.missing_code or ReasonCode.PRICE_MISSING, "unpriced order")
    if not exposure.increases_risk or not keys:
        return ok(check_id, policy, detail=f"the order does not increase {label} exposure")
    limit = dec(limit_str)
    worst_share = None
    worst_key = None
    worst_held = ZERO
    for key in sorted(keys):
        held = dec(buckets.get(key, "0"))
        share = _pct(add(held, exposure.change), equity)
        if share is not None and (worst_share is None or share > worst_share):
            worst_share, worst_key, worst_held = share, key, held
    if worst_share is not None and worst_share > limit:
        room = subtract(multiply(equity, limit), worst_held)
        return fail(
            check_id,
            policy,
            code,
            threshold=limit,
            actual=worst_share,
            cap=cap_from_budget(room, exposure.unit_gross),
            snapshot_ts=state.as_of,
            detail=(
                f"{label} {worst_key} would carry {dstr(worst_share)} of equity "
                f"against a limit of {limit_str}"
            ),
        )
    return ok(check_id, policy, threshold=limit, actual=worst_share, snapshot_ts=state.as_of)


def model_provider_concentration(
    proposal: TradeProposal, context: RiskContext, policy: Policy
) -> CheckResult | None:
    aggregate = policy.aggregate
    if aggregate is None:
        return None
    state = context.aggregate_state
    buckets = {} if state is None else dict(state.exposure_by_model_provider)
    return _concentration_by_bucket(
        "model_provider_concentration",
        ReasonCode.MODEL_PROVIDER_CONCENTRATION_EXCEEDED,
        aggregate.max_exposure_per_model_provider_pct,
        buckets,
        [proposal.model.provider],
        proposal,
        context,
        policy,
        "model provider",
    )


def signal_source_concentration(
    proposal: TradeProposal, context: RiskContext, policy: Policy
) -> CheckResult | None:
    aggregate = policy.aggregate
    if aggregate is None:
        return None
    state = context.aggregate_state
    buckets = {} if state is None else dict(state.exposure_by_signal_source)
    return _concentration_by_bucket(
        "signal_source_concentration",
        ReasonCode.SIGNAL_SOURCE_CONCENTRATION_EXCEEDED,
        aggregate.max_exposure_per_signal_source_pct,
        buckets,
        list(proposal.signal_sources),
        proposal,
        context,
        policy,
        "signal source",
    )


# ---------------------------------------------------------------------------------------------------
# Liquidity (R-LIQ)
# ---------------------------------------------------------------------------------------------------
def liquidity_adv(proposal: TradeProposal, context: RiskContext, policy: Policy) -> CheckResult | None:
    liquidity = policy.liquidity
    if liquidity is None:
        return None
    if proposal.asset_class != "equity":
        return ok("liquidity_adv", policy, detail="option liquidity is checked by option_liquidity")
    quote = quote_for(context.market_snapshot, proposal.symbol)
    if quote is None:
        return missing("liquidity_adv", ReasonCode.PRICE_MISSING, f"no quote for {proposal.symbol}")
    if quote.adv is None:
        return missing(
            "liquidity_adv",
            ReasonCode.LIQUIDITY_DATA_MISSING,
            f"no average daily volume for {proposal.symbol}; participation cannot be bounded",
            source=quote.source,
            snapshot_ts=quote.as_of,
        )
    adv = dec(quote.adv)
    limit = dec(liquidity.max_pct_of_adv)
    participation = _pct(proposal.total_quantity, adv)
    if participation is None or participation > limit:
        return fail(
            "liquidity_adv",
            policy,
            ReasonCode.ADV_PARTICIPATION_EXCEEDED,
            threshold=limit,
            actual=participation,
            cap=floor_units(multiply(adv, limit)),
            source=quote.source,
            snapshot_ts=quote.as_of,
            detail=(
                f"{dstr(proposal.total_quantity)} units is "
                f"{'an unbounded share' if participation is None else dstr(participation)} of the "
                f"{dstr(adv)} average daily volume; the limit is {liquidity.max_pct_of_adv}"
            ),
        )
    if liquidity.max_estimated_impact_bps is not None:
        if quote.spread_pct is None:
            return missing(
                "liquidity_adv",
                ReasonCode.LIQUIDITY_DATA_MISSING,
                f"no quoted spread for {proposal.symbol}; execution impact cannot be estimated",
                source=quote.source,
                snapshot_ts=quote.as_of,
            )
        impact = multiply(multiply(dec(quote.spread_pct), HALF), BASIS_POINTS)
        allowed = dec(liquidity.max_estimated_impact_bps)
        if impact > allowed:
            return fail(
                "liquidity_adv",
                policy,
                ReasonCode.ESTIMATED_IMPACT_EXCEEDED,
                threshold=allowed,
                actual=impact,
                source=quote.source,
                snapshot_ts=quote.as_of,
                detail=(
                    f"crossing half of the {quote.spread_pct} spread costs about {dstr(impact)} bps; "
                    f"the limit is {liquidity.max_estimated_impact_bps}"
                ),
            )
    return ok(
        "liquidity_adv",
        policy,
        threshold=limit,
        actual=participation,
        source=quote.source,
        snapshot_ts=quote.as_of,
    )


def option_liquidity(proposal: TradeProposal, context: RiskContext, policy: Policy) -> CheckResult | None:
    liquidity = policy.liquidity
    if liquidity is None:
        return None
    if proposal.asset_class != "equity_option":
        return ok("option_liquidity", policy, detail="not an options proposal")
    for leg in proposal.legs:
        occ = leg.occ_symbol(proposal.symbol)
        quote = option_quote_for(context.market_snapshot, occ)
        if quote is None:
            return missing("option_liquidity", ReasonCode.PRICE_MISSING, f"no option quote for {occ}")
        if liquidity.max_option_spread_pct is not None:
            if quote.spread_pct is None:
                return missing(
                    "option_liquidity",
                    ReasonCode.LIQUIDITY_DATA_MISSING,
                    f"no quoted spread for {occ}",
                    source=quote.source,
                    snapshot_ts=quote.as_of,
                )
            limit = dec(liquidity.max_option_spread_pct)
            spread = dec(quote.spread_pct)
            if spread > limit:
                return fail(
                    "option_liquidity",
                    policy,
                    ReasonCode.OPTION_SPREAD_TOO_WIDE,
                    threshold=limit,
                    actual=spread,
                    source=quote.source,
                    snapshot_ts=quote.as_of,
                    detail=f"{occ} spread {quote.spread_pct} is wider than {liquidity.max_option_spread_pct}",
                )
        if liquidity.min_option_open_interest is not None:
            if quote.open_interest is None:
                return missing(
                    "option_liquidity",
                    ReasonCode.LIQUIDITY_DATA_MISSING,
                    f"no open interest for {occ}",
                    source=quote.source,
                    snapshot_ts=quote.as_of,
                )
            if quote.open_interest < liquidity.min_option_open_interest:
                return fail(
                    "option_liquidity",
                    policy,
                    ReasonCode.OPTION_OPEN_INTEREST_TOO_LOW,
                    threshold=Decimal(liquidity.min_option_open_interest),
                    actual=Decimal(quote.open_interest),
                    source=quote.source,
                    snapshot_ts=quote.as_of,
                    detail=(
                        f"{occ} open interest {quote.open_interest} is below "
                        f"{liquidity.min_option_open_interest}"
                    ),
                )
    return ok("option_liquidity", policy, snapshot_ts=_market_ts(context))


# ---------------------------------------------------------------------------------------------------
# Time controls (R-TIME)
# ---------------------------------------------------------------------------------------------------
def time_blackout(proposal: TradeProposal, context: RiskContext, policy: Policy) -> CheckResult | None:
    time_policy = policy.time
    if time_policy is None:
        return None
    calendar = context.calendar
    if calendar is None:
        return missing(
            "time_blackout", ReasonCode.CALENDAR_MISSING, "no calendar in the context"
        )
    days = calendar.earnings_within_days.get(proposal.symbol)
    if days is not None and days <= time_policy.earnings_blackout_days_before:
        return fail(
            "time_blackout",
            policy,
            ReasonCode.EARNINGS_BLACKOUT,
            threshold=Decimal(time_policy.earnings_blackout_days_before),
            actual=Decimal(days),
            detail=(
                f"{proposal.symbol} reports in {days} day(s); the blackout starts "
                f"{time_policy.earnings_blackout_days_before} day(s) before"
            ),
        )
    minutes = calendar.macro_event_within_minutes
    if minutes is not None and minutes <= time_policy.macro_event_blackout_minutes:
        return fail(
            "time_blackout",
            policy,
            ReasonCode.MACRO_EVENT_BLACKOUT,
            threshold=Decimal(time_policy.macro_event_blackout_minutes),
            actual=Decimal(minutes),
            detail=(
                f"a macro event is {minutes} minute(s) away; the blackout window is "
                f"{time_policy.macro_event_blackout_minutes} minute(s)"
            ),
        )
    return ok("time_blackout", policy, snapshot_ts=context.evaluated_at)


def session_window(proposal: TradeProposal, context: RiskContext, policy: Policy) -> CheckResult | None:
    time_policy = policy.time
    if time_policy is None:
        return None
    calendar = context.calendar
    if calendar is None:
        return missing(
            "session_window", ReasonCode.CALENDAR_MISSING, "no calendar in the context"
        )
    if calendar.session not in TRADEABLE_SESSIONS:
        return fail(
            "session_window",
            policy,
            ReasonCode.SESSION_WINDOW_RESTRICTED,
            detail=f"the session is {calendar.session}; orders are accepted in the regular session only",
        )
    if time_policy.no_trade_first_minutes > 0:
        if calendar.minutes_since_open is None:
            return missing(
                "session_window",
                ReasonCode.CALENDAR_MISSING,
                "minutes since the open are unknown; the opening window cannot be enforced",
            )
        if calendar.minutes_since_open < time_policy.no_trade_first_minutes:
            return fail(
                "session_window",
                policy,
                ReasonCode.SESSION_WINDOW_RESTRICTED,
                threshold=Decimal(time_policy.no_trade_first_minutes),
                actual=Decimal(calendar.minutes_since_open),
                detail=(
                    f"{calendar.minutes_since_open} minute(s) since the open; the first "
                    f"{time_policy.no_trade_first_minutes} are closed to new orders"
                ),
            )
    if time_policy.no_trade_last_minutes > 0:
        if calendar.minutes_to_close is None:
            return missing(
                "session_window",
                ReasonCode.CALENDAR_MISSING,
                "minutes to the close are unknown; the closing window cannot be enforced",
            )
        if calendar.minutes_to_close < time_policy.no_trade_last_minutes:
            return fail(
                "session_window",
                policy,
                ReasonCode.SESSION_WINDOW_RESTRICTED,
                threshold=Decimal(time_policy.no_trade_last_minutes),
                actual=Decimal(calendar.minutes_to_close),
                detail=(
                    f"{calendar.minutes_to_close} minute(s) to the close; the last "
                    f"{time_policy.no_trade_last_minutes} are closed to new orders"
                ),
            )
    overnight = time_policy.max_overnight_exposure_pct
    if overnight is not None:
        result = _overnight_exposure(proposal, context, policy, dec(overnight), overnight)
        if result is not None:
            return result
    return ok("session_window", policy, snapshot_ts=context.evaluated_at)


def _overnight_exposure(
    proposal: TradeProposal, context: RiskContext, policy: Policy, limit: Decimal, limit_str: str
) -> CheckResult | None:
    """Any position opened may be carried overnight, so the cap applies whenever it is configured."""
    exposure = exposure_of(proposal, context)
    if not exposure.priced:
        return missing(
            "session_window",
            exposure.missing_code or ReasonCode.PRICE_MISSING,
            "the order cannot be valued against the overnight limit",
        )
    if not exposure.increases_risk:
        return None
    equity = equity_of(context.portfolio_snapshot)
    held = gross_exposure_of(context.portfolio_snapshot)
    if equity is None or held is None:
        return missing(
            "session_window", ReasonCode.PORTFOLIO_STATE_MISSING, "no portfolio exposure"
        )
    share = _pct(add(held, exposure.change), equity)
    if share is not None and share > limit:
        room = subtract(multiply(equity, limit), held)
        return fail(
            "session_window",
            policy,
            ReasonCode.OVERNIGHT_EXPOSURE_EXCEEDED,
            threshold=limit,
            actual=share,
            cap=cap_from_budget(room, exposure.unit_gross),
            snapshot_ts=_portfolio_ts(context),
            source=_portfolio_source(context),
            detail=f"exposure would be {dstr(share)} of equity against an overnight limit of {limit_str}",
        )
    return None


# ---------------------------------------------------------------------------------------------------
# Registry -- the order lives in CHECK_IDS, not here
# ---------------------------------------------------------------------------------------------------
# ---------------------------------------------------------------------------------------------------
# Account capability (REQ-35): is this account PERMITTED to do this at all?
# ---------------------------------------------------------------------------------------------------
def account_capability(
    proposal: TradeProposal, context: RiskContext, policy: Policy
) -> CheckResult | None:
    """What the broker says about the account, before anything is asked of it.

    Every other check asks whether the ORDER is sound. This one asks whether the ACCOUNT may place it -
    a different question with a different failure mode. Alpaca will reject a blocked account or an
    under-privileged options order at submission; discovering that at the venue means the decision
    record says APPROVE for an order that was never placeable, which is precisely the gap between "we
    decided" and "it happened" that this product exists to close.

    Fails closed throughout (E2): a field the broker did not report is ACCOUNT_STATE_MISSING, never an
    assumption of permission. A permissive default here would be a control that protects nobody.
    """
    account = policy.account
    if account is None:
        return None
    state = context.account_state
    if state is None:
        return missing(
            "account_capability",
            ReasonCode.ACCOUNT_STATE_MISSING,
            "an account capability check is enabled but the context carries no account state",
        )

    for field, description in (
        ("trading_blocked", "trading is blocked on this account"),
        ("account_blocked", "the account is blocked"),
        ("trade_suspended_by_user", "trading is suspended by the account holder"),
    ):
        value = getattr(state, field)
        if value is None:
            return missing(
                "account_capability",
                ReasonCode.ACCOUNT_STATE_MISSING,
                f"the broker did not report {field}; permission is not assumed",
                snapshot_ts=state.as_of,
            )
        if value:
            return fail(
                "account_capability",
                policy,
                ReasonCode.ACCOUNT_TRADING_BLOCKED,
                snapshot_ts=state.as_of,
                detail=description,
            )

    if account.require_active:
        if state.status is None:
            return missing(
                "account_capability",
                ReasonCode.ACCOUNT_STATE_MISSING,
                "the broker did not report an account status",
                snapshot_ts=state.as_of,
            )
        if state.status.strip().upper() != "ACTIVE":
            return fail(
                "account_capability",
                policy,
                ReasonCode.ACCOUNT_NOT_ACTIVE,
                snapshot_ts=state.as_of,
                detail=f"account status is {state.status!r}, not ACTIVE",
            )

    required_level = account.min_options_trading_level
    if required_level is not None and proposal.asset_class == "equity_option":
        if state.options_trading_level is None:
            return missing(
                "account_capability",
                ReasonCode.ACCOUNT_STATE_MISSING,
                "the broker did not report an options trading level",
                snapshot_ts=state.as_of,
            )
        if state.options_trading_level < required_level:
            return fail(
                "account_capability",
                policy,
                ReasonCode.OPTIONS_LEVEL_INSUFFICIENT,
                threshold=Decimal(required_level),
                actual=Decimal(state.options_trading_level),
                snapshot_ts=state.as_of,
                detail=(
                    f"options trading level {state.options_trading_level} is below the required "
                    f"{required_level}"
                ),
            )

    # NET-POSITION AWARE, not "any non-close sell is a short". `proposal.intent` is a label the caller
    # chose; the truth is in the portfolio. Selling 15 against a held 10 is a close of the 10 PLUS a
    # short of the 5 - the close needs no permission at all, and only the excess is a short. Deriving
    # this from state rather than trusting the declared intent matches the discipline F-1/F-2 already
    # apply to valuation: a safety-critical fact is computed from context, never taken on the caller's
    # word (Alpaca does not represent a simultaneous long and short in one equity symbol, so held-long
    # minus sold is a complete accounting, not an approximation).
    if account.require_shorting_enabled_for_short_legs and proposal.asset_class == "equity":
        sell_quantity = sum(
            (dec(leg.quantity) for leg in proposal.legs if leg.side == "sell"), start=ZERO
        )
        if sell_quantity > ZERO:
            portfolio = context.portfolio_snapshot
            held_long = (
                ZERO
                if portfolio is None
                else sum(
                    (
                        dec(position.quantity)
                        for position in portfolio.positions
                        if position.symbol == proposal.symbol and dec(position.quantity) > ZERO
                    ),
                    start=ZERO,
                )
            )
            short_excess = sell_quantity - held_long
            if short_excess > ZERO:
                if state.shorting_enabled is None:
                    return missing(
                        "account_capability",
                        ReasonCode.ACCOUNT_STATE_MISSING,
                        "the broker did not report whether shorting is enabled",
                        snapshot_ts=state.as_of,
                    )
                if not state.shorting_enabled:
                    # The closing portion needs no permission and must not be swept up with the short
                    # excess it did not ask for (E5: never a silent resize - the cap and its reason
                    # code are both recorded, and the authorization layer re-authorizes against it).
                    closing_quantity = sell_quantity - short_excess
                    return reduce_to(
                        "account_capability",
                        policy,
                        ReasonCode.SHORTING_NOT_PERMITTED,
                        floor_units(closing_quantity),
                        threshold=ZERO,
                        actual=short_excess,
                        snapshot_ts=state.as_of,
                        detail=(
                            f"the account may not sell short; {dstr(closing_quantity)} of "
                            f"{dstr(sell_quantity)} closes the held position, {dstr(short_excess)} "
                            "would open a short"
                        ),
                    )

    return ok(
        "account_capability",
        policy,
        source=state.source,
        snapshot_ts=state.as_of,
        detail=(
            f"account status {state.status!r}, not blocked or suspended"
            + (
                f", options level {state.options_trading_level} meets {required_level}"
                if required_level is not None and proposal.asset_class == "equity_option"
                else ""
            )
        ),
    )


# ---------------------------------------------------------------------------------------------------
# Defined-risk structure (F-31 / Risk Canon R-OPT-3): the legs must FORM the strategy they declare
# ---------------------------------------------------------------------------------------------------
#: What each named vertical must be: (contract_type, side of the LOWER strike, side of the HIGHER strike).
#: A vertical is defined-risk precisely because the long leg caps the short one; get the sides wrong and
#: the same two legs are an unhedged short wearing a spread's name.
VERTICAL_SHAPES: dict[str, tuple[str, str, str]] = {
    "bull_call_spread": ("call", "buy", "sell"),
    "bear_call_spread": ("call", "sell", "buy"),
    # A bull put spread is a CREDIT spread: sell the higher-strike put, buy the lower one for
    # protection. So the LOWER strike is the long leg. A bear put spread is its mirror - buy the
    # higher strike, sell the lower. These two were inverted when first written, which would have
    # refused every legitimate put credit spread and accepted its opposite.
    "bull_put_spread": ("put", "buy", "sell"),
    "bear_put_spread": ("put", "sell", "buy"),
}


def structure_valid(proposal: TradeProposal, context: RiskContext, policy: Policy) -> CheckResult | None:
    """Every short option leg must be covered, and a named spread must actually be that spread.

    `STRATEGY_LEG_COUNTS` constrains the NUMBER of legs and nothing else, so before this check a
    `bull_call_spread` of two SHORT calls passed the entire decision plane (F-31), as did an
    `iron_condor` of four short calls and a naked short dressed as `custom`. The greek caps bounded the
    exposure at size but are portfolio limits, not structure rules: they bind at 50 contracts, not at 5.

    Two rules, both structural, neither of which can be satisfied by getting the count right:
      1. COVERAGE. For each (contract_type, expiry), long contracts must be >= short contracts. This is
         what makes max loss computable at construction - the long leg caps the short one - and it is
         the whole reason a defined-risk strategy is defined-risk.
      2. SHAPE. A named vertical must have the sides its name claims: a bull call spread is long the
         lower strike and short the higher one. Anything else is a different position with a
         reassuring label, which is worse than an honest `custom`.
    """
    if proposal.asset_class != "equity_option":
        return ok("structure_valid", policy, detail="not an options proposal")

    longs: dict[tuple[str, str], Decimal] = {}
    shorts: dict[tuple[str, str], Decimal] = {}
    for leg in proposal.legs:
        if leg.contract_type is None or leg.expiry is None:
            return missing(
                "structure_valid",
                ReasonCode.STRUCTURE_INVALID,
                "an option leg carries no contract type or expiry; coverage cannot be established",
            )
        bucket = longs if leg.side == "buy" else shorts
        key = (leg.contract_type, leg.expiry)
        bucket[key] = add(bucket.get(key, ZERO), dec(leg.quantity))

    for key, short_quantity in shorts.items():
        covered = longs.get(key, ZERO)
        if covered < short_quantity:
            contract_type, expiry = key
            return fail(
                "structure_valid",
                policy,
                ReasonCode.NAKED_SHORT_NOT_PERMITTED,
                threshold=covered,
                actual=short_quantity,
                detail=(
                    f"{dstr(short_quantity)} short {contract_type} expiring {expiry} against "
                    f"{dstr(covered)} long: the uncovered portion has unbounded loss"
                ),
            )

    shape = VERTICAL_SHAPES.get(proposal.strategy)
    if shape is not None:
        contract_type, lower_side, upper_side = shape
        if len(proposal.legs) != 2:
            return fail(
                "structure_valid",
                policy,
                ReasonCode.STRUCTURE_INVALID,
                detail=f"{proposal.strategy} must have exactly two legs, not {len(proposal.legs)}",
            )
        if any(leg.contract_type != contract_type for leg in proposal.legs):
            return fail(
                "structure_valid",
                policy,
                ReasonCode.STRUCTURE_INVALID,
                detail=f"{proposal.strategy} must be built from {contract_type}s only",
            )
        if len({leg.expiry for leg in proposal.legs}) != 1:
            return fail(
                "structure_valid",
                policy,
                ReasonCode.STRUCTURE_INVALID,
                detail=f"{proposal.strategy} legs must share one expiry; this is a diagonal, not a vertical",
            )
        lower, upper = sorted(proposal.legs, key=lambda leg: dec(leg.strike or "0"))
        if dec(lower.strike or "0") == dec(upper.strike or "0"):
            return fail(
                "structure_valid",
                policy,
                ReasonCode.STRUCTURE_INVALID,
                detail=f"{proposal.strategy} legs must have different strikes",
            )
        if lower.side != lower_side or upper.side != upper_side:
            return fail(
                "structure_valid",
                policy,
                ReasonCode.STRUCTURE_INVALID,
                detail=(
                    f"{proposal.strategy} must be {lower_side} the {lower.strike} and {upper_side} the "
                    f"{upper.strike}; these legs are {lower.side}/{upper.side}"
                ),
            )
        if dec(lower.quantity) != dec(upper.quantity):
            return fail(
                "structure_valid",
                policy,
                ReasonCode.STRUCTURE_INVALID,
                detail=(
                    f"{proposal.strategy} legs must be equal size; {dstr(dec(lower.quantity))} vs "
                    f"{dstr(dec(upper.quantity))} leaves the difference uncovered"
                ),
            )

    return ok(
        "structure_valid",
        policy,
        actual=Decimal(len(proposal.legs)),
        detail=(
            f"{proposal.strategy}: every short leg is covered by a long of the same type and expiry"
        ),
    )


CHECK_FUNCTIONS: dict[str, CheckFunction] = {
    "market_data_presence": market_data_presence,
    "portfolio_state_presence": portfolio_state_presence,
    "proposal_expiry": proposal_expiry,
    "restricted_symbol": restricted_symbol,
    "restricted_strategy": restricted_strategy,
    "leg_limit": leg_limit,
    "position_limit": position_limit,
    "capital_threshold": capital_threshold,
    "buying_power_sufficiency": buying_power_sufficiency,
    "buying_power_utilization": buying_power_utilization,
    "concentration_limit": concentration_limit,
    "sector_concentration": sector_concentration,
    "drawdown_limit": drawdown_limit,
    "duplicate_order": duplicate_order,
    "erroneous_order": erroneous_order,
    "days_to_expiry": days_to_expiry_check,
    "options_delta_limit": options_delta_limit,
    "options_gamma_limit": options_gamma_limit,
    "options_vega_limit": options_vega_limit,
    "response_level_gate": response_level_gate,
    "agent_budget": agent_budget,
    "invalidation_defined": invalidation_defined,
    "reward_risk": reward_risk,
    "risk_per_trade": risk_per_trade,
    "drawdown_size_scaling": drawdown_size_scaling,
    "consecutive_loss_review": consecutive_loss_review,
    "aggregate_exposure": aggregate_exposure,
    "correlated_intent": correlated_intent,
    "model_provider_concentration": model_provider_concentration,
    "signal_source_concentration": signal_source_concentration,
    "liquidity_adv": liquidity_adv,
    "option_liquidity": option_liquidity,
    "time_blackout": time_blackout,
    "session_window": session_window,
    "options_short_gamma_limit": options_short_gamma_limit,
    "options_short_vega_limit": options_short_vega_limit,
    "account_capability": account_capability,
    "structure_valid": structure_valid,
}
