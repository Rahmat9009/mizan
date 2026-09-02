""".github/workflows/ci.yml -- the commit gate.

Master Plan W0: "nothing merges without schema, unit tests, integration tests, failure
tests, security tests, replay test". A gate that exists but does not run a suite is worse
than no gate, because the badge is green either way, so this module asserts that each
suite has a job of its own and that the job actually invokes it.

The separation matters most for `invariants`. Folding tests/invariants into the unit run
would let a pending Hard Rule hide inside a larger red, or -- worse -- inside a larger
green if somebody adds an ignore. Its own job means a pending invariant is a named,
visible red on every pull request until the implementation lands.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
WORKFLOW = REPO_ROOT / ".github" / "workflows" / "ci.yml"

TEXT = WORKFLOW.read_text(encoding="utf-8")
DOCUMENT = yaml.safe_load(TEXT)
JOBS: dict[str, dict] = DOCUMENT["jobs"]
ENV: dict[str, str] = DOCUMENT.get("env", {})

REQUIRED_JOBS = (
    "lint",
    "secret-scan",
    "contracts",
    "unit",
    "invariants",
    "security",
    "integration",
    "postgres-ddl",
)
PYTHON_VERSION = "3.12"


def _steps(job: str) -> list[dict]:
    return JOBS[job].get("steps", [])


def _run_script(job: str) -> str:
    return "\n".join(str(step.get("run", "")) for step in _steps(job))


def _resolve(value: str) -> str:
    """Substitute ${{ env.NAME }} with the workflow-level env value."""
    pattern = r"\$\{\{\s*env\.([A-Z_]+)\s*\}\}"
    return re.sub(pattern, lambda m: str(ENV.get(m.group(1), m.group(0))), str(value))


def test_the_workflow_parses_and_runs_on_push_and_pull_request() -> None:
    # PyYAML resolves the bare key `on` to the boolean True (YAML 1.1).
    triggers = DOCUMENT.get("on", DOCUMENT.get(True))
    assert triggers is not None, "the workflow has no trigger"
    assert set(triggers) >= {"push", "pull_request"}


@pytest.mark.parametrize("job", REQUIRED_JOBS)
def test_the_required_job_exists(job: str) -> None:
    assert job in JOBS, f"no {job} job; that suite is not gating anything"


def test_no_required_job_is_allowed_to_fail_silently() -> None:
    for job in REQUIRED_JOBS:
        assert not JOBS[job].get("continue-on-error"), (
            f"{job} is continue-on-error: it reports but does not gate"
        )


def test_only_typecheck_is_non_blocking_and_it_says_why() -> None:
    """mypy is advisory while the lane modules are NotImplementedError stubs -- and only until then."""
    soft = [name for name, job in JOBS.items() if job.get("continue-on-error")]
    assert soft == ["typecheck"], soft
    assert "Sprint" in TEXT, "a temporary exemption with no stated expiry becomes permanent"


# ---------------------------------------------------------------------------
# Python 3.12 everywhere
# ---------------------------------------------------------------------------
def test_the_workflow_pins_python_312() -> None:
    assert ENV["PYTHON_VERSION"] == PYTHON_VERSION


def test_every_setup_python_step_uses_312() -> None:
    seen = 0
    for name, job in JOBS.items():
        for step in job.get("steps", []):
            if str(step.get("uses", "")).startswith("actions/setup-python"):
                seen += 1
                resolved = _resolve(step["with"]["python-version"])
                assert resolved == PYTHON_VERSION, f"{name} sets up python {resolved}"
    assert seen >= len(REQUIRED_JOBS) - 1, "postgres-ddl is the only job that needs no Python"


def test_every_action_is_pinned_to_a_major_version() -> None:
    for name, job in JOBS.items():
        for step in job.get("steps", []):
            uses = step.get("uses")
            if uses:
                assert "@" in uses and not uses.endswith(("@main", "@master")), f"{name} uses {uses}"


# ---------------------------------------------------------------------------
# the suites
# ---------------------------------------------------------------------------
def test_lint_runs_ruff_over_the_repository() -> None:
    assert "ruff check" in _run_script("lint")


@pytest.mark.parametrize("flag", ["--self-test", "--all", "--history"])
def test_secret_scan_job_runs_the_scanner_with(flag: str) -> None:
    assert f"secret_scan.py {flag}" in _run_script("secret-scan")


def test_secret_scan_checks_out_the_whole_history() -> None:
    """`--history` on a shallow clone would scan one commit and call the repository clean."""
    checkout = next(s for s in _steps("secret-scan") if str(s.get("uses", "")).startswith("actions/checkout"))
    assert checkout.get("with", {}).get("fetch-depth") == 0


def test_contracts_job_runs_the_contract_suite() -> None:
    assert "tests/contracts" in _run_script("contracts")


def test_unit_job_excludes_the_suites_that_have_their_own_jobs() -> None:
    script = _run_script("unit")
    assert "pytest" in script
    for suite in ("invariants", "security", "integration"):
        assert f"--ignore=tests/{suite}" in script, f"tests/{suite} would run twice, or hide inside unit"


def test_invariants_have_a_job_of_their_own() -> None:
    script = _run_script("invariants")
    assert "tests/invariants" in script
    assert "--ignore" not in script, "an invariant excluded from the invariant job is not an invariant"
    assert JOBS["invariants"].get("continue-on-error") is None


def test_the_workflow_says_an_invariant_is_never_edited_to_go_green() -> None:
    assert "Never edit an invariant" in TEXT, (
        "the instruction belongs next to the job that will be red; that is where somebody reads it"
    )


@pytest.mark.parametrize("job", ["security", "integration"])
def test_optional_suite_job_runs_the_suite_when_it_exists(job: str) -> None:
    script = _run_script(job)
    assert f"tests/{job}" in script
    assert "::warning" in script, "an absent suite must be announced, not silently skipped"


# ---------------------------------------------------------------------------
# the postgres job -- the only place the DDL executes
# ---------------------------------------------------------------------------
def test_postgres_ddl_job_uses_a_real_postgres_service() -> None:
    service = JOBS["postgres-ddl"]["services"]["postgres"]
    assert service["image"].startswith("postgres:")
    assert "--health-cmd" in " ".join(service["options"].split())


def test_postgres_ddl_image_matches_docker_compose() -> None:
    """CI proving a different Postgres than developers run is CI proving nothing."""
    compose = yaml.safe_load((REPO_ROOT / "docker-compose.yml").read_text(encoding="utf-8"))
    assert JOBS["postgres-ddl"]["services"]["postgres"]["image"] == compose["services"]["postgres"]["image"]


def test_postgres_ddl_applies_the_init_scripts_twice_to_prove_idempotency() -> None:
    script = _run_script("postgres-ddl")
    assert script.count("infra/postgres/init/*.sql") >= 2, (
        "the scripts run on every container start; applying them once proves nothing about the second time"
    )
    assert "ON_ERROR_STOP=1" in script


def test_postgres_ddl_runs_the_append_only_proof() -> None:
    script = _run_script("postgres-ddl")
    assert "infra/postgres/verify/prove_append_only.sh" in script
    assert (REPO_ROOT / "infra" / "postgres" / "verify" / "prove_append_only.sh").is_file()


def test_the_ci_postgres_password_is_never_reused_as_a_real_credential() -> None:
    """It protects a container that lives for one job on the runner's loopback interface."""
    service = JOBS["postgres-ddl"]["services"]["postgres"]
    assert service["env"]["POSTGRES_PASSWORD"] == "mizan-ci"
    assert "ephemeral" in TEXT.lower(), "an unexplained password in a committed file invites copying"


# ---------------------------------------------------------------------------
# paper only, least privilege
# ---------------------------------------------------------------------------
def test_every_job_runs_with_the_paper_only_environment() -> None:
    assert ENV["ALPACA_PAPER"] == "true"
    assert ENV["MIZAN_EXECUTION_ENABLED"] == "false"
    assert ENV["MIZAN_EXECUTION_DRY_RUN"] == "true"
    assert ENV["MIZAN_KILL_SWITCH"] == "false"


def test_no_job_overrides_the_paper_only_environment() -> None:
    for name, job in JOBS.items():
        assert "ALPACA_PAPER" not in job.get("env", {}), f"{name} redefines ALPACA_PAPER"


def test_the_workflow_token_is_read_only() -> None:
    assert DOCUMENT["permissions"] == {"contents": "read"}


def test_no_secret_is_referenced_by_any_job() -> None:
    """Nothing in this pipeline needs a credential; a `secrets.` reference is a change of posture."""
    assert not re.search(r"\$\{\{\s*secrets\.", TEXT)
