#!/usr/bin/env python
"""Produce the demo transcript: one timestamped, self-checking, credential-free run.

    python scripts/demo_transcript.py

Three sections, in this order, because that is the order the story is told in:

1. **What Mizan takes away.** Alpaca's official MCP server is started and asked what it can do.
   Seven of the tools it offers liquidate an account or destroy a working order, and Mizan's client
   cannot send any of them. Then one of the seven is actually attempted, and is refused.
2. **Two policies, one instant, opposite verdicts.** The same proposal, against the same Alpaca
   market snapshot, at the same recorded timestamp: the policy that carries greek limits REJECTs with
   ``GREEKS_MISSING``; the defined-risk policy APPROVEs. Both are recorded decisions from the live
   run, and both re-derive bit for bit.
3. **The whole ledger replays, with no credentials.** The hash chain is verified and every recorded
   decision is recomputed from its own record.

**This script is read-only and needs no Alpaca key.** It places nothing, cancels nothing and closes
nothing - there is no code path here that could. Section 1 starts Alpaca's server with a placeholder
key on purpose, so that the refusal is visibly not an artefact of having no credentials.

Every section states what it expects before it runs, and the script exits non-zero if any expectation
is not met, so a transcript that ends ``ALL SECTIONS PASSED`` is a transcript that was checked rather
than merely captured.

The ledger is opened read-only and only to *find* the pair of decisions section 2 contrasts; every
claim in the transcript is then made by a shipped command, not by this file.

Exit status
    0   every section passed
    1   a section did not meet its expectation (the transcript says which)
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import sqlite3
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
CLI = [sys.executable, str(REPO_ROOT / "scripts" / "mizan_cli.py")]

#: The seven tools on Alpaca's official server that end a position or destroy a working order. The
#: other eight names on Mizan's denylist (watchlists, account config, crypto, locates) are refused
#: too, but these seven are the ones that can empty an account with no decision recorded anywhere.
DESTRUCTIVE_TOOLS = (
    "cancel_all_orders",
    "cancel_order_by_id",
    "replace_order_by_id",
    "close_all_positions",
    "close_position",
    "exercise_options_position",
    "do_not_exercise_options_position",
)

_ANSI = re.compile(r"\x1b\[[0-9;]*m")


class Transcript:
    """Writes to the terminal and to the file at once; the file gets no ANSI escapes."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._handle = self.path.open("w", encoding="utf-8", newline="\n")
        self.failures: list[str] = []

    def write(self, text: str = "") -> None:
        print(text)
        self._handle.write(_ANSI.sub("", text) + "\n")
        self._handle.flush()

    def rule(self, title: str) -> None:
        self.write("")
        self.write("=" * 96)
        self.write(f"{title}    [{_now()}]")
        self.write("=" * 96)

    def expect(self, statement: str) -> None:
        self.write(f"EXPECT   {statement}")
        self.write("")

    def verdict(self, label: str, ok: bool, detail: str = "") -> None:
        mark = "PASS" if ok else "FAIL"
        self.write("")
        self.write(f"{mark}     {label}{('  -  ' + detail) if detail else ''}")
        if not ok:
            self.failures.append(f"{label}{(': ' + detail) if detail else ''}")

    def close(self) -> None:
        self._handle.close()


def _now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _stamp() -> str:
    return datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")


