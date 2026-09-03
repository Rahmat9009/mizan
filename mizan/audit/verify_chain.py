"""The offline ledger verifier a customer runs without us (Hard Rule A5).

    python -m mizan.audit.verify_chain <ledger.sqlite | records.jsonl>

This is a shipped product surface, not a debug script. It takes a ledger file and nothing else: no
service, no network, no credentials, no Mizan account. It re-derives every ``audit_hash`` from the
record's own content and re-checks every link, and it names the first sequence that fails. Exit status
is 0 when the chain verifies, 1 when it does not and 2 when the file could not be read at all - so it
drops straight into a cron job, a CI step or an auditor's checklist.

The database file is opened READ-ONLY. Verification never writes to the evidence it is verifying.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from collections.abc import Sequence
from pathlib import Path

from mizan.audit import ChainVerification, verify_stored_rows
from mizan.contracts.canonical import ENGINE_VERSION, strict_json_loads

__all__ = ["ChainFile", "main", "read_chain_file"]

EXIT_VERIFIED = 0
EXIT_BROKEN = 1
EXIT_UNREADABLE = 2

_SQLITE_MAGIC = b"SQLite format 3\x00"

_CHAIN_SQL = (
    "SELECT sequence, record_json FROM ("
    "SELECT sequence, record_json FROM decision_records "
    "UNION ALL "
    "SELECT sequence, record_json FROM control_events"
    ") ORDER BY sequence"
)

_DESCRIPTION = """
Verify a Mizan audit chain offline.

Every decision Mizan governs is written to an append-only, hash-chained ledger, one chain per tenant.
This command re-derives every hash in that chain from the stored content and reports the first sequence
number that does not agree. It is the evidence half of the proof layer that decision replay stands on:
decision replay shows you WHY a decision came out the way it did; this shows you that the record it
reads from has not been altered since it was written.
"""

_EPILOG = """
files
  <ledger>.sqlite   a per-tenant ledger database (tables decision_records and control_events)
  <records>.jsonl   a JSON Lines export, one record or control event per line, any order

exit status
  0   the chain verifies
  1   the chain is broken (the first bad sequence is named in the output)
  2   the file could not be read as a Mizan chain

examples
  python -m mizan.audit.verify_chain ./ledger/tenant-a.sqlite
  python -m mizan.audit.verify_chain ./export/tenant-a.jsonl --json
