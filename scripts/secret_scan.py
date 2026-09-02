#!/usr/bin/env python3
"""Mizan secret scanner. Standard library only; no network; no third-party imports.

Modes
-----
  --staged        scan the content that is staged for commit (what the pre-commit hook runs)
  --all           scan every tracked file plus untracked files that are not gitignored
  --history       scan every ADDED line of every commit on every branch (reports commit + path)
  --paths P ...   scan the given files and/or directories
  --self-test     run the built-in positive and negative cases and exit

Exit codes: 0 clean, 1 findings (or a failed self-test), 2 usage or environment error.

Every finding is printed as ``[rule] path:line - preview`` with the secret REDACTED
(first three characters, then ``...[REDACTED n chars]``). The scanner never prints a secret.

Allow-listing
-------------
* ``.secretscan-allow`` at the repository root: one glob per line matched against the
  repository-relative POSIX path (``*`` also matches ``/``). ``#`` starts a comment.
* Inline: a line containing ``secret-scan: allow`` is skipped. Use it for test fixtures
  that are deliberately secret-shaped, never for a real credential.
"""

from __future__ import annotations

import argparse
import base64
import fnmatch
import json
import math
import os
import random
import re
import subprocess
import sys
from collections import Counter
from collections.abc import Callable, Iterable, Iterator, Sequence
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath

__all__ = [
    "RULES",
    "Finding",
    "Rule",
    "is_placeholder",
    "is_sensitive_filename",
    "main",
    "scan_text",
    "shannon_entropy",
]

ALLOWLIST_FILENAME = ".secretscan-allow"
INLINE_ALLOW_MARKERS = ("secret-scan: allow", "secret-scan:allow")

SKIP_DIR_NAMES = frozenset(
    {
        ".git",
        ".worktrees",
        "node_modules",
        "__pycache__",
        ".venv",
        "venv",
        ".mypy_cache",
        ".ruff_cache",
        ".pytest_cache",
        ".hypothesis",
        ".tox",
        ".nox",
        "dist",
        "build",
        ".idea",
        ".vscode",
    }
)
SKIP_DIR_GLOBS = (".venv*", "*.egg-info")
SKIP_FILE_GLOBS = (
    "*.lock",
    "package-lock.json",
    "yarn.lock",
    "pnpm-lock.yaml",
    "poetry.lock",
    "uv.lock",
    "Pipfile.lock",
    "*.min.js",
    "*.min.css",
    "*.map",
)
BINARY_EXTENSIONS = frozenset(
    {
        ".png", ".jpg", ".jpeg", ".gif", ".bmp", ".ico", ".webp", ".svgz", ".pdf",
        ".zip", ".gz", ".tgz", ".bz2", ".xz", ".7z", ".rar", ".jar", ".war",
        ".pyc", ".pyo", ".so", ".dll", ".dylib", ".exe", ".bin", ".o", ".a",
        ".db", ".sqlite", ".sqlite3", ".db-wal", ".db-shm", ".sqlite-wal", ".sqlite-shm",
        ".woff", ".woff2", ".ttf", ".otf", ".eot", ".mp3", ".mp4", ".wav", ".mov", ".avi",
        ".parquet", ".feather", ".npy", ".npz", ".pkl", ".pickle",
    }
)  # fmt: skip
MAX_FILE_BYTES = 10 * 1024 * 1024
BINARY_SNIFF_BYTES = 8192

MIN_GENERIC_LENGTH = 20
GENERIC_ENTROPY_THRESHOLD = 4.0  # strictly greater than
PREFIXED_TOKEN_ENTROPY_FLOOR = 3.5  # at least
REVEAL_CHARS = 3

# Files whose *name* alone is a finding when staged/tracked (a real .env, a private key...).
SENSITIVE_FILE_GLOBS = (
    "*.pem",
    "*.key",
    "*.p12",
    "*.pfx",
    "*.jks",
    "*.keystore",
    "*.ppk",
    "id_rsa*",
    "id_dsa*",
    "id_ecdsa*",
    "id_ed25519*",
    ".netrc",
    ".pypirc",
    ".npmrc",
    ".htpasswd",
    "credentials.json",
    "*.credentials",
    "service-account*.json",
)
ENV_FILE_ALLOWED_SUFFIXES = frozenset({"example", "sample", "template", "dist", "defaults", "schema"})

