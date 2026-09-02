#!/usr/bin/env bash
# Proves, against a LIVE PostgreSQL, that Mizan's per-tenant ledger is append-only,
# hash-chained and isolated -- using nothing but psql (Hard Rules A2, A5, B1, B3).
#
# Run it yourself (this is the exact script CI runs in the postgres-ddl job):
#
#   export PGHOST=127.0.0.1 PGPORT=5432 PGUSER=mizan PGDATABASE=mizan   # PGPASSWORD via ~/.pgpass or prompt
#   bash infra/postgres/verify/prove_append_only.sh
#
# or set DATABASE_URL. The connecting role must be a superuser or a member of mizan_admin,
# because the script provisions two throw-away tenants (proof-<epoch>-<n>) and then tries,
# as BOTH the superuser and the tenant's application role, to do everything the schema
# forbids. Every forbidden statement must be refused; every allowed one must succeed.
# Exit 0 means every expectation held.
set -euo pipefail

PSQL=(psql -X -q -A -t -v ON_ERROR_STOP=1)
if [[ -n "${DATABASE_URL:-}" ]]; then PSQL+=("$DATABASE_URL"); fi

pass=0
run() { "${PSQL[@]}" -c "$1"; }
expect_ok() {
  local out
  if out=$(run "$1" 2>&1); then
    pass=$((pass + 1)); echo "ok   (allowed) $2"
  else
    echo "FAIL expected success: $2"; echo "     statement: $1"; echo "     $out"; exit 1
  fi
}
expect_refused() {
  local out
  if out=$(run "$1" 2>&1); then
    echo "FAIL expected refusal: $2"; echo "     statement: $1"; exit 1
  else
    pass=$((pass + 1)); echo "ok   (refused) $2 -- $(echo "$out" | grep -m1 -E 'ERROR|FATAL' | cut -c1-140)"
  fi
}
expect_value() {  # statement, expected, label
  local out
  out=$(run "$1" 2>&1 | tr -d '[:space:]')
  if [[ "$out" == "$2" ]]; then
    pass=$((pass + 1)); echo "ok   (value)   $3 = $2"
  else
    echo "FAIL $3: expected '$2', got '$out'"; echo "     statement: $1"; exit 1
  fi
}

sha() { printf '%s' "$1" | sha256sum | cut -c1-64; }
ZERO=$(printf '0%.0s' $(seq 1 64))
STAMP="$(date +%s)-$RANDOM"
T="proof-${STAMP}-a"; T2="proof-${STAMP}-b"
S="tenant_${T//-/_}"; S2="tenant_${T2//-/_}"
R="${S}_app"
H1=$(sha "$T-record-1"); H2=$(sha "$T-record-2"); H3=$(sha "$T-record-3")
C1=$(sha "$T-control-1"); C2=$(sha "$T-control-2"); C3=$(sha "$T-control-3")
P1=$(sha "$T-policy-1")

decision_insert() {  # seq prev hash [tenant]
  local tnt="${4:-$T}"
  printf "INSERT INTO %s.decision_records (sequence, decision_id, audit_prev_hash, audit_hash, tenant_id, record) VALUES (%s, 'd-%s', '%s', '%s', '%s', '{\"schema_version\":\"1.0.0\",\"decision_id\":\"d-%s\",\"sequence\":%s,\"tenant_id\":\"%s\",\"audit_prev_hash\":\"%s\",\"audit_hash\":\"%s\",\"verdict\":\"REJECT\"}'::jsonb)" \
    "$S" "$1" "$1" "$2" "$3" "$tnt" "$1" "$1" "$tnt" "$2" "$3"
}
control_insert() {  # seq prev hash from to actor_type
  printf "INSERT INTO %s.control_events (sequence, event_id, event_type, from_level, to_level, actor_type, actor_id, audit_prev_hash, audit_hash, tenant_id, record, occurred_at) VALUES (%s, 'e-%s', 'response_level_changed', %s, %s, '%s', 'proof', '%s', '%s', '%s', '{\"event_id\":\"e-%s\",\"sequence\":%s,\"tenant_id\":\"%s\",\"audit_prev_hash\":\"%s\",\"audit_hash\":\"%s\"}'::jsonb, now())" \
    "$S" "$1" "$1" "$4" "$5" "$6" "$2" "$3" "$T" "$1" "$1" "$T" "$2" "$3"
}

