# Contributing to Mizan

Mizan is a deterministic pre-trade governance layer between AI trading agents and a broker. **Paper
trading only, always.** Read `docs/MIZAN-MASTER-PLAN-v2.md` §4 (Hard Rules) and `contracts/README.md`
before your first change; they are binding, and most of what follows is machinery for keeping them true.

---

## 1. The lane model

Work is divided into lanes that run concurrently against a frozen contract set. A lane writes **only**
inside the paths it owns. If your change needs something outside them, you do not make it — you file a
request (§5).

```
mizan/contracts/      L0   frozen with contracts/  (generated types + canonical utils)
mizan/policy/         L1
mizan/risk/           L1
mizan/governor/       L2
mizan/advisory/       L2   LLM adapter — the only enforcement-adjacent module allowed to read proposal.reasoning
mizan/authorization/  L2
mizan/audit/          L2
mizan/replay/         L2
mizan/execution/      L3
mizan/adapters/       L3
mizan/sdk/            L3
mizan/api/            L3
mizan/console/        L4
app/                  legacy (Rahmat) — read-only salvage reference; its tests must keep passing
tests/contracts/      L0        tests/invariants/   READ-ONLY to all after S1
tests/fixtures/       L0        tests/policy/ tests/risk/  L1
tests/governor/ tests/advisory/ tests/authorization/ tests/audit/ tests/replay/   L2
tests/execution/ tests/adapters/ tests/sdk/ tests/api/   L3
tests/console/        L4        tests/security/  L5        tests/integration/  L6
```

Additionally: `tests/infra/`, `docs/adr/`, `CONTRIBUTING.md`, `Makefile`, `pyproject.toml`, `.github/`,
`docker-compose.yml`, `infra/`, `scripts/`, `.env.example`, `.gitignore` and `.pre-commit-config.yaml`
belong to **L0 infra**.

L0 creates every `mizan/<lane-module>/__init__.py` as a typed stub whose functions raise
`NotImplementedError("L<n> implements this in Sprint 2")`. Lanes replace the stubs; they do not change the
signatures, which are specified in `docs/API-SURFACE.md` §3 and tested by the invariant suite.

### `tests/invariants/` is read-only

The eighteen invariant tests are the executable form of Master Plan §4. They are frozen after Sprint 1.

**You may not edit, skip, xfail, mark, rename or delete a file in `tests/invariants/`.** Nor
`contracts/`. If an invariant fails, the implementation is wrong — that is the entire purpose of the file.
If you believe an invariant itself is wrong, that is a HALT: append an entry to `ledger/escalations.md`
saying what and why, stop, and wait for the Orchestrator. Do not proceed on the assumption that approval
will come.

---

## 2. Definition of Done

A change is done when **all** of these hold. Not most.

- [ ] **Contract conformance.** Objects validate against `contracts/*.schema.json`; if a model changed,
      `python scripts/generate_schemas.py` was run and both files are committed. `make test-contracts` is
      green.
- [ ] **Unit tests, including the failure paths.** The happy path is the easy half. Missing data, expired
      authorization, malformed input, an unavailable dependency: each has a test, and each asserts the
      *reason code*, not just that something was refused.
- [ ] **The full invariant suite is green** — `make test-invariants` — or a still-pending invariant is
      failing for a reason you can name and that is not your change.
- [ ] **Integration tests pass** (`make test-integration`), where the lane has them.
- [ ] **No CRITICAL or HIGH security finding.** `make test-security`, and `security/findings.md` updated
      if you found something.
- [ ] **No secret introduced.** `make secret-scan` and `make secret-scan-history` are clean. A
      deliberately secret-shaped test fixture gets an inline `secret-scan: allow` marker or a
      `.secretscan-allow` glob — never a real credential, never a blanket exclusion.
- [ ] **A decision-replay test wherever determinism is claimed.** If your code contributes to a verdict,
      prove the same inputs reproduce the same `evaluation_id` and the same reason codes (A1).
- [ ] **Reason codes are registered in the taxonomy.** Every REJECT or REDUCE cites a code from
      `contracts/reason_codes.json` (A4). A new code is a contract change and goes through §5.
- [ ] **Every file you touched is inside a path your lane owns.** Check `git status` before you commit.
- [ ] **Lint and types.** `make lint` on your own paths; `make typecheck` for `mizan/`.
- [ ] **`ledger/progress.md` records what you finished and what you did not.**

---

## 3. HALT list

These are not guidelines. Hitting one means **stop, append to `ledger/escalations.md`, and wait for a
human.** Not "stop unless it seems fine".

1. **Never set `ALPACA_PAPER` to anything but `true`** — not in code, not in a test, not in a fixture, not
   in an example, not in a comment showing "how you would enable live". There is no live path and none is
   being prepared (B1, ADR-0004).
2. **Never add a live host, a live base URL, or a live code path**, and never add an `environment` value
   other than `"paper"`.
