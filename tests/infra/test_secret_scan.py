"""scripts/secret_scan.py -- the pre-commit and CI gate against a committed credential.

Every secret-shaped fixture in this module is BUILT AT RUNTIME from a seeded PRNG, so
this file contains no secret-shaped literal and stays clean under `secret_scan.py --all`,
which scans it. The tests depend only on the scanner's public surface
(``RULES``, ``scan_text``, ``shannon_entropy``, ``is_placeholder``,
``is_sensitive_filename``, ``main``) plus its documented configuration constants.
"""

from __future__ import annotations

import base64
import importlib.util
import json
import random
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SCANNER_PATH = REPO_ROOT / "scripts" / "secret_scan.py"


def _load_scanner():
    """Import the scanner, which is a script rather than a package module.

    The module must be registered in ``sys.modules`` before execution: ``@dataclass``
    resolves annotations through ``sys.modules[cls.__module__]`` and raises otherwise.
    """
    spec = importlib.util.spec_from_file_location("mizan_secret_scan_under_test", SCANNER_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


SCAN = _load_scanner()

DIGITS = "0123456789"
UPPER = "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
LOWER = UPPER.lower()
BASE62 = UPPER + LOWER + DIGITS
BASE64URL = BASE62 + "-_"
UPPER_DIGITS = UPPER + DIGITS
HEX = DIGITS + "abcdef"


def _rand(alphabet: str, length: int, seed: int) -> str:
    rng = random.Random(seed)
    return "".join(rng.choice(alphabet) for _ in range(length))


def _mixed(alphabet: str, length: int, seed: int) -> str:
    """A credential-shaped string: mixed character classes and high entropy."""
    for attempt in range(2000):
        candidate = _rand(alphabet, length, seed + attempt)
        has_classes = (
            any(c in LOWER for c in candidate)
            and any(c in UPPER for c in candidate)
            and any(c in DIGITS for c in candidate)
        )
        if has_classes and SCAN.shannon_entropy(candidate) > 4.0:
            return candidate
    raise AssertionError("could not build a credential-shaped sample")


def _jwt() -> str:
    def segment(obj: dict[str, object]) -> str:
        return base64.urlsafe_b64encode(json.dumps(obj).encode()).decode().rstrip("=")

    return ".".join(
        [segment({"alg": "HS256", "typ": "JWT"}), segment({"sub": "agent-a"}), _rand(BASE64URL, 43, 900)]
    )


# --- one positive fixture per rule family. (rule, line, the secret inside the line) -------
def _positives() -> list[tuple[str, str, str]]:
    aws = "AKIA" + _rand(UPPER_DIGITS, 16, 401)
    sts = "ASIA" + _rand(UPPER_DIGITS, 16, 402)
    alpaca_key = "PK" + _mixed(UPPER_DIGITS + LOWER, 18, 403).upper()
    alpaca_secret = _mixed(BASE62, 40, 404)
    openai = "sk-" + "proj-" + _mixed(BASE62, 48, 405)
    anthropic = "sk-ant-" + "api03-" + _mixed(BASE64URL, 60, 406)
    ghp = "ghp_" + _mixed(BASE62, 36, 407)
    gh_pat = "github_pat_" + _mixed(BASE62, 22, 408) + "_" + _mixed(BASE62, 59, 409)
    slack = "xoxb-" + _rand(DIGITS, 12, 410) + "-" + _rand(DIGITS, 12, 411) + "-" + _mixed(BASE62, 24, 412)
    jwt = _jwt()
    db_password = _mixed(BASE62, 26, 413)
    generic = _mixed(BASE62, 34, 414)
    public = _mixed(BASE62, 30, 415)
    bearer = _mixed(BASE62, 44, 416)
    token_a = _mixed(BASE62, 32, 417)
    pem_marker = "PRIVATE KEY"
    return [
        ("private-key-block", "-----BEGIN " + "RSA " + pem_marker + "-----", pem_marker),
        ("private-key-block", "-----BEGIN " + "OPENSSH " + pem_marker + "-----", pem_marker),
        ("anthropic-api-key", "ANTHROPIC_API_KEY=" + anthropic, anthropic),
        ("openai-or-featherless-api-key", 'FEATHERLESS_API_KEY="' + openai + '"', openai),
        ("aws-access-key-id", "AWS_ACCESS_KEY_ID=" + aws, aws),
        ("aws-access-key-id", "aws_session_key = '" + sts + "'", sts),
        ("github-token", "token = '" + ghp + "'", ghp),
        ("github-token", "GH_TOKEN: " + gh_pat, gh_pat),
        ("slack-token", "SLACK_BOT_TOKEN=" + slack, slack),
        ("jwt", "Authorization: Bearer " + jwt, jwt),
        (
            "database-url-with-password",
            "DATABASE_URL=postgresql://mizan:" + db_password + "@db.internal:5432/mizan",
            db_password,
        ),
        (
            "database-url-with-password",
            "REDIS_URL=redis://default:" + db_password + "@cache:6379/0",
            db_password,
        ),
        ("alpaca-key-id", "ALPACA_API_KEY=" + alpaca_key, alpaca_key),
        ("alpaca-secret-key", "ALPACA_SECRET_KEY=" + alpaca_secret, alpaca_secret),
        ("public-bundle-secret", "NEXT_PUBLIC_API_KEY=" + public, public),
        ("public-bundle-secret", "VITE_SECRET_TOKEN='" + public + "'", public),
        ("bearer-token", "Authorization: Bearer " + bearer, bearer),
        ("high-entropy-assignment", 'api_key = "' + generic + '"', generic),
        ("high-entropy-assignment", '  "client_secret": "' + generic + '",', generic),
        ("high-entropy-assignment", "MIZAN_API_TOKENS=agent-a:" + token_a, token_a),
    ]


# --- near misses: shaped like a credential, or named like one, but not one ----------------
def _negatives() -> list[tuple[str, str]]:
    sha256 = _rand(HEX, 64, 501)
    sha1 = _rand(HEX, 40, 502)
    high_entropy = _mixed(BASE62, 40, 503)
    jwt_shaped = ".".join(
        ["eyJ" + _rand(BASE64URL, 12, 504), _rand(BASE64URL, 20, 505), _rand(BASE64URL, 20, 506)]
    )
    return [
        # the paper-only boundary must never be mistaken for a secret assignment
        ("paper flag", "ALPACA_PAPER=true"),
        ("tenant id", "MIZAN_TENANT_ID=tenant-a"),
        # unset / templated values -- what .env.example and docker-compose.yml actually contain
        ("empty api key", "ALPACA_API_KEY="),
        ("empty secret", "ALPACA_SECRET_KEY="),
        ("empty password", "POSTGRES_PASSWORD="),
        ("empty token list", "MIZAN_API_TOKENS="),
        ("empty assignment with quotes", 'api_key = ""'),
        ("shell expansion", "OPENAI_API_KEY=${OPENAI_API_KEY}"),
        ("shell expansion in a value", 'password: "${POSTGRES_PASSWORD}"'),
        (
            "compose required-variable form",
            "POSTGRES_PASSWORD: ${POSTGRES_PASSWORD:?POSTGRES_PASSWORD is required}",
        ),
        ("bearer from the environment", "Authorization: Bearer ${MIZAN_API_TOKEN}"),
        ("read from os.environ", 'token = os.environ["MIZAN_API_TOKEN"]'),
        # angle-bracket and word placeholders
        ("angle placeholder", "api_key: <your-api-key-here>"),
        ("public bundle placeholder", "NEXT_PUBLIC_API_KEY=<your-publishable-key>"),
        ("masked value", "password = 'xxxxxxxxxxxxxxxxxxxxxxxx'"),
        ("shouting placeholder", "secret_key = 'REPLACE_ME_WITH_A_REAL_KEY_LATER'"),
        ("vendor documentation key", "AWS_ACCESS_KEY_ID=AKIAIOSFODNN7EXAMPLE"),
        # Mizan's own hex identifiers: 64-hex hashes are everywhere in this codebase
        ("record content hash", 'audit_hash = "' + sha256 + '"'),
        ("chain link hash", "audit_prev_hash: " + sha256),
        ("policy hash", 'policy_hash = "' + sha256 + '"'),
        ("git object id", "commit " + sha1),
        ("40 hex digits in a secret-named field", "ALPACA_SECRET_KEY=" + sha1),
        # high entropy is not enough on its own: the name or the context must implicate it
        ("high entropy, innocuous name", "identifier = '" + high_entropy + "'"),
        # shape-only lookalikes
        ("jwt-shaped, not a jwt", "value = " + jwt_shaped),
        ("lowercase akia", "not_a_key=akia" + _rand(LOWER + DIGITS, 16, 507)),
        ("url without a password", "DATABASE_URL=postgresql://mizan@127.0.0.1:5432/mizan?sslmode=require"),
        ("css class starting sk-", 'class="sk-fading-circle sk-circle-container-large"'),
        ("token counters", "prompt_tokens=100, completion_tokens=80, total_tokens=180"),
        ("comma-separated origins", "MIZAN_CORS_ORIGINS=http://localhost:5173,http://localhost:3000"),
        ("vendor base url", "FEATHERLESS_BASE_URL=https://api.featherless.ai/v1"),
        ("type annotation", "SENSITIVE_KEY_PATTERNS: tuple[str, ...]"),
        ("prose about secrets", "The secret key is never committed to this repository, ever"),
    ]


POSITIVES = _positives()
NEGATIVES = _negatives()


def _cli(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(SCANNER_PATH), *args],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )


