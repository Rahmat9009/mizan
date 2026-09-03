"""Mizan's OWN MCP server: the governance layer *is* the tool surface an agent is given.

The usual shape of an MCP trading integration is agent -> broker MCP server -> venue. Every control
then lives inside the agent's prompt, which is the one place an attacker can write to. This server
inverts that:

    agent  ->  MIZAN over MCP  ->  Alpaca (over Alpaca's own MCP server)  ->  paper venue

The agent is handed no tool that reaches a venue. ``submit_governed_order`` runs the deterministic
risk engine, the governor, the authorization mint and the execution gate, appends a hash-chained
DecisionRecord, and only then - if every check passed - lets an order through. A refusal comes back as
machine-readable reason codes, so the agent can revise rather than guess.

Two boundaries are visible in the *schemas* rather than merely enforced in the code, because a
capability an agent cannot describe is one it cannot ask for:

* **no tool accepts market data, a price, a portfolio or an account balance.** The engine reads those
  from the broker (Hard Rules F-1/F-2). An agent cannot hand in the numbers its order is judged
  against, and there is no field in which to try;
* **no tool cancels, replaces or closes anything** (Hard Rule B4), and none names an environment.
  Paper is proven from the environment before a socket is opened, never selected by an argument.

Transport is newline-delimited JSON-RPC 2.0 on stdio, implemented in the standard library - see
``mizan.mcp.client`` for why the reference SDK is not a dependency of this package.
"""

from __future__ import annotations

import json
import sys
from collections.abc import Mapping
from datetime import timedelta
from typing import IO, Any

from mizan.contracts import TradeProposal, format_ts
from mizan.contracts.errors import MizanError
from mizan.mcp.client import PROTOCOL_VERSION
from mizan.mcp.session import DEFAULT_AGENT, MizanSession, SessionConfig, build_session

__all__ = ["SERVER_INFO", "TOOLS", "MizanMCPServer", "main", "serve_stdio"]

SERVER_INFO = {"name": "mizan-governance", "version": "0.1.0"}

#: The model identity recorded for a proposal that arrived over MCP. The caller may override the
#: provider/model, but never the fact that it is recorded: provenance is not optional (Master Plan A3).
DEFAULT_MODEL = {
    "provider": "mcp",
    "model": "unspecified",
    "version": "0",
    "prompt_hash": "0" * 64,
}

#: How long an unspecified proposal stays live. Short on purpose: a stale intent is not an intent.
DEFAULT_TTL_SECONDS = 300

_LEG_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["side", "quantity"],
    "properties": {
        "leg_index": {"type": "integer", "minimum": 0, "description": "Position in the structure."},
        "side": {"type": "string", "enum": ["buy", "sell"]},
        "quantity": {
            "type": "string",
            "description": "Shares, or option CONTRACTS. A decimal string; never a float.",
        },
        "order_type": {"type": "string", "enum": ["limit", "market"], "default": "limit"},
        "limit_price": {
            "type": "string",
            "description": (
                "The price you are willing to pay PER SHARE or PER CONTRACT. This is your order's "
                "own limit; it is never used to value the position. The mark comes from the broker."
            ),
        },
        "contract_type": {"type": ["string", "null"], "enum": ["call", "put", None]},
        "strike": {"type": ["string", "null"], "description": "Option strike, decimal string."},
        "expiry": {"type": ["string", "null"], "description": "Option expiry, YYYY-MM-DD."},
    },
}

