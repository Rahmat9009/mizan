#!/usr/bin/env python
"""Build the complete Mizan submission evidence bundle with one command.

    python scripts/evidence_pack.py

Everything a judge needs to check the claim, written to ``evidence/pack/`` and summarised in one
file that reads top to bottom:

* **the account** - id, status, starting equity, current equity, and profit and loss as a plain
  signed number over a stated window;
* **the book** - every open position and every order the account has ever carried, each with its
  status, its ``order_class`` and its leg count, so a multi-leg defined-risk order is visibly one
  atomic order rather than several naked ones;
* **the audit trail** - the tenant's hash-chained ledger exported to a readable JSON Lines file;
* **the two proofs** - offline chain verification (over the ledger *and* over the export, so the
  export is shown to be faithful) and credential-free decision replay.

This script is READ-ONLY against the broker. It calls ``get_account``, ``get_all_positions`` and
``get_orders`` and nothing else. It places no order, and it has no way to cancel, replace or close
one: Mizan's broker adapter deliberately has no vocabulary for those (Hard Rule B4), and this script
opens no second path to the venue - it borrows the adapter's own client, already proven to be
pointed at the paper host and answered by a paper account.

**Honesty rules this script is built to keep.** It reports the profit and loss number and the window
it was measured over, and it does nothing else with it. It does not annualise, does not extrapolate,
does not compute a Sharpe ratio, and never calls a short paper run "alpha". A loss is printed as a
loss. A window of a few hours is labelled a few hours. The interesting claim in this repository is
that every decision is reproducible from its record - not that the account made money.

Exit status
    0   the bundle is complete and every proof passed
    1   a proof FAILED - the chain is broken, or a decision did not reproduce
    2   the broker could not be read (no credentials, or ``ALPACA_PAPER`` is not true); the
        credential-free half of the bundle is still written in full

The credential-free half needs no Alpaca account at all. Anyone holding the ledger file can run it.
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

#: The account started here. Overridable, because the number is a fact about the run, not a constant.
DEFAULT_STARTING_EQUITY = Decimal("100000")

DEFAULT_LEDGER = Path("evidence/live-ledger")
DEFAULT_OUT = Path("evidence/pack")

EXIT_OK = 0
EXIT_PROOF_FAILED = 1
EXIT_BROKER_UNAVAILABLE = 2


class BrokerUnavailable(Exception):
    """The broker half of the bundle could not be read. Never fatal to the credential-free half."""


# ---------------------------------------------------------------------------------------------
# small helpers
# ---------------------------------------------------------------------------------------------
def _decimal(value: Any) -> Decimal | None:
    """A Decimal, or None for "the broker did not say". Absence is never coerced to zero (E2)."""
    if value is None:
        return None
    try:
        return Decimal(str(value).strip())
    except (InvalidOperation, ValueError, ArithmeticError):
        return None


def _money(value: Decimal | None) -> str:
    """Two decimal places, or the honest ``(not reported)``."""
    if value is None:
        return "(not reported)"
    return f"{value.quantize(Decimal('0.01'))}"


def _signed(value: Decimal | None) -> str:
    """A plain signed number. A loss keeps its minus sign and is never softened."""
    if value is None:
        return "(not reported)"
    return f"{value.quantize(Decimal('0.01')):+}"


def _text(value: Any) -> str:
    """An SDK enum, a string or None, flattened to the value a human reads."""
    if value is None:
        return ""
    return str(getattr(value, "value", value))


def _humanise_window(start: datetime | None, end: datetime) -> str:
    """"3 hours 12 minutes", not "0.13 years". Short windows must look short."""
    if start is None:
        return "(no window recorded)"
    seconds = max(0, int((end - start).total_seconds()))
    days, seconds = divmod(seconds, 86_400)
    hours, seconds = divmod(seconds, 3_600)
    minutes = seconds // 60
    parts = [f"{days} day{'s' if days != 1 else ''}"] if days else []
    parts += [f"{hours} hour{'s' if hours != 1 else ''}"] if hours else []
    parts += [f"{minutes} minute{'s' if minutes != 1 else ''}"] if minutes or not parts else []
    return " ".join(parts)


def _parse_ts(value: Any) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=UTC)
    text = str(value).strip().replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _rel(path: Path | str) -> str:
    """A path a reader can retype: relative to the repository root, forward slashes."""
    candidate = Path(path)
    try:
        candidate = candidate.relative_to(REPO_ROOT)
    except ValueError:
        pass
    return candidate.as_posix()


def _run(argv: list[str]) -> tuple[int, str]:
    """Run a repository command and capture what a judge would see on their own terminal."""
    proc = subprocess.run(  # noqa: S603 - fixed argv, no shell, no user input
        argv,
        cwd=str(REPO_ROOT),
        capture_output=True,
        text=True,
    )
    return proc.returncode, ((proc.stdout or "") + (proc.stderr or "")).strip()


# ---------------------------------------------------------------------------------------------
# the broker half: three read-only calls, and no fourth
# ---------------------------------------------------------------------------------------------
def read_broker() -> dict[str, Any]:
    """Read the account, its positions and its full order history. Reads only.

    The client is the adapter's own, so the adapter's two paper proofs - the base URL names the
    paper host, and the account that answered carries Alpaca's paper account prefix - have already
    run before a single byte is read here. Both are re-asserted anyway: this script must not be the
    place where a paper guarantee is taken on trust.
    """
    try:
        from mizan.adapters.alpaca_paper import (
            AlpacaPaperBroker,
            _assert_paper_account,
            _assert_paper_client,
        )
    except ImportError as exc:  # pragma: no cover - dependency-shaped failure
        raise BrokerUnavailable(f"the Alpaca adapter could not be imported: {exc}") from exc

    try:
        broker = AlpacaPaperBroker.from_environment()
    except Exception as exc:  # noqa: BLE001 - every failure here is "the broker is unavailable"
        raise BrokerUnavailable(f"{type(exc).__name__}: {exc}") from exc

    # The proven paper client. This script deliberately does not build its own: a second client is a
    # second path to a venue, and there is exactly one in this repository.
    client = broker._client
    _assert_paper_client(client)

    from alpaca.trading.enums import QueryOrderStatus
    from alpaca.trading.requests import GetOrdersRequest

    try:
        account = client.get_account()
        _assert_paper_account(account)
        positions = list(client.get_all_positions() or ())
        orders = list(
            client.get_orders(
                filter=GetOrdersRequest(status=QueryOrderStatus.ALL, nested=True, limit=500)
            )
            or ()
        )
    except Exception as exc:  # noqa: BLE001
        raise BrokerUnavailable(f"{type(exc).__name__}: {exc}") from exc

    return {
        # The account ID identifies the account. The account NUMBER is redacted everywhere in this
        # repository and is not written here either.
        "account_id": _text(getattr(account, "id", None)),
        "status": _text(getattr(account, "status", None)),
        "currency": _text(getattr(account, "currency", None)),
        "pattern_day_trader": getattr(account, "pattern_day_trader", None),
        "trading_blocked": getattr(account, "trading_blocked", None),
        "equity": _decimal(getattr(account, "equity", None)),
        "last_equity": _decimal(getattr(account, "last_equity", None)),
        "cash": _decimal(getattr(account, "cash", None)),
        "buying_power": _decimal(getattr(account, "buying_power", None)),
        "options_approved_level": getattr(account, "options_approved_level", None),
        "positions": [_position_row(raw) for raw in positions],
        "orders": [_order_row(raw) for raw in orders],
    }


def _position_row(raw: Any) -> dict[str, Any]:
    return {
        "symbol": _text(getattr(raw, "symbol", None)),
        "asset_class": _text(getattr(raw, "asset_class", None)),
        "side": _text(getattr(raw, "side", None)),
        "quantity": _text(getattr(raw, "qty", None)),
        "avg_entry_price": _decimal(getattr(raw, "avg_entry_price", None)),
        "current_price": _decimal(getattr(raw, "current_price", None)),
        "market_value": _decimal(getattr(raw, "market_value", None)),
        "cost_basis": _decimal(getattr(raw, "cost_basis", None)),
        "unrealized_pl": _decimal(getattr(raw, "unrealized_pl", None)),
    }


def _order_row(raw: Any) -> dict[str, Any]:
    """One order, flattened. ``leg_count`` is what proves a vertical was submitted atomically."""
    legs = list(getattr(raw, "legs", None) or ())
    return {
        "broker_order_id": _text(getattr(raw, "id", None)),
        "client_order_id": _text(getattr(raw, "client_order_id", None)),
        "symbol": _text(getattr(raw, "symbol", None)),
        "asset_class": _text(getattr(raw, "asset_class", None)),
        "status": _text(getattr(raw, "status", None)),
        "order_class": _text(getattr(raw, "order_class", None)) or "simple",
        "order_type": _text(getattr(raw, "order_type", None) or getattr(raw, "type", None)),
        "side": _text(getattr(raw, "side", None)),
        "quantity": _text(getattr(raw, "qty", None)),
        "filled_quantity": _text(getattr(raw, "filled_qty", None)),
        "filled_avg_price": _decimal(getattr(raw, "filled_avg_price", None)),
        "limit_price": _decimal(getattr(raw, "limit_price", None)),
        "submitted_at": _text(getattr(raw, "submitted_at", None)),
        "leg_count": len(legs),
        "legs": [
            {
                "symbol": _text(getattr(leg, "symbol", None)),
                "side": _text(getattr(leg, "side", None)),
                "quantity": _text(getattr(leg, "qty", None)),
                "status": _text(getattr(leg, "status", None)),
                "filled_quantity": _text(getattr(leg, "filled_qty", None)),
                "limit_price": _decimal(getattr(leg, "limit_price", None)),
                "ratio_quantity": _text(getattr(leg, "ratio_qty", None)),
            }
            for leg in legs
        ],
    }


# ---------------------------------------------------------------------------------------------
# the fallback: what the engine RECORDED about the account, when the broker cannot be read
# ---------------------------------------------------------------------------------------------
def read_ledger_account(exports: list[dict[str, Any]]) -> dict[str, Any] | None:
    """The last account and portfolio state the ENGINE RECORDED, straight out of the chain.

    This is not a substitute for reading the account and it is never presented as one. It is the
    real broker state at the moment of the last governed decision - Alpaca's own numbers, captured
    inside a hash-chained record and therefore tamper-evident - which is strictly better evidence
    than a blank section when credentials are not to hand. Every number it produces is stamped with
    the time it was recorded, and the summary labels the whole section as recorded, not live.

    Nothing here is estimated, carried forward or filled in. If the chain does not hold a value, the
    value is absent.
    """
    payloads = [
        payload
        for export in exports
        for payload in export["payloads"]
        if payload.get("risk_context") and payload.get("verdict")
    ]
    if not payloads:
        return None
    latest = max(payloads, key=lambda payload: int(payload.get("sequence") or 0))
    context = latest.get("risk_context") or {}
    account_state = context.get("account_state") or {}
    portfolio = context.get("portfolio_snapshot") or {}
    return {
        "recorded_at": str(latest.get("recorded_at") or ""),
        "as_of": str(portfolio.get("as_of") or account_state.get("as_of") or ""),
        "sequence": latest.get("sequence"),
        "status": str(account_state.get("status") or ""),
        "trading_blocked": account_state.get("trading_blocked"),
        "options_trading_level": account_state.get("options_trading_level"),
        "source": str(portfolio.get("source") or account_state.get("source") or ""),
        "equity": _decimal(portfolio.get("equity")),
        "cash": _decimal(portfolio.get("cash")),
        "buying_power": _decimal(portfolio.get("buying_power")),
        "peak_equity": _decimal(portfolio.get("peak_equity")),
        "positions": [
            {
                # The recorded contract carries what the ENGINE needs to value a book - symbol,
                # quantity, market value. Entry price and unrealised P&L are broker bookkeeping the
                # engine has no use for, so they are not in the record and are reported absent here
                # rather than derived from something that is not them.
                "symbol": str(position.get("occ_symbol") or position.get("symbol") or ""),
                "asset_class": str(position.get("asset_class") or ""),
                "side": "",
                "quantity": str(position.get("quantity") or ""),
                "avg_entry_price": None,
                "current_price": None,
                "market_value": _decimal(position.get("market_value")),
                "cost_basis": None,
                "unrealized_pl": None,
            }
            for position in portfolio.get("positions") or ()
        ],
        "authorizations": authorized_orders(exports),
    }


def authorized_orders(exports: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Every execution authorization the chain holds, with its legs.

    An authorization is what Mizan permitted, not what the venue did with it. The two are reported
    separately and never merged: conflating "we allowed this" with "this filled" is exactly the kind
    of claim an audit trail exists to prevent.
    """
    rows = []
    for export in exports:
        for payload in export["payloads"]:
            authorization = payload.get("authorization")
            if not authorization:
                continue
            scope = authorization.get("scope") or {}
            legs = list(scope.get("legs") or ())
            rows.append(
                {
                    "sequence": payload.get("sequence"),
                    "auth_id": str(authorization.get("auth_id") or ""),
                    "issued_at": str(authorization.get("issued_at") or ""),
                    "expires_at": str(authorization.get("expires_at") or ""),
                    "environment": str(authorization.get("environment") or ""),
                    "idempotency_key": str(authorization.get("idempotency_key") or ""),
                    "symbol": str(scope.get("symbol") or ""),
                    "asset_class": str(scope.get("asset_class") or ""),
                    "intent": str(scope.get("intent") or ""),
                    "max_notional": _decimal(scope.get("max_notional")),
                    "total_quantity": str(scope.get("total_quantity") or ""),
                    "leg_count": len(legs),
                    "legs": [
                        {
                            "symbol": str(leg.get("occ_symbol") or leg.get("symbol") or ""),
                            "side": str(leg.get("side") or ""),
                            "quantity": str(leg.get("quantity") or ""),
                            "limit_price": _decimal(leg.get("limit_price")),
                            "status": "",
                            "filled_quantity": "",
                        }
                        for leg in legs
                    ],
                }
            )
    return sorted(rows, key=lambda row: row["issued_at"])


