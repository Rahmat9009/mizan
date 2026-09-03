"""An engine version is a promise about behaviour, so it must move when behaviour moves.

This test exists because it did not, and the omission was expensive. Adding the ``expected_value``
check changed what the engine decides; ``engine_version`` stayed ``mizan-core/0.1.0``. Replay of the
twelve live records then reported NOT IDENTICAL - the wording of which points at tampering - when the
stored bytes were provably untouched and the real cause was an engine that had changed without saying
so. Those two situations demand opposite responses (investigate a breach vs. re-baseline a version),
and a replay tool that cannot tell them apart is worse than none, because it cries wolf about fraud.

The fingerprint is DERIVED from behaviour and the version is HAND-WRITTEN, so only one of them can be
forgotten. Pinning them together in engine-versions.json makes forgetting fail here instead of in an
auditor's replay. There are exactly two honest ways to make this test pass again: revert the behaviour
change, or publish a new engine version and pin it. Re-pinning an existing version to a new
fingerprint is the dishonest third way - it silently rewrites what a published version promised, and
every record already written under that version becomes a record whose engine no longer exists.
"""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

from mizan.contracts.canonical import ENGINE_VERSION

ROOT = Path(__file__).resolve().parents[2]
PINS = json.loads((ROOT / "engine-versions.json").read_text(encoding="utf-8"))["versions"]


def _fingerprint() -> str:
    spec = importlib.util.spec_from_file_location(
        "_determinism_fingerprint", ROOT / "scripts" / "determinism_fingerprint.py"
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return str(module.compute()["fingerprint"])


def test_the_running_engine_version_is_published():
    assert ENGINE_VERSION in PINS, (
        f"{ENGINE_VERSION} decides trades but is not pinned in engine-versions.json. Every record "
        f"written now claims an engine whose behaviour was never published."
    )


def test_the_running_engine_decides_the_way_its_version_promises():
    pinned = PINS[ENGINE_VERSION]["fingerprint"]
    actual = _fingerprint()
    assert actual.startswith(pinned), (
        f"{ENGINE_VERSION} promises fingerprint {pinned} but decides with {actual[:16]}. The engine's "
        f"behaviour changed and its version did not. Publish a new version and pin it - do NOT re-pin "
        f"{ENGINE_VERSION}, which would rewrite the promise every existing record was written under."
    )


def test_the_determinism_reference_belongs_to_the_running_engine():
    """The cross-machine reference is only evidence if it is evidence about THIS engine."""
    reference = json.loads((ROOT / "determinism-reference.json").read_text(encoding="utf-8"))
    assert reference["engine_version"] == ENGINE_VERSION
    assert reference["fingerprint"].startswith(PINS[ENGINE_VERSION]["fingerprint"])


def test_no_two_engine_versions_claim_the_same_behaviour():
    """Two versions with one fingerprint means a version was bumped without a behaviour change, which
    makes replay's engine-mismatch warning fire on records that would have replayed identically."""
    seen: dict[str, str] = {}
    for version, entry in PINS.items():
        clash = seen.setdefault(entry["fingerprint"], version)
        assert clash == version, f"{version} and {clash} pin the same fingerprint"


@pytest.mark.parametrize("version", sorted(PINS))
def test_every_pin_is_a_real_fingerprint_prefix(version: str):
    pinned = PINS[version]["fingerprint"]
    assert len(pinned) >= 16 and all(c in "0123456789abcdef" for c in pinned), (
        f"{version} is pinned to {pinned!r}, which is not a fingerprint"
    )