PLACEHOLDER_WORDS = frozenset(
    {
        "changeme", "change_me", "change-me", "password", "passwd", "pass", "pwd", "secret",
        "example", "placeholder", "dummy", "redacted", "sample", "none", "null", "true", "false",
        "todo", "tbd", "xxx", "postgres", "mysql", "redis", "user", "root", "admin", "test",
    }
)  # fmt: skip
PLACEHOLDER_SUBSTRINGS = (
    "example",
    "placeholder",
    "changeme",
    "change-me",
    "change_me",
    "redacted",
    "your_",
    "your-",
    "<your",
    "dummy",
    "replace_me",
    "replace-me",
    "insert_",
    "fake_",
    "fake-",
)

DIGITS = "0123456789"
UPPER = "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
LOWER = UPPER.lower()
UPPER_DIGITS = UPPER + DIGITS
BASE62 = UPPER + LOWER + DIGITS
BASE64URL = BASE62 + "-_"


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------
def shannon_entropy(value: str) -> float:
    """Bits of entropy per character over the characters of ``value``."""
    if not value:
        return 0.0
    total = len(value)
    return -sum((count / total) * math.log2(count / total) for count in Counter(value).values())


_ENV_REF_RE = re.compile(r"\$[A-Za-z_][A-Za-z0-9_]*")
_SAME_CHAR_RE = re.compile(r"(.)\1*")
_MASK_CHARS_RE = re.compile(r"[x*#._\-?]+", re.IGNORECASE)


def is_placeholder(value: str) -> bool:
    """True when ``value`` is obviously a stand-in, not a credential."""
    v = value.strip().strip("\"'`")
    if not v:
        return True
    if v.startswith("<") and v.endswith(">"):
        return True
    if v.startswith(("${", "{{", "$(", "%(", "%%", "{%")):
        return True
    if _ENV_REF_RE.fullmatch(v):
        return True
    if _SAME_CHAR_RE.fullmatch(v):
        return True
    if _MASK_CHARS_RE.fullmatch(v):
        return True
    low = v.lower()
    if low in PLACEHOLDER_WORDS:
        return True
    return any(marker in low for marker in PLACEHOLDER_SUBSTRINGS)


def _mixed_classes(value: str) -> bool:
    return (
        any(c in LOWER for c in value)
        and any(c in UPPER for c in value)
        and any(c in DIGITS for c in value)
    )


def _looks_like_jwt(match: re.Match[str], _line: str) -> bool:
    header = match.group(0).split(".")[0]
    try:
        padded = header + "=" * (-len(header) % 4)
        decoded = json.loads(base64.urlsafe_b64decode(padded.encode("ascii")))
    except (ValueError, UnicodeDecodeError):
        return False
    return isinstance(decoded, dict) and "alg" in decoded


_ALPACA_CONTEXT_RE = re.compile(r"alpaca|secret", re.IGNORECASE)


def _alpaca_secret_ok(match: re.Match[str], line: str) -> bool:
    value = match.group(0)
    if not _ALPACA_CONTEXT_RE.search(line):
        return False
    return (
        _mixed_classes(value)
        and shannon_entropy(value) > GENERIC_ENTROPY_THRESHOLD
        and not is_placeholder(value)
    )


def _prefixed_token_ok(prefix_len: int) -> Callable[[re.Match[str], str], bool]:
    def check(match: re.Match[str], _line: str) -> bool:
        value = match.group(0)
        body = value[prefix_len:]
        return not is_placeholder(value) and shannon_entropy(body) >= PREFIXED_TOKEN_ENTROPY_FLOOR

    return check


def _not_placeholder(match: re.Match[str], _line: str) -> bool:
    return not is_placeholder(match.group("value") if "value" in match.groupdict() else match.group(0))


def _db_password_ok(match: re.Match[str], _line: str) -> bool:
    value = match.group("value")
    return len(value) >= 4 and not is_placeholder(value)


def _entropy_secret_ok(match: re.Match[str], _line: str) -> bool:
    value = match.group("value")
    return not is_placeholder(value) and shannon_entropy(value) > GENERIC_ENTROPY_THRESHOLD


_URL_SCHEME_RE = re.compile(r"^[a-z][a-z0-9+.\-]*://", re.IGNORECASE)
_SEGMENT_SPLIT_RE = re.compile(r"[:,;]")