# ---------------------------------------------------------------------------
# detection
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("rule", "line", "secret"), POSITIVES, ids=[f"{i}-{r}" for i, (r, _, _) in enumerate(POSITIVES)]
)
def test_positive_fixture_is_detected(rule: str, line: str, secret: str) -> None:
    found = SCAN.scan_text(line, "fixture.txt")
    assert rule in {f.rule for f in found}, f"{rule} did not fire; got {sorted({f.rule for f in found})}"
    assert secret  # the fixture is non-empty


def test_every_rule_has_a_positive_fixture() -> None:
    """A rule added without a fixture here is a rule nobody has proved fires."""
    assert {rule.name for rule in SCAN.RULES} == {rule for rule, _, _ in POSITIVES}


@pytest.mark.parametrize(("label", "line"), NEGATIVES, ids=[label for label, _ in NEGATIVES])
def test_near_miss_does_not_trip_the_scanner(label: str, line: str) -> None:
    found = SCAN.scan_text(line, "fixture.txt")
    assert found == [], f"false positive on {label!r}: {sorted({f.rule for f in found})}"


def test_repository_placeholders_are_recognised_as_placeholders() -> None:
    for value in ("<your-key>", "${POSTGRES_PASSWORD}", "changeme", "REPLACE_ME", "", "xxxxxxxx"):
        assert SCAN.is_placeholder(value), value
    assert not SCAN.is_placeholder(_mixed(BASE62, 40, 601))


