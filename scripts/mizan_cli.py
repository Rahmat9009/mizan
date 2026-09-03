#!/usr/bin/env python
"""``mizan`` - the command line for the governed trading loop.

The Alpaca hackathon requires the agent loop to reach Alpaca through an MCP server or a CLI rather
than through raw ``alpaca-py``. This file is the CLI half; ``python -m mizan.mcp`` is the MCP half.

They are not two implementations. Every subcommand here calls
:meth:`mizan.mcp.server.MizanMCPServer.call_tool` - the same dispatch an MCP client reaches over
stdio - so ``mizan evaluate`` and the ``evaluate_proposal`` tool cannot drift apart, and whichever one
a judge runs exercises the other. The CLI adds argument parsing and a readable rendering; it adds no
behaviour, and in particular it adds no way around the gate.

    python scripts/mizan_cli.py doctor
    python scripts/mizan_cli.py account          --broker alpaca-mcp
    python scripts/mizan_cli.py chain SPY        --broker alpaca-mcp --limit 5
    python scripts/mizan_cli.py evaluate --symbol AAPL --strategy long_call \\
        --leg "side=buy,qty=50,limit=1.85,type=call,strike=230,expiry=2026-09-25"
    python scripts/mizan_cli.py submit   --symbol AAPL --strategy long_call --live \\
        --leg "side=buy,qty=2,limit=1.85,type=call,strike=230,expiry=2026-09-25"
    python scripts/mizan_cli.py verify-chain
    python scripts/mizan_cli.py replay <decision-id>
    python scripts/mizan_cli.py mcp-tools --server alpaca

Paper only. ``--live`` means "actually place the paper order" - it stops the execution gate returning
WOULD_SUBMIT and lets it submit. There is no flag anywhere in this file that selects a live venue.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:  # run from a checkout without installing
    sys.path.insert(0, str(REPO_ROOT))

from mizan.mcp.server import TOOLS, MizanMCPServer  # noqa: E402
from mizan.mcp.session import BROKER_CHOICES, SessionConfig  # noqa: E402

BOLD = "\033[1m"
DIM = "\033[2m"
RESET = "\033[0m"

VERDICT_MARK = {"APPROVE": "APPROVED", "REDUCE": "REDUCED", "REJECT": "REJECTED"}


# ---------------------------------------------------------------------------------------------------
# Argument parsing
# ---------------------------------------------------------------------------------------------------
def _add_global_arguments(parser: argparse.ArgumentParser, *, suppress: bool) -> None:
    """The flags that apply to every command.

    They are attached to the top-level parser AND to every subparser, because
    ``mizan submit --broker alpaca-mcp --live`` is what a person actually types, while argparse only
    accepts a top-level flag before the subcommand. The subparser copies default to ``SUPPRESS`` so
    that an unused copy leaves the top-level value alone instead of resetting it.
    """
    default = argparse.SUPPRESS if suppress else None
    flag_default = argparse.SUPPRESS if suppress else False
    parser.add_argument("--tenant", default=default, help="tenant id (default: MIZAN_TENANT_ID or tenant-a)")
    parser.add_argument("--agent", default=default, help="agent id recorded on every decision")
    parser.add_argument(
        "--policy", default=default, help="policy YAML (default: policies/options-conservative.yaml)"
    )
    parser.add_argument("--ledger", default=default, help="SQLite ledger directory (default: in memory)")
    parser.add_argument(
        "--broker",
        default=default,
        choices=list(BROKER_CHOICES),
        help=(
            "mock: credential-free fixtures. alpaca-mcp: Alpaca's OFFICIAL MCP server over stdio. "
            "alpaca-py: the direct SDK adapter, for comparison."
        ),
    )
    parser.add_argument(
        "--alpaca-mcp-cmd",
        default=default,
        help="override the command that starts Alpaca's MCP server (whitespace-separated argv)",
    )
    parser.add_argument(
        "--live",
        action="store_true",
        default=flag_default,
        help="let the execution gate actually submit to the PAPER venue (default: stop at WOULD_SUBMIT)",
    )
    parser.add_argument(
        "--json", action="store_true", default=flag_default, help="print the raw tool result as JSON"
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=argparse.SUPPRESS if suppress else 60.0,
        help="MCP request timeout in seconds",
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="mizan",
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    _add_global_arguments(parser, suppress=False)
    subparsers = parser.add_subparsers(dest="command", required=True)

    def add(name: str, help_text: str) -> argparse.ArgumentParser:
        sub = subparsers.add_parser(name, help=help_text)
        _add_global_arguments(sub, suppress=True)
        return sub

    add("doctor", "what is wired up, and what is missing, before you trust a run")
    add("account", "account permissions and portfolio, read from the broker")
    add("governance", "tenant, policy, broker and execution mode in force")
    add("verify-chain", "verify the tenant's hash chain end to end")

    chain = add("chain", "option contracts for an underlying")
    chain.add_argument("symbol")
    chain.add_argument("--limit", type=int, default=10)

    for name, help_text in (
        ("evaluate", "govern a proposal and record the decision; never executes"),
        ("submit", "govern a proposal and, if it survives, put it through the execution gate"),
    ):
        _add_proposal_arguments(add(name, help_text))

    replay = add("replay", "re-derive a recorded decision from the record alone")
    replay.add_argument("decision_id")
    replay.add_argument("--under-policy", default=None, help="replay under a DIFFERENT policy file")

    decisions = add("decisions", "recorded decisions, newest first")
    decisions.add_argument("--limit", type=int, default=20)

    show = add("decision", "one full DecisionRecord by id")
    show.add_argument("decision_id")

    tools = add("mcp-tools", "list the tools an MCP server exposes")
    tools.add_argument(
        "--server",
        choices=["mizan", "alpaca"],
        default="mizan",
        help="mizan: our governed surface. alpaca: Alpaca's official server, through the allowlist.",
    )
    tools.add_argument("--verbose", action="store_true", help="include each tool's description")

    call = add("mcp-call", "call one tool on Alpaca's official MCP server (allowlisted reads only)")
    call.add_argument("tool")
    call.add_argument("--arg", action="append", default=[], metavar="KEY=VALUE")
    call.add_argument(
        "--no-credentials",
        action="store_true",
        help=(
            "start the server with a placeholder key. Proves the transport reaches Alpaca - every "
            "account call comes back 401 - on a machine that has no credentials."
        ),
    )
    return parser


def _add_proposal_arguments(sub: argparse.ArgumentParser) -> None:
    sub.add_argument("--symbol", help="the UNDERLYING symbol, e.g. AAPL")
    sub.add_argument("--strategy", default=None, help="long_call, bull_call_spread, long_equity, ...")
    sub.add_argument("--intent", default="open", choices=["open", "close", "adjust"])
    sub.add_argument("--asset-class", default=None, choices=["equity", "equity_option"])
    sub.add_argument(
        "--leg",
        action="append",
        default=[],
        metavar="side=buy,qty=10,limit=1.85,type=call,strike=230,expiry=2026-09-25",
        help="one leg as comma-separated key=value pairs; repeat for a spread (max 4)",
    )
    sub.add_argument("--reasoning", default="", help="recorded for the audit trail; never read by the engine")
    sub.add_argument("--confidence", default=None, help="0..1 as a decimal string")
    sub.add_argument("--proposal", default=None, help="a JSON file holding the whole proposal instead")


# ---------------------------------------------------------------------------------------------------
# Proposal construction
# ---------------------------------------------------------------------------------------------------
_LEG_KEYS = {
    "side": "side",
    "qty": "quantity",
    "quantity": "quantity",
    "limit": "limit_price",
    "limit_price": "limit_price",
    "order_type": "order_type",
    "type": "contract_type",
    "contract_type": "contract_type",
    "strike": "strike",
    "expiry": "expiry",
}


def parse_leg(index: int, text: str) -> dict[str, Any]:
    """``side=buy,qty=10,limit=1.85,type=call,strike=230,expiry=2026-09-25`` into a leg object.

    ``type=call`` names the CONTRACT type, not the order type: an option leg is a call or a put, and a
    leg with no ``type`` is an equity leg. ``order_type`` stays explicit and defaults to ``limit``,
    because a market order on a spread is a different risk and should have to be typed out.
    """
    leg: dict[str, Any] = {"leg_index": index, "order_type": "limit"}
    for pair in text.split(","):
        pair = pair.strip()
        if not pair:
            continue
        if "=" not in pair:
            raise SystemExit(f"leg {index}: {pair!r} is not key=value")
        key, _, value = pair.partition("=")
        field = _LEG_KEYS.get(key.strip().casefold())
        if field is None:
            raise SystemExit(f"leg {index}: unknown key {key.strip()!r}; known: {sorted(set(_LEG_KEYS))}")
        leg[field] = value.strip()
    if "side" not in leg or "quantity" not in leg:
        raise SystemExit(f"leg {index}: side= and qty= are both required")
    if leg.get("order_type") == "limit" and not leg.get("limit_price"):
        leg["order_type"] = "market"
    return leg


def proposal_from_args(args: argparse.Namespace) -> dict[str, Any]:
    if args.proposal:
        payload = json.loads(Path(args.proposal).read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            raise SystemExit("--proposal must hold a JSON object")
        return payload
    if not args.symbol:
        raise SystemExit("--symbol is required (or pass --proposal FILE.json)")
    if not args.leg:
        raise SystemExit("at least one --leg is required")
    legs = [parse_leg(index, text) for index, text in enumerate(args.leg)]
    is_option = any(leg.get("contract_type") for leg in legs)
    asset_class = args.asset_class or ("equity_option" if is_option else "equity")
    strategy = args.strategy or _infer_strategy(legs, is_option=is_option)
    payload: dict[str, Any] = {
        "intent": args.intent,
        "symbol": args.symbol.upper(),
        "asset_class": asset_class,
        "strategy": strategy,
        "legs": legs,
        "reasoning": args.reasoning,
    }
    if args.confidence:
        payload["confidence"] = args.confidence
    return payload


def _infer_strategy(legs: list[dict[str, Any]], *, is_option: bool) -> str:
    """A single obvious case, and ``custom`` for everything else.

    Guessing a multi-leg strategy would be guessing the RISK SHAPE of the order, and the engine
    refuses an undefined-risk structure by name. ``custom`` makes the engine derive the shape from the
    legs themselves rather than believing a label the CLI invented.
    """
    if not is_option:
        return "long_equity" if legs[0].get("side") == "buy" else "short_equity"
    if len(legs) == 1:
        contract = legs[0].get("contract_type")
        if legs[0].get("side") == "buy" and contract in {"call", "put"}:
            return f"long_{contract}"
    return "custom"


# ---------------------------------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------------------------------
def render(command: str, payload: Any, *, as_json: bool) -> None:
    if as_json or not isinstance(payload, dict):
        print(json.dumps(payload, indent=2, sort_keys=True, default=str))
        return
    if "error" in payload:
        print(f"{BOLD}REFUSED{RESET}  {payload['error']}")
        for code in payload.get("reason_codes") or []:
            print(f"  reason code      {code}")
        if payload.get("detail"):
            print(f"  detail           {payload['detail']}")
        return
    renderer = {
        "governance": _render_governance,
        "doctor": _render_governance,
        "account": _render_account,
        "chain": _render_chain,
        "evaluate": _render_decision,
        "submit": _render_decision,
        "verify-chain": _render_chain_verification,
        "replay": _render_replay,
        "decisions": _render_decisions,
    }.get(command)
    if renderer is None:
        print(json.dumps(payload, indent=2, sort_keys=True, default=str))
        return
    renderer(payload)


def _render_governance(payload: dict[str, Any]) -> None:
    policy = payload.get("policy") or {}
    broker = payload.get("broker") or {}
    execution = payload.get("execution") or {}
    print(f"{BOLD}governance in force{RESET}")
    print(f"  tenant           {payload.get('tenant_id')}")
    print(f"  agent            {payload.get('agent_id')}")
    print(f"  policy           {policy.get('policy_id')} v{policy.get('version')}")
    if policy.get("error"):
        print(f"  policy ERROR     {policy['error']}")
    print(f"  broker           {broker.get('name')} ({broker.get('environment')})")
    print(f"  ledger           {payload.get('ledger')} {payload.get('ledger_dir') or ''}".rstrip())
    print(f"  execution        enabled={execution.get('enabled')} dry_run={execution.get('dry_run')}")
    for note in payload.get("notes") or []:
        print(f"  {DIM}note{RESET}             {note}")
    if payload.get("tools"):
        print(f"  tools            {', '.join(payload['tools'])}")
    if payload.get("no_tool_can"):
        print(f"  no tool can      {'; '.join(payload['no_tool_can'])}")


def _render_account(payload: dict[str, Any]) -> None:
    broker = payload.get("broker") or {}
    portfolio = payload.get("portfolio") or {}
    permissions = payload.get("permissions") or {}
    print(f"{BOLD}account{RESET}  via {broker.get('name')} ({broker.get('environment')})")
    print(f"  equity           {portfolio.get('equity')}")
    print(f"  cash             {portfolio.get('cash')}")
    print(f"  buying power     {portfolio.get('buying_power')}")
    print(f"  as of            {portfolio.get('as_of')}")
    if permissions:
        print(f"  status           {permissions.get('status')}")
        print(f"  trading blocked  {permissions.get('trading_blocked')}")
        print(f"  account blocked  {permissions.get('account_blocked')}")
        print(f"  options level    {permissions.get('options_trading_level')}")
        print(f"  shorting         {permissions.get('shorting_enabled')}")
    positions = portfolio.get("positions") or []
    print(f"  positions        {len(positions)}")
    for position in positions[:20]:
        symbol = position.get("occ_symbol") or position.get("symbol")
        print(f"    {symbol:<24} {position.get('quantity'):>10}  mv {position.get('market_value')}")
    for delta in payload.get("deltas") or []:
        print(f"  {BOLD}DELTA{RESET}            {delta}")


def _render_chain(payload: dict[str, Any]) -> None:
    print(f"{BOLD}option contracts{RESET}  {payload.get('symbol')}  ({payload.get('count', 0)})")
    if payload.get("note"):
        print(f"  note             {payload['note']}")
    for contract in payload.get("contracts") or []:
        print(
            f"  {str(contract.get('symbol')):<24} {str(contract.get('type') or ''):<5}"
            f" strike {str(contract.get('strike_price') or ''):<10}"
            f" exp {contract.get('expiration_date')}"
            f" oi {contract.get('open_interest')}"
        )


def _render_decision(payload: dict[str, Any]) -> None:
    verdict = str(payload.get("verdict"))
    print(f"{BOLD}MIZAN -> {VERDICT_MARK.get(verdict, verdict)}{RESET}")
    print(f"  decision id      {payload.get('decision_id')}")
    print(f"  requested        {payload.get('requested_quantity')}")
    print(f"  authorized       {payload.get('authorized_quantity')}")
    print(f"  reason codes     {', '.join(payload.get('reason_codes') or ['none'])}")
    for check in payload.get("failed_checks") or []:
        print(
            f"    {check['check_id']:<26} {check['severity']:<9}"
            f" actual {check['actual']} vs threshold {check['threshold']}"
        )
    for reduction in payload.get("reductions") or []:
        print(
            f"    reduced by {reduction['source']:<14} "
            f"{reduction['from_quantity']} -> {reduction['to_quantity']} ({reduction['reason_code']})"
        )
    policy = payload.get("policy") or {}
    print(f"  policy           {policy.get('policy_id')} v{policy.get('version')}")
    print(f"  checks run       {payload.get('checks_run')}")
    print(f"  verdict hash     {str(payload.get('verdict_hash'))[:32]}...")
    print(f"  audit hash       {str(payload.get('audit_hash'))[:32]}...")
    authorization = payload.get("authorization")
    if authorization:
        print(
            f"  authorization    expires {authorization['expires_at']} "
            f"({authorization['ttl_seconds']}s, {authorization['environment']})"
        )
    execution = payload.get("execution")
    if execution:
        print(f"{BOLD}  execution{RESET}")
        print(f"    status         {execution.get('status')}")
        if execution.get("why"):
            print(f"    why            {execution['why']}")
        for code in execution.get("reason_codes") or []:
            print(f"    reason code    {code}")
        if execution.get("client_order_id"):
            print(f"    client order   {execution['client_order_id']}")
        if execution.get("broker_order_id"):
            print(f"    broker order   {execution['broker_order_id']}")
        if execution.get("broker_status"):
            print(f"    broker status  {execution['broker_status']}")
        if execution.get("kill_switch_checked_at"):
            print(f"    kill switch    read at {execution['kill_switch_checked_at']}")


def _render_chain_verification(payload: dict[str, Any]) -> None:
    print(f"{BOLD}hash chain{RESET}  tenant {payload.get('tenant_id')}")
    print(f"  ok               {payload.get('ok')}")
    print(f"  length           {payload.get('length')}")
    if payload.get("first_bad_sequence") is not None:
        print(f"  FIRST BAD SEQ    {payload['first_bad_sequence']}")


def _render_replay(payload: dict[str, Any]) -> None:
    print(f"{BOLD}replay{RESET}  {payload.get('decision_id')}")
    print(f"  mode             {payload.get('mode')}")
    print(f"  identical        {payload.get('identical')}")
    print(f"  verdict          {payload.get('original_verdict')} -> {payload.get('replayed_verdict')}")
    print(f"  original hash    {payload.get('original_verdict_hash')}")
    print(f"  replayed hash    {payload.get('replayed_verdict_hash')}")
    print(f"  codes            {', '.join(payload.get('replayed_reason_codes') or ['none'])}")
    print(f"  engine matches   {payload.get('engine_version_matches')}")
    if payload.get("detail"):
        print(f"  detail           {payload['detail']}")


def _render_decisions(payload: dict[str, Any]) -> None:
    print(f"{BOLD}decisions{RESET}  ({payload.get('count', 0)})")
    for record in payload.get("decisions") or []:
        codes = ", ".join(record.get("reason_codes") or []) or "-"
        print(
            f"  seq {record['sequence']:<4} {record['verdict']:<8} {record['symbol']:<8}"
            f" {record['decision_id']}  {codes}"
        )


# ---------------------------------------------------------------------------------------------------
# Commands that talk to Alpaca's server rather than to ours
# ---------------------------------------------------------------------------------------------------
def alpaca_mcp_tools(verbose: bool, timeout: float, command: list[str] | None = None) -> int:
    """Start Alpaca's official server and print what it offers, marked against Mizan's allowlist.

    Deliberately shows the DENIED tools too. The interesting fact about this integration is not that
    Alpaca's server has 72 tools; it is that seven of them can liquidate an account and that Mizan's
    client cannot send any of them.
    """
    from mizan.mcp.alpaca import (
        ALLOWED_TOOLS,
        FORBIDDEN_TOOLS,
        alpaca_mcp_environment,
        resolve_alpaca_mcp_command,
    )
    from mizan.mcp.client import StdioMCPClient

    argv = resolve_alpaca_mcp_command(command)
    print(f"{BOLD}alpaca official MCP server{RESET}")
    print(f"  command          {Path(argv[0]).name} {' '.join(argv[1:])}")
    # No credential is required to ask a server what it can DO. Every call then fails 401 at Alpaca,
    # which is the right answer to asking about an account with no key.
    with StdioMCPClient(
        argv,
        env=alpaca_mcp_environment(require_credentials=False),
        allowed_tools=ALLOWED_TOOLS,
        timeout=timeout,
    ) as client:
        tools = client.list_tools()
        print(f"  server           {client.server_info.get('name')} {client.server_info.get('version')}")
        print(f"  protocol         {client.negotiated_version}")
        print("  base url         https://paper-api.alpaca.markets (ALPACA_PAPER_TRADE forced true)")
        print(f"  tools offered    {len(tools)}")
        allowed = [t for t in tools if t["name"] in ALLOWED_TOOLS]
        denied = [t for t in tools if t["name"] in FORBIDDEN_TOOLS]
        print(f"  mizan allows     {len(allowed)}")
        for tool in sorted(allowed, key=lambda t: t["name"]):
            summary = (tool.get("description") or "").splitlines()[0][:70]
            suffix = f"  {DIM}{summary}{RESET}" if verbose else ""
            print(f"    ALLOW  {tool['name']}{suffix}")
        print(f"  mizan denies     {len(denied)} (Hard Rule B4: no cancel, replace or close)")
        for tool in sorted(denied, key=lambda t: t["name"]):
            print(f"    DENY   {tool['name']}")
        unreachable = len(tools) - len(allowed)
        print(f"  unreachable      {unreachable} of {len(tools)} tools are off this client's allowlist")
    return 0


def alpaca_mcp_call(
    tool: str,
    pairs: list[str],
    timeout: float,
    as_json: bool,
    *,
    unauthenticated: bool = False,
    command: list[str] | None = None,
) -> int:
    """One raw tool call against Alpaca's server, through the allowlist. The proof of the transport."""
    from mizan.mcp.alpaca import ALLOWED_TOOLS, alpaca_mcp_environment, resolve_alpaca_mcp_command
    from mizan.mcp.client import MCPToolDenied, StdioMCPClient

    arguments: dict[str, Any] = {}
    for pair in pairs:
        key, _, value = pair.partition("=")
        if not key or not _:
            raise SystemExit(f"--arg {pair!r} is not KEY=VALUE")
        arguments[key.strip()] = _coerce(value.strip())
    if unauthenticated:
        print(f"{BOLD}UNAUTHENTICATED{RESET}  placeholder key; a 401 here proves the request reached Alpaca")
    with StdioMCPClient(
        resolve_alpaca_mcp_command(command),
        env=alpaca_mcp_environment(require_credentials=not unauthenticated),
        allowed_tools=ALLOWED_TOOLS,
        timeout=timeout,
    ) as client:
        try:
            result = client.call_tool(tool, arguments)
        except MCPToolDenied as denied:
            print(f"{BOLD}DENIED{RESET}  {denied}")
            return 2
        payload = result.json()
        if as_json or not isinstance(payload, str):
            print(json.dumps(payload, indent=2, sort_keys=True, default=str))
        else:
            print(payload)
        return 1 if result.is_error else 0