_PROPOSAL_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["symbol", "asset_class", "strategy", "legs"],
    "properties": {
        "intent": {"type": "string", "enum": ["open", "close", "adjust"], "default": "open"},
        "symbol": {"type": "string", "description": "The UNDERLYING symbol, e.g. AAPL."},
        "asset_class": {"type": "string", "enum": ["equity", "equity_option"]},
        "strategy": {
            "type": "string",
            "enum": [
                "long_equity",
                "short_equity",
                "long_call",
                "long_put",
                "bull_call_spread",
                "bear_put_spread",
                "bull_put_spread",
                "bear_call_spread",
                "iron_condor",
                "custom",
            ],
        },
        "legs": {"type": "array", "minItems": 1, "maxItems": 4, "items": _LEG_SCHEMA},
        "reasoning": {
            "type": "string",
            "description": (
                "Free text, recorded for the audit trail. It is NEVER read by the risk engine: no "
                "wording here can change a verdict, which is what makes prompt injection inert."
            ),
        },
        "confidence": {"type": ["string", "null"], "description": "0..1 as a decimal string."},
        "ttl_seconds": {"type": "integer", "minimum": 1, "maximum": 3600},
        "model": {
            "type": "object",
            "additionalProperties": False,
            "description": "Which model produced this proposal. Recorded in the decision, for provenance.",
            "properties": {
                "provider": {"type": "string"},
                "model": {"type": "string"},
                "version": {"type": "string"},
                "prompt_hash": {"type": "string"},
            },
        },
        "market_snapshot_ref": {
            "type": "string",
            "description": (
                "A LABEL for what you looked at, recorded as provenance. It is not data: the engine "
                "prices your order from the broker's own quotes, never from anything you send."
            ),
        },
        "portfolio_snapshot_ref": {"type": "string", "description": "A provenance label, as above."},
    },
}

TOOLS: list[dict[str, Any]] = [
    {
        "name": "describe_governance",
        "description": (
            "What is in force right now: tenant, agent, policy id and version, which broker and "
            "transport, whether execution is live or dry-run, and the ledger in use. Call this first."
        ),
        "inputSchema": {"type": "object", "additionalProperties": False, "properties": {}},
    },
    {
        "name": "get_account",
        "description": (
            "The account as the risk engine sees it: permissions (blocked flags, options level) and "
            "the portfolio (equity, cash, buying power, positions). Read from the broker, never cached."
        ),
        "inputSchema": {"type": "object", "additionalProperties": False, "properties": {}},
    },
    {
        "name": "get_option_chain",
        "description": (
            "Tradable option contracts for an underlying, read from the broker. Research only: "
            "nothing here authorizes anything."
        ),
        "inputSchema": {
            "type": "object",
            "additionalProperties": False,
            "required": ["symbol"],
            "properties": {
                "symbol": {"type": "string", "description": "Underlying symbol, e.g. SPY."},
                "limit": {"type": "integer", "minimum": 1, "maximum": 500, "default": 25},
            },
        },
    },
    {
        "name": "evaluate_proposal",
        "description": (
            "Govern a trade proposal and record the decision. NEVER executes. Returns the verdict "
            "(APPROVE / REDUCE / REJECT), the machine-readable reason codes, every check that ran "
            "with its actual value against its threshold, the authorized quantity, and the decision "
            "id. A REJECT tells you exactly which limit you crossed, so you can revise and re-ask."
        ),
        "inputSchema": _PROPOSAL_SCHEMA,
    },
    {
        "name": "submit_governed_order",
        "description": (
            "Evaluate a proposal AND, only if it survives every check, put the authorized order "
            "through the execution gate to the broker. This is the only tool that can reach a venue, "
            "and it cannot reach one without an authorization it minted itself moments earlier. "
            "The order that is submitted is the AUTHORIZED order: if the governor cut the size, the "
            "reduced size is what goes to the broker."
        ),
        "inputSchema": _PROPOSAL_SCHEMA,
    },
    {
        "name": "verify_chain",
        "description": (
            "Verify the tenant's hash chain end to end. Every decision is linked to the one before "
            "it, so a record cannot be altered or removed after the fact without this failing and "
            "naming the first bad sequence number."
        ),
        "inputSchema": {"type": "object", "additionalProperties": False, "properties": {}},
    },
    {
        "name": "replay_decision",
        "description": (
            "Re-derive a recorded decision from the record alone and compare it, bit for bit, with "
            "what was decided at the time. Optionally replay it under a DIFFERENT policy file to ask "
            "what today's rules would have said about yesterday's trade."
        ),
        "inputSchema": {
            "type": "object",
            "additionalProperties": False,
            "required": ["decision_id"],
            "properties": {
                "decision_id": {"type": "string"},
                "policy_path": {
                    "type": "string",
                    "description": "Replay under this policy file instead of the recorded one.",
                },
            },
        },
    },
    {
        "name": "list_decisions",
        "description": "Recorded decisions for this tenant, newest first, with verdict and reason codes.",
        "inputSchema": {
            "type": "object",
            "additionalProperties": False,
            "properties": {"limit": {"type": "integer", "minimum": 1, "maximum": 200, "default": 20}},
        },
    },
    {
        "name": "get_decision",
        "description": "One full DecisionRecord by id, including its audit hashes and policy snapshot.",
        "inputSchema": {
            "type": "object",
            "additionalProperties": False,
            "required": ["decision_id"],
            "properties": {"decision_id": {"type": "string"}},
        },
    },
]


