"""Mizan backup demo UI.

A deliberately minimal Streamlit client for the Portfolio Governor PAPER API.

Safety invariants enforced by this file:
  * The UI never talks to Alpaca. Every broker-touching action goes through the
    backend execution endpoint, which remains the sole mutation boundary.
  * No market data, agent output, or risk value is fabricated. Anything the
    operator typed is labelled as manually supplied, never as live.
  * No credential is read, displayed, or logged here.
"""

from __future__ import annotations

import os
from datetime import datetime, timezone
from typing import Any

import httpx
import streamlit as st

DEFAULT_BASE_URL = os.getenv("MIZAN_BACKEND_URL", "http://localhost:8000")
DEFAULT_TIMEOUT = float(os.getenv("MIZAN_BACKEND_TIMEOUT", "30"))

# Backend states that mean a real PAPER order exists at the broker.
BROKER_ORDER_STATES = {"SUBMITTED", "RECONCILED_EXISTING_ORDER"}
DRY_RUN_STATES = {"WOULD_SUBMIT"}

STAGES = (
    ("Scout / Data Agent", "Candidate discovery and market context"),
    ("Analyst", "Evidence synthesis"),
    ("Bull thesis", "Constructive case"),
    ("Bear thesis", "Destructive case"),
    ("Winning thesis", "Debate adjudication"),
    ("Trader Agent", "Trade construction"),
)

CSS = """
<style>
.block-container {padding-top: 2.2rem; max-width: 1400px;}
.mz-title {font-size: 2.0rem; font-weight: 650; letter-spacing: .02em; margin-bottom: .1rem;}
.mz-sub {color: #6b7280; font-size: .95rem; margin-bottom: .9rem;}
.mz-pill {display:inline-block; padding:2px 9px; margin:0 6px 6px 0; border-radius:3px;
          font-size:.76rem; font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
          border:1px solid #d0d7de; color:#374151; background:#f6f8fa;}
.mz-ok  {border-color:#1a7f37; color:#1a7f37;}
.mz-warn{border-color:#9a6700; color:#9a6700;}
.mz-bad {border-color:#b42318; color:#b42318;}
.mz-mut {border-color:#d0d7de; color:#6b7280;}
.mz-card {border:1px solid #e5e7eb; border-radius:4px; padding:.7rem .9rem; margin-bottom:.5rem;
          background:#ffffff;}
.mz-card h4 {margin:0 0 .25rem 0; font-size:.95rem; font-weight:600;}
.mz-card p {margin:0; color:#4b5563; font-size:.85rem;}
.mz-decision {border:2px solid; border-radius:4px; padding:.9rem 1.1rem; margin:.4rem 0 .8rem 0;}
.mz-decision .d {font-size:1.55rem; font-weight:700; letter-spacing:.04em;}
.mz-decision .r {font-size:.88rem; color:#374151; margin-top:.35rem;}
.mz-ev {border-left:2px solid #d0d7de; padding:.1rem 0 .1rem .8rem; margin-bottom:.35rem;}
.mz-ev .t {font-family: ui-monospace, SFMono-Regular, Menlo, monospace; font-size:.74rem;
           color:#6b7280;}
.mz-ev .a {font-weight:600; font-size:.88rem;}
</style>
"""