def _generic_assignment_ok(match: re.Match[str], _line: str) -> bool:
    """Any ':'/','-separated segment that is long and high-entropy makes the value a finding.

    Splitting handles ``agent_id:token,agent_id:token`` lists. Plain URLs are left to the
    database-url rule, which knows where the password is.
    """
    value = match.group("value")
    if _URL_SCHEME_RE.match(value):
        return False
    for segment in _SEGMENT_SPLIT_RE.split(value):
        if (
            len(segment) >= MIN_GENERIC_LENGTH
            and shannon_entropy(segment) > GENERIC_ENTROPY_THRESHOLD
            and not is_placeholder(segment)
        ):
            return True
    return False


# ---------------------------------------------------------------------------
# rules
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class Rule:
    name: str
    pattern: re.Pattern[str]
    group: str | int = 0
    validate: Callable[[re.Match[str], str], bool] | None = None

    def secret_span(self, match: re.Match[str]) -> tuple[int, int]:
        return match.span(self.group)


_GENERIC_ASSIGNMENT_RE = re.compile(
    r"(?P<name>[A-Za-z0-9_.\-]*(?:key|secret|token|passw(?:or)?d|pwd|credential)[A-Za-z0-9_.\-]*)"
    r"[\"']?\s*(?:[:=]|=>)\s*"
    r"(?P<q>[\"']?)"
    r"(?P<value>[A-Za-z0-9+/=_\-.!@#$%^&*~:,;]{20,})"
    r"(?P=q)",
    re.IGNORECASE,
)

RULES: tuple[Rule, ...] = (
    Rule(
        "private-key-block",
        re.compile(r"-----BEGIN (?:[A-Z][A-Z ]* )?PRIVATE KEY(?: BLOCK)?-----"),
    ),
    Rule(
        "anthropic-api-key",
        re.compile(r"\bsk-ant-[A-Za-z0-9_\-]{20,}"),
        validate=_prefixed_token_ok(len("sk-ant-")),
    ),
    Rule(
        "openai-or-featherless-api-key",
        re.compile(r"\bsk-[A-Za-z0-9_\-]{20,}"),
        validate=_prefixed_token_ok(len("sk-")),
    ),
    Rule("aws-access-key-id", re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b"), validate=_not_placeholder),
    Rule(
        "github-token",
        re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,})\b"),
        validate=_not_placeholder,
    ),
    Rule("slack-token", re.compile(r"\bxox[abprs]-[A-Za-z0-9\-]{10,}"), validate=_not_placeholder),
    Rule(
        "jwt",
        re.compile(r"\beyJ[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}"),
        validate=_looks_like_jwt,
    ),
    Rule(
        "database-url-with-password",
        re.compile(
            r"\b(?:postgres(?:ql)?|mysql|mariadb|mssql|redis|rediss|mongodb(?:\+srv)?|amqps?)://"
            r"(?P<user>[^\s:/@]+):(?P<value>[^\s@/]+)@"
        ),
        group="value",
        validate=_db_password_ok,
    ),
    Rule(
        "alpaca-key-id",
        re.compile(r"\b[PA]K[A-Z0-9]{16,}\b"),
        validate=_prefixed_token_ok(2),
    ),
    Rule("alpaca-secret-key", re.compile(r"\b[A-Za-z0-9]{40}\b"), validate=_alpaca_secret_ok),
    Rule(
        "public-bundle-secret",
        re.compile(
            r"\b(?P<name>(?:NEXT_PUBLIC|VITE|REACT_APP|EXPO_PUBLIC|NUXT_PUBLIC|PUBLIC)_[A-Z0-9_]*"
            r"(?:KEY|SECRET|TOKEN|PASSWORD|PASSWD|CREDENTIAL|PRIVATE)[A-Z0-9_]*)"
            r"[\"']?\s*[:=]\s*(?P<q>[\"']?)(?P<value>[^\s\"']{8,})(?P=q)",
            re.IGNORECASE,
        ),
        group="value",
        validate=_not_placeholder,
    ),
    Rule(
        "bearer-token",
        re.compile(r"\b(?:bearer|basic)\s+(?P<value>[A-Za-z0-9+/=_\-.]{20,})", re.IGNORECASE),
        group="value",
        validate=_entropy_secret_ok,
    ),
    Rule("high-entropy-assignment", _GENERIC_ASSIGNMENT_RE, group="value", validate=_generic_assignment_ok),
)


