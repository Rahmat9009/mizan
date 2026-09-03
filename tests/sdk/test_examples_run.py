"""The examples are executable documentation, so CI runs them.

An example that has quietly stopped working is worse than no example: it is the first thing a
prospective user runs, and the killer demo is the first thing an audience sees. Both are executed here
as subprocesses - the way a reader will run them - and their output is checked for the claims they
make and for the things they must never print (F-16: no local paths, no raw exception text, no return
figures).
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

import pytest

EXAMPLES = Path(__file__).resolve().parents[2] / "examples"
SCRIPTS = ("tradingagents_ten_lines.py", "killer_demo.py")

#: Things an example must never print. A local path or a vendor traceback in a demo is a disclosure
#: rehearsal; a return figure is a claim the company does not make (B5/B6).
FORBIDDEN_OUTPUT = (
    "Traceback",
    "site-packages",
    "C:\\Users",
    "/home/",
    "sqlite",
    "% return",
    "profit",
    "ALPACA_SECRET",
)


def run(script: str) -> str:
    result = subprocess.run(
        [sys.executable, str(EXAMPLES / script)],
        capture_output=True,
        text=True,
        timeout=180,
        cwd=str(EXAMPLES),
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert result.stderr.strip() == "", result.stderr
    return result.stdout


@pytest.mark.parametrize("script", SCRIPTS)
def test_the_example_runs_cleanly_and_discloses_nothing(script):
    output = run(script)
    assert output.strip()
    for forbidden in FORBIDDEN_OUTPUT:
        assert forbidden not in output, (script, forbidden)


def test_the_ten_line_example_really_is_ten_lines():
    """The W8 promise is a number, so it is asserted as one."""
    source = (EXAMPLES / "tradingagents_ten_lines.py").read_text(encoding="utf-8").splitlines()
    banners = [index for index, line in enumerate(source) if line.strip().startswith("# ====")]
    assert len(banners) >= 2, "the integration block must be delimited by two banner comments"
    start, end = banners[-2], banners[-1]
    body = [
        line
        for line in source[start + 1 : end]
        if line.strip() and not line.strip().startswith("#")
    ]
    marker = re.compile(r"#\s*(\d{1,2})")
    numbered: list[int] = []
    for line in body:
        found = marker.search(line)
        if found is not None:
            numbered.append(int(found.group(1)))
    assert sorted(numbered) == list(range(1, 11)), [line.strip() for line in body]


def test_the_ten_line_example_governs_and_never_submits_in_dry_run():
    output = run("tradingagents_ten_lines.py")
    assert "APPROVE" in output
    assert "chain verified    True" in output
    assert "replay identical  True" in output
    assert "orders submitted  0" in output


def test_the_killer_demo_prints_every_beat_of_master_plan_section_11():
    output = run("killer_demo.py")

    # 1: the oversized order is refused, with the reason and the numbers behind it
    assert "MIZAN -> REJECT" in output
    assert "OPTIONS_DELTA_LIMIT_EXCEEDED" in output
    assert "options_delta_limit" in output and "vs maximum 500" in output
    # 2: the revised order is approved and authorized, with an expiry
    assert "MIZAN -> APPROVE" in output
    assert "authorization    expires" in output and "paper" in output
    # 3: it executes to a paper broker, and asking twice yields one order
    assert "status           SUBMITTED" in output
    assert "RECONCILED_EXISTING" in output and "one order, not two" in output
    # 4: replay is identical
    assert "identical        True" in output
    # 5: the policy change flips the verdict on the same recorded inputs
    assert "old policy       APPROVE" in output
    assert "new policy       REJECT" in output
    # 6: the kill switch stops execution at the mutation boundary
    assert "KILL_SWITCH_ACTIVE" in output
    assert "nothing new reached the venue" in output
    # 7: the adversarial transcript changes nothing
    assert "same verdict     True" in output
    assert "same hash        True" in output
    # evidence
    assert "chain            ok=True" in output
    assert "orders placed    1 (paper)" in output


def test_no_example_can_express_live_trading():
    """B1, at the surface a user copies from: nothing in examples/ names a live path."""
    for path in sorted(EXAMPLES.glob("*.py")):
        text = path.read_text(encoding="utf-8")
        assert "paper=False" not in text, path.name
        assert "ALPACA_PAPER=false" not in text, path.name
        for line in text.splitlines():
            assert "api.alpaca.markets" not in line or "paper-api.alpaca.markets" in line, path.name