# ---------------------------------------------------------------------------------------------
# the credential-free half: the chain, its export, and the two proofs
# ---------------------------------------------------------------------------------------------
def export_chain(ledger_dir: Path, out_dir: Path) -> list[dict[str, Any]]:
    """Export every tenant chain to JSON Lines, verbatim, one record per line.

    Verbatim matters: the export is the *same bytes* the hash was taken over, so the exported file
    verifies on its own. A prettified export would be a different document that happens to describe
    the ledger, and a judge would have to take the description on trust.
    """
    from mizan.audit.verify_chain import read_chain_file

    exports: list[dict[str, Any]] = []
    for path in sorted(ledger_dir.glob("*.sqlite")):
        chain = read_chain_file(path)
        target = out_dir / f"audit-trail-{path.stem}.jsonl"
        target.write_text(
            "".join(f"{text}\n" for _sequence, text in chain.rows),
            encoding="utf-8",
        )
        payloads = [json.loads(text) for _sequence, text in chain.rows]
        readable = out_dir / f"audit-trail-{path.stem}.txt"
        readable.write_text(_readable_chain(path, payloads), encoding="utf-8")
        exports.append(
            {
                "tenant": path.stem,
                "ledger": _rel(path),
                "jsonl": _rel(target),
                "readable": _rel(readable),
                "links": len(chain.rows),
                "decision_records": chain.decisions,
                "control_events": chain.control_events,
                "payloads": payloads,
            }
        )
    return exports