echo "== provisioning (tenant ids: $T, $T2)"
expect_refused "SELECT mizan_admin.create_tenant('Bad_Tenant')"            "tenant id with uppercase/underscore refused"
expect_refused "SELECT mizan_admin.create_tenant('-leading-dash')"          "tenant id starting with '-' refused"
expect_refused "SELECT mizan_admin.create_tenant('x; DROP SCHEMA public')"  "tenant id with SQL refused by the regex"
expect_refused "SELECT mizan_admin.create_tenant(repeat('a', 53))"          "tenant id longer than 52 chars refused"
expect_value   "SELECT mizan_admin.create_tenant('$T')"  "$S" "create_tenant returns the schema name"
expect_value   "SELECT mizan_admin.create_tenant('$T')"  "$S" "create_tenant is idempotent"
expect_value   "SELECT mizan_admin.create_tenant('$T2')" "$S2" "second tenant provisioned"
expect_value   "SELECT count(*) FROM mizan_admin.tenants WHERE tenant_id IN ('$T','$T2')" "2" "registry rows"

echo "== decision_records chain (as the connecting superuser/admin)"
expect_refused "$(decision_insert 2 "$ZERO" "$H1")"   "first record must be sequence 1"
expect_refused "$(decision_insert 1 "$H2" "$H1")"     "first record must link to the zero hash"
expect_ok      "$(decision_insert 1 "$ZERO" "$H1")"   "genesis record appended"
expect_refused "$(decision_insert 3 "$H1" "$H2")"     "sequence gap refused"
expect_refused "$(decision_insert 2 "$ZERO" "$H2")"   "wrong audit_prev_hash refused"
expect_refused "$(decision_insert 2 "$H1" "$H1")"     "duplicate audit_hash refused"
expect_refused "$(decision_insert 2 "$H1" "$H2" other-tenant)" "tenant_id mismatch refused"
expect_ok      "$(decision_insert 2 "$H1" "$H2")"     "record 2 appended, linked to record 1"

echo "== append-only, even for the superuser (statement-level triggers)"
expect_refused "UPDATE $S.decision_records SET record = record || '{\"x\":1}'::jsonb WHERE sequence = 1" "UPDATE refused"
expect_refused "UPDATE $S.decision_records SET recorded_at = now() WHERE false"                        "UPDATE matching nothing still refused"
expect_refused "DELETE FROM $S.decision_records WHERE sequence = 1"                                    "DELETE refused"
expect_refused "TRUNCATE $S.decision_records"                                                          "TRUNCATE refused"

echo "== append-only and isolation as the tenant application role ($R)"
expect_refused "SET ROLE $R; UPDATE $S.decision_records SET record = '{}'::jsonb WHERE sequence = 1"  "app role: UPDATE refused"
expect_refused "SET ROLE $R; DELETE FROM $S.decision_records"                                          "app role: DELETE refused"
expect_refused "SET ROLE $R; TRUNCATE $S.decision_records"                                             "app role: TRUNCATE refused"
expect_refused "SET ROLE $R; ALTER TABLE $S.decision_records DISABLE TRIGGER ALL"                      "app role: cannot disable triggers"
expect_refused "SET ROLE $R; DROP TRIGGER decision_records_append_only ON $S.decision_records"         "app role: cannot drop the trigger"
expect_refused "SET ROLE $R; DROP TABLE $S.decision_records"                                           "app role: cannot drop the table"
expect_refused "SET ROLE $R; SELECT count(*) FROM $S2.decision_records"                                "app role: cannot read another tenant's schema"
expect_refused "SET ROLE $R; INSERT INTO $S2.policies (policy_id, policy_version, policy_hash, tenant_id, document) VALUES ('p','1.0.0','$P1','$T2','{}')" "app role: cannot write another tenant's schema"
expect_refused "SET ROLE $R; SELECT count(*) FROM mizan_admin.tenants"                                 "app role: cannot enumerate tenants"
expect_refused "SET ROLE $R; SELECT mizan_admin.create_tenant('evil')"                                 "app role: cannot provision tenants"
expect_refused "SET ROLE $R; CREATE TABLE $S.scratch (x int)"                                          "app role: cannot create tables (no CREATE on schema)"
expect_ok      "SET ROLE $R; $(decision_insert 3 "$H2" "$H3")"                                         "app role: CAN append a correctly linked record"
expect_value   "SET ROLE $R; SELECT count(*) FROM $S.decision_records" "3"                              "app role: reads its own ledger"
expect_value   "SET ROLE $R; SELECT ok FROM $S.verify_chain()" "t"                                      "app role: verify_chain() on its own chain"

