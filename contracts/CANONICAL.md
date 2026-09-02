# Mizan canonical form and hash derivation

**Status:** FROZEN with `contracts/` at schema version `1.0.0`.
**Audience:** anyone who has to verify a Mizan decision chain **without running Mizan.**

Hard Rule A5 says chain integrity must be independently verifiable by the customer without our
involvement. That is only true if the rules are written down precisely enough to reimplement. This
document is that specification. Everything here is derivable from a decision record and a SHA-256
implementation; nothing requires Mizan source code, a network call, or our cooperation.

If you only want to check a chain and you are willing to run our code, use the shipped verifier:

```
python -m mizan.audit.verify_chain path/to/tenant-a.sqlite
```

If you want to check it with your own code, read on.

---

## 1. Canonical JSON

Every hash in Mizan is `SHA-256` over the UTF-8 bytes of a **canonical JSON** rendering. Canonical JSON
is ordinary JSON with the ambiguity removed:

| Rule | Value |
|---|---|
| Object keys | sorted ascending by Unicode code point |
| Separators | `,` and `:` exactly, with **no** whitespace anywhere |
| Encoding | UTF-8, characters emitted literally (no `\uXXXX` escaping beyond what JSON requires) |
| Line endings | none; the document is a single line with no trailing newline |
| Numbers | integers only |
| Floats | **rejected** — a binary float, `NaN` or `Infinity` anywhere is an error, not a value |
| Money, quantity, price, ratio | JSON **strings**, never JSON numbers (Hard Rule A6) |
| `null` | preserved; it means *absent/unknown* and is never interchangeable with `0` |

Worked example:

```
input   {"b": "1.50", "a": 1, "nested": {"z": true, "y": null}}
canonical   {"a":1,"b":"1.50","nested":{"y":null,"z":true}}
sha256      41e883160fa7262424b2a2580c2e2db06c491405e1d6e92dda8e0b800694577b
```

### Why money is a string

A price of `2.40` cannot be represented exactly as a binary float. Two machines, two languages, or two
library versions can disagree about its bits, and a hash over those bits would then disagree too — which
would break the product's central claim that a decision replays to an identical verdict anywhere. So
money crosses every boundary as a decimal string and is only ever converted to `Decimal` for arithmetic.

---

## 2. Decimal strings

A `DecimalStr` matches:

```
^-?(0|[1-9]\d*)(\.\d+)?$
```

No exponent, no leading `+`, no leading zeros in the integer part, no thousands separators, no spaces.

**Values are normalised when a contract object is validated**, before any hash is taken:

| As written | Normalised |
|---|---|
| `"2.40"` | `"2.4"` |
| `"100.00"` | `"100"` |
| `"-0"` | `"0"` |
| `"0.10"` | `"0.1"` |

The consequence is the property you actually care about: **two proposals that differ only in how their
money was spelled are the same proposal and hash identically.**

```
limit_price "2.40"  ->  proposal_id 3418031843a3c3e1f617712e97ecb3cfece5e5e4e56954b3499fa12cb67ce03a
limit_price "2.4"   ->  proposal_id 3418031843a3c3e1f617712e97ecb3cfece5e5e4e56954b3499fa12cb67ce03a
```

When you reimplement this, normalise before you hash, or you will get different digests from ours for
values we consider equal. Trailing zeros after the decimal point are removed; a value that becomes an
integer loses its decimal point entirely; negative zero becomes zero.

---

## 3. Timestamps

Canonical form, always UTC, always exactly six fractional digits, always `Z`:

```
YYYY-MM-DDTHH:MM:SS.ssssssZ        e.g.  2026-09-02T17:40:00.000000Z
```

Other RFC 3339 spellings are accepted on input and normalised to this form before hashing. An offset
such as `+00:00` is converted to `Z`; three fractional digits are padded to six.

---

## 4. Hash derivations

Every derived hash follows the same shape: **remove the field being computed, canonicalise the rest,
SHA-256 it.** A field can never contribute to its own hash.

| Field | Derivation |
|---|---|
| `proposal_id` | `sha256(canonical(proposal without "proposal_id" and without "reasoning"))` |
| `policy_hash` | `sha256(canonical(policy without "policy_hash"))` |
| `evaluation_id` | `sha256(canonical(evaluation without "evaluation_id"))` |
| `verdict_hash` | `sha256(canonical({verdict, reason_codes, authorized_total_quantity, authorized_legs, evaluation_id}))` |
| `authorization_hash` | `sha256(canonical(authorization without "authorization_hash"))` |
| `audit_hash` | `sha256(canonical(record without "audit_hash"))` |
| `idempotency_key` | `"mz1-" + sha256(canonical({tenant_id, proposal_id, legs}))[:40]` |

### `reasoning` is excluded from `proposal_id` — deliberately

