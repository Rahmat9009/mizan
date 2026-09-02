""".pre-commit-config.yaml -- the optional framework wiring for the commit-time secret scan.

This file was committed in a state that did not parse: the `block-env-files` entry is a Python
one-liner containing "refusing to commit: ", and a plain YAML scalar may not contain ": " (it
reads as a mapping). The failure mode is quiet -- pre-commit reports a config error and the
developer, mid-commit, reaches for `--no-verify`. Hence a parse test.
"""

from __future__ import annotations

import shlex
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
CONFIG = REPO_ROOT / ".pre-commit-config.yaml"

DOCUMENT = yaml.safe_load(CONFIG.read_text(encoding="utf-8"))
HOOKS: dict[str, dict] = {hook["id"]: hook for repo in DOCUMENT["repos"] for hook in repo["hooks"]}


def test_the_config_parses_and_declares_the_expected_hooks() -> None:
    assert set(HOOKS) == {"mizan-secret-scan", "ruff-check", "block-env-files"}
    assert DOCUMENT["default_stages"] == ["pre-commit"]


def test_every_hook_runs_the_local_toolchain_rather_than_fetching_one() -> None:
    """`language: system` means the hook, `make lint` and CI run the same pinned tools."""
    for name, hook in HOOKS.items():
        assert hook["language"] == "system", f"{name} would install its own copy of the tool"
    assert all(repo["repo"] == "local" for repo in DOCUMENT["repos"])


def test_the_secret_scan_hook_scans_staged_content() -> None:
    """The working tree is not what is being committed; `--staged` is the only correct mode here."""
    hook = HOOKS["mizan-secret-scan"]
    assert hook["entry"].endswith("scripts/secret_scan.py --staged")
    assert hook["always_run"] is True
    assert hook["pass_filenames"] is False


@pytest.mark.parametrize(
    ("paths", "expected"),
    [
        (["app/models.py", ".env.example"], 0),
        ([".env.sample"], 0),
        ([".env.template"], 0),
        ([".env"], 1),
        (["config/.env.local"], 1),
        (["deploy/.env.production"], 1),
    ],
)
def test_the_env_blocking_hook_behaves(paths: list[str], expected: int) -> None:
    argv = shlex.split(HOOKS["block-env-files"]["entry"])
    assert argv[0] == "python" and argv[1] == "-c", argv[:2]
    result = subprocess.run(
        [sys.executable, *argv[1:], *paths], cwd=REPO_ROOT, capture_output=True, text=True, check=False
    )
    assert result.returncode == expected, result.stdout + result.stderr
    if expected:
        assert "refusing to commit" in result.stdout