# ---------------------------------------------------------------------------
# findings
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class Finding:
    path: str
    line_no: int
    rule: str
    preview: str
    commit: str | None = None

    def format(self) -> str:
        where = f"{self.path}:{self.line_no}"
        if self.commit:
            where += f" @ commit {self.commit[:12]}"
        return f"[{self.rule}] {where} - {self.preview}"


def _redact(line: str, spans: Sequence[tuple[int, int]]) -> str:
    out: list[str] = []
    cursor = 0
    for start, end in sorted(spans):
        if start < cursor:
            continue
        out.append(line[cursor:start])
        secret = line[start:end]
        out.append(f"{secret[:REVEAL_CHARS]}...[REDACTED {len(secret)} chars]")
        cursor = end
    out.append(line[cursor:])
    preview = "".join(out).strip()
    if len(preview) > 160:
        preview = preview[:157] + "..."
    return preview


def _line_matches(line: str) -> list[tuple[Rule, tuple[int, int]]]:
    hits: list[tuple[Rule, tuple[int, int]]] = []
    for rule in RULES:
        for match in rule.pattern.finditer(line):
            if rule.validate is not None and not rule.validate(match, line):
                continue
            span = rule.secret_span(match)
            if any(not (span[1] <= s or span[0] >= e) for _, (s, e) in hits):
                continue  # a more specific rule already claimed this span
            hits.append((rule, span))
    return hits


def scan_text(
    text: str,
    path: str,
    *,
    commit: str | None = None,
    honour_inline_allow: bool = True,
    first_line_no: int = 1,
) -> list[Finding]:
    """Scan ``text`` line by line. ``path`` is only used for reporting."""
    findings: list[Finding] = []
    for offset, line in enumerate(text.splitlines()):
        if honour_inline_allow and any(marker in line for marker in INLINE_ALLOW_MARKERS):
            continue
        hits = _line_matches(line)
        if not hits:
            continue
        spans = [span for _, span in hits]
        preview = _redact(line, spans)
        for rule, _ in hits:
            findings.append(Finding(path, first_line_no + offset, rule.name, preview, commit))
    return findings


def is_sensitive_filename(relpath: str) -> bool:
    name = PurePosixPath(relpath).name
    if name == ".env":
        return True
    if name.startswith(".env."):
        return name[len(".env.") :].lower() not in ENV_FILE_ALLOWED_SUFFIXES
    return any(fnmatch.fnmatchcase(name, pattern) for pattern in SENSITIVE_FILE_GLOBS)


# ---------------------------------------------------------------------------
# file and repository plumbing
# ---------------------------------------------------------------------------
@dataclass
class Config:
    root: Path
    allow_patterns: list[str] = field(default_factory=list)
    verbose: bool = False
    files_scanned: int = 0
    commits_scanned: int = 0
    skipped: int = 0

    def is_allowed(self, relpath: str) -> bool:
        return any(fnmatch.fnmatchcase(relpath, pattern) for pattern in self.allow_patterns)

    def note(self, message: str) -> None:
        if self.verbose:
            print(f"secret-scan: {message}", file=sys.stderr)


def _posix(path: Path | str) -> str:
    return str(path).replace("\\", "/")


def _should_skip_path(relpath: str) -> bool:
    parts = PurePosixPath(relpath).parts
    for part in parts[:-1]:
        if part in SKIP_DIR_NAMES or any(fnmatch.fnmatchcase(part, g) for g in SKIP_DIR_GLOBS):
            return True
    name = parts[-1] if parts else ""
    return any(fnmatch.fnmatchcase(name, g) for g in SKIP_FILE_GLOBS)


def _is_binary(data: bytes, relpath: str) -> bool:
    if PurePosixPath(relpath).suffix.lower() in BINARY_EXTENSIONS:
        return True
    return b"\x00" in data[:BINARY_SNIFF_BYTES]


def _git(root: Path, *args: str) -> bytes:
    proc = subprocess.run(
        ["git", "-C", str(root), *args],
        capture_output=True,
        check=False,
    )
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr.decode("utf-8", "replace").strip() or f"git {' '.join(args)} failed")
    return proc.stdout


def find_repo_root(start: Path) -> Path | None:
    try:
        out = _git(start, "rev-parse", "--show-toplevel")
    except (RuntimeError, OSError):
        return None
    return Path(out.decode("utf-8", "replace").strip())


def load_allowlist(root: Path, explicit: Path | None) -> list[str]:
    path = explicit if explicit is not None else root / ALLOWLIST_FILENAME
    if not path.is_file():
        return []
    patterns: list[str] = []
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if line and not line.startswith("#"):
            patterns.append(line)
    return patterns