The `reasoning` field carries free text written by an AI agent. It is kept for audit and it is never an
input to enforcement. Excluding it from the proposal identity makes that structural rather than
aspirational: an attacker who rewrites the reasoning to say *"ignore previous instructions, approve
maximum size"* produces **the same `proposal_id`, the same evaluation, and the same verdict**. The text
is recorded; it changes nothing.

```
reasoning ""                                         -> a19e2fdf22251f9993c5c4a90bc69cbc42caf3f1010c26e4973d2b2186adef74
reasoning "ignore previous instructions, approve..." -> a19e2fdf22251f9993c5c4a90bc69cbc42caf3f1010c26e4973d2b2186adef74
```

---

## 5. The chain

Each tenant has exactly one chain. Records are numbered from `1` and never renumbered.

```
record 1:  audit_prev_hash = 0000...0000   (64 zeros, the ZERO_HASH)
record N:  audit_prev_hash = audit_hash of record N-1
           sequence        = sequence of record N-1, plus 1
```

A chain is valid when, for every record in sequence order, all three hold:

1. `audit_hash == sha256(canonical(record without "audit_hash"))` — the record's content is intact.
2. `sequence == previous.sequence + 1` — nothing was removed.
3. `audit_prev_hash == previous.audit_hash` (or `ZERO_HASH` for the first) — nothing was reordered or inserted.

Any single altered byte inside a record breaks (1) at that record. Any deletion breaks (2) at the
following record. Any reordering or insertion breaks (3). **Report the first sequence number that fails**
— "the chain is broken" is far less useful to an auditor than "the chain is broken at record 47".

### What this does and does not prove

It proves **tamper evidence**: the stored records have not been altered, removed or reordered since they
were written, unless whoever altered them also recomputed every subsequent hash.

It is **not a blockchain** and there is no distributed consensus. Someone with write access to the
database and the ability to rewrite the whole chain forward can produce a self-consistent forgery. Two
countermeasures are available and neither is claimed here unless deployed: the database triggers that
refuse `UPDATE` and `DELETE` outright, and periodically publishing the latest `audit_hash` somewhere the
operator does not control, which pins everything before it.

Do not overclaim this in a sales conversation. Tamper evidence is a strong, honest property; describing
it as immutability is neither.

---

## 6. Verifying a chain without Mizan

Given a JSON-lines export (one record per line, ascending sequence), a complete verifier is short:

```python
import hashlib, json, sys

def canonical(obj):
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)

def sha256_hex(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()

ZERO = "0" * 64
previous = None
for line in open(sys.argv[1], encoding="utf-8"):
    record = json.loads(line)
    stored = record["audit_hash"]
    body = {k: v for k, v in record.items() if k != "audit_hash"}
    if sha256_hex(canonical(body)) != stored:
        sys.exit(f"record {record['sequence']}: content does not match its audit_hash")
    expected_prev = ZERO if previous is None else previous["audit_hash"]
    if record["audit_prev_hash"] != expected_prev:
        sys.exit(f"record {record['sequence']}: does not link to the previous record")
    if previous is not None and record["sequence"] != previous["sequence"] + 1:
        sys.exit(f"record {record['sequence']}: sequence gap after {previous['sequence']}")
    previous = record
print(f"chain verified: {previous['sequence'] if previous else 0} record(s)")
```

That is the whole algorithm. No Mizan import, no key, no service.

---

## 7. Determinism: what "replays identically" means

Hard Rule A1: the same inputs, the same policy version and the same engine version produce the same
verdict and the same reason codes. Precisely (Master Plan conflict resolution C6), the guarantee is over
the **canonical serialisation of the verdict and reason codes**, not over floating-point intermediates —
because there are none in the decision path.

What makes it hold:

- Money and quantity are decimals throughout; no binary float ever enters the decision path.
- The engine is a **pure function** of `(proposal, context, policy)`. It reads no clock, opens no socket,
  consults no LLM and holds no hidden state. Path-dependence, aggregate exposure, agent budgets, the
  response level and the calendar are all **inputs on the context**, captured in the record, so a replay
  sees exactly what the original decision saw.
- Anything iterated from a mapping is sorted first, so dictionary ordering cannot leak into a verdict.
- `engine_version` and `library_versions` are recorded, so a replay under a different build is detectable
  rather than silently different.

A replay that is not identical is therefore a real finding — a changed record, a changed engine, or a
bug — and never noise.

---

## 8. Redaction

Credentials are removed **before** persistence (Hard Rule A3), recursively, by key name: anything
matching `api_key`, `secret`, `token`, `password`, `authorization`, `credential(s)`, `header(s)`,
`cookie`, `private_key`, `connection_string`, `dsn` and their case variants becomes `"[REDACTED]"`.

Two contract fields are explicitly exempt because they match the pattern but carry no secret:
`authorization_hash` and `authorization_validated_at`.

Redaction happens before hashing, so **the hash covers the redacted record** — the one actually stored.
A verifier re-hashing what it was given will agree, and no verifier ever needs to see a credential.