def _readable_chain(path: Path, payloads: list[dict[str, Any]]) -> str:
    """The chain as a table. Same content as the JSON Lines file, laid out for a person."""
    lines = [
        f"Mizan audit trail - {path.stem}",
        f"source: {_rel(path)}",
        f"links : {len(payloads)}",
        "",
        "Every line below is one link of an append-only hash chain. audit_prev_hash of each link is",
        "the audit_hash of the one before it, so a record cannot be altered, inserted or removed",
        "without breaking every link after it. Verify it yourself:",
        "",
        f"    python -m mizan.audit.verify_chain {_rel(path)}",
        "",
        f"{'seq':>4}  {'recorded_at':<28} {'verdict':<8} {'symbol':<8} {'strategy':<20} "
        f"{'hash':<18} reason codes",
        "-" * 130,
    ]
    for payload in payloads:
        proposal = payload.get("proposal") or {}
        reasons = ", ".join(payload.get("reason_codes") or ()) or "-"
        lines.append(
            f"{payload.get('sequence', ''):>4}  "
            f"{str(payload.get('recorded_at', ''))[:28]:<28} "
            f"{str(payload.get('verdict', 'CONTROL')):<8} "
            f"{str(proposal.get('symbol', '')):<8} "
            f"{str(proposal.get('strategy', '')):<20} "
            f"{str(payload.get('audit_hash', ''))[:16]:<18} "
            f"{reasons}"
        )
    lines.append("")
    return "\n".join(lines)


