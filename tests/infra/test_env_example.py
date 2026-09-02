""".env.example -- the only environment file that may ever be committed.

Two separate properties are asserted here. First, that the template is a template: every
line is `KEY=` or `KEY=<benign placeholder>`, and the scanner that guards commits finds
nothing in it. Second, that the defaults it hands a new developer are the safe ones --
`ALPACA_PAPER=true` (Hard Rule B1: paper and live are deployment boundaries, and this
build has no live side at all), execution disabled, dry-run on, kill switch present.

A developer copies this file and edits two values. Whatever it says by default is what
most environments will run with, so the defaults are part of the security boundary.
"""

from __future__ import annotations

import importlib.util
import re
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
ENV_EXAMPLE = REPO_ROOT / ".env.example"

ASSIGNMENT = re.compile(r"^(?P<key>[A-Z][A-Z0-9_]*)=(?P<value>.*)$")

# A key whose NAME implies a credential must carry no value at all -- not a sample, not a
# masked string, nothing. "It is only an example key" is how examples get used verbatim.
CREDENTIAL_KEY = re.compile(r"(KEY|SECRET|TOKEN|PASSWORD|PASSWD|CREDENTIAL|DATABASE_URL|DSN)")

# Config artefacts that are allowed to mention ALPACA_PAPER as an assignment at all.
CONFIG_ARTEFACTS = (".env.example", ".github/workflows/ci.yml", "docker-compose.yml", "Makefile")
PAPER_ASSIGNMENT = re.compile(r"^\s*(?:-\s*)?ALPACA_PAPER\s*[:=]\s*[\"']?([^\"'\s#]*)")


def _load_scanner():
    path = REPO_ROOT / "scripts" / "secret_scan.py"
    spec = importlib.util.spec_from_file_location("mizan_secret_scan_for_env", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module  # dataclasses resolve annotations via sys.modules
    spec.loader.exec_module(module)
    return module


SCAN = _load_scanner()
TEXT = ENV_EXAMPLE.read_text(encoding="utf-8")
LINES = [(number, line) for number, line in enumerate(TEXT.splitlines(), 1)]
CONTENT_LINES = [(n, line) for n, line in LINES if line.strip() and not line.lstrip().startswith("#")]
SETTINGS = {m.group("key"): m.group("value") for _, line in CONTENT_LINES if (m := ASSIGNMENT.match(line))}


def _git(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["git", "-C", str(REPO_ROOT), *args], capture_output=True, text=True, check=False)


# ---------------------------------------------------------------------------
# shape
# ---------------------------------------------------------------------------
def test_every_content_line_is_a_bare_key_assignment() -> None:
    for number, line in CONTENT_LINES:
        assert ASSIGNMENT.match(line), f".env.example:{number} is neither a comment nor KEY=value: {line!r}"
        assert not line.startswith(("export ", " ", "\t")), f".env.example:{number} is not plain KEY=value"
        assert line == line.rstrip(), f".env.example:{number} has trailing whitespace"


def test_no_key_is_declared_twice() -> None:
    keys = [m.group("key") for _, line in CONTENT_LINES if (m := ASSIGNMENT.match(line))]
    duplicates = {key for key in keys if keys.count(key) > 1}
    assert not duplicates, f"a later assignment silently wins: {sorted(duplicates)}"


@pytest.mark.parametrize("key", sorted(k for k in SETTINGS if CREDENTIAL_KEY.search(k)))
def test_credential_shaped_key_has_no_value(key: str) -> None:
    assert SETTINGS[key] == "", (
        f"{key} carries a value in the committed template. Credential keys ship empty; an empty "
        f"value means 'unset' and the process refuses to start or leaves the feature off."
    )


# ---------------------------------------------------------------------------
# nothing in here looks like a real credential -- checked with the commit gate itself
# ---------------------------------------------------------------------------
def test_the_secret_scanner_finds_nothing_in_the_template() -> None:
    findings = SCAN.scan_text(TEXT, ".env.example")
    assert findings == [], [f.format() for f in findings]


def test_the_template_does_not_silence_the_scanner_with_an_inline_marker() -> None:
    for marker in SCAN.INLINE_ALLOW_MARKERS:
        assert marker not in TEXT, "an allow marker in .env.example would exempt a real credential"


def test_the_template_is_not_allow_listed() -> None:
    allowlist = REPO_ROOT / SCAN.ALLOWLIST_FILENAME
    if not allowlist.is_file():
        return
    patterns = [
        line.strip()
        for line in allowlist.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.strip().startswith("#")
    ]
    config = SCAN.Config(root=REPO_ROOT, allow_patterns=patterns)
    assert not config.is_allowed(".env.example")


# ---------------------------------------------------------------------------
# the defaults are the safe ones
# ---------------------------------------------------------------------------
def test_alpaca_paper_is_true() -> None:
    assert SETTINGS["ALPACA_PAPER"] == "true", "B1: there is no live side of this build to switch to"


def test_execution_is_disabled_and_dry_run_by_default() -> None:
    assert SETTINGS["MIZAN_EXECUTION_ENABLED"] == "false"
    assert SETTINGS["MIZAN_EXECUTION_DRY_RUN"] == "true"


def test_kill_switch_is_present_and_off() -> None:
    assert "MIZAN_KILL_SWITCH" in SETTINGS, "E4 needs the variable to exist before anyone has to flip it"
    assert SETTINGS["MIZAN_KILL_SWITCH"] == "false"


def test_legacy_execution_defaults_submit_nothing_either() -> None:
    assert SETTINGS["ALPACA_EXECUTION_ENABLED"] == "false"
    assert SETTINGS["ALPACA_EXECUTION_DRY_RUN"] == "true"
    assert SETTINGS["ALPACA_EXECUTION_KILL_SWITCH"] == "false"


@pytest.mark.parametrize("relpath", CONFIG_ARTEFACTS)
def test_no_config_artefact_assigns_a_non_paper_value(relpath: str) -> None:
    path = REPO_ROOT / relpath
    if not path.is_file():
        pytest.skip(f"{relpath} is absent")
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if line.lstrip().startswith("#"):
            continue
        match = PAPER_ASSIGNMENT.match(line)
        if match:
            assert match.group(1) == "true", f"{relpath}:{number} sets ALPACA_PAPER to {match.group(1)!r}"


# ---------------------------------------------------------------------------
# .env is ignored; .env.example is not
# ---------------------------------------------------------------------------
def test_dotenv_is_gitignored() -> None:
    assert _git("check-ignore", "-q", ".env").returncode == 0, ".env is committable"
    assert _git("check-ignore", "-q", ".env.local").returncode == 0


def test_dotenv_example_is_not_gitignored() -> None:
    result = _git("check-ignore", "-q", ".env.example")
    assert result.returncode == 1, ".env.example must stay committed; it is the only template we ship"


def test_dotenv_example_is_tracked() -> None:
    tracked = _git("ls-files", "--error-unmatch", ".env.example")
    assert tracked.returncode == 0, ".env.example is not tracked by git"