# ---------------------------------------------------------------------------
# a finding must never print the secret
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("rule", "line", "secret"), POSITIVES, ids=[f"{i}-{r}" for i, (r, _, _) in enumerate(POSITIVES)]
)
def test_finding_never_prints_the_secret_value(rule: str, line: str, secret: str) -> None:
    for finding in SCAN.scan_text(line, "fixture.txt"):
        rendered = finding.format()
        assert secret not in rendered, "the whole secret was printed"
        # at most REVEAL_CHARS leading characters may survive; nothing past them may.
        assert secret[SCAN.REVEAL_CHARS :] not in rendered
        if rule != "private-key-block":  # that rule's "secret" is the literal banner text
            assert "[REDACTED" in rendered


def test_cli_output_never_prints_the_secret(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    secret = _mixed(BASE62, 36, 602)
    (tmp_path / "leaky.py").write_text(f'api_key = "{secret}"\n', encoding="utf-8")
    assert SCAN.main(["--paths", str(tmp_path), "--root", str(tmp_path)]) == 1
    out = capsys.readouterr()
    combined = out.out + out.err
    assert secret not in combined
    assert secret[SCAN.REVEAL_CHARS :] not in combined
    assert "[REDACTED" in combined
    assert "leaky.py" in combined  # the location IS reported


# ---------------------------------------------------------------------------
# sensitive filenames
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "relpath",
    [".env", ".env.local", ".env.production", "deploy/id_rsa", "certs/server.key", "a/b/credentials.json",
     "secrets/service-account-prod.json", "ops/cluster.pem", "windows/store.pfx", ".netrc", ".npmrc"],
)  # fmt: skip
def test_sensitive_filename_is_a_finding_on_its_own(relpath: str) -> None:
    assert SCAN.is_sensitive_filename(relpath)


@pytest.mark.parametrize(
    "relpath",
    [".env.example", ".env.sample", ".env.template", "README.md", "docs/keys.md",
     "contracts/policy.schema.json", "mizan/contracts/canonical.py", "infra/postgres/init/001_roles.sql"],
)  # fmt: skip
def test_harmless_filename_is_not_a_finding(relpath: str) -> None:
    assert not SCAN.is_sensitive_filename(relpath)