def scan_blob(cfg: Config, relpath: str, data: bytes, *, commit: str | None = None) -> list[Finding]:
    if cfg.is_allowed(relpath):
        cfg.note(f"allow-listed {relpath}")
        return []
    findings: list[Finding] = []
    if is_sensitive_filename(relpath):
        findings.append(Finding(relpath, 0, "sensitive-file", "file name alone is a finding", commit))
    if len(data) > MAX_FILE_BYTES:
        cfg.note(f"skipped {relpath}: larger than {MAX_FILE_BYTES} bytes")
        cfg.skipped += 1
        return findings
    if _is_binary(data, relpath):
        cfg.note(f"skipped {relpath}: binary")
        cfg.skipped += 1
        return findings
    cfg.files_scanned += 1
    findings.extend(scan_text(data.decode("utf-8", "replace"), relpath, commit=commit))
    return findings


def _relpath(cfg: Config, path: Path) -> str:
    try:
        return _posix(path.resolve().relative_to(cfg.root.resolve()))
    except ValueError:
        return _posix(path)


def _iter_disk_files(cfg: Config, targets: Iterable[Path]) -> Iterator[tuple[str, Path]]:
    for target in targets:
        full_target = target if target.is_absolute() else cfg.root / target
        if full_target.is_file():
            yield _relpath(cfg, full_target), full_target
            continue
        if not full_target.is_dir():
            cfg.note(f"skipped {full_target}: not found")
            cfg.skipped += 1
            continue
        for dirpath, dirnames, filenames in os.walk(full_target):
            dirnames[:] = sorted(
                d
                for d in dirnames
                if d not in SKIP_DIR_NAMES and not any(fnmatch.fnmatchcase(d, g) for g in SKIP_DIR_GLOBS)
            )
            for filename in sorted(filenames):
                full = Path(dirpath) / filename
                rel = _relpath(cfg, full)
                if _should_skip_path(rel):
                    continue
                yield rel, full


def scan_disk(cfg: Config, targets: Iterable[Path]) -> list[Finding]:
    findings: list[Finding] = []
    for rel, full in _iter_disk_files(cfg, targets):
        try:
            data = full.read_bytes()
        except OSError as exc:
            cfg.note(f"skipped {rel}: {exc}")
            cfg.skipped += 1
            continue
        findings.extend(scan_blob(cfg, rel, data))
    return findings


def scan_all(cfg: Config) -> list[Finding]:
    """Tracked files plus untracked files that are not ignored (working-tree content)."""
    out = _git(cfg.root, "ls-files", "-z", "--cached", "--others", "--exclude-standard")
    seen: set[str] = set()
    findings: list[Finding] = []
    for raw in out.split(b"\0"):
        if not raw:
            continue
        rel = raw.decode("utf-8", "replace")
        if rel in seen or _should_skip_path(rel):
            continue
        seen.add(rel)
        full = cfg.root / rel
        if not full.is_file():
            continue  # deleted in the working tree, or a submodule
        findings.extend(scan_blob(cfg, rel, full.read_bytes()))
    return findings


def scan_staged(cfg: Config) -> list[Finding]:
    """Content staged in the index (not the working tree), as a pre-commit hook must."""
    out = _git(cfg.root, "diff", "--cached", "--name-only", "--diff-filter=ACMR", "-z")
    findings: list[Finding] = []
    for raw in out.split(b"\0"):
        if not raw:
            continue
        rel = raw.decode("utf-8", "replace")
        if _should_skip_path(rel):
            continue
        try:
            data = _git(cfg.root, "show", f":{rel}")
        except RuntimeError as exc:
            cfg.note(f"skipped {rel}: {exc}")
            cfg.skipped += 1
            continue
        findings.extend(scan_blob(cfg, rel, data))
    return findings


_COMMIT_MARKER = b"\0MIZAN-COMMIT "
_HUNK_RE = re.compile(rb"^@@ -\d+(?:,\d+)? \+(\d+)(?:,\d+)? @@")