class MizanMCPServer:
    """Dispatch for the tools above, over one :class:`~mizan.mcp.session.MizanSession`.

    The session is built lazily, on the first tool call rather than at construction, so a client can
    complete the MCP handshake and read ``tools/list`` even when no broker is reachable - an agent
    that cannot see the governance surface cannot be told why it is refused.
    """

    def __init__(self, config: SessionConfig | None = None) -> None:
        self._config = config or SessionConfig()
        self._session: MizanSession | None = None
        self._initialized = False

    # -- lifecycle ---------------------------------------------------------------------------------
    @property
    def session(self) -> MizanSession:
        if self._session is None:
            self._session = build_session(self._config)
        return self._session

    def close(self) -> None:
        if self._session is not None:
            self._session.close()
            self._session = None

    # -- JSON-RPC ----------------------------------------------------------------------------------
    def handle(self, message: Mapping[str, Any]) -> dict[str, Any] | None:
        """One message in, at most one message out. ``None`` means "a notification; say nothing"."""
        method = message.get("method")
        request_id = message.get("id")
        params = message.get("params") or {}
        if not isinstance(params, Mapping):
            params = {}

        if method == "initialize":
            self._initialized = True
            return _result(
                request_id,
                {
                    "protocolVersion": PROTOCOL_VERSION,
                    "capabilities": {"tools": {"listChanged": False}},
                    "serverInfo": dict(SERVER_INFO),
                    "instructions": (
                        "Every order must go through submit_governed_order. There is no tool that "
                        "reaches a broker directly, and no tool that cancels, replaces or closes a "
                        "position. Prices and balances are read from the broker; nothing you send is "
                        "used to value your own order."
                    ),
                },
            )
        if method in {"notifications/initialized", "notifications/cancelled"}:
            return None
        if request_id is None:
            return None  # any other notification: acknowledge by silence, per JSON-RPC
        if method == "ping":
            return _result(request_id, {})
        if method == "tools/list":
            return _result(request_id, {"tools": TOOLS})
        if method == "tools/call":
            name = str(params.get("name") or "")
            arguments = params.get("arguments") or {}
            if not isinstance(arguments, Mapping):
                arguments = {}
            return _result(request_id, self.call_tool(name, arguments))
        return _error(request_id, -32601, f"unknown method {method!r}")

    def call_tool(self, name: str, arguments: Mapping[str, Any]) -> dict[str, Any]:
        """Run a tool and wrap it in an MCP tool result.

        A refusal by the engine is an in-band ``isError`` result carrying its reason codes, not a
        JSON-RPC error: "your order broke the delta limit" is an ANSWER, and an agent that receives it
        as a transport failure learns nothing it can act on.
        """
        handler = getattr(self, f"_tool_{name.replace('-', '_')}", None)
        if handler is None:
            return _tool_error(f"unknown tool {name!r}")
        try:
            return _tool_ok(handler(dict(arguments)))
        except MizanError as failure:
            return _tool_error(
                str(getattr(failure, "message", None) or failure),
                reason_codes=[str(getattr(c, "value", c)) for c in getattr(failure, "reason_codes", [])],
                detail=getattr(failure, "detail", None),
            )
        except Exception as failure:  # noqa: BLE001 - a tool must never take the server down
            return _tool_error(f"{type(failure).__name__}: {failure}")

    # -- tools -------------------------------------------------------------------------------------
    def _tool_describe_governance(self, _: Mapping[str, Any]) -> dict[str, Any]:
        described = self.session.describe()
        described["tools"] = [tool["name"] for tool in TOOLS]
        described["mutating_tools"] = ["submit_governed_order"]
        described["no_tool_can"] = ["cancel an order", "replace an order", "close a position"]
        return described

    def _tool_get_account(self, _: Mapping[str, Any]) -> dict[str, Any]:
        session = self.session
        mizan = session.mizan
        broker = mizan.broker
        if broker is None:  # pragma: no cover - build_session always supplies one
            raise RuntimeError("no broker is configured")
        now = mizan.now()
        portfolio = broker.get_portfolio_snapshot(as_of=now)
        payload: dict[str, Any] = {
            "broker": {"name": session.broker_name, "environment": session.broker_environment},
            "portfolio": portfolio.model_dump(mode="json"),
        }
        account_state = getattr(broker, "get_account_state", None)
        if callable(account_state):
            payload["permissions"] = account_state(as_of=now).model_dump(mode="json")
        deltas = list(getattr(broker, "deltas", []) or [])
        if deltas:
            payload["deltas"] = deltas
        return payload

    def _tool_get_option_chain(self, arguments: Mapping[str, Any]) -> dict[str, Any]:
        broker = self.session.mizan.broker
        symbol = str(arguments.get("symbol") or "").strip().upper()
        if not symbol:
            raise ValueError("symbol is required")
        reader = getattr(broker, "get_option_chain", None)
        if not callable(reader):
            return {
                "symbol": symbol,
                "contracts": [],
                "note": (
                    f"the {self.session.broker_name} broker exposes no option chain; "
                    "run with --broker alpaca-mcp for live contracts"
                ),
            }
        limit = int(arguments.get("limit") or 25)
        contracts = reader(symbol, limit=limit)
        return {"symbol": symbol, "count": len(contracts), "contracts": contracts}

    def _tool_evaluate_proposal(self, arguments: Mapping[str, Any]) -> dict[str, Any]:
        record = self.session.mizan.evaluate(self._proposal(arguments))
        return decision_summary(record)

    def _tool_submit_governed_order(self, arguments: Mapping[str, Any]) -> dict[str, Any]:
        mizan = self.session.mizan
        record = mizan.evaluate(self._proposal(arguments))
        summary = decision_summary(record)
        if record.verdict == "REJECT" or record.authorization is None:
            summary["submitted"] = False
            summary["execution"] = {
                "status": "NOT_ATTEMPTED",
                "why": "the proposal was refused by policy; nothing was sent to the broker",
            }
            return summary
        result = mizan.execute(record.decision_id)
        summary["submitted"] = result.status == "SUBMITTED"
        summary["execution"] = {
            "status": result.status,
            "reason_codes": [str(getattr(code, "value", code)) for code in result.reason_codes],
            "client_order_id": result.client_order_id,
            "broker_order_id": result.broker_order_id,
            "broker": result.broker.model_dump(mode="json") if result.broker else None,
            "kill_switch_checked_at": result.kill_switch_checked_at,
            "submitted_at": result.submitted_at,
            "broker_status": result.broker_status,
            "message": result.message,
        }
        return summary

    def _tool_verify_chain(self, _: Mapping[str, Any]) -> dict[str, Any]:
        verification = self.session.mizan.verify_chain()
        return {
            "ok": verification.ok,
            "length": verification.length,
            "first_bad_sequence": verification.first_bad_sequence,
            "tenant_id": self.session.config.tenant_id,
        }

    def _tool_replay_decision(self, arguments: Mapping[str, Any]) -> dict[str, Any]:
        decision_id = str(arguments.get("decision_id") or "")
        kwargs: dict[str, Any] = {}
        policy_path = arguments.get("policy_path")
        if policy_path:
            from pathlib import Path

            from mizan.policy import load_policy

            kwargs["policy"] = load_policy(Path(str(policy_path)).read_text(encoding="utf-8"))
        replayed = self.session.mizan.replay(decision_id, **kwargs)
        return {
            "decision_id": replayed.decision_id,
            "mode": replayed.mode,
            "identical": replayed.identical,
            "original_verdict": replayed.original_verdict,
            "replayed_verdict": replayed.replayed_verdict,
            "original_reason_codes": _codes(replayed.original_reason_codes),
            "replayed_reason_codes": _codes(replayed.replayed_reason_codes),
            "original_verdict_hash": replayed.original_verdict_hash,
            "replayed_verdict_hash": replayed.replayed_verdict_hash,
            "engine_version_matches": replayed.engine_version_matches,
            "detail": replayed.detail,
        }

    def _tool_list_decisions(self, arguments: Mapping[str, Any]) -> dict[str, Any]:
        limit = int(arguments.get("limit") or 20)
        records = self.session.mizan.list_decisions(limit=limit)
        return {
            "count": len(records),
            "decisions": [
                {
                    "sequence": record.sequence,
                    "decision_id": record.decision_id,
                    "recorded_at": record.recorded_at,
                    "verdict": record.verdict,
                    "symbol": record.proposal.symbol,
                    "strategy": record.proposal.strategy,
                    "reason_codes": _codes(record.reason_codes),
                    "audit_hash": record.audit_hash,
                }
                for record in records
            ],
        }

    def _tool_get_decision(self, arguments: Mapping[str, Any]) -> dict[str, Any]:
        record = self.session.mizan.get_decision(str(arguments.get("decision_id") or ""))
        return record.model_dump(mode="json")

    # -- proposal construction ---------------------------------------------------------------------
    def _proposal(self, arguments: Mapping[str, Any]) -> TradeProposal:
        """Build a TradeProposal from what the agent sent, and from what it is not allowed to send.

        The agent identity and the clock come from the SESSION, never from the payload: a proposal
        claiming another agent would otherwise be governed under that agent's budget. ``Mizan.evaluate``
        refuses an impersonation as well, so this is the second of two independent checks, not the only
        one.
        """
        session = self.session
        now = session.mizan.now()
        ttl = int(arguments.get("ttl_seconds") or DEFAULT_TTL_SECONDS)
        legs = []
        for index, raw in enumerate(arguments.get("legs") or []):
            if not isinstance(raw, Mapping):
                raise ValueError(f"leg {index} is not an object")
            legs.append(
                {
                    "leg_index": int(raw.get("leg_index", index)),
                    "side": raw.get("side"),
                    "quantity": _as_decimal_text(raw.get("quantity")),
                    "order_type": raw.get("order_type") or "limit",
                    "limit_price": _as_decimal_text(raw.get("limit_price")),
                    "contract_type": raw.get("contract_type"),
                    "strike": _as_decimal_text(raw.get("strike")),
                    "expiry": raw.get("expiry"),
                }
            )
        model = dict(DEFAULT_MODEL)
        supplied = arguments.get("model")
        if isinstance(supplied, Mapping):
            model.update({k: str(v) for k, v in supplied.items() if v is not None})
        return TradeProposal.build(
            # From the SESSION, never from the payload. There is no key an agent could send to
            # change it, and Mizan.evaluate refuses an impersonation independently.
            agent=session.mizan.agent,
            model=model,
            created_at=format_ts(now),
            expires_at=format_ts(now + timedelta(seconds=ttl)),
            intent=arguments.get("intent") or "open",
            symbol=str(arguments.get("symbol") or "").strip().upper(),
            asset_class=arguments.get("asset_class"),
            strategy=arguments.get("strategy"),
            legs=legs,
            reasoning=str(arguments.get("reasoning") or ""),
            confidence=_as_decimal_text(arguments.get("confidence")),
            market_snapshot_ref=str(arguments.get("market_snapshot_ref") or "mcp:agent-declared"),
            portfolio_snapshot_ref=str(arguments.get("portfolio_snapshot_ref") or "mcp:agent-declared"),
        )


