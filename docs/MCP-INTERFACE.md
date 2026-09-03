# MCP interface

The Alpaca hackathon requires the agent loop to reach Alpaca through **Alpaca's MCP server or a CLI**
rather than through raw `alpaca-py`. This document states which route Mizan took, why, and how to
check it.

## Which route

**All three, because they answer different halves of the same question.**

| | Route | Status |
|---|---|---|
| 1 | Alpaca's **official** MCP server (`alpacahq/alpaca-mcp-server`) is Mizan's broker transport | shipped, `--broker alpaca-mcp` |
| 2 | Mizan runs **its own** MCP server exposing governed operations as MCP tools | shipped, `python -m mizan.mcp` |
| 3 | A **CLI** over the same operations | shipped, `scripts/mizan_cli.py` |

Route 1 was tried first, as instructed, and it worked: `alpaca-mcp-server==2.3.1` installs from PyPI,
runs over stdio, negotiates MCP `2025-06-18`, and its calls reach `paper-api.alpaca.markets`. So
`mizan/mcp/alpaca.py` is a full `BrokerAdapter` — four reads and the one mutation — in which every
call is an MCP `tools/call`. When it is selected, `alpaca-py` is not on the path at all.

Route 2 was built as well, and it is the more interesting claim. The usual shape of an MCP trading
integration is:

```
agent  ->  broker MCP server  ->  venue
```

Every control then lives in the agent's prompt, which is the one place an attacker can write to.
Mizan inverts it:

```
agent  --MCP-->   MIZAN   --MCP-->   Alpaca   -->   paper venue
                    |
                    +-- deterministic risk engine, governor, authorization,
                        hash-chained ledger, replay
```

The agent is handed no tool that reaches a venue. `submit_governed_order` runs every check, appends a
chained `DecisionRecord`, and only then — if the order survived — lets it through. A refusal comes
back as machine-readable reason codes with the actual value against the threshold for each failed
check, so an agent can revise rather than guess.

Route 3 exists because a CLI satisfies the "or CLI" half of the requirement and is what a demo video
actually shows. It is **not** a second implementation: every subcommand calls
`MizanMCPServer.call_tool` — the same dispatch an MCP client reaches over stdio — so the two surfaces
cannot drift apart, and whichever one a judge runs exercises the other.

## Why the protocol is hand-written

`mizan/mcp/client.py` implements MCP's stdio transport (newline-delimited JSON-RPC 2.0) in the
standard library. No `mcp`, no `fastmcp`, no new dependency of any kind.

That is not an aesthetic choice. Mizan pins `pydantic`, `jsonschema` and `PyYAML` **exactly**, because
those versions are recorded in every `DecisionRecord.library_versions`; an unpinned upgrade would
change the recorded provenance of every decision ever made (Master Plan C6). The reference MCP SDK and
`fastmcp` both float those pins. Installing either would silently move the decision path.

An MCP server is a separate process by definition, so the isolation is free: Alpaca's server runs in
its own ephemeral environment via `uvx` and holds whatever versions it likes — FastMCP 3.4.7 and its
own pydantic — while Mizan's determinism fingerprint does not move.

## The safety problem, and what was done about it

Alpaca's official server exposes 72 tools. **Seven of them can destroy an account without a decision
ever being recorded:**

```
cancel_all_orders   cancel_order_by_id   replace_order_by_id
close_all_positions   close_position
exercise_options_position   do_not_exercise_options_position
```

Mizan's `BrokerAdapter` Protocol deliberately has no vocabulary for any of those — Hard Rule B4: four
reads and exactly one mutation, cancel/replace/close out of scope for v1. Wiring the raw server into
the agent loop would hand back every capability the Protocol was shaped to remove.

So B4 is re-imposed on the MCP surface in **three independent places**:

1. `ALPACA_TOOLSETS` is set so the server *creates* fewer tools (72 → 53): no crypto, no watchlists,
   no locates, no corporate actions.
2. `ALLOWED_TOOLS` is enforced by the client **before a byte is written to the pipe**. A denied tool
   is unreachable, not merely uncalled — the check lives at the transport, because a capability that
   is only "not called" is still reachable by the next bug or the next helpful refactor.
