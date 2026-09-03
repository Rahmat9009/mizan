"""``scripts/evidence_pack.py`` is the artifact a judge is handed, so it is tested as a process.

Three properties matter, in this order.

**It must not overstate.** The brief for this script was explicit: report the profit and loss number
and the window, and do nothing else with it - no annualising, no extrapolation, no calling a few
hours on a paper account "alpha". A loss must read as a loss. Those are tested as text, because text
is what a judge reads and text is where the overstatement would appear.

**It must be honest about what it could not do.** Run without broker credentials, the bundle must say
the account was not read, and must not fill the gap with an invented number.

**It must actually verify.** The chain export in the bundle is checked with the same offline verifier
a customer would run, and the decisions are replayed - end to end, through the real command.
"""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from mizan.audit import SqliteLedger
from tests.fixtures import FIXED_NOW
from tests.invariants._support import append_engine_record

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "scripts" / "evidence_pack.py"


def _load():
    spec = importlib.util.spec_from_file_location("mizan_evidence_pack", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


pack = _load()


# -- the honesty rules -----------------------------------------------------------------------
def test_a_loss_keeps_its_minus_sign() -> None:
    assert pack._signed(Decimal("-8.05")) == "-8.05"


def test_a_gain_is_explicitly_signed_so_the_two_cannot_be_confused() -> None:
    assert pack._signed(Decimal("8.05")) == "+8.05"


def test_a_number_the_broker_did_not_report_is_not_a_zero() -> None:
    """E2. A blank field rendered as 0.00 would read as "flat" when it means "unknown"."""
    assert pack._signed(None) == "(not reported)"
    assert pack._money(None) == "(not reported)"


@pytest.mark.parametrize(
    ("minutes", "expected"),
    [(43, "43 minutes"), (60, "1 hour"), (90, "1 hour 30 minutes"), (1500, "1 day 1 hour")],
)
def test_a_short_window_is_described_in_minutes_and_hours(minutes: int, expected: str) -> None:
    """The window must look as short as it is. "0.08 years" is the shape of a lie."""
    end = datetime(2026, 9, 3, 20, 0, tzinfo=UTC)
    start = end - timedelta(minutes=minutes)
    assert pack._humanise_window(start, end) == expected


def test_no_window_recorded_says_so() -> None:
    assert pack._humanise_window(None, datetime.now(UTC)) == "(no window recorded)"


# -- orders: the leg count is the load-bearing fact --------------------------------------------
class _Leg:
    def __init__(self, **fields: object) -> None:
        self.__dict__.update(fields)


class _Order:
    def __init__(self, **fields: object) -> None:
        self.__dict__.update(fields)


def _mleg_order() -> dict:
    return pack._order_row(
        _Order(
            id="3869a51a-e26a-4d21-8a74-21a3dfba6e80",
            client_order_id="mz1-1a1a0b0acb498f7a6f3aeead100899530754be0b",
            symbol="SPY",
            asset_class="us_option",
            status="filled",
            order_class="mleg",
            order_type="limit",
            side=None,
            qty="5",
            filled_qty="5",
            filled_avg_price="0.02",
            limit_price="0.02",
            submitted_at="2026-09-03T19:44:48Z",
            legs=[
                _Leg(symbol="SPY260908P00737000", side="buy", qty="5", status="filled",
                     filled_qty="5", limit_price="0.02", ratio_qty="1"),
                _Leg(symbol="SPY260908P00738000", side="sell", qty="5", status="filled",
                     filled_qty="5", limit_price="0.04", ratio_qty="1"),
            ],
        )
    )


def test_a_multi_leg_order_reports_its_class_and_its_leg_count() -> None:
    """A vertical submitted atomically is one mleg order with two legs, and must be visibly that."""
    row = _mleg_order()
    assert row["order_class"] == "mleg"
    assert row["leg_count"] == 2
    assert [leg["side"] for leg in row["legs"]] == ["buy", "sell"]


def test_an_order_with_no_class_is_reported_as_simple_not_as_blank() -> None:
    row = pack._order_row(_Order(id="x", client_order_id="y", symbol="SPY", status="new", legs=None))
    assert row["order_class"] == "simple"
    assert row["leg_count"] == 0


def test_the_orders_table_calls_out_why_atomicity_matters() -> None:
    block = "\n".join(pack._orders_block([_mleg_order()]))
    assert "mleg" in block
    assert "naked short" in block


# -- authorizations read back out of the chain -------------------------------------------------
def _authorization_payload() -> dict:
    return {
        "sequence": 10,
        "authorization": {
            "auth_id": "01a068cd-6f06-7454-bbb9-64ef4973886c",
            "issued_at": "2026-09-03T19:44:48Z",
            "expires_at": "2026-09-03T19:45:03Z",
            "environment": "paper",
            "idempotency_key": "mz1-1a1a0b0acb498f7a6f3aeead100899530754be0b",
            "scope": {
                "symbol": "SPY",
                "asset_class": "equity_option",
                "intent": "open",
                "max_notional": "30",
                "total_quantity": "10",
                "legs": [
                    {"occ_symbol": "SPY260908P00737000", "side": "buy", "quantity": "5",
                     "limit_price": "0.02"},
                    {"occ_symbol": "SPY260908P00738000", "side": "sell", "quantity": "5",
                     "limit_price": "0.04"},
                ],
            },
        },
    }


def test_authorizations_are_read_out_of_the_chain_with_their_legs() -> None:
    rows = pack.authorized_orders([{"payloads": [_authorization_payload()]}])
    assert len(rows) == 1
    assert rows[0]["leg_count"] == 2
    assert rows[0]["environment"] == "paper"
    assert rows[0]["legs"][0]["symbol"] == "SPY260908P00737000"


def test_a_record_with_no_authorization_contributes_nothing() -> None:
    """A REJECT authorizes nothing, and must not appear as an order that was permitted."""
    assert pack.authorized_orders([{"payloads": [{"sequence": 1, "authorization": None}]}]) == []


def test_an_authorization_is_never_presented_as_a_fill() -> None:
    block = "\n".join(pack._authorizations_block(pack.authorized_orders(
        [{"payloads": [_authorization_payload()]}]
    )))
    assert "permission to submit, not a fill" in block


# -- the readable chain table ------------------------------------------------------------------
def test_the_readable_chain_names_the_command_that_verifies_it() -> None:
    table = pack._readable_chain(
        Path("evidence/live-ledger/tenant-a.sqlite"),
        [{"sequence": 1, "recorded_at": "2026-09-03T19:13:23Z", "verdict": "REJECT",
          "reason_codes": ["GREEKS_MISSING"], "audit_hash": "a" * 64,
          "proposal": {"symbol": "SPY", "strategy": "bull_put_spread"}}],
    )
    assert "python -m mizan.audit.verify_chain" in table
    assert "GREEKS_MISSING" in table
    assert "bull_put_spread" in table


# -- end to end, through the real command ------------------------------------------------------
@pytest.fixture(scope="module")
def built_pack(tmp_path_factory) -> tuple[Path, subprocess.CompletedProcess[str]]:
    """A real ledger written by the real engine, then the real script run over it as a process."""
    ledger_dir = tmp_path_factory.mktemp("ledger")
    out_dir = tmp_path_factory.mktemp("pack")
    tenant = SqliteLedger(root_dir=ledger_dir).for_tenant("tenant-a")
    for _ in range(3):
        append_engine_record(tenant, recorded_at=FIXED_NOW)
    completed = subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "--ledger",
            str(ledger_dir),
            "--out",
            str(out_dir),
            "--no-broker",
        ],
        cwd=str(REPO_ROOT),
        capture_output=True,
        text=True,
        timeout=600,
    )
    return out_dir, completed