def verify_chains(ledger_dir: Path, exports: list[dict[str, Any]], out_dir: Path) -> dict[str, Any]:
    """Verify each ledger file offline, and verify its export too.

    Verifying the export as well as the source is the point: it closes the gap between "the ledger
    is intact" and "the file in this bundle is that ledger".
    """
    results = []
    transcript: list[str] = []
    for export in exports:
        for label, target in (("ledger", export["ledger"]), ("export", export["jsonl"])):
            code, output = _run([sys.executable, "-m", "mizan.audit.verify_chain", _rel(target)])
            transcript += [f"$ python -m mizan.audit.verify_chain {_rel(target)}", output, ""]
            results.append(
                {
                    "tenant": export["tenant"],
                    "target": label,
                    "path": _rel(target),
                    "exit_code": code,
                    "ok": code == 0,
                }
            )
    if not results:
        transcript = [f"no *.sqlite ledger found under {ledger_dir}"]
    (out_dir / "verify-chain.txt").write_text("\n".join(transcript) + "\n", encoding="utf-8")
    return {
        "ok": bool(results) and all(entry["ok"] for entry in results),
        "results": results,
        "transcript": "\n".join(transcript).strip(),
    }


def replay_ledger(ledger_dir: Path, out_dir: Path) -> dict[str, Any]:
    """Re-derive every recorded decision from the record alone. No credentials, no network."""
    # The RELATIVE path is passed to the child - its cwd is the repository root - so the transcript
    # records the exact command a reader can retype, not one machine's absolute path.
    argv = [sys.executable, "-m", "mizan.replay", "--ledger", _rel(ledger_dir)]
    code, output = _run(argv)
    transcript = f"$ python -m mizan.replay --ledger {_rel(ledger_dir)}\n{output}\n"
    (out_dir / "replay.txt").write_text(transcript, encoding="utf-8")
    headline = next(
        (line.strip() for line in output.splitlines() if "reproduced identically" in line),
        "",
    )
    # "the engine changed" and "the ledger was altered" both surface as a non-zero exit, and reporting
    # them the same way is how an honest version bump gets read as evidence of tampering. They are
    # separated here, CONSERVATIVELY: every single differing record must carry the engine-version
    # explanation. One unexplained difference and this is a plain FAIL, which is the safe direction to
    # be wrong in - a real alteration must never be softened into a version note.
    differing = output.count("MISMATCH decision=")
    version_explained = output.count("ENGINE VERSION MISMATCH")
    engine_changed = bool(differing) and differing == version_explained
    return {
        "ok": code == 0,
        "exit_code": code,
        "headline": headline,
        "transcript": output,
        "engine_changed": engine_changed,
        "verdict": "PASS" if code == 0 else ("ENGINE CHANGED" if engine_changed else "FAIL"),
        "differing": differing,
        "version_explained": version_explained,
    }


def ledger_window(exports: list[dict[str, Any]]) -> tuple[datetime | None, datetime | None]:
    """First and last decision timestamps across every exported chain."""
    stamps = [
        parsed
        for export in exports
        for payload in export["payloads"]
        if (parsed := _parse_ts(payload.get("recorded_at"))) is not None
    ]
    return (min(stamps), max(stamps)) if stamps else (None, None)