def run(transcript: Transcript, argv: list[str], *, timeout: float = 300.0) -> tuple[int, str]:
    """Run a command, echo it and its whole output into the transcript, return (exit code, output)."""
    shown = " ".join(["python" if part == sys.executable else _short(part) for part in argv])
    transcript.write(f"$ {shown}")
    try:
        proc = subprocess.run(  # noqa: S603 - fixed argv, no shell
            argv,
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired:
        transcript.write(f"  (timed out after {timeout:.0f}s)")
        return 124, ""
    except OSError as failure:
        transcript.write(f"  (could not start: {failure})")
        return 127, ""
    output = ((proc.stdout or "") + (proc.stderr or "")).rstrip()
    for line in output.splitlines():
        transcript.write("  " + line)
    transcript.write(f"  [exit {proc.returncode}]")
    return proc.returncode, _ANSI.sub("", output)


def _short(part: str) -> str:
    """Print repo-relative paths, so the transcript is not full of one machine's directory layout."""
    try:
        return str(Path(part).resolve().relative_to(REPO_ROOT)).replace("\\", "/")
    except (ValueError, OSError):
        return part


# ---------------------------------------------------------------------------------------------------
# Section 1 - the capabilities Mizan removes
# ---------------------------------------------------------------------------------------------------
def section_destructive_tools(transcript: Transcript, *, timeout: float) -> None:
    transcript.rule("SECTION 1 of 3   Alpaca's official MCP server, and the seven tools Mizan cannot send")
    transcript.write(
        "Alpaca's own MCP server is a separate process, started here from its pinned package. It is"
    )
    transcript.write(
        "asked what it can do. No credential is needed to ask, and none is used: the point of this"
    )
    transcript.write(
        "section is that the refusal below is a property of Mizan's client, not of a missing key."
    )
    transcript.write("")
    transcript.write("The seven that end a position or destroy a working order:")
    for name in DESTRUCTIVE_TOOLS:
        transcript.write(f"  - {name}")
    transcript.write("")
    transcript.expect(
        "every one of those seven is listed DENY, and the attempted call is refused before it is sent"
    )

    code, output = run(transcript, [*CLI, "mcp-tools", "--server", "alpaca"], timeout=timeout)
    denied = {line.split("DENY", 1)[1].strip() for line in output.splitlines() if "DENY" in line}
    missing = [name for name in DESTRUCTIVE_TOOLS if name not in denied]
    transcript.verdict(
        "all seven destructive tools are on the denylist",
        code == 0 and not missing,
        "not shown as DENY: " + ", ".join(missing) if missing else f"exit {code}",
    )

    transcript.write("")
    transcript.write(
        "Listing them is cheap. Now one of the seven is actually attempted, against the real server:"
    )
    transcript.write("")
    code, output = run(
        transcript,
        [*CLI, "mcp-call", "close_all_positions", "--no-credentials"],
        timeout=timeout,
    )
    transcript.verdict(
        "close_all_positions is refused by the client, before a byte reaches Alpaca",
        code == 2 and "DENIED" in output,
        f"exit {code} (2 == denied)",
    )


# ---------------------------------------------------------------------------------------------------
# Section 2 - two policies, one instant
# ---------------------------------------------------------------------------------------------------
def _records(ledger: Path) -> list[dict]:
    """Read the tenant ledger read-only, to LOCATE the contrast. Nothing here is a claim."""
    database = next(iter(sorted(ledger.glob("*.sqlite"))), None)
    if database is None:
        return []
    connection = sqlite3.connect(f"file:{database.as_posix()}?mode=ro", uri=True)
    try:
        rows = connection.execute(
            "select sequence, record_json from decision_records order by sequence"
        ).fetchall()
    finally:
        connection.close()
    found = []
    for sequence, payload in rows:
        record = json.loads(payload)
        decision = record["governor_decision"]
        market = next(
            (c for c in record["checks"] if c["check_id"] == "market_data_presence"), {}
        )
        found.append(
            {
                "sequence": sequence,
                "decision_id": record["decision_id"],
                "timestamp": record["decision_timestamp"],
                "proposal_id": decision["proposal_id"],
                "policy_id": decision["policy"]["policy_id"],
                "policy_version": decision["policy"]["version"],
                "policy_hash": decision["policy"]["hash"],
                "verdict": decision["verdict"],
                "reason_codes": decision["reason_codes"],
                "market_detail": market.get("detail", ""),
                "market_source": market.get("data_source", ""),
            }
        )
    return found


def _find_contrast(records: list[dict]) -> tuple[dict, dict] | None:
    """The pair: one proposal, one instant, a greeks REJECT and a defined-risk APPROVE."""
    by_proposal: dict[str, list[dict]] = {}
    for record in records:
        by_proposal.setdefault(record["proposal_id"], []).append(record)
    for group in by_proposal.values():
        rejected = next(
            (r for r in group if r["verdict"] == "REJECT" and "GREEKS_MISSING" in r["reason_codes"]),
            None,
        )
        approved = next((r for r in group if r["verdict"] == "APPROVE"), None)
        if rejected and approved and rejected["timestamp"] == approved["timestamp"]:
            return rejected, approved
    return None


def section_two_policies(
    transcript: Transcript, ledger: Path, relative_ledger: Path, *, timeout: float
) -> None:
    transcript.rule("SECTION 2 of 3   Two policies, one proposal, one instant, opposite verdicts")
    transcript.write(
        "These are not fixtures. Both decisions below were taken during the live paper run, against"
    )
    transcript.write(
        "Alpaca's own quotes, and both are links in the hash chain. The proposal is identical, the"
    )
    transcript.write(
        "market snapshot is identical, the timestamp is identical. Only the policy differs."
    )
    transcript.write("")
    transcript.expect(
        "the greek-limit policy REJECTs with GREEKS_MISSING; the defined-risk policy APPROVEs; both "
        "re-derive bit for bit"
    )

    records = _records(ledger)
    pair = _find_contrast(records)
    if pair is None:
        transcript.write(f"  (no contrasting pair found in {relative_ledger.as_posix()})")
        transcript.verdict("the two-policy contrast is present in the ledger", False, "no pair found")
        return
    rejected, approved = pair

    transcript.write("  the two recorded decisions, side by side")
    transcript.write("")
    labels = (
        ("sequence", "sequence"),
        ("decision id", "decision_id"),
        ("decision timestamp", "timestamp"),
        ("proposal id", "proposal_id"),
        ("policy", None),
        ("policy hash", "policy_hash"),
        ("market data", "market_detail"),
        ("data source", "market_source"),
        ("VERDICT", "verdict"),
        ("reason codes", None),
    )
    for label, key in labels:
        if key is None and label == "policy":
            left = f"{rejected['policy_id']} v{rejected['policy_version']}"
            right = f"{approved['policy_id']} v{approved['policy_version']}"
        elif key is None:
            left = ", ".join(rejected["reason_codes"]) or "(none)"
            right = ", ".join(approved["reason_codes"]) or "(none)"
        else:
            left, right = str(rejected[key]), str(approved[key])
        if key == "policy_hash":
            left, right = left[:16] + "...", right[:16] + "..."
        transcript.write(f"    {label:<20} {left}")
        transcript.write(f"    {'':<20} {right}")
        transcript.write("")

    same_proposal = rejected["proposal_id"] == approved["proposal_id"]
    same_instant = rejected["timestamp"] == approved["timestamp"]
    same_market = rejected["market_detail"] == approved["market_detail"]
    different_policy = rejected["policy_hash"] != approved["policy_hash"]
    transcript.write(
        f"    same proposal id {same_proposal} | same instant {same_instant} | "
        f"same market snapshot {same_market} | different policy hash {different_policy}"
    )
    transcript.verdict(
        "one proposal, one market snapshot, one instant, two policies, two verdicts",
        same_proposal and same_instant and same_market and different_policy,
        f"{rejected['verdict']} under {rejected['policy_id']}, "
        f"{approved['verdict']} under {approved['policy_id']}",
    )

    transcript.write("")
    transcript.write("Neither verdict is taken on trust. Each is recomputed from its own record:")
    transcript.write("")
    # These records were decided by whichever engine was running when they were written, which is not
    # necessarily this one. Asserting a bit-for-bit hash match regardless would make an honest version
    # bump look like a failed demo - and, worse, would tempt someone to hold the version still to keep
    # the demo green, which is precisely the bug this build just spent a day removing. So the claim
    # adapts to the truth: identical under the same engine, and under a different one, the same
    # VERDICT with the difference attributed to the version by name.
    all_identical = True
    all_sound = True
    for record in (rejected, approved):
        code, output = run(
            transcript,
            [*CLI, "replay", record["decision_id"], "--ledger", relative_ledger.as_posix()],
            timeout=timeout,
        )
        identical = code == 0 and "identical        True" in output
        attributed = (
            "engine matches   False" in output
            and "ENGINE VERSION MISMATCH" in output
            and f"{record['verdict']} -> {record['verdict']}" in output
        )
        all_identical &= identical
        all_sound &= identical or attributed
    if all_identical:
        transcript.verdict(
            "both decisions re-derive bit for bit from the record alone", True
        )
    else:
        transcript.verdict(
            "both decisions re-derive the same verdict from the record alone, and the hash "
            "difference is attributed to the engine version rather than to the records",
            all_sound,
        )

    transcript.write("")
    transcript.write(
        "And the counterfactual, which is the same question asked the other way round: what would"
    )
    transcript.write(
        "the REJECTED proposal have received under the other policy? The engine answers from the"
    )
    transcript.write("record - same proposal, same market data, different policy:")
    transcript.write("")
    code, output = run(
        transcript,
        [
            *CLI,
            "replay",
            rejected["decision_id"],
            "--ledger",
            relative_ledger.as_posix(),
            "--under-policy",
            "policies/options-defined-risk.yaml",
        ],
        timeout=timeout,
    )
    transcript.verdict(
        "replayed under the defined-risk policy, the same recorded proposal APPROVEs",
        code == 0 and "REJECT -> APPROVE" in output,
        f"exit {code}",
    )


# ---------------------------------------------------------------------------------------------------
# Section 3 - the whole ledger, credential-free
# ---------------------------------------------------------------------------------------------------
def section_replay(
    transcript: Transcript, ledger: Path, relative_ledger: Path, *, timeout: float
) -> None:
    transcript.rule("SECTION 3 of 3   The whole chain verified and every decision replayed, no credentials")
    transcript.write(
        "Nothing below reads an account, opens a socket or needs a key. Anyone handed the .sqlite"
    )
    transcript.write(
        "file and this repository gets the same two answers - which is the point, because evidence"
    )
    transcript.write("that needs our cooperation to check is not evidence.")
    transcript.write("")
    expected = len(_records(ledger))
    transcript.expect(
        f"the chain verifies over all {expected} links; a ledger built here and now replays "
        f"bit-for-bit; and any difference against the shipped records is attributed to the engine "
        f"version rather than reported as tampering"
    )

    code, output = run(
        transcript,
        [
            sys.executable,
            "-m",
            "mizan.audit.verify_chain",
            (relative_ledger / "tenant-a.sqlite").as_posix(),
        ],
        timeout=timeout,
    )
    transcript.verdict(
        "the hash chain verifies offline",
        code == 0 and "CHAIN VERIFIED" in output,
        f"exit {code}",
    )

    # A replay that only ever runs against a ledger WE shipped proves less than one the viewer
    # generates on the spot. This builds a fresh chain with no credentials and no network, then
    # re-derives every decision in it - evidence that needs our cooperation to check is not evidence.
    transcript.write("")
    seeded = Path("evidence/demo-ledger")
    if seeded.exists():
        shutil.rmtree(seeded)
    run(
        transcript,
        [sys.executable, "examples/seed_ledger.py", "--out", seeded.as_posix()],
        timeout=timeout,
    )
    code, output = run(
        transcript,
        [sys.executable, "-m", "mizan.replay", "--ledger", seeded.as_posix(), "--assert-identical"],
        timeout=timeout,
    )
    seeded_count = len(_records(seeded))
    headline = f"{seeded_count}/{seeded_count} decisions reproduced identically"
    transcript.verdict(
        f"a ledger built moments ago replays {headline}, credential-free",
        code == 0 and headline in output,
        f"exit {code}",
    )

    # The shipped records were decided by an EARLIER engine. Replaying them here must not be reported
    # as an integrity failure - "the engine changed" and "the ledger was altered" demand opposite
    # responses, and a tool that says the same thing for both cries wolf about fraud. Whichever case
    # this run is in, the transcript has to be right about which one it is.
    transcript.write("")
    code, output = run(
        transcript,
        [sys.executable, "-m", "mizan.replay", "--ledger", relative_ledger.as_posix()],
        timeout=timeout,
    )
    differing = output.count("MISMATCH decision=")
    version_explained = output.count("ENGINE VERSION MISMATCH")
    if code == 0:
        headline = f"{expected}/{expected} decisions reproduced identically"
        transcript.verdict(
            f"the {expected} shipped records replay {headline}, credential-free",
            headline in output,
            f"exit {code}",
        )
    else:
        transcript.verdict(
            f"all {differing} differences against the shipped records are attributed to the engine "
            f"version, not reported as tampering",
            differing > 0 and differing == version_explained,
            f"exit {code}; {version_explained}/{differing} carry the version explanation",
        )


# ---------------------------------------------------------------------------------------------------
def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python scripts/demo_transcript.py",
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--ledger",
        type=Path,
        default=Path("evidence/live-ledger"),
        help="the ledger to verify and replay (default: evidence/live-ledger)",
    )
    parser.add_argument(
        "--out",
        type=Path,
        default=Path("evidence"),
        help="directory the transcript is written to (default: evidence/, which is gitignored)",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=300.0,
        help="seconds any one command may take (default: 300)",
    )
    parser.add_argument(
        "--skip-mcp",
        action="store_true",
        help="skip section 1, which starts Alpaca's server and may need to fetch its package",
    )
    arguments = parser.parse_args(argv)

    ledger = (REPO_ROOT / arguments.ledger).resolve()
    out_dir = (REPO_ROOT / arguments.out).resolve()
    # Commands are run with cwd=REPO_ROOT and handed a repo-relative ledger path, so the transcript
    # reads the same on every machine instead of quoting one checkout's directory layout.
    relative_ledger = Path(_short(str(ledger)))
    stamp = _stamp()
    transcript = Transcript(out_dir / f"demo-transcript-{stamp}.txt")

    transcript.write("MIZAN - demo transcript")
    transcript.write(f"generated     {_now()}")
    transcript.write(f"repository    {REPO_ROOT.name} @ {_git_head()}")
    transcript.write(f"python        {sys.version.split()[0]}")
    transcript.write(f"ledger        {relative_ledger.as_posix()}")
    transcript.write("credentials   none used; this run is read-only and places no order")
    transcript.write("")
    transcript.write(
        "Three sections: what Mizan takes away, what the policy decides, and what anyone can check."
    )

    if arguments.skip_mcp:
        transcript.rule("SECTION 1 of 3   SKIPPED (--skip-mcp)")
    else:
        section_destructive_tools(transcript, timeout=arguments.timeout)
    section_two_policies(transcript, ledger, relative_ledger, timeout=arguments.timeout)
    section_replay(transcript, ledger, relative_ledger, timeout=arguments.timeout)

    transcript.rule("RESULT")
    if transcript.failures:
        transcript.write(f"{len(transcript.failures)} SECTION CHECK(S) FAILED:")
        for failure in transcript.failures:
            transcript.write(f"  - {failure}")
        status = 1
    else:
        transcript.write("ALL SECTIONS PASSED")
        status = 0
    transcript.write("")
    transcript.write(f"transcript    {_short(str(transcript.path))}")
    transcript.close()

    latest = out_dir / "demo-transcript.txt"
    latest.write_text(transcript.path.read_text(encoding="utf-8"), encoding="utf-8")
    print(f"\nalso written to {_short(str(latest))}")
    return status


def _git_head() -> str:
    try:
        proc = subprocess.run(  # noqa: S603 - fixed argv, no shell
            ["git", "rev-parse", "--short", "HEAD"],  # noqa: S607
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return "unknown"
    return (proc.stdout or "").strip() or "unknown"


if __name__ == "__main__":
    raise SystemExit(main())