def test_the_bundle_is_written_and_reports_the_broker_as_unavailable(built_pack) -> None:
    out_dir, completed = built_pack
    assert completed.returncode == pack.EXIT_BROKER_UNAVAILABLE, completed.stdout + completed.stderr
    assert (out_dir / "SUMMARY.md").is_file()
    assert (out_dir / "pack.json").is_file()
    assert (out_dir / "audit-trail-tenant-a.jsonl").is_file()
    assert (out_dir / "audit-trail-tenant-a.txt").is_file()
    assert (out_dir / "verify-chain.txt").is_file()
    assert (out_dir / "replay.txt").is_file()


def test_the_exported_chain_verifies_on_its_own(built_pack) -> None:
    """The export must BE the ledger, not a description of it: the same bytes, so the same hashes."""
    out_dir, _ = built_pack
    verified = subprocess.run(
        [sys.executable, "-m", "mizan.audit.verify_chain", str(out_dir / "audit-trail-tenant-a.jsonl")],
        cwd=str(REPO_ROOT),
        capture_output=True,
        text=True,
        timeout=300,
    )
    assert verified.returncode == 0, verified.stdout + verified.stderr
    assert "CHAIN VERIFIED" in verified.stdout


def test_both_proofs_pass_and_are_recorded_in_the_bundle(built_pack) -> None:
    out_dir, _ = built_pack
    report = json.loads((out_dir / "pack.json").read_text(encoding="utf-8"))
    assert report["verify"]["ok"] is True
    assert report["replay"]["ok"] is True
    assert "reproduced identically" in report["replay"]["headline"]
    assert report["chain"]["decision_records"] == 3