# ---------------------------------------------------------------------------------------------------
# Result shaping
# ---------------------------------------------------------------------------------------------------
def decision_summary(record: Any) -> dict[str, Any]:
    """What an agent needs to understand a verdict and revise: the codes, and the check that bound it."""
    return {
        "decision_id": record.decision_id,
        "sequence": record.sequence,
        "verdict": record.verdict,
        "reason_codes": _codes(record.reason_codes),
        "tenant_id": record.tenant_id,
        "agent_id": record.agent_id,
        "proposal_id": record.proposal_id,
        "symbol": record.proposal.symbol,
        "strategy": record.proposal.strategy,
        "requested_quantity": record.original.total_quantity,
        "authorized_quantity": record.authorized.total_quantity,
        "authorized_legs": [leg.model_dump(mode="json") for leg in record.authorized.legs],
        "reductions": [r.model_dump(mode="json") for r in record.authorized.reductions],
        "policy": {"policy_id": record.policy.policy_id, "version": record.policy.version},
        "engine_version": record.engine_version,
        "decision_timestamp": record.decision_timestamp,
        "verdict_hash": record.governor_decision.verdict_hash,
        "audit_hash": record.audit_hash,
        "audit_prev_hash": record.audit_prev_hash,
        "authorization": (
            None
            if record.authorization is None
            else {
                "auth_id": record.authorization.auth_id,
                "expires_at": record.authorization.expires_at,
                "ttl_seconds": record.authorization.ttl_seconds,
                "environment": record.authorization.environment,
            }
        ),
        "failed_checks": [
            {
                "check_id": check.check_id,
                "severity": check.severity,
                "reason_code": str(getattr(check.reason_code, "value", check.reason_code)),
                "actual": check.actual,
                "threshold": check.threshold,
                "recommended_quantity": check.recommended_quantity,
            }
            for check in record.checks
            if not check.passed
        ],
        "checks_run": len(record.checks),
    }