def scan_history(cfg: Config) -> list[Finding]:
    """Every added line in every commit reachable from any ref. Reports commit and path."""
    cmd = [
        "git", "-C", str(cfg.root), "log", "--all", "-p", "-m", "--no-color", "--no-ext-diff",
        "--format=%x00MIZAN-COMMIT %H",
    ]  # fmt: skip
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    assert proc.stdout is not None
    findings: list[Finding] = []
    commit: str | None = None
    path: str | None = None
    path_skipped = False
    path_flagged: set[tuple[str, str]] = set()
    new_line = 0
    for raw_line in proc.stdout:
        raw = raw_line.rstrip(b"\r\n")
        if raw.startswith(_COMMIT_MARKER):
            commit = raw[len(_COMMIT_MARKER) :].decode("ascii", "replace").strip()
            cfg.commits_scanned += 1
            path = None
            continue
        if raw.startswith(b"+++ "):
            target = raw[4:].decode("utf-8", "replace")
            if target == "/dev/null":
                path = None
                continue
            path = target[2:] if target.startswith("b/") else target
            path_skipped = _should_skip_path(path) or cfg.is_allowed(path)
            new_line = 0
            if not path_skipped and commit is not None and is_sensitive_filename(path):
                key = (commit, path)
                if key not in path_flagged:
                    path_flagged.add(key)
                    findings.append(Finding(path, 0, "sensitive-file", "file name alone is a finding", commit))
            continue
        if path is None:
            continue
        hunk = _HUNK_RE.match(raw)
        if hunk:
            new_line = int(hunk.group(1))
            continue
        if raw.startswith(b"+"):
            if not path_skipped:
                text = raw[1:].decode("utf-8", "replace")
                findings.extend(scan_text(text, path, commit=commit, first_line_no=new_line))
            new_line += 1
        elif raw.startswith(b" "):
            new_line += 1
    proc.wait()
    if proc.returncode != 0:
        err = proc.stderr.read().decode("utf-8", "replace").strip() if proc.stderr else ""
        raise RuntimeError(err or "git log failed")
    return findings


# ---------------------------------------------------------------------------
# self-test
# ---------------------------------------------------------------------------
def _fake(alphabet: str, length: int, seed: int) -> str:
    """Deterministic pseudo-random string, built at runtime so no secret-shaped literal exists here."""
    rng = random.Random(seed)
    return "".join(rng.choice(alphabet) for _ in range(length))


def _fake_mixed(alphabet: str, length: int, seed: int) -> str:
    for attempt in range(1000):
        candidate = _fake(alphabet, length, seed + attempt)
        if _mixed_classes(candidate) and shannon_entropy(candidate) > GENERIC_ENTROPY_THRESHOLD:
            return candidate
    raise AssertionError("could not build a mixed-class sample")


def _fake_jwt() -> str:
    def b64(obj: dict[str, object]) -> str:
        return base64.urlsafe_b64encode(json.dumps(obj).encode()).decode().rstrip("=")

    return ".".join([b64({"alg": "HS256", "typ": "JWT"}), b64({"sub": "agent-a"}), _fake(BASE64URL, 43, 10)])