# --------------------------------------------------------------------------
# Backend client
# --------------------------------------------------------------------------
class BackendError(Exception):
    def __init__(
        self,
        message: str,
        *,
        code: str = "CLIENT_ERROR",
        status: int | None = None,
        details: Any = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.code = code
        self.status = status
        self.details = details


class BackendClient:
    """Thin, timeout-bounded HTTP client. The only network peer is the backend."""

    def __init__(self, base_url: str, timeout: float) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout

    def _request(self, method: str, path: str, payload: dict[str, Any] | None = None) -> Any:
        url = f"{self.base_url}{path}"
        try:
            response = httpx.request(method, url, json=payload, timeout=self.timeout)
        except httpx.TimeoutException as exc:
            raise BackendError(
                f"Request timed out after {self.timeout:g}s: {method} {path}",
                code="TIMEOUT",
            ) from exc
        except httpx.HTTPError as exc:
            raise BackendError(
                f"Backend unreachable at {self.base_url} ({type(exc).__name__}).",
                code="UNREACHABLE",
            ) from exc
        try:
            body = response.json()
        except ValueError:
            raise BackendError(
                f"Backend returned a non-JSON response (HTTP {response.status_code}).",
                code="BAD_RESPONSE",
                status=response.status_code,
            ) from None
        if response.status_code >= 400:
            error = body.get("error", {}) if isinstance(body, dict) else {}
            raise BackendError(
                error.get("message", f"HTTP {response.status_code}"),
                code=error.get("code", f"HTTP_{response.status_code}"),
                status=response.status_code,
                details=error.get("details"),
            )
        return body

    def health(self) -> dict[str, Any]:
        return self._request("GET", "/health")

    def portfolio(self) -> dict[str, Any]:
        return self._request("GET", "/portfolio")

    def evaluate(self, proposal: dict[str, Any], market_risk: dict[str, Any]) -> dict[str, Any]:
        return self._request(
            "POST", "/proposals/evaluate", {"proposal": proposal, "market_risk": market_risk}
        )

    def execute(self, proposal_id: str) -> dict[str, Any]:
        return self._request("POST", f"/proposals/{proposal_id}/execute")

    def lifecycle(self, proposal_id: str) -> dict[str, Any]:
        return self._request("GET", f"/proposals/{proposal_id}")

    def audit(self, proposal_id: str) -> list[dict[str, Any]]:
        return self._request("GET", f"/proposals/{proposal_id}/audit")

    def recent(self, limit: int = 20) -> list[dict[str, Any]]:
        return self._request("GET", f"/recent?limit={limit}")


# --------------------------------------------------------------------------
# Small helpers
# --------------------------------------------------------------------------
def pill(text: str, tone: str = "mut") -> str:
    return f'<span class="mz-pill mz-{tone}">{text}</span>'


def show_error(exc: BackendError, context: str) -> None:
    st.error(f"**{context}** - `{exc.code}` {exc.message}")
    if exc.details:
        with st.expander("Validation details"):
            st.json(exc.details)


def parse_ts(raw: str | None) -> datetime | None:
    if not raw:
        return None
    try:
        value = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    except ValueError:
        return None
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def fmt_ts(raw: str | None) -> str:
    parsed = parse_ts(raw)
    return parsed.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC") if parsed else "-"


def money(value: Any) -> str:
    try:
        return f"${float(value):,.2f}"
    except (TypeError, ValueError):
        return "-"


def check_row(label: str, tone: str, detail: str) -> None:
    icons = {"ok": "\U0001F7E2", "bad": "\U0001F534", "warn": "\U0001F7E1", "pending": "⚪"}
    left, right = st.columns([0.36, 0.64])
    left.markdown(f"{icons.get(tone, '⚪')} **{label}**")
    right.markdown(
        f"<span style='color:#4b5563;font-size:.86rem'>{detail}</span>", unsafe_allow_html=True
    )


def state(flag: bool) -> str:
    return "ok" if flag else "bad"


# --------------------------------------------------------------------------
# Sections
# --------------------------------------------------------------------------
def render_header(
    health: dict[str, Any] | None, error: BackendError | None, base_url: str
) -> None:
    st.markdown('<div class="mz-title">MĪZĀN / ميزان</div>',
                unsafe_allow_html=True)
    st.markdown(
        '<div class="mz-sub">Evidence-weighted options decisions</div>', unsafe_allow_html=True
    )

    if health is None:
        badges = pill("Backend: DISCONNECTED", "bad") + pill(base_url, "mut")
        st.markdown(badges, unsafe_allow_html=True)
        if error is not None:
            show_error(error, "Backend health check failed")
        return

    db_ok = health.get("status") == "ok"
    execution_enabled = bool(health.get("execution_enabled"))
    dry_run = bool(health.get("dry_run"))
    kill = bool(health.get("kill_switch"))
    mode = "ALPACA_PAPER_DRY_RUN" if dry_run else "ALPACA_PAPER"

    badges = pill(f"Backend: {'CONNECTED' if db_ok else 'DEGRADED'}", state(db_ok))
    badges += pill(
        "Alpaca PAPER" if health.get("paper_only") else "PAPER FLAG MISSING",
        state(bool(health.get("paper_only"))),
    )
    badges += pill(
        f"Execution: {'ENABLED' if execution_enabled else 'DISABLED'}",
        "ok" if execution_enabled else "warn",
    )
    badges += pill(f"Mode: {mode}", "warn" if dry_run else "ok")
    badges += pill(f"Dry-run: {'ON' if dry_run else 'OFF'}", "warn" if dry_run else "ok")
    if kill:
        badges += pill("KILL SWITCH ACTIVE", "bad")
    badges += pill(f"AI provider: {health.get('ai_provider', 'unknown')}", "mut")
    badges += pill(
        f"DB: {health.get('database', {}).get('technology', '?')}",
        state(health.get("database", {}).get("status") == "ok"),
    )
    st.markdown(badges, unsafe_allow_html=True)


def render_candidate_input(client: BackendClient | None) -> dict[str, Any]:
    st.subheader("1 - Market / candidate input")
    st.warning(
        "**Scout Agent is not integrated with this backend.** No candidate-discovery, "
        "market-summary, signal, or news endpoint exists on the API. Everything below is "
        "**manual demo input supplied by the operator**, not live market data.",
        icon="⚠️",
    )

    defaults = st.session_state.get("form_defaults", {})
    with st.form("candidate"):
        c1, c2, c3, c4 = st.columns(4)
        symbol = c1.text_input("Ticker (US equity)", defaults.get("symbol", "AAPL")).strip().upper()
        side = c2.selectbox(
            "Side", ["BUY", "SELL"], index=0 if defaults.get("side", "BUY") == "BUY" else 1
        )
        quantity = c3.number_input(
            "Quantity (whole shares)", min_value=1, step=1, value=int(defaults.get("quantity", 1))
        )
        price = c4.number_input(
            "Estimated price",
            min_value=0.01,
            step=1.0,
            value=float(defaults.get("estimated_price", 250.00)),
            format="%.2f",
        )

        c5, c6 = st.columns([0.3, 0.7])
        confidence = c5.slider(
            "Strategy confidence", 0.0, 1.0, float(defaults.get("strategy_confidence", 0.82)), 0.01
        )
        proposal_id = c6.text_input(
            "Proposal ID (immutable in the backend - reuse only to reload)",
            defaults.get(
                "proposal_id", f"mizan-demo-{datetime.now(timezone.utc):%Y%m%d-%H%M%S}"
            ),
        ).strip()

        thesis = st.text_area(
            "Thesis",
            defaults.get("thesis", "Manual demo thesis for the Mizan backup lifecycle run."),
            height=70,
        )
        invalidation = st.text_area(
            "Invalidation condition",
            defaults.get("invalidation_condition", "Upstream strategy signal reverses."),
            height=70,
        )

        st.markdown("**Demo / manually supplied risk snapshot** - not a live market-data feed.")
        r1, r2, r3 = st.columns(3)
        volatility = r1.number_input(
            "Annualized volatility (ratio)",
            min_value=0.0,
            max_value=5.0,
            value=float(defaults.get("annualized_volatility", 0.30)),
            step=0.01,
            format="%.2f",
        )
        drawdown = r2.number_input(
            "Max drawdown 30d (ratio)",
            min_value=0.0,
            max_value=1.0,
            value=float(defaults.get("max_drawdown_30d", 0.10)),
            step=0.01,
            format="%.2f",
        )
        liquidity = r3.number_input(
            "Liquidity score (0-1)",
            min_value=0.0,
            max_value=1.0,
            value=float(defaults.get("liquidity_score", 0.95)),
            step=0.01,
            format="%.2f",
        )

        submitted = st.form_submit_button(
            "Evaluate through Risk -> AI -> Governor", type="primary", disabled=client is None
        )

    return {
        "submitted": submitted,
        "proposal": {
            "proposal_id": proposal_id,
            "symbol": symbol,
            "side": side,
            "quantity": int(quantity),
            "estimated_price": float(price),
            "strategy_confidence": float(confidence),
            "thesis": thesis,
            "invalidation_condition": invalidation,
        },
        "market_risk": {
            "symbol": symbol,
            "annualized_volatility": float(volatility),
            "max_drawdown_30d": float(drawdown),
            "liquidity_score": float(liquidity),
        },
    }


def render_pipeline(proposal: dict[str, Any] | None) -> None:
    st.subheader("2 - Agent reasoning pipeline")
    st.info(
        "Scout, Analyst, Bull/Bear debate, and Trader Agent stages are **not present in this "
        "backend**. There is no endpoint, model, or persisted record for them, so no reasoning "
        "is displayed. The cards below mark where upstream output will land.",
        icon="ℹ️",
    )
    columns = st.columns(3)
    for index, (name, purpose) in enumerate(STAGES):
        with columns[index % 3]:
            st.markdown(
                f'<div class="mz-card"><h4>{name} {pill("NOT INTEGRATED", "mut")}</h4>'
                f"<p>{purpose}</p></div>",
                unsafe_allow_html=True,
            )
    if proposal:
        with st.expander("Trader Agent stand-in - the proposal actually sent to the backend"):
            st.json(proposal)


def render_options_section(proposal: dict[str, Any] | None) -> None:
    st.subheader("3 - Options trade proposal")
    st.error(
        "**CONTRACT MISMATCH - options are not supported end-to-end.** The backend "
        "`TradeProposal` (`app/models.py`) is single-leg **US-equity only**, and the execution "
        "service hard-blocks any asset whose Alpaca asset class is not `us_equity`. This UI "
        "will **not** silently convert an options structure into an equity order.",
        icon="\U0001F6AB",
    )
    left, right = st.columns(2)
    with left:
        st.markdown("**Requested by the options spec**")
        st.markdown(
            "- underlying\n- strategy\n- direction\n- expiry\n- legs (multi-leg)\n- strikes\n"
            "- option type (call/put)\n- quantity (contracts)\n- estimated debit/credit\n"
            "- max risk\n- max reward\n- thesis\n- invalidation condition"
        )
    with right:
        st.markdown("**Supported by the backend today**")
        st.markdown(
            "- `symbol` (equity underlying)\n- `side` (BUY/SELL)\n- `quantity` (whole shares)\n"
            "- `estimated_price`\n- `strategy_confidence`\n- `thesis`\n"
            "- `invalidation_condition`\n\n"
            "Missing: expiry, legs, strikes, option type, contract multiplier, debit/credit, "
            "max risk, max reward, options asset-class validation, options order submission."
        )
    if proposal:
        st.caption(
            "Equity proposal in flight - explicitly labelled; this is not an options structure:"
        )
        cols = st.columns(5)
        cols[0].metric("Underlying", proposal.get("symbol", "-"))
        cols[1].metric("Instrument", "EQUITY")
        cols[2].metric("Direction", proposal.get("side", "-"))
        cols[3].metric("Quantity", f"{proposal.get('quantity', '-')} sh")
        cols[4].metric(
            "Est. notional",
            money(
                (proposal.get("quantity") or 0) * (proposal.get("estimated_price") or 0)
            ),
        )
        st.markdown(f"**Thesis** - {proposal.get('thesis', '-')}")
        st.markdown(f"**Invalidation** - {proposal.get('invalidation_condition', '-')}")


def render_risk_and_governor(lifecycle: dict[str, Any] | None) -> None:
    st.subheader("4 - Risk & Governor")
    if not lifecycle:
        st.caption("No evaluated proposal loaded yet.")
        return

    governor = lifecycle.get("governor_decision")
    if governor:
        decision = governor.get("decision", "UNKNOWN")
        colors = {"APPROVE": "#1a7f37", "REDUCE": "#9a6700", "REJECT": "#b42318"}
        color = colors.get(decision, "#6b7280")
        st.markdown(
            f'<div class="mz-decision" style="border-color:{color}">'
            f'<div class="d" style="color:{color}">{decision}</div>'
            f'<div class="r"><b>{governor.get("approved_quantity")}</b> of '
            f'<b>{governor.get("original_quantity")}</b> shares approved &middot; '
            f'risk score {governor.get("risk_score")} &middot; '
            f'decided {fmt_ts(governor.get("decided_at"))}'
            f'<br>{governor.get("reason", "")}</div></div>',
            unsafe_allow_html=True,
        )

    left, right = st.columns(2)
    with left:
        st.markdown("**Portfolio snapshot** (fetched by the backend from Alpaca PAPER)")
        portfolio = lifecycle.get("portfolio_snapshot")
        if portfolio:
            st.markdown(
                pill(f"source: {portfolio.get('source', '?')}", "ok"), unsafe_allow_html=True
            )
            p1, p2, p3 = st.columns(3)
            p1.metric("Equity", money(portfolio.get("equity")))
            p2.metric("Cash", money(portfolio.get("cash")))
            p3.metric("Buying power", money(portfolio.get("buying_power")))
            pnl = portfolio.get("daily_pnl_pct")
            st.caption(
                f"Daily P&L: {pnl:.2%}"
                if isinstance(pnl, (int, float))
                else "Daily P&L: unavailable (the backend fails closed on this)."
            )
            with st.expander("Positions"):
                st.json(portfolio.get("positions", []))
        else:
            st.caption("No stored portfolio snapshot.")

        st.markdown("**Market-risk snapshot**")
        market = lifecycle.get("market_risk_snapshot")
        if market:
            st.markdown(
                pill("Demo / manually supplied risk snapshot - NOT LIVE", "warn"),
                unsafe_allow_html=True,
            )
            m1, m2, m3 = st.columns(3)
            m1.metric("Ann. vol", f"{market.get('annualized_volatility', 0):.2%}")
            m2.metric("Max DD 30d", f"{market.get('max_drawdown_30d', 0):.2%}")
            m3.metric("Liquidity", f"{market.get('liquidity_score', 0):.2f}")
        else:
            st.caption("No stored market-risk snapshot.")

    with right:
        st.markdown("**Deterministic risk report**")
        risk = lifecycle.get("risk_report")
        if risk:
            st.markdown(
                pill(
                    "BLOCKED" if risk.get("blocked") else "PASSED",
                    "bad" if risk.get("blocked") else "ok",
                )
                + pill(f"score {risk.get('risk_score')}", "mut")
                + pill(f"recommended qty {risk.get('recommended_quantity')}", "mut"),
                unsafe_allow_html=True,
            )
            for check in risk.get("checks", []):
                tone = {"BLOCK": "bad", "HIGH": "bad", "WATCH": "warn", "INFO": "mut"}.get(
                    check.get("severity"), "mut"
                )
                mark = "✅" if check.get("passed") else "❌"
                st.markdown(
                    f"{mark} `{check.get('rule')}` {pill(check.get('severity', ''), tone)}<br>"
                    f"<span style='color:#4b5563;font-size:.84rem'>"
                    f"{check.get('message', '')}</span>",
                    unsafe_allow_html=True,
                )
            if risk.get("reasons"):
                st.caption("Reasons: " + "; ".join(risk["reasons"]))
        else:
            st.caption("No stored risk report.")

        st.markdown("**AI risk analysis**")
        ai = lifecycle.get("ai_risk_analysis")
        if ai:
            st.markdown(
                pill(ai.get("recommendation", "?"), "mut")
                + pill(f"confidence {ai.get('confidence', 0):.2f}", "mut")
                + pill(f"qty {ai.get('recommended_quantity')}", "mut")
                + pill(ai.get("model_name", "?"), "mut"),
                unsafe_allow_html=True,
            )
            st.markdown(f"_{ai.get('risk_thesis', '')}_")
            if ai.get("hidden_risks"):
                st.markdown("**Hidden risks:** " + "; ".join(ai["hidden_risks"]))
            with st.expander("AI reasoning"):
                for line in ai.get("reasoning", []):
                    st.markdown(f"- {line}")
        else:
            st.caption("No stored AI risk analysis.")

    with st.expander("Raw evaluation payload"):
        st.json(
            {
                key: lifecycle.get(key)
                for key in ("risk_report", "ai_risk_analysis", "governor_decision")
            }
        )


def render_execution_gate(
    client: BackendClient | None,
    health: dict[str, Any] | None,
    lifecycle: dict[str, Any] | None,
    max_age: int,
) -> None:
    st.subheader("5 - Execution gate")
    st.caption(
        "This UI never submits to Alpaca. It calls POST /proposals/{id}/execute only; the "
        "backend owns every broker mutation."
    )
    if not lifecycle or not health:
        st.caption("Evaluate a proposal first.")
        return

    governor = lifecycle.get("governor_decision") or {}
    decision = governor.get("decision")
    approved = int(governor.get("approved_quantity") or 0)
    decided_at = parse_ts(governor.get("decided_at"))
    age = (datetime.now(timezone.utc) - decided_at).total_seconds() if decided_at else None

    paper_ok = bool(health.get("paper_only"))
    enabled = bool(health.get("execution_enabled"))
    dry_run = bool(health.get("dry_run"))
    kill = bool(health.get("kill_switch"))
    decision_ok = decision in {"APPROVE", "REDUCE"} and approved > 0
    fresh_ok = age is not None and age <= max_age

    left, right = st.columns([0.55, 0.45])
    with left:
        st.markdown("**Safety checks**")
        check_row(
            "PAPER environment",
            state(paper_ok),
            "Backend reports paper_only=true." if paper_ok else "Backend is not paper-only.",
        )
        check_row(
            "Execution enabled",
            "ok" if enabled else "warn",
            "ALPACA_EXECUTION_ENABLED is true."
            if enabled
            else "Execution disabled; the backend returns EXECUTION_DISABLED.",
        )
        check_row(
            "Kill switch",
            "bad" if kill else "ok",
            "ACTIVE - the backend will refuse." if kill else "Inactive.",
        )
        check_row(
            "Dry-run",
            "warn" if dry_run else "ok",
            "ON - the backend returns WOULD_SUBMIT and places no order."
            if dry_run
            else "OFF - an accepted order becomes a real PAPER order.",
        )
        check_row(
            "Governor authorization",
            state(decision_ok),
            f"{decision or '-'} - approved quantity {approved}.",
        )
        check_row(
            "Authorization freshness",
            "ok" if fresh_ok else "bad",
            f"Decision age {age:.0f}s (UI reference limit {max_age}s; the backend enforces its "
            "own EXECUTION_MAX_DECISION_AGE_SECONDS)."
            if age is not None
            else "No Governor timestamp.",
        )
        check_row(
            "Asset validation",
            "pending",
            "The backend checks for an active, tradable us_equity at execute time.",
        )
        check_row(
            "Market clock", "pending", "The backend checks the Alpaca clock at execute time."
        )
        check_row(
            "Idempotency",
            "pending",
            "The backend derives one deterministic client_order_id per proposal.",
        )

    with right:
        st.markdown("**Order structure sent to the backend**")
        st.json(
            {
                "proposal_id": governor.get("proposal_id"),
                "symbol": governor.get("symbol"),
                "side": governor.get("side"),
                "approved_quantity": approved,
                "order_type": "market",
                "time_in_force": "day",
                "extended_hours": False,
            }
        )
        can_call = bool(client) and paper_ok and enabled and not kill and decision_ok
        if not can_call:
            st.caption("Execution call disabled: a blocking safety check above is not satisfied.")
        if st.button("Call backend execution endpoint", type="primary", disabled=not can_call):
            with st.spinner("Calling backend..."):
                try:
                    st.session_state["execution_result"] = client.execute(
                        governor.get("proposal_id")
                    )
                    st.session_state["refresh_lifecycle"] = True
                    st.rerun()
                except BackendError as exc:
                    show_error(exc, "Execution call failed")

    result = st.session_state.get("execution_result") or lifecycle.get("execution_result")
    if result:
        status = result.get("status", "UNKNOWN")
        if status in BROKER_ORDER_STATES:
            icon, headline = "\U0001F7E2", "A PAPER order exists at Alpaca."
        elif status in DRY_RUN_STATES:
            icon, headline = (
                "\U0001F7E1",
                "DRY RUN - no order was placed. Nothing was sent to Alpaca.",
            )
        else:
            icon, headline = "\U0001F534", "No order was placed."
        st.markdown("---")
        st.markdown(f"### {icon} `{status}`")
        st.markdown(f"**{headline}**")
        st.markdown(result.get("message", ""))
        e1, e2, e3, e4 = st.columns(4)
        e1.metric("Quantity", result.get("quantity", "-"))
        e2.metric("Mode", result.get("execution_mode", "-"))
        e3.metric("Broker status", result.get("broker_status") or "-")
        e4.metric(
            "Filled",
            result.get("filled_quantity") if result.get("filled_quantity") is not None else "-",
        )
        st.caption(
            f"client_order_id: `{result.get('client_order_id') or '-'}` - "
            f"alpaca_order_id: `{result.get('alpaca_order_id') or '-'}` - "
            f"submitted: {fmt_ts(result.get('submitted_at'))}"
        )
        with st.expander("Raw execution result"):
            st.json(result)

    if lifecycle.get("broker_order"):
        with st.expander("Persisted broker order snapshot"):
            st.json(lifecycle["broker_order"])


def render_audit(client: BackendClient | None, proposal_id: str | None) -> None:
    st.subheader("6 - Audit timeline")
    if not client or not proposal_id:
        st.caption("No proposal loaded.")
        return
    try:
        events = client.audit(proposal_id)
    except BackendError as exc:
        show_error(exc, "Audit fetch failed")
        return
    if not events:
        st.caption("No audit events recorded.")
        return
    st.caption(f"{len(events)} events, oldest to newest (backend ordering preserved).")
    for event in events:
        payload = event.get("payload", {})
        message = ""
        if isinstance(payload, dict):
            message = (
                payload.get("message")
                or payload.get("reason")
                or payload.get("risk_thesis")
                or ", ".join(
                    f"{key}={value}"
                    for key, value in list(payload.items())[:3]
                    if not isinstance(value, (dict, list))
                )
            )
        st.markdown(
            f'<div class="mz-ev"><span class="t">{fmt_ts(event.get("created_at"))}</span> &middot; '
            f'<span class="a">{event.get("action")}</span> '
            f'{pill(event.get("actor", "?"), "mut")}<br>'
            f'<span style="color:#4b5563;font-size:.85rem">{str(message)[:300]}</span></div>',
            unsafe_allow_html=True,
        )
        with st.expander(f"payload - {event.get('action')} - {str(event.get('event_id'))[:8]}"):
            st.json(payload)


def render_recent(client: BackendClient | None) -> None:
    st.subheader("7 - Recent runs")
    if not client:
        st.caption("Backend unavailable.")
        return
    try:
        runs = client.recent(20)
    except BackendError as exc:
        show_error(exc, "Recent runs fetch failed")
        return
    if not runs:
        st.caption("No proposals stored yet.")
        return

    rows = []
    for run in runs:
        proposal = run.get("proposal") or {}
        governor = run.get("governor_decision") or {}
        execution = run.get("execution_result") or {}
        rows.append(
            {
                "proposal_id": proposal.get("proposal_id"),
                "symbol": proposal.get("symbol"),
                "side": proposal.get("side"),
                "qty": proposal.get("quantity"),
                "governor": governor.get("decision") or "-",
                "approved": governor.get("approved_quantity"),
                "execution": execution.get("status") or "-",
                "created_at": fmt_ts(proposal.get("created_at")),
            }
        )
    st.dataframe(rows, use_container_width=True, hide_index=True)

    ids = [row["proposal_id"] for row in rows if row["proposal_id"]]
    if not ids:
        return
    left, right = st.columns([0.7, 0.3])
    selected = left.selectbox("Reload a stored proposal", ids, key="recent_select")
    if right.button("Load", use_container_width=True):
        try:
            lifecycle = client.lifecycle(selected)
            st.session_state["lifecycle"] = lifecycle
            st.session_state["proposal_id"] = selected
            st.session_state["execution_result"] = None
            st.session_state["form_defaults"] = {
                **(lifecycle.get("proposal") or {}),
                **(lifecycle.get("market_risk_snapshot") or {}),
            }
            st.rerun()
        except BackendError as exc:
            show_error(exc, "Proposal reload failed")


# --------------------------------------------------------------------------
# Main
# --------------------------------------------------------------------------
def main() -> None:
    st.set_page_config(page_title="Mizan - backup demo", layout="wide")
    st.markdown(CSS, unsafe_allow_html=True)

    # Keyed widgets keep their own values across programmatic reruns; passing a
    # `value=` default instead would silently discard an unapplied edit.
    st.session_state.setdefault("base_url", DEFAULT_BASE_URL)
    st.session_state.setdefault("timeout", DEFAULT_TIMEOUT)
    st.session_state.setdefault("max_age", 120)

    with st.sidebar:
        st.markdown("### Connection")
        base_url = st.text_input("Backend base URL", key="base_url")
        timeout = st.number_input("Request timeout (s)", 1.0, 120.0, step=1.0, key="timeout")
        max_age = st.number_input(
            "Authorization freshness reference (s)",
            1,
            3600,
            key="max_age",
            help="Display only. The backend enforces its own limit.",
        )
        st.caption("This UI reads no credentials. The backend holds all keys.")
        if st.button("Refresh backend state", use_container_width=True):
            st.session_state["refresh_lifecycle"] = True
            st.rerun()

    client = BackendClient(base_url, float(timeout))
    health: dict[str, Any] | None = None
    health_error: BackendError | None = None
    try:
        health = client.health()
    except BackendError as exc:
        health_error = exc

    render_header(health, health_error, base_url)
    if health is None:
        st.stop()
    st.divider()

    form = render_candidate_input(client)
    if form["submitted"]:
        with st.spinner("Evaluating..."):
            try:
                client.evaluate(form["proposal"], form["market_risk"])
                proposal_id = form["proposal"]["proposal_id"]
                st.session_state["proposal_id"] = proposal_id
                st.session_state["lifecycle"] = client.lifecycle(proposal_id)
                st.session_state["execution_result"] = None
                st.session_state["form_defaults"] = {
                    **form["proposal"],
                    **form["market_risk"],
                }
            except BackendError as exc:
                show_error(exc, "Evaluation failed")

    proposal_id = st.session_state.get("proposal_id")
    if st.session_state.pop("refresh_lifecycle", False) and proposal_id:
        try:
            st.session_state["lifecycle"] = client.lifecycle(proposal_id)
        except BackendError as exc:
            show_error(exc, "Lifecycle refresh failed")

    lifecycle = st.session_state.get("lifecycle")
    proposal = (lifecycle or {}).get("proposal")

    st.divider()
    render_pipeline(proposal)
    st.divider()
    render_options_section(proposal)
    st.divider()
    render_risk_and_governor(lifecycle)
    st.divider()
    render_execution_gate(client, health, lifecycle, int(max_age))
    st.divider()
    render_audit(client, proposal_id)
    st.divider()
    render_recent(client)


if __name__ == "__main__":
    main()