3. `FORBIDDEN_TOOLS` names each banned tool explicitly and is asserted disjoint from the allowlist at
   import time, so a future edit that adds `close_all_positions` fails the test suite rather than
   shipping. A ban by omission is not testable.

Net effect: **42 of the 53 tools the server offers are unreachable.** 11 are allowed — nine reads and
the two order-placement tools, which map exactly onto the Protocol's four reads and one mutation.

### The paper proof, re-derived for a new transport

`mizan/adapters/alpaca_paper.py` proves paper mode twice: the client's base URL, and the account's own
`PA` prefix. The MCP transport changes *how* those can be proven, not *whether* they must be.

- **Signal one.** A base URL is not inspectable through a tool call. But the official server computes
  its endpoint purely from `ALPACA_PAPER_TRADE` — a variable it *defaults to true*, meaning a parent
  shell setting it to `false` would point the broker at a non-paper venue. That is finding F-19 with a
  new transport. So the child's environment is **constructed, not inherited**: the variable is
  overwritten unconditionally, and an inherited non-paper value is **refused loudly** rather than
  quietly corrected — an operator who set it deserves an error, not a silent override.
- **Signal two.** The account must still identify itself with Alpaca's `PA` prefix — checked when the
  broker connects, on every account read, and again immediately before every submission. Absent,
  empty or non-`PA` is refused. Silence is not permission, and neither signal is trusted alone.

`mizan/mcp/alpaca.py` names no broker hostname at all — it imports `PAPER_HOST` and writes no literal
of its own, so neither a typo nor a helpful edit can introduce a second endpoint (INV-16).

### Configuration is explicit, never ambient

`SessionConfig` reads **nothing** from the environment. The policy file decides *which checks run* and
the broker decides *which venue is reached*; an inherited variable can set either without appearing in
the command anyone typed, so `--policy`, `--broker`, `--ledger`, `--tenant` and `--alpaca-mcp-cmd` are
flags only. `MIZAN_ALPACA_MCP_CMD` in particular is gone: selecting which *executable* becomes the
broker transport is wider than selecting among named broker implementations.

Two environment variables remain in `mizan/mcp/`, and both can only make the system more restrictive:

- `ALPACA_PAPER` — must be explicitly true before any Alpaca-touching broker is built (Hard Rule B1);
- `MIZAN_KILL_SWITCH` — when set, the session boots with the switch already down, so every check still
  runs and the gate then refuses at the mutation boundary (Hard Rule E4).

Credentials are read from the environment where they are needed, passed to the child process, and
never stored or written anywhere. Mizan holds no broker keys (Hard Rule B2).

## What a judge runs

Nothing below needs a credential except where marked.

### 1. The governed surface, end to end, with no Alpaca key at all

```
$ python scripts/mizan_cli.py doctor
$ python scripts/mizan_cli.py evaluate --symbol AAPL \
    --leg "side=buy,qty=50,limit=1.85,type=call,strike=230,expiry=2026-09-25"
```

```
MIZAN -> REJECTED
  decision id      01a068f0-fe5c-76be-b55f-70411cd14a5b
  requested        50
  authorized       0
  reason codes     HARD_REJECTION_UPHELD, OPTIONS_DELTA_LIMIT_EXCEEDED,
                   OPTIONS_GAMMA_LIMIT_EXCEEDED, OPTIONS_VEGA_LIMIT_EXCEEDED,
                   POSITION_LIMIT_EXCEEDED
    position_limit             blocking  actual 50 vs threshold 20
    options_delta_limit        blocking  actual 840 vs threshold 500
    options_gamma_limit        blocking  actual 105 vs threshold 100
    options_vega_limit         blocking  actual 710 vs threshold 300
  policy           options-conservative v1.4.0
  checks run       45
  verdict hash     bd434065943b15c3eb659e4369727788...
```

Revise and it goes through the gate:

```
$ python scripts/mizan_cli.py submit --symbol AAPL \
    --leg "side=buy,qty=10,limit=1.85,type=call,strike=230,expiry=2026-09-25"

MIZAN -> APPROVED
  authorized       10
  authorization    expires 2026-09-02T17:40:15.000000Z (15s, paper)
  execution
    status         WOULD_SUBMIT
    client order   mz1-62522f340bf0632f4d2ef7939656a3f506334355
    kill switch    read at 2026-09-02T17:40:00.000000Z
```

Add `--live` to let the gate actually submit. Set `MIZAN_KILL_SWITCH=true` and the same command
APPROVEs at the decision layer and then stops:

```
  execution
    status         BLOCKED
    reason code    KILL_SWITCH_ACTIVE
    kill switch    read at 2026-09-02T17:40:00.000000Z
```

### 2. Alpaca's official MCP server, and what Mizan will not send it

```
$ python scripts/mizan_cli.py mcp-tools --server alpaca
```

```
alpaca official MCP server
  command          uvx.EXE --from alpaca-mcp-server==2.3.1 alpaca-mcp-server --transport stdio
  server           Alpaca MCP Server 3.4.7
  protocol         2025-06-18
  base url         https://paper-api.alpaca.markets (ALPACA_PAPER_TRADE forced true)
  tools offered    53
  mizan allows     11
    ALLOW  get_account_info          ALLOW  get_option_snapshot
    ALLOW  get_all_positions         ALLOW  get_order_by_client_id
    ALLOW  get_clock                 ALLOW  get_order_by_id
    ALLOW  get_option_contracts      ALLOW  get_stock_latest_quote
    ALLOW  get_option_latest_quote   ALLOW  place_option_order
                                     ALLOW  place_stock_order
  mizan denies     9 (Hard Rule B4: no cancel, replace or close)
    DENY   cancel_all_orders         DENY   do_not_exercise_options_position
    DENY   cancel_order_by_id        DENY   exercise_options_position
    DENY   close_all_positions       DENY   place_crypto_order
    DENY   close_position            DENY   replace_order_by_id
                                     DENY   update_account_config
  unreachable      42 of 53 tools are off this client's allowlist
```

The allowlist is enforced at the transport, and a denied tool never reaches the pipe:

```
$ python scripts/mizan_cli.py mcp-call close_all_positions --no-credentials
DENIED  MCP tool 'close_all_positions' is not on this client's allowlist; allowed: [...]
```

An allowed read does reach Alpaca. With no credential the answer is a real 401 from the paper API,
which is itself proof the request left the machine:

```
$ python scripts/mizan_cli.py mcp-call get_account_info --no-credentials
UNAUTHENTICATED  placeholder key; a 401 here proves the request reached Alpaca
Error calling tool 'get_account_info': HTTP error 401: Unauthorized - {'message': 'unauthorized.'}
```

### 3. With paper credentials — the whole loop over MCP

```
$ export ALPACA_PAPER=true APCA_API_KEY_ID=... APCA_API_SECRET_KEY=...
$ python scripts/mizan_cli.py account --broker alpaca-mcp
$ python scripts/mizan_cli.py chain SPY --broker alpaca-mcp --limit 5
$ python scripts/mizan_cli.py submit --broker alpaca-mcp --live \
    --ledger ./data/live --symbol SPY --strategy bull_call_spread \
    --leg "side=buy,qty=1,limit=3.10,type=call,strike=560,expiry=2026-09-25" \
    --leg "side=sell,qty=1,limit=1.70,type=call,strike=565,expiry=2026-09-25"
$ python scripts/mizan_cli.py verify-chain --ledger ./data/live
$ python -m mizan.replay --ledger ./data/live
```

The spread goes to Alpaca as **one atomic `mleg` order** — never two single-leg orders, because that
has a window in which the short leg fills and the long one does not, which is exactly the
undefined-risk position `structure_valid` refuses at decision time.

### 4. Mizan as an MCP server

```
$ python -m mizan.mcp --broker alpaca-mcp --ledger ./data/live
```

For an MCP client's config:

```json
{
  "mcpServers": {
    "mizan": {
      "command": "python",
      "args": ["-m", "mizan.mcp", "--broker", "alpaca-mcp", "--ledger", "./data/live"],
      "env": { "ALPACA_PAPER": "true" }
    }
  }
}
```