def _codes(codes: Any) -> list[str]:
    return [str(getattr(code, "value", code)) for code in codes or []]


def _as_decimal_text(value: Any) -> str | None:
    """Numbers arrive as JSON. A float is turned into its own exact text, never re-rounded here.

    The contract refuses anything that is not a decimal string, so a caller that sends ``1.85`` as a
    JSON number gets its repr - which is what they typed - rather than a rejection they cannot read.
    """
    if value is None:
        return None
    if isinstance(value, bool):
        raise ValueError("a boolean is not a quantity")
    if isinstance(value, str):
        return value.strip() or None
    return repr(value) if isinstance(value, float) else str(value)


def _result(request_id: Any, result: Mapping[str, Any]) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": request_id, "result": dict(result)}


def _error(request_id: Any, code: int, message: str) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": message}}


def _tool_ok(payload: Any) -> dict[str, Any]:
    text = json.dumps(payload, indent=2, sort_keys=True, default=str)
    return {"content": [{"type": "text", "text": text}], "isError": False}


def _tool_error(
    message: str, *, reason_codes: list[str] | None = None, detail: Any = None
) -> dict[str, Any]:
    payload: dict[str, Any] = {"error": message}
    if reason_codes:
        payload["reason_codes"] = reason_codes
    if detail:
        payload["detail"] = str(detail)
    return {
        "content": [{"type": "text", "text": json.dumps(payload, indent=2, sort_keys=True)}],
        "isError": True,
    }


