# Escalations ledger (append-only) — THE HUMAN READS THIS FIRST

Format: `[UTC timestamp] severity | lane | what | why it needs a human | what the Orchestrator did meanwhile`

- [2026-09-02T14:32:10Z] INFO | ORCH | Docker Desktop daemon is not running on this machine | Postgres per-tenant schema isolation (B3) is authored as SQL + docker-compose but cannot be executed or tested in this run; SQLite one-file-per-tenant ledger is the tested isolation for now | Continued; L0 ships the compose base and init SQL untested and says so in progress.md.
- [2026-09-02T14:32:10Z] INFO | ORCH | TradingAgents licence is unverified (Master Plan §14) | A human must confirm the licence before the W8 adapter ships in a release | L3 builds the adapter against a local stub; it is not marked shippable.
