"""W8 — putting a TradingAgents-style agent behind the gate in ten lines.

Run it:  python examples/tradingagents_ten_lines.py

Everything above the banner is the deployment's own setup - its broker, its ledger, its policy, its
agent - and everything between the banners is the integration. The framework keeps its own control
flow; the only change is that what it decides is now governed, recorded and replayable before anything
can act on it.

Paper trading only. This example never opens a network connection: it runs against ``MockBroker``. A
real deployment passes ``AlpacaPaperBroker.from_environment()`` instead, and nothing else changes.

This is a governance demonstration, not investment advice, and it makes no claim about outcomes.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from _setup import AGENT, MODEL, clock, demo_broker, rule  # noqa: I001  (path bootstrap first)

from mizan.audit import InMemoryLedger
from mizan.execution import ExecutionConfig
from mizan.policy import load_policy

# ==============================================================================================
# Setup the deployment already has: a policy, a broker, a ledger, and the agent itself.
# ==============================================================================================
POLICY_YAML = """
schema_version: "1.0.0"
policy_id: acme-equity
policy_version: "1.0.0"
tenant_id: acme
order: {max_notional: "10000", max_quantity: "50", max_legs: 1}
portfolio:
  max_single_symbol_pct: "0.25"
  max_sector_concentration_pct: "0.40"
  max_drawdown_pct: "0.20"
  max_buying_power_utilization: "0.80"
options: null
restricted: {symbols: [], strategies: []}
checks: {}
advisory: {enabled: false, profile: "offline", authority_ceiling: "reduce_or_reject"}
authorization: {ttl_seconds: 15}
fail_closed: {on_advisory_unavailable: false}
"""


@dataclass
class TradeDecision:
    """The shape a TradingAgents run hands back. Mizan needs no import of the framework itself."""

    action: str
    ticker: str
    quantity: int
    reasoning: str = ""
    limit_price: str | None = None
    signal_sources: list[str] = field(default_factory=list)


class TradingAgentsRun:
    """Stands in for the framework. Any object with a ``propose`` returning the shape above works."""

    def propose(self, ticker: str) -> TradeDecision:
        return TradeDecision(
            action="BUY",
            ticker=ticker,
            quantity=40,
            limit_price="228.50",
            reasoning="Bull researcher and trader agreed; risk manager flagged elevated implied vol.",
            signal_sources=["vendor:polygon", "news:reuters"],
        )


def main() -> None:
    broker, ledger, agents = demo_broker(), InMemoryLedger(), TradingAgentsRun()
    policy = load_policy(POLICY_YAML)
    config = ExecutionConfig(enabled=True, dry_run=True)

    rule("TEN LINES OF INTEGRATION")
    # ==========================================================================================
    from mizan.adapters.tradingagents import TradingAgentsAdapter   # 1  # noqa: I001
    from mizan.sdk import Mizan                                                               # 2

    mizan = Mizan(tenant_id="acme", agent=AGENT, policy=policy, broker=broker,                # 3
                  ledger=ledger, config=config, clock=clock)                                  # 4
    guard = TradingAgentsAdapter(mizan, model=MODEL)                                          # 5
    decide = guard.wrap(agents.propose)                                                       # 6
    record = decide(ticker="AAPL")                                                            # 7
    if record is not None and record.verdict != "REJECT":                                     # 8
        guard.execute(record)                                                                 # 9
    print(f"{record.verdict}   authorized {record.authorized.total_quantity} of "             # 10
          f"{record.original.total_quantity} {record.proposal.symbol}")
    # ==========================================================================================

    rule("WHAT MIZAN RECORDED")
    print(f"  agent             {record.agent_id} ({record.proposal.agent.framework})")
    print(f"  model             {record.proposal.model.provider}/{record.proposal.model.model}")
    print(f"  policy            {record.policy.policy_id} v{record.policy.version}")
    print(f"  reason codes      {[str(code.value) for code in record.reason_codes] or ['none']}")
    print(f"  decision hash     {record.governor_decision.verdict_hash[:32]}...")
    print(f"  audit hash        {record.audit_hash[:32]}...")
    print(f"  authorization     expires {record.authorization.expires_at} (paper)")
    print(f"  chain verified    {mizan.verify_chain().ok}")
    print(f"  replay identical  {mizan.replay(record.decision_id).identical}")
    print(f"  orders submitted  {len(broker.submitted)}  (dry run: the gate stopped before the venue)")


if __name__ == "__main__":
    main()