3. **Never weaken an invariant to make a test pass.** If an invariant fails, the implementation is wrong.
   Editing the assertion, adding a skip, widening a tolerance or marking it xfail are all the same act.
4. **Never modify or delete an audit record, and never add an update or delete path to a ledger table**
   (A2). A correction is a new record. A state change is a new row.
5. **Never commit a secret.** If one reaches a commit, it is a HALT even if you notice immediately: the
   credential must be rotated, not just removed, and the history rewrite is the Orchestrator's call.
6. **Never rewrite published history.** No force-push, no rebase of a shared branch, no amend of a pushed
   commit.
7. **Never edit `contracts/` or `tests/invariants/` after the freeze.** A contract change needs a HALT
   entry, Orchestrator approval, a schema-version bump, and regeneration — in that order
   (`contracts/README.md`).
8. **Never add a cancel, replace or close broker mutation** (B4). The only mutation in v1 is a single
   order submission.
9. **Never publish, imply or endorse a return figure** (B5) — not in docs, not in the console, not in a
   docstring, not in a demo. Policy defaults in the Risk Canon are governance defaults, not portfolio
   recommendations.
10. **Never make a regulatory determination**, and never give personalized investment advice on any
    surface, including examples and tests (B6). Mizan produces evidence; it does not conclude that a
    customer is compliant.

Also stop, without exception, if you find yourself about to: give the advisory/LLM layer any way to
increase an authorized size (E1); make the engine depend on the LLM being reachable (E8); treat missing
risk data as zero or skip a check because data is absent (E2); or add an admin override, debug flag or
bypass around the execution gate (E3).

---

## 4. Running everything

`make` is a convenience; every recipe is a plain `python -m ...` command, so run them directly if you have
no `make`.

| Target | What it runs |
|---|---|
| `make install` | editable install with the `dev` and `advisory` extras, plus the git pre-commit hook |
| `make hooks` | install the pre-commit hook only (`python scripts/install_hooks.py`) |
| `make lint` | `ruff check .` |
| `make typecheck` | `mypy` over `mizan/` |
| `make test` | everything except invariants, security and integration |
| `make test-contracts` | `tests/contracts` — contract conformance (L0) |
| `make test-invariants` | `tests/invariants` — the Hard Rules |
| `make test-security` | `tests/security` (L5) |
| `make test-integration` | `tests/integration` (L6) |
| `make test-all` | every suite |
| `make secret-scan` | scanner self-test, then tracked + untracked files |
| `make secret-scan-history` | every added line of every commit on every branch |
| `make verify-chain ARGS=<ledger.sqlite\|records.jsonl>` | independent hash-chain verification |
| `make compose-up` / `make compose-down` | local Postgres (needs `POSTGRES_PASSWORD` in `.env`) |
| `make ci` | what CI runs, in CI order |

Infrastructure itself is tested: `python -m pytest -q tests/infra` checks `pyproject.toml`, the Makefile's
CI workflow, `docker-compose.yml`, `.env.example`, the Postgres DDL and the secret scanner. The DDL checks
there are **static** — the Docker daemon is not assumed. The CI `postgres-ddl` job applies the same files
to a real PostgreSQL, applies them twice to prove idempotency, and runs
`infra/postgres/verify/prove_append_only.sh`, which attempts every forbidden statement as both the
superuser and the tenant application role and requires each to be refused.

### Local setup

```
cp .env.example .env          # then fill in locally; .env is gitignored and must stay that way
make install
make ci
```

`.env.example` ships every credential key empty. An empty value means *unset*: the process refuses to
start or leaves the feature off. Do not put a real value in it, or a "sample" one.

---

## 5. Cross-lane requests

You will need something from a path you do not own. That is normal and it has a path.

**Append to `ledger/requests.md`:**

```
[UTC timestamp] REQ-n | from lane → to lane | request | status (OPEN/ROUTED/DONE/DECLINED) | resolution
```

Say what you need and why, in one entry. The Orchestrator routes it and updates the status in place. While
it is open, work around it or stop — do not edit the other lane's files "just this once", and do not add a
shim in your own lane that duplicates their responsibility, because the duplicate is what survives.

**`ledger/escalations.md` is different.** It is for things a human must decide: a HALT (§3), a Hard Rule
that appears to be wrong, a contract that must change, a security finding, an external dependency that
cannot be verified. Format:

```
[UTC timestamp] severity | lane | what | why it needs a human | what the Orchestrator did meanwhile
```

Both ledgers are append-only. Do not rewrite an entry; add a new one.

---

## 6. Commits

- The Orchestrator commits. Lanes leave a clean working tree and a `ledger/progress.md` entry.
- The pre-commit hook runs the secret scan on **staged** content and blocks on any finding. Do not bypass
  it (`--no-verify`) — if it fires on a deliberately secret-shaped fixture, mark that line
  `# secret-scan: allow` or add a glob to `.secretscan-allow`, and say so in the commit.
- Commit messages state what changed and which rule or ADR it serves. If a decision was made, it belongs
  in `docs/adr/` (see ADR-0000), not only in the message.