def test_a_staged_dotenv_is_reported_by_filename_alone(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    nested = tmp_path / "config"
    nested.mkdir()
    (nested / ".env").write_text("HARMLESS=1\n", encoding="utf-8")
    assert SCAN.main(["--paths", str(nested), "--root", str(tmp_path)]) == 1
    assert "sensitive-file" in capsys.readouterr().out


# ---------------------------------------------------------------------------
# allow-listing
# ---------------------------------------------------------------------------
def _leaky_tree(tmp_path: Path, seed: int) -> str:
    (tmp_path / "fixtures").mkdir(exist_ok=True)
    secret = _mixed(BASE62, 32, seed)
    (tmp_path / "fixtures" / "sample.py").write_text(f'api_key = "{secret}"\n', encoding="utf-8")
    return secret


def test_allowlist_file_suppresses_a_matching_glob(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _leaky_tree(tmp_path, 701)
    args = ["--paths", str(tmp_path), "--root", str(tmp_path)]

    assert SCAN.main(args) == 1  # no allow-list yet
    capsys.readouterr()

    (tmp_path / SCAN.ALLOWLIST_FILENAME).write_text(
        "# deliberately secret-shaped test fixtures\n\nfixtures/*.py\n", encoding="utf-8"
    )
    assert SCAN.main(args) == 0
    assert "clean" in capsys.readouterr().out

    # ...and the allow-list can be switched off, which is what a review does.
    assert SCAN.main([*args, "--no-allowlist"]) == 1


def test_allowlist_glob_star_crosses_directory_separators(tmp_path: Path) -> None:
    deep = tmp_path / "tests" / "fixtures" / "deep"
    deep.mkdir(parents=True)
    secret = _mixed(BASE62, 32, 702)
    (deep / "sample.py").write_text(f'api_key = "{secret}"\n', encoding="utf-8")
    (tmp_path / SCAN.ALLOWLIST_FILENAME).write_text("tests/*\n", encoding="utf-8")
    assert SCAN.main(["--paths", str(tmp_path), "--root", str(tmp_path)]) == 0


def test_allowlist_does_not_suppress_paths_outside_the_glob(tmp_path: Path) -> None:
    _leaky_tree(tmp_path, 703)
    secret = _mixed(BASE62, 32, 704)
    (tmp_path / "app.py").write_text(f'client_secret = "{secret}"\n', encoding="utf-8")
    (tmp_path / SCAN.ALLOWLIST_FILENAME).write_text("fixtures/*.py\n", encoding="utf-8")
    assert SCAN.main(["--paths", str(tmp_path), "--root", str(tmp_path)]) == 1


@pytest.mark.parametrize("marker", list(SCAN.INLINE_ALLOW_MARKERS))
def test_inline_allow_marker_suppresses_that_line(tmp_path: Path, marker: str) -> None:
    secret = _mixed(BASE62, 32, 705)
    (tmp_path / "fixture.py").write_text(f'api_key = "{secret}"  # {marker}\n', encoding="utf-8")
    assert SCAN.main(["--paths", str(tmp_path), "--root", str(tmp_path)]) == 0


def test_inline_allow_marker_only_suppresses_its_own_line(tmp_path: Path) -> None:
    allowed, leaked = _mixed(BASE62, 32, 706), _mixed(BASE62, 32, 707)
    (tmp_path / "fixture.py").write_text(
        f'allowed = "{allowed}"  # {SCAN.INLINE_ALLOW_MARKERS[0]}\nsecret_token = "{leaked}"\n',
        encoding="utf-8",
    )
    assert SCAN.main(["--paths", str(tmp_path), "--root", str(tmp_path)]) == 1


# ---------------------------------------------------------------------------
# exit codes
# ---------------------------------------------------------------------------
def test_clean_tree_exits_zero(tmp_path: Path) -> None:
    (tmp_path / "ok.py").write_text("ALPACA_PAPER = 'true'\n", encoding="utf-8")
    assert SCAN.main(["--paths", str(tmp_path), "--root", str(tmp_path)]) == 0


def test_findings_exit_one(tmp_path: Path) -> None:
    _leaky_tree(tmp_path, 708)
    assert SCAN.main(["--paths", str(tmp_path), "--root", str(tmp_path)]) == 1


def test_no_mode_is_a_usage_error(capsys: pytest.CaptureFixture[str]) -> None:
    assert SCAN.main([]) == 2
    assert "required" in capsys.readouterr().err


def test_git_mode_outside_a_repository_is_an_environment_error(tmp_path: Path) -> None:
    if SCAN.find_repo_root(tmp_path) is not None:  # pragma: no cover - depends on the temp dir
        pytest.skip("the temporary directory is inside a git repository")
    assert SCAN.main(["--all", "--root", str(tmp_path)]) == 2


# ---------------------------------------------------------------------------
# the scanner, run exactly as CI and the pre-commit hook run it
# ---------------------------------------------------------------------------
def test_self_test_exits_zero() -> None:
    result = _cli("--self-test")
    assert result.returncode == 0, result.stdout + result.stderr
    assert "OK" in result.stdout


def test_all_is_clean_on_this_repository() -> None:
    result = _cli("--all")
    assert result.returncode == 0, result.stdout + result.stderr
    assert "clean" in result.stdout


def test_history_is_clean_on_this_repository() -> None:
    result = _cli("--history")
    assert result.returncode == 0, result.stdout + result.stderr
    assert "clean" in result.stdout