def _kill_switch_set() -> bool:
    import os

    return (os.getenv("MIZAN_KILL_SWITCH") or "").strip().casefold() in {"1", "true", "yes", "on"}


def _coerce(value: str) -> Any:
    lowered = value.casefold()
    if lowered in {"true", "false"}:
        return lowered == "true"
    if value.lstrip("-").isdigit():
        return int(value)
    return value


def doctor(config: SessionConfig, timeout: float) -> dict[str, Any]:
    """What is wired up and what is missing, without touching the network unless asked to.

    A demo that fails at the venue because a variable was unset should say so in one line, here,
    rather than in a stack trace half way through a decision.
    """
    import os
    import shutil

    from mizan.mcp.alpaca import ALLOWED_TOOLS, FORBIDDEN_TOOLS

    policy: dict[str, Any] = {"path": str(config.policy_path), "readable": config.policy_path.is_file()}
    if policy["readable"]:
        try:
            from mizan.policy import load_policy

            loaded = load_policy(config.policy_path.read_text(encoding="utf-8"))
            policy |= {"policy_id": loaded.policy_id, "version": loaded.policy_version}
        except Exception as failure:  # noqa: BLE001 - doctor reports, it does not fail
            policy["error"] = f"{type(failure).__name__}: {failure}"

    has_key = bool(os.getenv("ALPACA_API_KEY") or os.getenv("APCA_API_KEY_ID"))
    has_secret = bool(os.getenv("ALPACA_SECRET_KEY") or os.getenv("APCA_API_SECRET_KEY"))
    paper = (os.getenv("ALPACA_PAPER") or "").strip().casefold() in {"1", "true", "yes", "on"}
    return {
        "tenant_id": config.tenant_id,
        "agent_id": config.agent_id,
        "policy": policy,
        "broker": {"name": config.broker, "environment": "paper"},
        "ledger": "sqlite" if config.ledger_dir else "memory",
        "ledger_dir": str(config.ledger_dir) if config.ledger_dir else None,
        "execution": {"enabled": config.execution_enabled, "dry_run": config.dry_run},
        "tools": [tool["name"] for tool in TOOLS],
        "no_tool_can": ["cancel an order", "replace an order", "close a position"],
        "notes": [
            f"uvx: {'found' if shutil.which('uvx') else 'MISSING - alpaca-mcp cannot start'}",
            f"ALPACA_API_KEY: {'set' if has_key else 'MISSING'}",
            f"ALPACA_SECRET_KEY: {'set' if has_secret else 'MISSING'}",
            f"ALPACA_PAPER=true: {'yes' if paper else 'NO - the alpaca brokers will refuse to start'}",
            f"alpaca MCP tools allowed: {len(ALLOWED_TOOLS)}, forbidden: {len(FORBIDDEN_TOOLS)}",
            f"mcp request timeout: {timeout}s",
            "MIZAN_KILL_SWITCH: "
            + ("SET - this session refuses at the mutation boundary" if _kill_switch_set() else "not set"),
            "policy and broker are explicit flags only; neither is read from the environment",
        ],
    }