echo "== policies are append-only too"
expect_ok      "INSERT INTO $S.policies (policy_id, policy_version, policy_hash, tenant_id, document) VALUES ('options-conservative','1.0.0','$P1','$T','{\"policy_hash\":\"$P1\",\"tenant_id\":\"$T\"}')" "policy inserted"
expect_refused "UPDATE $S.policies SET document = '{}'::jsonb"  "policies: UPDATE refused"
expect_refused "DELETE FROM $S.policies"                        "policies: DELETE refused"
expect_refused "TRUNCATE $S.policies"                           "policies: TRUNCATE refused"
expect_refused "INSERT INTO $S.policies (policy_id, policy_version, policy_hash, tenant_id, document) VALUES ('p2','1.0.0','$P1','$T','{\"policy_hash\":\"$P1\",\"tenant_id\":\"$T\"}')" "policies: duplicate policy_hash refused"

echo "== control_events chain (own chain until Sprint 3) and R-GRAD-1"
expect_ok      "$(control_insert 1 "$ZERO" "$C1" 0 2 system)"   "system escalation 0->2 appended"
expect_refused "$(control_insert 2 "$ZERO" "$C2" 2 3 system)"   "wrong prev hash refused"
expect_refused "$(control_insert 2 "$C1" "$C2" 2 1 system)"     "system DE-escalation 2->1 refused (needs a human)"
expect_ok      "$(control_insert 2 "$C1" "$C2" 2 1 human)"      "human de-escalation 2->1 appended"
expect_refused "UPDATE $S.control_events SET to_level = 0"      "control_events: UPDATE refused"
expect_refused "DELETE FROM $S.control_events"                  "control_events: DELETE refused"
expect_value   "SELECT ok FROM mizan_admin.chain_report('$S', 'control_events')" "t" "control_events chain verified"

echo "== paper-only at the schema level (B1)"
expect_refused "INSERT INTO $S.execution_results (decision_id, status, tenant_id, document) VALUES ('d-1','BLOCKED','$T','{\"environment\":\"live\"}')" "execution_results: environment=live refused"
expect_ok      "INSERT INTO $S.execution_results (decision_id, status, tenant_id, document) VALUES ('d-1','BLOCKED','$T','{\"environment\":\"paper\"}')" "execution_results: environment=paper accepted"
expect_refused "UPDATE $S.execution_results SET status = 'FILLED'" "execution_results: UPDATE refused (state changes are new rows)"

echo "== independent verification with plain SQL (no Mizan function involved)"
expect_value "SELECT count(*) FROM (
  SELECT sequence,
         audit_prev_hash::text = lag(audit_hash::text, 1, repeat('0', 64)) OVER (ORDER BY sequence) AS linked,
         sequence = lag(sequence, 1, 0::bigint) OVER (ORDER BY sequence) + 1 AS contiguous
  FROM $S.decision_records) chain WHERE NOT (linked AND contiguous)" "0" "rows breaking linkage or contiguity"

echo
echo "prove_append_only: all $pass expectations held (tenants $T, $T2 left in place for inspection)"