Tools: `describe_governance`, `get_account`, `get_option_chain`, `evaluate_proposal`,
`submit_governed_order`, `verify_chain`, `replay_decision`, `list_decisions`, `get_decision`.

Two boundaries are visible in the **schemas**, not merely enforced in the handlers, because a
capability an agent cannot describe is one it cannot ask for:

- **No tool accepts market data, a portfolio, a balance or a mark.** The engine reads those from the
  broker (F-1/F-2). `limit_price` is the caller's own order limit and is explicitly not a valuation
  input; every other price-shaped field is absent, and every schema is `additionalProperties: false`.
- **No tool accepts an agent identity or a tenant.** The identity is the session's; impersonation is
  refused, not governed. And no tool names an environment or a broker — paper is proven from the
  environment before a socket is opened, never selected by an argument.

### 5. The tests

```
$ python -m pytest -q tests/mcp                        # 138 passed, hermetic
$ MIZAN_MCP_INTEGRATION=1 python -m pytest -q tests/mcp/test_integration_alpaca.py
```

The integration file is skipped by default because it downloads a package, starts a subprocess and
reaches the network. Five of its tests need no credentials — they assert that the official server
starts, that it still offers every tool Mizan reads through, that it still offers the destructive
tools Mizan refuses to send, that none of those can be sent, and that an allowed read reaches Alpaca.

## Deltas found against the real API

Recorded rather than papered over, per the lane's rules. Nothing below has a fallback that makes it
parse; the adapter's `deltas` list surfaces them and `get_account` returns them.

1. **`ALPACA_PAPER_TRADE` defaults to true, and a non-paper value silently redirects the endpoint.**
   The official server computes its base URL from this one variable with no other confirmation. Any
   integration that inherits its environment can be pointed at a non-paper venue by a parent process.
   Mizan overwrites it and refuses an inherited non-paper value.
2. **The official server exposes destructive tools with no gate.** `cancel_all_orders`,
   `close_all_positions`, `close_position`, `replace_order_by_id`, `cancel_order_by_id` and the two
   exercise tools are ordinary tools with ordinary schemas. Any agent handed this server unmediated
   can liquidate an account in one call. This is the finding that motivates the allowlist.
3. **`ALPACA_TOOLSETS` does not filter the order-placement overrides.** `place_crypto_order` is
   registered regardless of the active toolsets, so toolset filtering alone cannot remove a venue.
   The client-side allowlist is what actually removes it.
4. **Alpaca exposes no sector classification on any endpoint** (unchanged from the `alpaca-py` path).
   `sector_concentration` therefore blocks on `SECTOR_DATA_MISSING` until a sector is supplied out of
   band. Inventing one at the adapter would silently disable that check, so `Position.sector` stays
   `None`.
5. **Option greeks are not on the latest-quote endpoint.** `get_option_latest_quote` returns bid/ask
   only; greeks and implied volatility come from `get_option_snapshot`. The MCP adapter reads
   snapshots for that reason, which makes it strictly better-informed than the SDK path.
6. **Market-data JSON abbreviates its keys** (`bp`, `ap`, `t`) while the trading JSON spells them out
   (`bid_price`, `ask_price`, `timestamp`). Both spellings are read at the boundary; neither is
   invented, and a field that is absent stays absent.

## Layout

| Path | What it is |
|---|---|
| `mizan/mcp/client.py` | MCP stdio client, stdlib only. Holds the allowlist. |
| `mizan/mcp/alpaca.py` | `AlpacaMCPBroker` — a `BrokerAdapter` over Alpaca's official server. |
| `mizan/mcp/server.py` | Mizan's own MCP server: governed operations as tools. |
| `mizan/mcp/session.py` | The one builder both surfaces use. Explicit configuration only. |
| `mizan/mcp/__main__.py` | `python -m mizan.mcp`. |
| `scripts/mizan_cli.py` | The CLI, dispatching to the same handler. |
| `tests/mcp/` | 138 hermetic tests plus a credentialed integration file. |
