"""The CLI, and the property that makes it worth having: it is not a second implementation.

Every subcommand dispatches to ``MizanMCPServer.call_tool`` - the same handler an MCP client reaches
over stdio - so the two surfaces cannot drift apart. The tests below assert that equivalence directly,
then cover the parts that are genuinely the CLI's own: leg parsing, strategy inference, exit codes,
and the refusal to take its configuration from the environment.
"""

from __future__ import annotations

import json
import runpy
import sys
from pathlib import Path
from typing import Any

import pytest

from mizan.contracts.errors import ConfigurationError
from mizan.mcp.server import MizanMCPServer
from mizan.mcp.session import SessionConfig

REPO_ROOT = Path(__file__).resolve().parents[2]
CLI_PATH = REPO_ROOT / "scripts" / "mizan_cli.py"
POLICY = REPO_ROOT / "policies" / "options-conservative.yaml"

LEG = "side=buy,qty=10,limit=1.85,type=call,strike=230,expiry=2026-09-25"


@pytest.fixture(scope="module")
def cli() -> Any:
    """The CLI loaded as a module. ``runpy`` keeps it a script that a judge can copy and paste."""
    if str(REPO_ROOT) not in sys.path:
        sys.path.insert(0, str(REPO_ROOT))
    return runpy.run_path(str(CLI_PATH), run_name="mizan_cli_under_test")


def run(cli: Any, argv: list[str], capsys: pytest.CaptureFixture[str]) -> tuple[int, str]:
    code = cli["main"](argv)
    return code, capsys.readouterr().out


class TestLegParsing:
    def test_a_full_option_leg_parses(self, cli: Any) -> None:
        leg = cli["parse_leg"](0, LEG)
        assert leg == {
            "leg_index": 0,
            "order_type": "limit",
            "side": "buy",
            "quantity": "10",
            "limit_price": "1.85",
            "contract_type": "call",
            "strike": "230",
            "expiry": "2026-09-25",
        }

    def test_type_names_the_CONTRACT_not_the_order(self, cli: Any) -> None:
        """An option leg is a call or a put. ``order_type`` stays separate and explicit."""
        assert cli["parse_leg"](0, "side=sell,qty=1,type=put,strike=200,expiry=2026-09-25")[
            "contract_type"
        ] == "put"

    def test_a_leg_with_no_limit_becomes_a_market_order_rather_than_an_unpriced_limit(
        self, cli: Any
    ) -> None:
        assert cli["parse_leg"](0, "side=buy,qty=5")["order_type"] == "market"

    def test_an_unknown_key_is_refused_rather_than_ignored(self, cli: Any) -> None:
        with pytest.raises(SystemExit, match="unknown key"):
            cli["parse_leg"](0, "side=buy,qty=1,slippage=0.5")

    def test_a_leg_missing_side_or_quantity_is_refused(self, cli: Any) -> None:
        with pytest.raises(SystemExit, match="required"):
            cli["parse_leg"](0, "qty=1")

    def test_a_pair_without_an_equals_sign_is_refused(self, cli: Any) -> None:
        with pytest.raises(SystemExit, match="not key=value"):
            cli["parse_leg"](0, "side=buy,nonsense")


class TestStrategyInference:
    def test_a_single_bought_call_is_a_long_call(self, cli: Any) -> None:
        legs = [cli["parse_leg"](0, LEG)]
        assert cli["_infer_strategy"](legs, is_option=True) == "long_call"

    def test_a_multi_leg_structure_is_never_guessed(self, cli: Any) -> None:
        """Guessing a spread's label would be guessing its RISK SHAPE; ``custom`` makes the engine
        derive it from the legs, which is what refuses an undefined-risk structure by name."""
        legs = [cli["parse_leg"](0, LEG), cli["parse_leg"](1, LEG.replace("buy", "sell"))]
        assert cli["_infer_strategy"](legs, is_option=True) == "custom"

    def test_equity_sides_map_to_the_two_equity_strategies(self, cli: Any) -> None:
        assert cli["_infer_strategy"]([{"side": "buy"}], is_option=False) == "long_equity"
        assert cli["_infer_strategy"]([{"side": "sell"}], is_option=False) == "short_equity"