# ---------------------------------------------------------------------------------------------------
# stdio transport
# ---------------------------------------------------------------------------------------------------
def serve_stdio(
    server: MizanMCPServer,
    stdin: IO[str] | None = None,
    stdout: IO[str] | None = None,
) -> int:
    """Read newline-delimited JSON-RPC from stdin, write responses to stdout, until EOF.

    Nothing but protocol is ever written to stdout. Diagnostics go to stderr, because a stray print
    on this stream corrupts the transport for every client.
    """
    source = stdin if stdin is not None else sys.stdin
    sink = stdout if stdout is not None else sys.stdout
    try:
        for line in source:
            text = line.strip()
            if not text:
                continue
            try:
                message = json.loads(text)
            except ValueError:
                _write(sink, _error(None, -32700, "parse error: not JSON"))
                continue
            if not isinstance(message, Mapping):
                _write(sink, _error(None, -32600, "invalid request: not an object"))
                continue
            response = server.handle(message)
            if response is not None:
                _write(sink, response)
    except KeyboardInterrupt:  # pragma: no cover - interactive only
        pass
    finally:
        server.close()
    return 0


def _write(sink: IO[str], message: Mapping[str, Any]) -> None:
    sink.write(json.dumps(message, separators=(",", ":"), default=str) + "\n")
    sink.flush()