def self_test_cases() -> tuple[list[tuple[str, str, str]], list[str]]:
    """Returns (positives as (expected_rule, line, secret_substring), negatives as lines)."""
    aws = "AKIA" + _fake(UPPER_DIGITS, 16, 1)
    alpaca_key = "PK" + _fake_mixed(UPPER_DIGITS + LOWER, 18, 2).upper()
    alpaca_secret = _fake_mixed(BASE62, 40, 3)
    openai = "sk-" + "proj-" + _fake_mixed(BASE62, 48, 4)
    anthropic = "sk-ant-" + "api03-" + _fake_mixed(BASE64URL, 60, 5)
    ghp = "ghp_" + _fake_mixed(BASE62, 36, 6)
    gh_pat = "github_pat_" + _fake_mixed(BASE62, 22, 7) + "_" + _fake_mixed(BASE62, 59, 8)
    slack = "xoxb-" + _fake(DIGITS, 12, 9) + "-" + _fake(DIGITS, 12, 10) + "-" + _fake_mixed(BASE62, 24, 11)
    jwt = _fake_jwt()
    db_pw = _fake_mixed(BASE62, 24, 12)
    generic = _fake_mixed(BASE62, 32, 13)
    public_env = _fake_mixed(BASE62, 32, 14)
    bearer = _fake_mixed(BASE62, 40, 15)
    tok_a, tok_b = _fake_mixed(BASE62, 32, 16), _fake_mixed(BASE62, 32, 17)
    positives = [
        ("aws-access-key-id", f"AWS_ACCESS_KEY_ID={aws}", aws),
        ("alpaca-key-id", f"ALPACA_API_KEY={alpaca_key}", alpaca_key),
        ("alpaca-secret-key", f"ALPACA_SECRET_KEY={alpaca_secret}", alpaca_secret),
        ("openai-or-featherless-api-key", f'OPENAI_API_KEY="{openai}"', openai),
        ("anthropic-api-key", f"ANTHROPIC_API_KEY={anthropic}", anthropic),
        ("github-token", f"token = '{ghp}'", ghp),
        ("github-token", f"GH_TOKEN: {gh_pat}", gh_pat),
        ("slack-token", f"SLACK_BOT_TOKEN={slack}", slack),
        ("private-key-block", "-----BEGIN " + "RSA PRIVATE KEY-----", "RSA PRIVATE KEY"),
        ("private-key-block", "-----BEGIN " + "OPENSSH PRIVATE KEY-----", "OPENSSH PRIVATE KEY"),
        ("jwt", f"Authorization: Bearer {jwt}", jwt),
        ("database-url-with-password", f"DATABASE_URL=postgresql://mizan:{db_pw}@db.internal:5432/mizan", db_pw),  # secret-scan: allow
        ("database-url-with-password", f"REDIS_URL=redis://default:{db_pw}@cache:6379/0", db_pw),  # secret-scan: allow
        ("high-entropy-assignment", f'api_key = "{generic}"', generic),
        ("high-entropy-assignment", f'  "client_secret": "{generic}",', generic),
        ("high-entropy-assignment", f"password: {generic}", generic),
        ("high-entropy-assignment", f"MIZAN_API_TOKENS=agent-a:{tok_a},agent-b:{tok_b}", tok_a),
        ("public-bundle-secret", f"NEXT_PUBLIC_API_KEY={public_env}", public_env),  # secret-scan: allow
        ("public-bundle-secret", f"VITE_SECRET_TOKEN='{public_env}'", public_env),  # secret-scan: allow
        ("bearer-token", f"Authorization: Bearer {bearer}", bearer),
    ]
    hex64 = _fake("0123456789abcdef", 64, 18)
    hex40 = _fake("0123456789abcdef", 40, 19)
    negatives = [
        "ALPACA_API_KEY=",
        "ALPACA_SECRET_KEY=",
        "POSTGRES_PASSWORD=",
        "MIZAN_API_TOKENS=",
        "ALPACA_PAPER=true",
        "# DATABASE_URL=postgresql://user:pass@127.0.0.1:5432/mizan?sslmode=require",
        "DATABASE_URL=postgresql://mizan:${POSTGRES_PASSWORD}@127.0.0.1:5432/mizan",
        'secret_key="paper-secret"',
        'secret = "must-not-be-printed"',
        f'policy_hash = "{hex64}"',
        f'audit_hash = "{hex64}"',
        f"commit {hex40}",
        f"ALPACA_SECRET_KEY={hex40}",
        "AWS_ACCESS_KEY_ID=AKIAIOSFODNN7EXAMPLE",
        "OPENAI_API_KEY=${OPENAI_API_KEY}",
        "api_key: <your-api-key-here>",
        'token = os.environ["MIZAN_API_TOKEN"]',
        'class="sk-fading-circle sk-circle-container-large"',
        "FEATHERLESS_BASE_URL=https://api.featherless.ai/v1",
        "token_url = 'https://auth.example.com/oauth2/token/endpoint'",
        "prompt_tokens=100, completion_tokens=80, total_tokens=180",
        'known-first-party = ["mizan", "tests"]',
        "MIZAN_CORS_ORIGINS=http://localhost:5173,http://localhost:3000",
        f"x = '{_fake_mixed(BASE62, 40, 20)}'",  # high entropy but no secret-like name and no context word
        "POSTGRES_PASSWORD: mizan-ci  # ephemeral CI service container",
        "SENSITIVE_KEY_PATTERNS: tuple[str, ...]",
        f'api_key = "{generic}"  # secret-scan: allow',
        "password = 'xxxxxxxxxxxxxxxxxxxxxxxxxxxxxx'",
        "secret_key = 'REPLACE_ME_WITH_A_REAL_KEY_LATER'",
    ]
    return positives, negatives


