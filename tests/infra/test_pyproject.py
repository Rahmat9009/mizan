"""pyproject.toml -- packaging, determinism pins, lint scope and test configuration.

The pins matter beyond hygiene: ``library_versions()`` records pydantic, jsonschema and
PyYAML in every DecisionRecord, and Hard Rule A1 says the same inputs under the same
engine version must replay to the same verdict. A floating dependency makes the recorded
provenance a lie (Master Plan C6), so the three decision-path libraries are pinned to an
exact version -- and the environment actually running the suite must be on that version,
or the records this machine writes cannot be reproduced from what the manifest claims.
"""

from __future__ import annotations

import tomllib
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
PYPROJECT = REPO_ROOT / "pyproject.toml"

# The libraries that participate in canonical serialisation and validation.
DECISION_PATH_LIBRARIES = ("pydantic", "jsonschema", "PyYAML")


def _config() -> dict:
    with PYPROJECT.open("rb") as handle:
        return tomllib.load(handle)


CONFIG = _config()


def _requirements() -> dict[str, str]:
    """{distribution: full requirement string} for [project.dependencies]."""
    out: dict[str, str] = {}
    for requirement in CONFIG["project"]["dependencies"]:
        name = requirement.split("==")[0].split(">=")[0].split("<")[0].split("[")[0].strip()
        out[name.lower()] = requirement
    return out


REQUIREMENTS = _requirements()


def test_pyproject_parses_as_toml() -> None:
    assert CONFIG["project"]["name"] == "mizan-core"
    assert CONFIG["build-system"]["build-backend"] == "setuptools.build_meta"


def test_python_is_pinned_to_312_or_later() -> None:
    assert CONFIG["project"]["requires-python"] == ">=3.12"
    assert CONFIG["tool"]["ruff"]["target-version"] == "py312"
    assert CONFIG["tool"]["mypy"]["python_version"] == "3.12"


@pytest.mark.parametrize("library", DECISION_PATH_LIBRARIES)
def test_decision_path_library_is_pinned_exactly(library: str) -> None:
    requirement = REQUIREMENTS.get(library.lower())
    assert requirement is not None, f"{library} is not declared in [project.dependencies]"
    assert "==" in requirement, (
        f"{library} must be pinned exactly (Master Plan C6): its version is recorded in every "
        f"DecisionRecord, so a range makes the recorded provenance unreproducible. Got {requirement!r}."
    )
    pinned = requirement.split("==", 1)[1].strip()
    assert pinned and not any(c in pinned for c in "<>,*~ "), f"{requirement!r} is not a single exact version"


@pytest.mark.parametrize("library", DECISION_PATH_LIBRARIES)
def test_pinned_version_is_the_version_installed_here(library: str) -> None:
    pinned = REQUIREMENTS[library.lower()].split("==", 1)[1].strip()
    try:
        installed = version(library)
    except PackageNotFoundError:  # pragma: no cover - the suite cannot run without these
        pytest.fail(f"{library} is not installed; run `make install`")
    assert installed == pinned, (
        f"{library} {installed} is installed but pyproject.toml pins {pinned}. Records written on "
        f"this machine would claim a provenance the manifest does not describe."
    )


def test_transport_dependencies_are_bounded_even_where_they_are_not_exact() -> None:
    """A range is acceptable off the decision path, but an unbounded one is not."""
    for name, requirement in REQUIREMENTS.items():
        if name in {lib.lower() for lib in DECISION_PATH_LIBRARIES}:
            continue
        assert "<" in requirement, f"{requirement!r} has no upper bound; a major bump would land silently"


def test_advisory_extra_is_optional_so_the_engine_runs_with_no_llm_installed() -> None:
    """Hard Rule E8: the deterministic engine must evaluate and reject with the LLM offline."""
    optional = CONFIG["project"]["optional-dependencies"]
    assert any("anthropic" in requirement for requirement in optional["advisory"])
    assert not any("anthropic" in requirement for requirement in CONFIG["project"]["dependencies"])


def test_ruff_excludes_the_worktrees_and_the_legacy_app() -> None:
    excluded = CONFIG["tool"]["ruff"]["extend-exclude"]
    assert ".worktrees" in excluded, (
        "parallel lane worktrees are copies of the repo; linting them double-counts"
    )
    assert "app" in excluded, "app/ is a read-only salvage reference, not code this project styles"


def test_ruff_selects_the_rule_families_the_project_claims() -> None:
    lint = CONFIG["tool"]["ruff"]["lint"]
    assert {"E", "F", "I", "B", "UP"} <= set(lint["select"])
    assert lint["ignore"] == []
    assert CONFIG["tool"]["ruff"]["lint"]["isort"]["known-first-party"] == ["mizan", "tests"]


def test_pytest_testpaths_and_markers_are_configured() -> None:
    pytest_config = CONFIG["tool"]["pytest"]["ini_options"]
    assert pytest_config["testpaths"] == ["tests"]
    assert pytest_config["pythonpath"] == ["."]
    declared = {entry.split(":", 1)[0].strip() for entry in pytest_config["markers"]}
    assert {"invariant", "security", "integration", "contract"} <= declared
    # Unknown markers must be an error, not a warning, or a typo silently disables a suite.
    assert "--import-mode=importlib" in pytest_config["addopts"]


def test_the_package_is_mizan() -> None:
    find = CONFIG["tool"]["setuptools"]["packages"]["find"]
    assert find["include"] == ["mizan*"], "one package root; app/ and ui/ are not shipped"
    assert find["namespaces"] is False
    assert (REPO_ROOT / "mizan" / "__init__.py").is_file()
    assert CONFIG["tool"]["coverage"]["run"]["source"] == ["mizan"]
    assert CONFIG["tool"]["mypy"]["files"] == ["mizan"]