def main(argv: list[str] | None = None) -> int:
    """``python -m mizan.mcp`` - serve Mizan's governed operations as MCP tools over stdio."""
    import argparse

    parser = argparse.ArgumentParser(
        prog="python -m mizan.mcp",
        description="Mizan's MCP server: governed trading operations as MCP tools.",
    )
    parser.add_argument("--tenant", default=None, help="tenant id (default: tenant-a)")
    parser.add_argument("--agent", default=None, help=f"agent id (default {DEFAULT_AGENT.agent_id})")
    parser.add_argument("--policy", default=None, help="path to the policy YAML")
    parser.add_argument("--ledger", default=None, help="directory for the SQLite ledger (default: memory)")
    parser.add_argument(
        "--broker",
        default=None,
        choices=["mock", "alpaca-mcp", "alpaca-py"],
        help="mock (credential-free), alpaca-mcp (Alpaca's official MCP server), alpaca-py (SDK)",
    )
    parser.add_argument(
        "--alpaca-mcp-cmd",
        default=None,
        help="override the command that starts Alpaca's MCP server (whitespace-separated argv)",
    )
    parser.add_argument(
        "--live",
        action="store_true",
        help="actually submit to the PAPER venue; without it the gate stops at WOULD_SUBMIT",
    )
    parser.add_argument("--print-tools", action="store_true", help="print the tool list and exit")
    args = parser.parse_args(argv)

    # Every setting comes from a flag. Nothing about which checks run, or which venue is reached, is
    # inherited from the environment - see mizan/mcp/session.py for why.
    config = SessionConfig.resolve(
        tenant_id=args.tenant,
        agent_id=args.agent,
        policy_path=args.policy,
        ledger_dir=args.ledger,
        broker=args.broker,
        dry_run=not args.live,
        alpaca_mcp_command=args.alpaca_mcp_cmd.split() if args.alpaca_mcp_cmd else None,
    )
    if args.print_tools:
        for tool in TOOLS:
            print(f"{tool['name']:24} {tool['description'].split('.')[0]}.")
        return 0
    print(
        f"mizan mcp server | tenant={config.tenant_id} broker={config.broker} "
        f"dry_run={config.dry_run} | stdio",
        file=sys.stderr,
    )
    return serve_stdio(MizanMCPServer(config))