"""


class ChainFile:
    """A chain read from disk: the raw rows to verify, plus what the file says about itself."""

    def __init__(
        self,
        *,
        path: Path,
        kind: str,
        rows: list[tuple[int, str]],
        tenants: list[str],
        decisions: int,
        control_events: int,
    ) -> None:
        self.path = path
        self.kind = kind
        self.rows = rows
        self.tenants = tenants
        self.decisions = decisions
        self.control_events = control_events


class ChainFileError(Exception):
    """The file is not a readable Mizan chain."""


def _classify(payloads: Sequence[dict[str, object]]) -> tuple[list[str], int, int]:
    tenants: list[str] = []
    decisions = 0
    control_events = 0
    for payload in payloads:
        tenant = payload.get("tenant_id")
        if isinstance(tenant, str) and tenant not in tenants:
            tenants.append(tenant)
        if "event_id" in payload:
            control_events += 1
        else:
            decisions += 1
    return tenants, decisions, control_events


def _read_sqlite(path: Path) -> ChainFile:
    uri = f"{path.resolve().as_uri()}?mode=ro"
    try:
        connection = sqlite3.connect(uri, uri=True)
    except sqlite3.Error as exc:  # pragma: no cover - platform dependent
        raise ChainFileError(f"cannot open {path} read-only: {exc}") from exc
    try:
        rows = [(int(sequence), str(text)) for sequence, text in connection.execute(_CHAIN_SQL)]
    except sqlite3.DatabaseError as exc:
        raise ChainFileError(
            f"{path} is not a Mizan ledger database "
            f"(expected tables decision_records and control_events): {exc}"
        ) from exc
    finally:
        connection.close()
    payloads = [strict_json_loads(text) for _sequence, text in rows]
    tenants, decisions, control_events = _classify(payloads)
    return ChainFile(
        path=path,
        kind="sqlite",
        rows=rows,
        tenants=tenants,
        decisions=decisions,
        control_events=control_events,
    )


def _read_json_lines(path: Path) -> ChainFile:
    text = path.read_text(encoding="utf-8")
    stripped = text.lstrip()
    items: list[str]
    if stripped.startswith("["):
        try:
            loaded = strict_json_loads(stripped)
        except ValueError as exc:
            raise ChainFileError(f"{path} is not valid JSON: {exc}") from exc
        if not isinstance(loaded, list):
            raise ChainFileError(f"{path} must hold a JSON array or one JSON object per line")
        items = [
            json.dumps(entry, separators=(",", ":"), sort_keys=True, ensure_ascii=False)
            for entry in loaded
        ]
    else:
        items = [line for line in (raw.strip() for raw in text.splitlines()) if line]

    rows: list[tuple[int, str]] = []
    payloads: list[dict[str, object]] = []
    for number, item in enumerate(items, start=1):
        try:
            payload = strict_json_loads(item)
        except ValueError as exc:
            raise ChainFileError(f"{path} line {number} is not valid JSON: {exc}") from exc
        if not isinstance(payload, dict) or not isinstance(payload.get("sequence"), int):
            raise ChainFileError(f"{path} line {number} has no integer 'sequence' field")
        rows.append((int(payload["sequence"]), item))
        payloads.append(payload)
    rows.sort(key=lambda row: row[0])
    tenants, decisions, control_events = _classify(payloads)
    return ChainFile(
        path=path,
        kind="json-lines",
        rows=rows,
        tenants=tenants,
        decisions=decisions,
        control_events=control_events,
    )


def read_chain_file(path: Path, *, fmt: str = "auto") -> ChainFile:
    """Read a chain from a SQLite ledger or a JSON Lines export. Never writes to the file."""
    if not path.is_file():
        raise ChainFileError(f"no such file: {path}")
    if fmt == "auto":
        with path.open("rb") as handle:
            fmt = "sqlite" if handle.read(len(_SQLITE_MAGIC)) == _SQLITE_MAGIC else "jsonl"
    if fmt == "sqlite":
        return _read_sqlite(path)
    return _read_json_lines(path)


def _report(chain: ChainFile, result: ChainVerification) -> str:
    tenant = ", ".join(chain.tenants) if chain.tenants else "(not recorded)"
    first = chain.rows[0][0] if chain.rows else 0
    last = chain.rows[-1][0] if chain.rows else 0
    lines = [
        "Mizan audit chain - offline verification",
        f"  file      : {chain.path}",
        f"  format    : {chain.kind}",
        f"  tenant    : {tenant}",
        f"  links     : {len(chain.rows)} "
        f"({chain.decisions} decision record(s), {chain.control_events} control event(s))",
        f"  sequence  : {first} .. {last}" if chain.rows else "  sequence  : (empty chain)",
        f"  verifier  : {ENGINE_VERSION} (offline; no network, no credentials, read-only)",
        "",
    ]
    if result.ok:
        lines += [
            "  RESULT: CHAIN VERIFIED",
            "  Every audit_hash recomputes from the record's own content and every record links to",
            f"  its predecessor. {result.detail}.",
        ]
        if result.head_hash:
            lines += [
                "",
                f"  head      : sequence {result.head_sequence}, {result.head_hash}",
                "  Keep that head. A chain proves its records are consistent with each other; it",
                "  cannot prove they are ALL of them, because deleting the last few takes the",
                "  evidence they existed with them. Re-run with --expect-head to detect that.",
            ]
    else:
        lines += [
            f"  RESULT: CHAIN BROKEN at sequence {result.first_bad_sequence}",
            f"  {result.detail}.",
            "  This ledger has been altered since it was written; the records at and after that",
            "  sequence can no longer be trusted, and decision replay of them proves nothing.",
        ]
    return "\n".join(lines)



def _check_anchor(
    result: ChainVerification, expect_head: str | None, expect_length: int | None
) -> ChainVerification:
    """Compare a verified chain against what the holder already knew about it.

    Everything else this module checks is internal: the records agree with each other. Truncation is
    the one attack that leaves a perfectly self-consistent chain, because the proof that the deleted
    records ever existed is exactly what was deleted. Nothing stored beside them helps either - an
    attacker who can remove records can remove a counter just as easily.

    So the only witness is one the customer already holds. Certificate Transparency solves this by
    publishing a signed tree head rather than letting the log describe itself; this is the small
    version of the same idea, and the reason the head is printed on every successful verification.
    """
    if not result.ok:
        return result
    if expect_length is not None and result.length != expect_length:
        missing = expect_length - result.length
        return result.model_copy(update={
            "ok": False,
            "first_bad_sequence": result.length + 1 if missing > 0 else None,
            "detail": (
                f"expected {expect_length} link(s) and found {result.length}: "
                + (f"{missing} record(s) have been removed from the end of this chain"
                   if missing > 0 else
                   f"{-missing} more record(s) than expected - this is not the chain you anchored")
            ),
        })
    if expect_head is not None and result.head_hash != expect_head:
        return result.model_copy(update={
            "ok": False,
            "first_bad_sequence": result.head_sequence,
            "detail": (
                f"the chain is internally consistent but its head is {result.head_hash}, not the "
                f"{expect_head} you anchored: records have been removed from the end, or this is a "
                f"different chain"
            ),
        })
    return result


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m mizan.audit.verify_chain",
        description=_DESCRIPTION.strip(),
        epilog=_EPILOG.strip(),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("path", type=Path, help="the ledger file to verify (.sqlite or .jsonl)")
    parser.add_argument(
        "--format",
        dest="fmt",
        choices=("auto", "sqlite", "jsonl"),
        default="auto",
        help="how to read the file (default: auto, from its contents)",
    )
    parser.add_argument(
        "--json",
        dest="as_json",
        action="store_true",
        help="print the verdict as one JSON object instead of a report",
    )
    parser.add_argument(
        "--quiet",
        action="store_true",
        help="print nothing; report the verdict through the exit status alone",
    )
    parser.add_argument(
        "--expect-head",
        metavar="HASH",
        help=(
            "the audit_hash you last saw at the end of this chain. A hash chain cannot detect its "
            "own truncation - delete the last records and the rest still verifies - so completeness "
            "can only be checked against a value held OUTSIDE the file. This is that check."
        ),
    )
    parser.add_argument(
        "--expect-length",
        type=int,
        metavar="N",
        help="the number of links you expect; fewer means records were removed from the end",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Verify one chain file. Returns the process exit status."""
    arguments = _parser().parse_args(list(argv) if argv is not None else None)
    try:
        chain = read_chain_file(arguments.path, fmt=arguments.fmt)
    except (ChainFileError, OSError, ValueError) as exc:
        if not arguments.quiet:
            print(f"mizan.audit.verify_chain: {exc}", file=sys.stderr)
        return EXIT_UNREADABLE

    result = verify_stored_rows(chain.rows)
    result = _check_anchor(result, arguments.expect_head, arguments.expect_length)
    if not arguments.quiet:
        if arguments.as_json:
            print(
                json.dumps(
                    {
                        "file": str(chain.path),
                        "format": chain.kind,
                        "tenants": chain.tenants,
                        "links": len(chain.rows),
                        "decision_records": chain.decisions,
                        "control_events": chain.control_events,
                        "ok": result.ok,
                        "first_bad_sequence": result.first_bad_sequence,
                        "head_sequence": result.head_sequence,
                        "head_hash": result.head_hash,
                        "detail": result.detail,
                    },
                    indent=2,
                    sort_keys=True,
                )
            )
        else:
            stream = sys.stdout if result.ok else sys.stderr
            print(_report(chain, result), file=stream)
    return EXIT_VERIFIED if result.ok else EXIT_BROKEN


if __name__ == "__main__":  # pragma: no cover - process entry point
    sys.exit(main())
