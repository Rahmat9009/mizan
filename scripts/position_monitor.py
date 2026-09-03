#!/usr/bin/env python
"""Report every open position: unrealised P&L, days to expiry, and distance from the short strike.

    python scripts/position_monitor.py

**THIS MONITOR REPORTS ONLY. IT NEVER CLOSES ANYTHING, AND IT CANNOT.**

That is not a caution, it is a description of the code that exists. Mizan deliberately has no
cancel, no replace and no close broker path anywhere - a Hard Rule, enforced at the abstraction
itself: ``mizan.adapters.base.BrokerAdapter`` has four reads and exactly one mutation, and the words
``cancel_order``, ``replace_order``, ``close_position`` and ``close_all_positions`` appear nowhere in
it. A capability that cannot be named cannot be reached by a bug, a debug flag, a panicking operator
or a helpful refactor. So when this monitor tells you a short strike is one point away and expiry is
tomorrow, the response is a human one, taken deliberately, outside this system. There is no button
here, and adding one is not a small change - it is a change to what Mizan is.

Why it is built that way: an automated close path is a second, unreviewed way for software to reach
the venue, and it runs precisely when things are going badly and supervision is worst. v1 governs
what goes ON. Taking risk OFF stays with a person.

What it reports, per position:

* unrealised profit and loss, in currency and as a percentage of cost basis;
* days to expiry, parsed from the OCC symbol - with 0DTE and expired called out;
* distance from the short strike, in points and as a percentage of spot, together with which
  direction the underlying has to move for that strike to be threatened.

Positions are grouped by underlying and expiry, so a defined-risk vertical is shown as the one
structure it is rather than as two unrelated rows.

Exit status
    0   positions reported (including "none open", which is a report, not a failure)
    2   the account could not be read - no credentials, or ``ALPACA_PAPER`` is not true
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import UTC, date, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

EXIT_OK = 0
EXIT_UNAVAILABLE = 2

#: An OCC option symbol: root, YYMMDD, C or P, then the strike in thousandths.
_OCC = re.compile(r"^(?P<root>[A-Z][A-Z0-9]{0,5})(?P<expiry>\d{6})(?P<kind>[CP])(?P<strike>\d{8})$")


class AccountUnavailable(Exception):
    """The account could not be read. Reported plainly; never worked around."""


# ---------------------------------------------------------------------------------------------
# parsing and formatting
# ---------------------------------------------------------------------------------------------
def parse_occ(symbol: str) -> dict[str, Any] | None:
    """Split an OCC symbol into underlying, expiry, call/put and strike. None if it is not one.

    Equity positions land here too and are correctly rejected: an equity has no expiry and no
    strike, and inventing either would put a number on a row that has no such number.
    """
    match = _OCC.match(symbol.strip().upper())
    if match is None:
        return None
    raw = match.group("expiry")
    try:
        expiry = date(2000 + int(raw[0:2]), int(raw[2:4]), int(raw[4:6]))
    except ValueError:
        return None
    return {
        "underlying": match.group("root"),
        "expiry": expiry,
        "contract_type": "call" if match.group("kind") == "C" else "put",
        "strike": Decimal(match.group("strike")) / Decimal(1000),
    }


def _decimal(value: Any) -> Decimal | None:
    if value is None:
        return None
    try:
        return Decimal(str(value).strip())
    except (InvalidOperation, ValueError, ArithmeticError):
        return None


def _money(value: Decimal | None) -> str:
    return "n/a" if value is None else f"{value.quantize(Decimal('0.01'))}"


def _signed(value: Decimal | None) -> str:
    """A plain signed number. A loss keeps its minus sign."""
    return "n/a" if value is None else f"{value.quantize(Decimal('0.01')):+}"


def _percent(value: Decimal | None) -> str:
    return "n/a" if value is None else f"{(value * 100).quantize(Decimal('0.01')):+}%"


def days_to_expiry(expiry: date, *, today: date) -> int:
    """Calendar days from today to expiry. Negative once it is past."""
    return (expiry - today).days


def expiry_words(days: int) -> str:
    if days < 0:
        return f"EXPIRED {abs(days)} day(s) ago"
    if days == 0:
        return "0DTE - expires today"
    if days == 1:
        return "1 day"
    return f"{days} days"


def strike_distance(
    *, spot: Decimal | None, strike: Decimal, contract_type: str
) -> dict[str, Any]:
    """How far the underlying is from a short strike, and which way it must move to reach it.

    Sign convention: ``points`` is the distance the underlying still has to travel to reach the
    strike, always reported as a magnitude, with ``breached`` saying whether it has already got
    there. A short put is threatened by a fall, a short call by a rise; the two are not symmetric
    and are not collapsed into one signed number that a reader would have to decode.
    """
    if spot is None:
        return {
            "spot": None,
            "points": None,
            "percent": None,
            "breached": None,
            "direction": "down" if contract_type == "put" else "up",
        }
    if contract_type == "put":
        breached = spot <= strike
        points = spot - strike
    else:
        breached = spot >= strike
        points = strike - spot
    percent = (points / spot) if spot != 0 else None
    return {
        "spot": spot,
        "points": abs(points),
        "percent": abs(percent) if percent is not None else None,
        "breached": breached,
        "direction": "down" if contract_type == "put" else "up",
    }


# ---------------------------------------------------------------------------------------------
# reads - and there is nothing here but reads
# ---------------------------------------------------------------------------------------------
def read_positions() -> tuple[list[dict[str, Any]], dict[str, Decimal | None], str]:
    """Open positions plus a spot quote per underlying. Two read calls, no mutation of any kind."""
    try:
        from mizan.adapters.alpaca_paper import (
            AlpacaPaperBroker,
            _assert_paper_account,
            _assert_paper_client,
        )
    except ImportError as exc:  # pragma: no cover - dependency-shaped failure
        raise AccountUnavailable(f"the Alpaca adapter could not be imported: {exc}") from exc

    try:
        broker = AlpacaPaperBroker.from_environment()
    except Exception as exc:  # noqa: BLE001 - every failure here is "the account is unavailable"
        raise AccountUnavailable(f"{type(exc).__name__}: {exc}") from exc

    # The adapter's own client, already proven to be pointed at the paper host and answered by a
    # paper account. Re-asserted rather than trusted; this script opens no second path to a venue.
    client = broker._client
    _assert_paper_client(client)

    try:
        account = client.get_account()
        _assert_paper_account(account)
        raw_positions = list(client.get_all_positions() or ())
    except Exception as exc:  # noqa: BLE001
        raise AccountUnavailable(f"{type(exc).__name__}: {exc}") from exc

    positions = [_position_row(raw) for raw in raw_positions]
    underlyings = sorted(
        {row["underlying"] for row in positions if row["underlying"]}
    )
    spots = _spot_quotes(broker, underlyings)
    return positions, spots, str(getattr(account, "id", "") or "")


def _spot_quotes(broker: Any, underlyings: list[str]) -> dict[str, Decimal | None]:
    """A midpoint per underlying, or None where the venue did not quote one.

    A missing quote stays missing. The alternative - falling back to a last close or to the option's
    own mark - would silently change what "distance from the short strike" measures, and the reader
    would have no way to tell which answer they were looking at.
    """
    if not underlyings:
        return {}
    try:
        snapshot = broker.get_market_snapshot(symbols=underlyings, as_of=datetime.now(UTC))
    except Exception:  # noqa: BLE001 - a quote failure degrades the report, it does not end it
        return dict.fromkeys(underlyings)
    spots: dict[str, Decimal | None] = {}
    for symbol in underlyings:
        quote = snapshot.quotes.get(symbol)
        # ``Quote.price`` is already the bid/ask MIDPOINT, computed in the adapter - never the
        # proposal's own number and never a last close (F-1). A symbol the venue did not quote is
        # simply absent from the snapshot, and stays absent here.
        spots[symbol] = _decimal(getattr(quote, "price", None)) if quote is not None else None
    return spots


def _position_row(raw: Any) -> dict[str, Any]:
    symbol = str(getattr(raw, "symbol", "") or "")
    option = parse_occ(symbol)
    quantity = _decimal(getattr(raw, "qty", None))
    cost_basis = _decimal(getattr(raw, "cost_basis", None))
    unrealized = _decimal(getattr(raw, "unrealized_pl", None))
    percent = _decimal(getattr(raw, "unrealized_plpc", None))
    if percent is None and unrealized is not None and cost_basis not in (None, Decimal(0)):
        percent = unrealized / abs(cost_basis)
    return {
        "symbol": symbol,
        "asset_class": str(
            getattr(getattr(raw, "asset_class", None), "value", getattr(raw, "asset_class", ""))
        ),
        "side": str(getattr(getattr(raw, "side", None), "value", getattr(raw, "side", ""))),
        "quantity": quantity,
        "is_short": quantity is not None and quantity < 0,
        "avg_entry_price": _decimal(getattr(raw, "avg_entry_price", None)),
        "current_price": _decimal(getattr(raw, "current_price", None)),
        "market_value": _decimal(getattr(raw, "market_value", None)),
        "cost_basis": cost_basis,
        "unrealized_pl": unrealized,
        "unrealized_pct": percent,
        "underlying": option["underlying"] if option else symbol,
        "expiry": option["expiry"] if option else None,
        "contract_type": option["contract_type"] if option else None,
        "strike": option["strike"] if option else None,
    }


def read_recorded_positions(ledger_dir: Path) -> tuple[list[dict[str, Any]], str] | None:
    """The last portfolio the ENGINE recorded, for when the account cannot be read live.

    The recorded contract holds what the engine needs to value a book - symbol, quantity, market
    value - and not the broker's own entry price or unrealised P&L. Those columns therefore come
    back empty here rather than being reconstructed from something that is not them.
    """
    from mizan.audit.verify_chain import read_chain_file

    payloads = []
    for path in sorted(ledger_dir.glob("*.sqlite")):
        for _sequence, text in read_chain_file(path).rows:
            payload = json.loads(text)
            if payload.get("risk_context"):
                payloads.append(payload)
    if not payloads:
        return None
    latest = max(payloads, key=lambda payload: int(payload.get("sequence") or 0))
    portfolio = (latest.get("risk_context") or {}).get("portfolio_snapshot") or {}
    rows = []
    for position in portfolio.get("positions") or ():
        symbol = str(position.get("occ_symbol") or position.get("symbol") or "")
        option = parse_occ(symbol)
        quantity = _decimal(position.get("quantity"))
        rows.append(
            {
                "symbol": symbol,
                "asset_class": str(position.get("asset_class") or ""),
                "side": "",
                "quantity": quantity,
                "is_short": quantity is not None and quantity < 0,
                "avg_entry_price": None,
                "current_price": None,
                "market_value": _decimal(position.get("market_value")),
                "cost_basis": None,
                "unrealized_pl": None,
                "unrealized_pct": None,
                "underlying": option["underlying"] if option else symbol,
                "expiry": option["expiry"] if option else None,
                "contract_type": option["contract_type"] if option else None,
                "strike": option["strike"] if option else None,
            }
        )
    return rows, str(portfolio.get("as_of") or "")


# ---------------------------------------------------------------------------------------------
# the report
# ---------------------------------------------------------------------------------------------
BANNER = (
    "Mizan position monitor - READ ONLY\n"
    "  This monitor reports. It does not close, cancel or replace anything, and it has no code\n"
    "  path that could: Mizan's broker adapter has no cancel/replace/close vocabulary at all\n"
    "  (Hard Rule B4). Acting on anything below is a human decision, taken outside this system."
)


def render(
    positions: list[dict[str, Any]],
    spots: dict[str, Decimal | None],
    *,
    account_id: str,
    today: date,
    as_of_label: str,
) -> str:
    lines = [BANNER, "", f"  account : {account_id or '(not read)'}", f"  as of   : {as_of_label}", ""]
    if not positions:
        lines += [
            "  No open positions.",
            "",
            "  Nothing to report is a report. Mizan has authorized defined-risk verticals on this",
            "  account; whether any are open right now is a separate question, and this is its",
            "  answer at the time above.",
            "",
        ]
        return "\n".join(lines)

    total = sum(
        (row["unrealized_pl"] for row in positions if row["unrealized_pl"] is not None),
        Decimal(0),
    )
    reported = [row for row in positions if row["unrealized_pl"] is not None]
    lines += [
        f"  {len(positions)} open position(s).",
        (
            f"  Total unrealised P&L across the {len(reported)} position(s) that report one: "
            f"{_signed(total)}"
            if reported
            else "  Unrealised P&L is not reported for these positions."
        ),
        "",
    ]

    groups: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for row in positions:
        key = (row["underlying"], row["expiry"].isoformat() if row["expiry"] else "-")
        groups.setdefault(key, []).append(row)

    for (underlying, expiry_text), rows in sorted(groups.items()):
        spot = spots.get(underlying)
        header = f"  {underlying}"
        if expiry_text != "-":
            days = days_to_expiry(date.fromisoformat(expiry_text), today=today)
            header += f"  expiry {expiry_text}  ({expiry_words(days)})"
        header += f"  spot {_money(spot) if spot is not None else 'not quoted'}"
        lines += [header, "  " + "-" * 100]
        for row in sorted(rows, key=lambda entry: str(entry["strike"] or "")):
            lines.append(_position_line(row))
        for row in rows:
            if row["is_short"] and row["strike"] is not None and row["contract_type"]:
                lines.append(_short_strike_line(row, spot))
        lines.append("")

    lines += [
        "  Reminder: there is no close path. If one of these needs to come off, a person does it,",
        "  deliberately, somewhere other than here.",
        "",
    ]
    return "\n".join(lines)


def _position_line(row: dict[str, Any]) -> str:
    kind = "SHORT" if row["is_short"] else "long "
    quantity = row["quantity"]
    return (
        f"    {kind} {str(quantity) if quantity is not None else '?':>5}  {row['symbol']:<22}"
        f"entry {_money(row['avg_entry_price']):>8}  mark {_money(row['current_price']):>8}  "
        f"value {_money(row['market_value']):>10}  P&L {_signed(row['unrealized_pl']):>9} "
        f"({_percent(row['unrealized_pct'])})"
    )


def _short_strike_line(row: dict[str, Any], spot: Decimal | None) -> str:
    distance = strike_distance(
        spot=spot, strike=row["strike"], contract_type=row["contract_type"]
    )
    strike = _money(row["strike"])
    if distance["points"] is None:
        return (
            f"      short {row['contract_type']} strike {strike}: distance unknown - the "
            f"underlying was not quoted, and no substitute price is used in its place."
        )
    state = "BREACHED - the underlying is already through it" if distance["breached"] else "intact"
    return (
        f"      short {row['contract_type']} strike {strike}: {_money(distance['points'])} points "
        f"({_percent(distance['percent']).lstrip('+')}) {distance['direction']} from spot - {state}"
    )


def _json_safe(value: Any) -> Any:
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    if isinstance(value, dict):
        return {key: _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(item) for item in value]
    return value


def _load_dotenv() -> None:
    """Honour a local ``.env`` the way the rest of the project does, if python-dotenv is installed.

    Reads configuration only. It cannot enable live trading: the adapter requires ``ALPACA_PAPER``
    to be explicitly true, and there is no live client path to configure in this build.
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
        prog="python scripts/position_monitor.py",
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--json", dest="as_json", action="store_true", help="emit JSON instead")
    parser.add_argument(
        "--ledger",
        type=Path,
        default=Path("evidence/live-ledger"),
        help="ledger to fall back to when the account cannot be read live",
    )
    parser.add_argument(
        "--no-fallback",
        action="store_true",
        help="do not fall back to the last recorded portfolio; report the failure and stop",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    arguments = _parser().parse_args(argv)
    _load_dotenv()
    today = datetime.now(UTC).date()

    account_id = ""
    as_of_label = datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z") + " (live)"
    failure = ""
    try:
        positions, spots, account_id = read_positions()
    except AccountUnavailable as exc:
        failure = str(exc)
        recorded = (
            None
            if arguments.no_fallback
            else read_recorded_positions((REPO_ROOT / arguments.ledger).resolve())
        )
        if recorded is None:
            print(BANNER, file=sys.stderr)
            print("", file=sys.stderr)
            print(f"  THE ACCOUNT COULD NOT BE READ: {failure}", file=sys.stderr)
            print("", file=sys.stderr)
            print(
                "  Set ALPACA_API_KEY / ALPACA_SECRET_KEY (or APCA_API_KEY_ID /\n"
                "  APCA_API_SECRET_KEY) and ALPACA_PAPER=true, then run this again. Nothing is\n"
                "  guessed in their absence.",
                file=sys.stderr,
            )
            return EXIT_UNAVAILABLE
        positions, recorded_as_of = recorded
        spots = dict.fromkeys({row["underlying"] for row in positions})
        as_of_label = f"{recorded_as_of} (RECORDED in the ledger, not live)"

    if arguments.as_json:
        print(
            json.dumps(
                _json_safe(
                    {
                        "account_id": account_id,
                        "as_of": as_of_label,
                        "live": not failure,
                        "read_only": True,
                        "close_path": "none - Mizan has no cancel/replace/close broker path (B4)",
                        "unavailable": failure,
                        "spots": spots,
                        "positions": [
                            {
                                **row,
                                "days_to_expiry": (
                                    days_to_expiry(row["expiry"], today=today)
                                    if row["expiry"]
                                    else None
                                ),
                                "short_strike_distance": (
                                    strike_distance(
                                        spot=spots.get(row["underlying"]),
                                        strike=row["strike"],
                                        contract_type=row["contract_type"],
                                    )
                                    if row["is_short"] and row["strike"] and row["contract_type"]
                                    else None
                                ),
                            }
                            for row in positions
                        ],
                    }
                ),
                indent=2,
                sort_keys=True,
            )
        )
        return EXIT_OK if not failure else EXIT_UNAVAILABLE

    if failure:
        print(f"  (the account could not be read live: {failure})", file=sys.stderr)
    print(render(positions, spots, account_id=account_id, today=today, as_of_label=as_of_label))
    return EXIT_OK if not failure else EXIT_UNAVAILABLE


if __name__ == "__main__":  # pragma: no cover - process entry point
    raise SystemExit(main())