class TestTheCliIsNotASecondImplementation:
    def test_the_cli_and_the_mcp_tool_return_the_same_decision_for_the_same_input(
        self, cli: Any, capsys: pytest.CaptureFixture[str]
    ) -> None:
        argv = ["evaluate", "--symbol", "AAPL", "--leg", LEG.replace("qty=10", "qty=50"), "--json"]
        code, printed = run(cli, argv, capsys)
        via_cli = json.loads(printed)

        server = MizanMCPServer(SessionConfig(policy_path=POLICY, broker="mock"))
        try:
            result = server.call_tool(
                "evaluate_proposal",
                {
                    "symbol": "AAPL",
                    "asset_class": "equity_option",
                    "strategy": "long_call",
                    "legs": [cli["parse_leg"](0, LEG.replace("qty=10", "qty=50"))],
                    "reasoning": "",
                },
            )
        finally:
            server.close()
        via_mcp = json.loads(result["content"][0]["text"])

        assert code == 3  # a refusal is neither a crash nor a success
        assert via_cli["verdict"] == via_mcp["verdict"] == "REJECT"
        assert via_cli["reason_codes"] == via_mcp["reason_codes"]
        # The decision id and audit hash differ (different ledgers); everything decided is identical.
        assert via_cli["verdict_hash"] == via_mcp["verdict_hash"]