def verdict_counts(exports: list[dict[str, Any]]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for export in exports:
        for payload in export["payloads"]:
            verdict = str(payload.get("verdict") or "CONTROL_EVENT")
            counts[verdict] = counts.get(verdict, 0) + 1
    return dict(sorted(counts.items()))


# ---------------------------------------------------------------------------------------------
# the summary a judge reads top to bottom
# ---------------------------------------------------------------------------------------------
def render_summary(pack: dict[str, Any]) -> str:
    generated = pack["generated_at"]
    broker = pack.get("broker")
    lines = [
        "# Mizan - submission evidence pack",
        "",
        f"Generated: **{generated}**",
        "",
        "One command produced everything in this directory:",
        "",
        "```",
        "python scripts/evidence_pack.py",
        "```",
        "",
        "Read it top to bottom. Sections 3 and 4 need no credentials of any kind - anyone holding",
        "the ledger file can re-run them and get the same answer.",
        "",
        "---",
        "",
        "## 1. The account",
        "",
    ]

    recorded = pack.get("recorded_account")
    if broker is None:
        lines += [
            "**THE ACCOUNT WAS NOT READ LIVE.** " + str(pack["broker_error"]),
            "",
            "The live read needs `ALPACA_API_KEY` / `ALPACA_SECRET_KEY` (or `APCA_API_KEY_ID` /",
            "`APCA_API_SECRET_KEY`) plus `ALPACA_PAPER=true` in the environment. Re-run this",
            "command with those set and this section fills in from the account itself.",
            "",
        ]
        if recorded is None:
            lines += [
                "Nothing is inferred, cached or reconstructed in their absence: a section that",
                "cannot be read says so. Sections 3 and 4 below need no credentials, are",
                "unaffected, and are complete.",
                "",
            ]
        else:
            lines += [
                f"What follows is **the state Alpaca reported at {recorded['as_of']}**, as captured",
                f"inside decision record #{recorded['sequence']} of the hash-chained ledger. These",
                "are the broker's own numbers, not a reconstruction - but they are as of that",
                "moment, not now, and they are labelled recorded rather than live throughout.",
                "Because they sit inside the chain, section 3's verification covers them: altering",
                "them would break every link after that record.",
                "",
                f"| account id | `{pack['account_id'] or '(not in the record)'}` |",
                "|---|---|",
                f"| status (recorded) | {recorded['status']} |",
                f"| trading blocked | {recorded['trading_blocked']} |",
                f"| options trading level | {recorded['options_trading_level']} |",
                f"| source | `{recorded['source']}` |",
                f"| recorded at | {recorded['as_of']} |",
                f"| starting equity | {_money(pack['starting_equity'])} |",
                f"| equity (recorded) | {_money(recorded['equity'])} |",
                f"| **profit and loss (recorded)** | **{_signed(pack['pnl'])}** |",
                f"| cash (recorded) | {_money(recorded['cash'])} |",
                f"| buying power (recorded) | {_money(recorded['buying_power'])} |",
                f"| peak equity (recorded) | {_money(recorded['peak_equity'])} |",
                "",
                f"Profit and loss as last recorded: **{_signed(pack['pnl'])}** "
                f"({pack['pnl_words']}), over {pack['window_words']} "
                f"({pack['window_from']} to {recorded['as_of']}).",
                "",
                "That number is the number. It is not annualised, not extrapolated, not scaled to a",
                "notional book, and not described as alpha or edge. A window this short is noise.",
                "The claim this repository makes is reproducibility, not return.",
                "",
            ]
    else:
        pnl = pack["pnl"]
        lines += [
            f"| account id | `{broker['account_id']}` |",
            "|---|---|",
            f"| status | {broker['status']} |",
            "| environment | Alpaca **paper** (proven twice: the client's base URL names the "
            "paper host, and the account carries Alpaca's paper prefix) |",
            f"| starting equity | {_money(pack['starting_equity'])} |",
            f"| current equity | {_money(broker['equity'])} |",
            f"| **profit and loss** | **{_signed(pnl)}** |",
            f"| cash | {_money(broker['cash'])} |",
            f"| buying power | {_money(broker['buying_power'])} |",
            f"| equity at previous close | {_money(broker['last_equity'])} |",
            "",
            f"Profit and loss over this run: **{_signed(pnl)}** "
            f"({pack['pnl_words']}), measured over {pack['window_words']} "
            f"({pack['window_from']} to {generated}).",
            "",
            "That number is the number. It is not annualised, not extrapolated, not scaled to a",
            "notional book, and not described as alpha or edge. A window this short over a handful",
            "of defined-risk positions is noise, and the correct reading of it is that Mizan placed",
            "real, governed, defined-risk orders on a real broker and recorded exactly what",
            "happened. The claim this repository makes is reproducibility, not return.",
            "",
        ]

    lines += ["---", "", "## 2. The book", ""]
    if broker is not None:
        lines += _positions_block(broker["positions"], live=True)
        lines += _orders_block(broker["orders"])
    elif recorded is not None:
        lines += [
            f"Recorded state, as of {recorded['as_of']}. Not live - see section 1.",
            "",
        ]
        lines += _positions_block(recorded["positions"], live=False)
        lines += _authorizations_block(recorded["authorizations"])
    else:
        lines += ["**NOT READ** - see section 1.", ""]

    chain = pack["chain"]
    lines += [
        "---",
        "",
        "## 3. The audit trail (no credentials needed)",
        "",
        f"{chain['links']} link(s) across {len(chain['exports'])} tenant chain(s): "
        f"{chain['decision_records']} decision record(s), {chain['control_events']} control "
        "event(s).",
        "",
        f"Verdicts recorded: {chain['verdicts'] or '(none)'}",
        "",
        "Exported, verbatim, to:",
        "",
    ]
    for export in chain["exports"]:
        lines += [
            f"* `{export['jsonl']}` - the bytes the hashes were taken over, one record per line",
            f"* `{export['readable']}` - the same chain as a table",
        ]
    lines += [
        "",
        "Every record carries the policy snapshot, the market and portfolio state, the model",
        "provenance, the verdict, the reason codes and the hash of the record before it. The",
        "chain is append-only at the storage layer and each link commits to its predecessor.",
        "",
        "### Offline chain verification",
        "",
        "```",
        *[f"python -m mizan.audit.verify_chain {export['ledger']}" for export in chain["exports"]],
        "```",
        "",
        f"**{'PASS' if pack['verify']['ok'] else 'FAIL'}** - "
        + (
            "every audit_hash recomputes from the record's own content and every link matches its "
            "predecessor, in the ledger and in the export in this bundle alike."
            if pack["verify"]["ok"]
            else "the chain did not verify. See `verify-chain.txt`."
        ),
        "",
        "Full transcript: `verify-chain.txt`.",
        "",
        "---",
        "",
        "## 4. Credential-free decision replay (no credentials needed)",
        "",
        "```",
        f"python -m mizan.replay --ledger ./{pack['ledger']}",
        "```",
        "",
        f"**{pack['replay'].get('verdict', 'PASS' if pack['replay']['ok'] else 'FAIL')}**"
        + (f" - `{pack['replay']['headline']}`" if pack["replay"]["headline"] else ""),
        "",
        "Each decision is recomputed from the record alone - the same policy snapshot, the same",
        "market and portfolio state, the same engine - and both the verdict and its hash must match",
        "bit for bit. The verdict alone would not be enough: a changed reason code or a changed",
        "authorized quantity would slip past it, and verdict_hash covers those too.",
        "",
        "This runs with no Alpaca key, no network and no access to our infrastructure. That is the",
        "whole point - the evidence is checkable by someone who does not trust us.",
        "",
        "Full transcript: `replay.txt`.",
        "",
        "---",
        "",
        "## 5. What is in this directory",
        "",
    ]
    for name in sorted(pack["files"]):
        lines.append(f"* `{name}`")
    lines += [
        "",
        "`pack.json` is the same content as this file, machine-readable.",
        "",
        "## 6. What this pack does not prove",
        "",
        "* It says nothing about whether the strategy is any good. One trading session on a paper",
        "  account is far too small a sample to say anything about returns, and no attempt is made",
        "  here to dress it up as one.",
        "* Option greeks are not in it. They need an OPRA market-data agreement this account does",
        "  not hold, so the greeks-based checks block on `GREEKS_MISSING` rather than guessing - a",
        "  refusal that is visible in the reason codes of the recorded decisions above.",
        "* There is no close, cancel or replace path anywhere in Mizan. Positions run to expiry or",
        "  are closed by hand outside the system. That is a deliberate scope boundary, not an",
        "  omission, and it is why `scripts/position_monitor.py` only ever reports.",
        "",
    ]
    return "\n".join(lines)


def _positions_block(positions: list[dict[str, Any]], *, live: bool) -> list[str]:
    heading = "### Positions" if live else "### Positions (recorded)"
    if not positions:
        return [
            heading,
            "",
            "None open. (An account can hold no positions and still have a full order history "
            "below; both are reported, and neither is inferred from the other.)",
            "",
        ]
    lines = [
        heading,
        "",
        "| symbol | class | side | qty | avg entry | current | market value | unrealised P&L |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for row in positions:
        lines.append(
            f"| `{row['symbol']}` | {row['asset_class']} | {row['side']} | {row['quantity']} | "
            f"{_money(row['avg_entry_price'])} | {_money(row['current_price'])} | "
            f"{_money(row['market_value'])} | {_signed(row['unrealized_pl'])} |"
        )
    lines.append("")
    return lines


def _authorizations_block(authorizations: list[dict[str, Any]]) -> list[str]:
    """What Mizan authorized, from the chain. Deliberately NOT presented as broker order state."""
    lines = ["### Orders", ""]
    if not authorizations:
        return lines + [
            "No execution authorization appears in the chain, so Mizan authorized no order. "
            "Whether the account carries orders from anywhere else cannot be answered without "
            "reading it live.",
            "",
        ]
    multi = [row for row in authorizations if row["leg_count"] > 1]
    lines += [
        f"The broker's own order list needs credentials and was not read. What the ledger *can* "
        f"show is what Mizan authorized: {len(authorizations)} execution authorization(s), of "
        f"which {len(multi)} multi-leg. An authorization is permission to submit, not a fill; the "
        "two are never merged here.",
        "",
        "| issued | symbol | intent | env | legs | qty | max notional | idempotency key |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for row in authorizations:
        lines.append(
            f"| {row['issued_at'][:19]} | `{row['symbol']}` | {row['intent']} | "
            f"**{row['environment']}** | {row['leg_count']} | {row['total_quantity']} | "
            f"{_money(row['max_notional'])} | `{row['idempotency_key']}` |"
        )
    lines.append("")
    for row in multi:
        lines += [
            f"**{row['auth_id']}** - {row['leg_count']} legs, {row['asset_class']}, "
            f"expires {row['expires_at'][:19]} (single use)",
            "",
            "| leg | contract | side | qty | limit |",
            "|---|---|---|---|---|",
        ]
        for index, leg in enumerate(row["legs"]):
            lines.append(
                f"| {index} | `{leg['symbol']}` | {leg['side']} | {leg['quantity']} | "
                f"{_money(leg['limit_price'])} |"
            )
        lines.append("")
    if multi:
        lines += [
            "A buy leg and a sell leg on the same underlying and expiry, authorized together under",
            "one notional cap, is a defined-risk vertical. Mizan submits these to Alpaca as a single",
            "atomic `order_class=mleg` order for a reason: split into two single-leg orders, one side",
            "can fill while the other does not, and the account is left holding a naked short that",
            "no policy ever approved.",
            "",
        ]
    return lines


def _orders_block(orders: list[dict[str, Any]]) -> list[str]:
    lines = ["### Orders", ""]
    if not orders:
        return lines + ["None. No order has ever been submitted on this account.", ""]
    multi = [row for row in orders if row["leg_count"] > 1]
    lines += [
        f"{len(orders)} order(s), of which {len(multi)} multi-leg.",
        "",
        "| submitted | symbol | order_class | legs | side | qty | status | limit | filled |",
        "|---|---|---|---|---|---|---|---|---|",
    ]
    for row in sorted(orders, key=lambda entry: entry["submitted_at"]):
        lines.append(
            f"| {row['submitted_at'][:19]} | `{row['symbol']}` | **{row['order_class']}** | "
            f"{row['leg_count']} | {row['side']} | {row['quantity']} | {row['status']} | "
            f"{_money(row['limit_price'])} | {row['filled_quantity']} |"
        )
    lines.append("")
    if multi:
        lines += [
            "`order_class = mleg` with more than one leg is the load-bearing detail. A defined-risk",
            "vertical submitted as two separate single-leg orders is not defined-risk: one side can",
            "fill while the other does not, leaving a naked short. These went to the venue as one",
            "atomic order, which is why the risk they carry is the risk the policy approved.",
            "",
        ]
        for row in multi:
            lines += [
                f"**`{row['broker_order_id']}`** - {row['order_class']}, {row['leg_count']} legs, "
                f"status `{row['status']}`, client order id `{row['client_order_id']}`",
                "",
                "| leg | symbol | side | qty | limit | status | filled |",
                "|---|---|---|---|---|---|---|",
            ]
            for index, leg in enumerate(row["legs"]):
                lines.append(
                    f"| {index} | `{leg['symbol']}` | {leg['side']} | {leg['quantity']} | "
                    f"{_money(leg['limit_price'])} | {leg['status']} | {leg['filled_quantity']} |"
                )
            lines.append("")
    return lines


# ---------------------------------------------------------------------------------------------
# entry point
# ---------------------------------------------------------------------------------------------
def _json_safe(value: Any) -> Any:
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, datetime):
        return value.isoformat().replace("+00:00", "Z")
    if isinstance(value, dict):
        return {key: _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(item) for item in value]
    return value


def _load_dotenv() -> None:
    """Honour a local ``.env`` the way the rest of the project does, if python-dotenv is installed.

    This only ever READS configuration. It cannot enable live trading: the adapter requires
    ``ALPACA_PAPER`` to be explicitly true and there is no live client path to configure.
    """
    try:
        from dotenv import load_dotenv
    except ImportError:  # pragma: no cover - optional dependency
        return
    env_file = REPO_ROOT / ".env"
    if env_file.is_file():
        load_dotenv(env_file)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python scripts/evidence_pack.py",
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--ledger",
        type=Path,
        default=DEFAULT_LEDGER,
        help=f"the ledger directory to export, verify and replay (default: {DEFAULT_LEDGER})",
    )
    parser.add_argument(
        "--out",
        type=Path,
        default=DEFAULT_OUT,
        help=f"where to write the bundle (default: {DEFAULT_OUT})",
    )
    parser.add_argument(
        "--starting-equity",
        type=Decimal,
        default=DEFAULT_STARTING_EQUITY,
        help=f"equity the account started with (default: {DEFAULT_STARTING_EQUITY})",
    )
    parser.add_argument(
        "--no-broker",
        action="store_true",
        help="skip the broker reads and build the credential-free half only",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    arguments = _parser().parse_args(argv)
    _load_dotenv()

    ledger_dir = (REPO_ROOT / arguments.ledger).resolve()
    out_dir = (REPO_ROOT / arguments.out).resolve()
    out_dir.mkdir(parents=True, exist_ok=True)

    now = datetime.now(UTC)
    generated_at = now.isoformat(timespec="seconds").replace("+00:00", "Z")
    stamp = now.strftime("%Y%m%dT%H%M%SZ")

    print(f"Mizan evidence pack -> {out_dir}")
    print(f"  ledger : {ledger_dir}")
    print()

    broker: dict[str, Any] | None = None
    broker_error = ""
    if arguments.no_broker:
        broker_error = "--no-broker was passed; the broker was not contacted."
        print(f"  [1/4] account and book  SKIPPED ({broker_error})")
    else:
        try:
            broker = read_broker()
            print(
                f"  [1/4] account and book  OK  account={broker['account_id']} "
                f"positions={len(broker['positions'])} orders={len(broker['orders'])}"
            )
        except BrokerUnavailable as exc:
            broker_error = f"The broker could not be read: {exc}"
            print(f"  [1/4] account and book  UNAVAILABLE ({exc})")

    exports = export_chain(ledger_dir, out_dir)
    links = sum(export["links"] for export in exports)
    print(f"  [2/4] audit trail       OK  {links} link(s) exported")

    recorded = None if broker is not None else read_ledger_account(exports)
    if recorded is not None:
        print(
            f"        -> using the account state RECORDED in the chain at {recorded['as_of']}; "
            "recorded, not live"
        )

    verify = verify_chains(ledger_dir, exports, out_dir)
    print(f"  [3/4] verify-chain      {'PASS' if verify['ok'] else 'FAIL'}")

    replay = replay_ledger(ledger_dir, out_dir)
    print(f"  [4/4] decision replay   {replay['verdict']}  {replay['headline']}")
    if replay["engine_changed"]:
        print("        every difference is explained by the engine version, not by the records:")
        print("        the chain verifies; this engine simply no longer decides the way that one did.")

    first, _last = ledger_window(exports)
    equity = broker["equity"] if broker is not None else (recorded or {}).get("equity")
    pnl = None
    pnl_words = "not measured - the account was not read"
    if equity is not None:
        pnl = equity - arguments.starting_equity
        if pnl > 0:
            pnl_words = "a gain"
        elif pnl < 0:
            pnl_words = "a LOSS"
        else:
            pnl_words = "flat"

    pack: dict[str, Any] = {
        "generated_at": generated_at,
        "engine_version": _engine_version(),
        "ledger": _rel(ledger_dir),
        "starting_equity": arguments.starting_equity,
        "account_id": (broker or {}).get("account_id") or _recorded_account_id(),
        "account_source": (
            "live broker read"
            if broker is not None
            else ("ledger (recorded, not live)" if recorded is not None else "not read")
        ),
        "recorded_account": recorded,
        "pnl": pnl,
        "pnl_words": pnl_words,
        "window_from": (
            first.isoformat(timespec="seconds").replace("+00:00", "Z") if first else "(unknown)"
        ),
        "window_words": _humanise_window(first, now),
        "broker": broker,
        "broker_error": broker_error,
        "chain": {
            "links": links,
            "decision_records": sum(export["decision_records"] for export in exports),
            "control_events": sum(export["control_events"] for export in exports),
            "verdicts": ", ".join(
                f"{count} {verdict}" for verdict, count in verdict_counts(exports).items()
            ),
            "exports": [
                {key: value for key, value in export.items() if key != "payloads"}
                for export in exports
            ],
        },
        "verify": {key: value for key, value in verify.items() if key != "transcript"},
        "replay": {key: value for key, value in replay.items() if key != "transcript"},
        "files": [],
    }

    # The summary lists this directory, and the summary is written INTO this directory, so the
    # listing names the two files that do not exist yet rather than reporting a directory that is
    # missing its own summary.
    written = {entry.name for entry in out_dir.iterdir() if entry.is_file()}
    pack["files"] = sorted(written | {"pack.json", "SUMMARY.md", f"summary-{stamp}.md"})
    (out_dir / "pack.json").write_text(
        json.dumps(_json_safe(pack), indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    (out_dir / "SUMMARY.md").write_text(render_summary(pack), encoding="utf-8")
    # A stable name a judge can always be pointed at, plus an immutable timestamped copy so an
    # earlier pack is never silently overwritten by a later one.
    shutil.copyfile(out_dir / "SUMMARY.md", out_dir / f"summary-{stamp}.md")

    print()
    if broker is not None or recorded is not None:
        state = broker or recorded or {}
        label = "live" if broker is not None else f"RECORDED at {recorded['as_of']}"
        print(f"  account   : {pack['account_id'] or '(not read)'}  ({state.get('status')}, paper)")
        print(f"  equity    : {_money(equity)}  (started {_money(pack['starting_equity'])}) [{label}]")
        print(f"  P&L       : {_signed(pnl)}  -> {pnl_words}, over {pack['window_words']}")
        print("              Not annualised. Not extrapolated. Not alpha.")
    print(f"  chain     : {links} link(s), verify-chain {'PASS' if verify['ok'] else 'FAIL'}")
    print(f"  replay    : {replay['verdict']}  {replay['headline']}")
    if replay["engine_changed"]:
        print("              engine-version comparison, NOT an integrity failure - see engine-versions.json")
    print()
    print(f"  read this : {_rel(out_dir / 'SUMMARY.md')}")

    if not verify["ok"] or not replay["ok"]:
        return EXIT_PROOF_FAILED
    if broker is None:
        return EXIT_BROKER_UNAVAILABLE
    return EXIT_OK


def _recorded_account_id() -> str:
    """The account id the first live run wrote down, when the account cannot be asked directly.

    ``evidence/live-run.json`` is written by the run that made first contact with the paper account.
    The account ID identifies an account; it is not a credential and grants nothing. The account
    NUMBER is a different field, is redacted everywhere in this repository, and is never read here.
    """
    source = REPO_ROOT / "evidence" / "live-run.json"
    if not source.is_file():
        return ""
    try:
        return str(json.loads(source.read_text(encoding="utf-8")).get("account_id") or "")
    except (OSError, ValueError):
        return ""


def _engine_version() -> str:
    try:
        from mizan.contracts.canonical import ENGINE_VERSION

        return str(ENGINE_VERSION)
    except Exception:  # noqa: BLE001 - the version is decoration, never a reason to fail
        return "unknown"


if __name__ == "__main__":  # pragma: no cover - process entry point
    raise SystemExit(main())