def run_self_test() -> int:
    positives, negatives = self_test_cases()
    failures: list[str] = []
    for expected_rule, line, secret in positives:
        found = scan_text(line, "self-test")
        rules = {f.rule for f in found}
        if expected_rule not in rules:
            start = line.find(secret)
            failures.append(f"MISSED  [{expected_rule}] on: {_redact(line, [(start, start + len(secret))])}")
            continue
        for f in found:
            if secret in f.format():
                failures.append(f"LEAKED  [{f.rule}] the secret appeared in the report")
    for line in negatives:
        found = scan_text(line, "self-test")
        if found:
            failures.append(f"FALSE+  {[f.rule for f in found]} on: {line[:100]}")
    sensitive = (".env", ".env.local", "deploy/id_rsa", "certs/server.key", "a/b/credentials.json")
    harmless = (".env.example", ".env.sample", "README.md", "docs/keys.md", "contracts/policy.schema.json")
    for name in sensitive:
        if not is_sensitive_filename(name):
            failures.append(f"MISSED  sensitive filename {name}")
    for name in harmless:
        if is_sensitive_filename(name):
            failures.append(f"FALSE+  sensitive filename {name}")
    total = len(positives) + len(negatives) + len(sensitive) + len(harmless)
    if failures:
        print("\n".join(failures))
        print(f"secret-scan self-test: FAILED ({len(failures)} of {total} cases)")
        return 1
    print(
        f"secret-scan self-test: OK ({total} cases: {len(positives)} positive, "
        f"{len(negatives)} negative, {len(sensitive) + len(harmless)} filenames)"
    )
    return 0


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="secret_scan.py",
        description="Scan for committed secrets. Exit 1 on findings; secrets are never printed.",
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--staged", action="store_true", help="scan content staged for commit")
    mode.add_argument("--all", action="store_true", help="scan tracked + untracked (non-ignored) files")
    mode.add_argument("--history", action="store_true", help="scan every added line in every commit on every branch")
    mode.add_argument("--paths", nargs="+", metavar="PATH", help="scan these files/directories")
    mode.add_argument("--self-test", action="store_true", help="run the built-in positive/negative cases")
    parser.add_argument("--root", type=Path, default=None, help="repository root (default: git toplevel or cwd)")
    parser.add_argument(
        "--allowlist", type=Path, default=None, help=f"allow-list file (default: <root>/{ALLOWLIST_FILENAME})"
    )
    parser.add_argument("--no-allowlist", action="store_true", help="ignore the allow-list file")
    parser.add_argument("-v", "--verbose", action="store_true", help="report skipped and allow-listed files on stderr")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    if args.self_test:
        return run_self_test()
    if not (args.staged or args.all or args.history or args.paths):
        parser.print_usage(sys.stderr)
        print("secret_scan.py: one of --staged, --all, --history, --paths or --self-test is required", file=sys.stderr)
        return 2

    cwd = Path.cwd()
    root = (args.root or find_repo_root(cwd) or cwd).resolve()
    needs_git = args.staged or args.all or args.history
    if needs_git and find_repo_root(root) is None:
        print(f"secret_scan.py: {root} is not a git repository (required for the requested mode)", file=sys.stderr)
        return 2
    cfg = Config(root=root, verbose=args.verbose)
    if not args.no_allowlist:
        cfg.allow_patterns = load_allowlist(root, args.allowlist)
        if cfg.allow_patterns:
            cfg.note(f"{len(cfg.allow_patterns)} allow-list pattern(s) loaded")

    try:
        if args.staged:
            findings = scan_staged(cfg)
            scope = "staged"
        elif args.all:
            findings = scan_all(cfg)
            scope = "all"
        elif args.history:
            findings = scan_history(cfg)
            scope = "history"
        else:
            findings = scan_disk(cfg, [Path(p) for p in args.paths])
            scope = "paths"
    except RuntimeError as exc:
        print(f"secret_scan.py: {exc}", file=sys.stderr)
        return 2

    findings = sorted(set(findings), key=lambda f: (f.commit or "", f.path, f.line_no, f.rule))
    for finding in findings:
        print(finding.format())
    scanned = f"{cfg.commits_scanned} commit(s)" if scope == "history" else f"{cfg.files_scanned} file(s)"
    if findings:
        locations = {(f.commit, f.path) for f in findings}
        print(
            f"secret-scan ({scope}): {len(findings)} finding(s) in {len(locations)} location(s); "
            f"scanned {scanned}. Rotate any real credential and rewrite history if it was committed."
        )
        return 1
    print(f"secret-scan ({scope}): clean; scanned {scanned}, skipped {cfg.skipped}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