def test_the_summary_says_the_account_was_not_read_rather_than_inventing_one(built_pack) -> None:
    out_dir, _ = built_pack
    summary = (out_dir / "SUMMARY.md").read_text(encoding="utf-8")
    assert "THE ACCOUNT WAS NOT READ LIVE" in summary
    assert "--no-broker" in summary


def test_the_summary_never_annualises_extrapolates_or_claims_alpha(built_pack) -> None:
    """The only place these words may appear is in a sentence disclaiming them."""
    out_dir, _ = built_pack
    summary = (out_dir / "SUMMARY.md").read_text(encoding="utf-8").lower()
    for word in ("annualis", "annualiz", "alpha", "extrapolat", "sharpe", "cagr", "apy"):
        for line in summary.splitlines():
            if word in line:
                assert " not " in line or "does not" in line, f"unqualified {word!r}: {line}"


def test_the_summary_carries_the_limitations_a_judge_should_know(built_pack) -> None:
    out_dir, _ = built_pack
    summary = (out_dir / "SUMMARY.md").read_text(encoding="utf-8")
    assert "does not prove" in summary
    assert "OPRA" in summary
    assert "no close, cancel or replace path" in summary


def test_the_bundle_holds_no_credential(built_pack) -> None:
    """Nothing in a shareable bundle may look like a key. The account ID is not one; a key is."""
    out_dir, _ = built_pack
    for path in out_dir.iterdir():
        text = path.read_text(encoding="utf-8", errors="replace")
        for marker in ("APCA_API_SECRET_KEY=", "ALPACA_SECRET_KEY=", "secret_key", "api_secret"):
            assert marker not in text, f"{path.name} mentions {marker}"


# -- the read-only guarantee -------------------------------------------------------------------
def test_the_evidence_pack_contains_no_broker_mutation_call() -> None:
    """B4 again, on the second script. Three reads, and provably no fourth call that writes."""
    source = SCRIPT.read_text(encoding="utf-8")
    for name in (
        "cancel_order",
        "cancel_orders",
        "close_position",
        "close_all_positions",
        "replace_order",
        "submit_order",
    ):
        assert f".{name}(" not in source, f"evidence_pack.py calls {name}; it must only read"


def test_neither_script_can_turn_paper_mode_off() -> None:
    """The one configuration line that must never appear anywhere in this repository."""
    for script in (SCRIPT, REPO_ROOT / "scripts" / "position_monitor.py"):
        text = script.read_text(encoding="utf-8")
        assert "ALPACA_PAPER=false" not in text
        assert 'ALPACA_PAPER"] =' not in text
        assert "setenv" not in text