# ---------------------------------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------------------------------
def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    override = args.alpaca_mcp_cmd.split() if args.alpaca_mcp_cmd else None
    if args.command == "mcp-tools" and args.server == "alpaca":
        return alpaca_mcp_tools(args.verbose, args.timeout, override)
    if args.command == "mcp-call":
        return alpaca_mcp_call(
            args.tool,
            args.arg,
            args.timeout,
            args.json,
            unauthenticated=args.no_credentials,
            command=override,
        )
    if args.command == "mcp-tools":
        for tool in TOOLS:
            print(f"{tool['name']:24} {tool['description'] if args.verbose else tool['description'][:78]}")
        return 0

    # Every setting is an explicit flag. The policy file decides WHICH CHECKS RUN and the broker
    # decides which venue is reached, so neither is inherited from the environment: an inherited
    # value would not appear in the command anyone typed. See mizan/mcp/session.py.
    config = SessionConfig.resolve(
        tenant_id=args.tenant,
        agent_id=args.agent,
        policy_path=args.policy,
        ledger_dir=args.ledger,
        broker=args.broker,
        dry_run=not args.live,
        timeout=args.timeout,
        alpaca_mcp_command=args.alpaca_mcp_cmd.split() if args.alpaca_mcp_cmd else None,
    )
    if args.command == "doctor":
        render("doctor", doctor(config, args.timeout), as_json=args.json)
        return 0

    tool, arguments = _tool_for(args)
    server = MizanMCPServer(config)
    try:
        result = server.call_tool(tool, arguments)
    finally:
        server.close()
    payload = json.loads(result["content"][0]["text"])
    render(args.command, payload, as_json=args.json)
    if result.get("isError"):
        return 1
    if args.command in {"evaluate", "submit"} and payload.get("verdict") == "REJECT":
        return 3  # a refusal is not a crash, and it is not a success either
    if args.command == "verify-chain" and not payload.get("ok"):
        return 1
    if args.command == "replay" and payload.get("mode") == "exact" and not payload.get("identical"):
        # A differing verdict under a DIFFERENT policy is the answer to the question that was asked,
        # not a failure. Only an exact replay that fails to reproduce is a broken determinism claim.
        return 1
    return 0


def _tool_for(args: argparse.Namespace) -> tuple[str, dict[str, Any]]:
    if args.command == "governance":
        return "describe_governance", {}
    if args.command == "account":
        return "get_account", {}
    if args.command == "chain":
        return "get_option_chain", {"symbol": args.symbol, "limit": args.limit}
    if args.command == "evaluate":
        return "evaluate_proposal", proposal_from_args(args)
    if args.command == "submit":
        return "submit_governed_order", proposal_from_args(args)
    if args.command == "verify-chain":
        return "verify_chain", {}
    if args.command == "replay":
        arguments: dict[str, Any] = {"decision_id": args.decision_id}
        if args.under_policy:
            arguments["policy_path"] = args.under_policy
        return "replay_decision", arguments
    if args.command == "decisions":
        return "list_decisions", {"limit": args.limit}
    if args.command == "decision":
        return "get_decision", {"decision_id": args.decision_id}
    raise SystemExit(f"unknown command {args.command!r}")


if __name__ == "__main__":
    raise SystemExit(main())