class TestCommands:
    def test_doctor_reports_what_is_wired_up_without_touching_a_network(
        self, cli: Any, capsys: pytest.CaptureFixture[str]
    ) -> None:
        code, printed = run(cli, ["doctor", "--json"], capsys)
        payload = json.loads(printed)
        assert code == 0
        assert payload["policy"]["policy_id"] == "options-conservative"
        assert any("explicit flags only" in note for note in payload["notes"])

    def test_an_approved_proposal_exits_zero(self, cli: Any, capsys: pytest.CaptureFixture[str]) -> None:
        code, _ = run(cli, ["evaluate", "--symbol", "AAPL", "--leg", LEG], capsys)
        assert code == 0

    def test_a_refusal_exits_three(self, cli: Any, capsys: pytest.CaptureFixture[str]) -> None:
        code, _ = run(cli, ["evaluate", "--symbol", "AAPL", "--leg", LEG.replace("qty=10", "qty=50")], capsys)
        assert code == 3

    def test_submit_without_live_stops_at_the_mutation_boundary(
        self, cli: Any, capsys: pytest.CaptureFixture[str]
    ) -> None:
        code, printed = run(cli, ["submit", "--symbol", "AAPL", "--leg", LEG, "--json"], capsys)
        assert code == 0
        assert json.loads(printed)["execution"]["status"] == "WOULD_SUBMIT"

    def test_submit_with_live_reaches_the_broker(
        self, cli: Any, capsys: pytest.CaptureFixture[str]
    ) -> None:
        code, printed = run(cli, ["submit", "--symbol", "AAPL", "--leg", LEG, "--live", "--json"], capsys)
        assert code == 0
        assert json.loads(printed)["execution"]["status"] == "SUBMITTED"

    def test_a_global_flag_works_after_the_subcommand(
        self, cli: Any, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """``mizan submit --broker mock --live`` is what a person types; argparse alone would refuse."""
        code, printed = run(cli, ["evaluate", "--symbol", "AAPL", "--leg", LEG, "--broker", "mock",
                                 "--json"], capsys)
        assert code == 0
        assert json.loads(printed)["verdict"] == "APPROVE"

    def test_verify_chain_and_replay_work_over_a_persisted_ledger(
        self, cli: Any, capsys: pytest.CaptureFixture[str], tmp_path: Path
    ) -> None:
        ledger = ["--ledger", str(tmp_path / "ledger")]
        run(cli, ["evaluate", "--symbol", "AAPL", "--leg", LEG, *ledger], capsys)

        code, printed = run(cli, ["verify-chain", *ledger, "--json"], capsys)
        assert code == 0 and json.loads(printed)["ok"] is True

        code, printed = run(cli, ["decisions", *ledger, "--json"], capsys)
        decision_id = json.loads(printed)["decisions"][0]["decision_id"]

        code, printed = run(cli, ["replay", decision_id, *ledger, "--json"], capsys)
        assert code == 0
        assert json.loads(printed)["identical"] is True

    def test_replaying_under_a_different_policy_is_not_a_failure(
        self, cli: Any, capsys: pytest.CaptureFixture[str], tmp_path: Path
    ) -> None:
        """A differing verdict under different rules is the ANSWER to the question that was asked."""
        ledger = ["--ledger", str(tmp_path / "ledger")]
        run(cli, ["evaluate", "--symbol", "AAPL", "--leg", LEG, *ledger], capsys)
        _, printed = run(cli, ["decisions", *ledger, "--json"], capsys)
        decision_id = json.loads(printed)["decisions"][0]["decision_id"]

        code, printed = run(
            cli,
            ["replay", decision_id, *ledger, "--under-policy",
             str(REPO_ROOT / "policies" / "options-defined-risk.yaml"), "--json"],
            capsys,
        )
        assert code == 0
        assert json.loads(printed)["mode"] == "policy"

    def test_mizans_own_tool_list_is_printable_with_no_broker_at_all(
        self, cli: Any, capsys: pytest.CaptureFixture[str]
    ) -> None:
        code, printed = run(cli, ["mcp-tools"], capsys)
        assert code == 0
        assert "submit_governed_order" in printed

    def test_a_proposal_with_no_legs_is_refused_before_anything_is_governed(self, cli: Any) -> None:
        with pytest.raises(SystemExit, match="--leg"):
            cli["main"](["evaluate", "--symbol", "AAPL"])

    def test_a_proposal_with_no_symbol_is_refused(self, cli: Any) -> None:
        with pytest.raises(SystemExit, match="--symbol"):
            cli["main"](["evaluate", "--leg", LEG])


class TestConfigurationIsExplicit:
    def test_the_policy_is_never_taken_from_the_environment(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The policy file decides WHICH CHECKS RUN. An inherited value would not appear in the
        command anyone typed, so a parent process could change what a run enforces invisibly."""
        monkeypatch.setenv("MIZAN_POLICY_PATH", "/nowhere/attacker.yaml")
        assert SessionConfig.resolve().policy_path.name == "options-conservative.yaml"

    def test_the_broker_is_never_taken_from_the_environment(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("MIZAN_BROKER", "alpaca-py")
        assert SessionConfig.resolve().broker == "mock"

    def test_the_tenant_and_ledger_are_never_taken_from_the_environment(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("MIZAN_TENANT_ID", "someone-else")
        monkeypatch.setenv("MIZAN_LEDGER_DIR", "/nowhere")
        config = SessionConfig.resolve()
        assert config.tenant_id == "tenant-a"
        assert config.ledger_dir is None

    def test_an_unknown_setting_is_an_error_not_a_shrug(self) -> None:
        with pytest.raises(ConfigurationError, match="Unknown session setting"):
            SessionConfig.resolve(polcy_path="typo")

    def test_an_unknown_broker_is_refused(self) -> None:
        with pytest.raises(ConfigurationError, match="Unknown broker"):
            SessionConfig.resolve(broker="whatever-exchange")

    def test_dry_run_is_the_default_so_the_gate_stops_short_unless_asked(self) -> None:
        assert SessionConfig.resolve().dry_run is True
