#!/usr/bin/env bash
# =====================================================================
# MedMatch — guided live demo, one command.
#
#   ./demo.sh            step through everything, press Enter between steps
#   ./demo.sh 6          start at step 6
#   ./demo.sh --auto     run straight through without pauses (rehearsal)
#
# Every SQL / Cypher statement is printed before it runs, then executed
# against the real PostgreSQL and Neo4j containers.
# =====================================================================
cd "$(dirname "$0")"

AUTO=0; START=1
for a in "$@"; do
  case "$a" in
    --auto) AUTO=1 ;;
    [0-9]|[0-9][0-9]) START=$a ;;
    -h|--help) sed -n '2,12p' "$0"; exit 0 ;;
  esac
done

# ---------- presentation helpers ----------
B=$'\033[1m'; DIM=$'\033[2m'; RED=$'\033[31m'; GRN=$'\033[32m'; YEL=$'\033[33m'
CYN=$'\033[36m'; MAG=$'\033[35m'; RST=$'\033[0m'
TOTAL=12

step()  { printf '\n%s━━━ Step %s/%s  %s %s\n' "$B$MAG" "$1" "$TOTAL" "$2" "$RST"; }
say()   { printf '%s%s%s\n' "$YEL" "$*" "$RST"; }
note()  { printf '%s%s%s\n' "$DIM" "$*" "$RST"; }
ok()    { printf '%s✔ %s%s\n' "$GRN" "$*" "$RST"; }
bad()   { printf '%s✘ %s%s\n' "$RED" "$*" "$RST"; }
pause() {
  [ "$AUTO" = 1 ] && { sleep 1; return; }
  printf '\n%s[Enter] next  ·  q quit%s ' "$DIM" "$RST"
  read -r ans </dev/tty
  [ "$ans" = q ] && exit 0
}

# ---------- database helpers (overridable for testing) ----------
PSQL=${MM_PSQL:-"docker exec -i -e PGOPTIONS=-csearch_path=medmatch medmatch-postgres psql -U medmatch -d medmatch"}
CYPHER=${MM_CYPHER:-"docker exec -i medmatch-neo4j cypher-shell -u neo4j -p medmatch123 --format plain"}
PY=${MM_PY:-".venv/bin/python"}
API=http://127.0.0.1:8000

show_code() { printf '%s' "$1" | sed "s/^/$CYN  │ /; s/\$/$RST/"; echo; }

sql() {            # sql "<statements>"   — print, then run
  show_code "$1"
  printf '%s\n' "$1" | $PSQL -q -P pager=off -v ON_ERROR_STOP=0 2>&1 | sed 's/^/    /'
}
sql_quiet() { printf '%s\n' "$1" | $PSQL -qtAX -P pager=off 2>/dev/null; }

cypher() {
  show_code "$1"
  printf '%s\n' "$1" | $CYPHER 2>&1 | sed 's/^/    /'
}

api() { curl -s "$@"; }
pretty() { $PY -c '
import json,sys
for k, v in json.load(sys.stdin).items(): print("    " + k.replace("_", " ").ljust(22), v)'; }

# ---------- preflight ----------
preflight() {
  printf '%sMedMatch — Inter-Hospital Rare Resource Logistics Network%s\n' "$B" "$RST"
  note "Checking services…"
  if [ -z "$MM_PSQL" ]; then
    command -v docker >/dev/null || { bad "Docker is not installed."; exit 1; }
    docker info >/dev/null 2>&1 || { bad "Docker Desktop is not running. Start it and retry."; exit 1; }
    docker compose up -d --wait postgres >/dev/null 2>&1 || { bad "PostgreSQL did not start."; exit 1; }
    docker compose up -d neo4j >/dev/null 2>&1
  fi
  [ -x "$PY" ] || [ -n "$MM_PY" ] || { bad "Run ./start.sh once first (it creates .venv and the data)."; exit 1; }
  [ "$(sql_quiet 'SELECT count(*) FROM hospitals')" -gt 0 ] 2>/dev/null \
    || { bad "Database is empty. Run ./start.sh once first."; exit 1; }
  ok "PostgreSQL ready"

  if [ -z "$MM_CYPHER" ]; then
    for i in $(seq 1 20); do
      printf 'RETURN 1;' | $CYPHER >/dev/null 2>&1 && break
      [ "$i" = 1 ] && note "Waiting for Neo4j to boot…"
      sleep 3
    done
  fi
  if printf 'RETURN 1;' | $CYPHER >/dev/null 2>&1; then ok "Neo4j ready"; NEO=1
  else bad "Neo4j not reachable — graph step will use the app's built-in fallback"; NEO=0; fi

  STARTED_SERVER=0
  if ! api -f "$API/api/health" >/dev/null 2>&1; then
    note "Starting the MedMatch web server in the background…"
    $PY -m uvicorn app.main:app --port 8000 >/tmp/medmatch-demo-server.log 2>&1 &
    SERVER_PID=$!; STARTED_SERVER=1
    trap '[ "$STARTED_SERVER" = 1 ] && kill $SERVER_PID 2>/dev/null' EXIT
    for i in $(seq 1 30); do api -f "$API/api/health" >/dev/null 2>&1 && break; sleep 1; done
  fi
  api -f "$API/api/health" >/dev/null 2>&1 && ok "Web server ready at $API (open it in the browser any time)" \
    || { bad "Web server failed to start; see /tmp/medmatch-demo-server.log"; exit 1; }
}

# =====================================================================
step1() {
  step 1 "The problem"
  say "A hospital needs a rare unit — e.g. the last O-negative blood in the region."
  say "Today: phone calls. MedMatch: one shared, locked, routed reservation ledger."
  echo
  note "Live network state (GET /api/stats):"
  api "$API/api/stats" | pretty
}

step2() {
  step 2 "Schema: tables, views, functions, procedures, triggers"
  sql '\dt'
  sql '\dv'
  sql "SELECT routine_type, count(*) AS how_many, string_agg(routine_name, ', ' ORDER BY routine_name) AS names
FROM information_schema.routines
WHERE routine_schema = 'medmatch' AND routine_name NOT LIKE 'trg_%'
GROUP BY routine_type;"
  sql "SELECT tgname AS trigger, tgrelid::regclass AS on_table FROM pg_trigger WHERE NOT tgisinternal ORDER BY 2;"
  say "3NF: shelf life / storage temp live in asset_types, not repeated on 20,000 units."
}

step3() {
  step 3 "Search: compatible, unexpired units, nearest first"
  say "Katpadi Lifeline Hospital has an O-negative patient and needs packed red cells."
  sql "SELECT asset_code, blood_group, hours_to_expiry, hospital_name, distance_km
FROM fn_search_assets(
       (SELECT asset_type_id FROM asset_types WHERE asset_type_name = 'Packed Red Cells'),
       (SELECT hospital_id   FROM hospitals   WHERE hospital_code = 'KPD01'),
       'O-', NULL, NULL, 5);"
  sql "SELECT blood_compatible('O-','AB+') AS \"O- to AB+\", blood_compatible('A+','O-') AS \"A+ to O-\";"
  say "fn_search_assets uses the B+ tree index + blood_compatible() + haversine_km()."
}

step4() {
  step 4 "Reserve a unit, then follow it through its lifecycle"
  KPD=$(sql_quiet "SELECT hospital_id FROM hospitals WHERE hospital_code='KPD01'")
  PRC=$(sql_quiet "SELECT asset_type_id FROM asset_types WHERE asset_type_name='Packed Red Cells'")
  AID=$(api "$API/api/search?asset_type_id=$PRC&requester_id=$KPD&blood_group=O-&limit=1" \
        | $PY -c 'import json,sys; r=json.load(sys.stdin)["results"]; print(r[0]["asset_id"] if r else "")')
  [ -z "$AID" ] && { bad "No O- unit available; run with a fresh database."; return; }
  CODE=$(sql_quiet "SELECT asset_code FROM emergency_assets WHERE asset_id=$AID")
  note "Before:"
  sql "SELECT asset_code, status, version FROM emergency_assets WHERE asset_id = $AID;"
  note "POST /api/reserve  (Katpadi reserves $CODE)"
  RES=$(api -X POST "$API/api/reserve" -H 'content-type: application/json' \
        -d "{\"asset_id\":$AID,\"requester_id\":$KPD,\"department\":\"Trauma\",\"urgency_level\":\"CRITICAL\"}")
  echo "$RES" | $PY -c '
import json,sys; r=json.load(sys.stdin)
if "transaction_id" not in r: print("    ", r); sys.exit()
rt=r.get("route") or {}
print("    outcome         :", r["outcome"], " (restarts:", r["restarts"], ")")
print("    transaction_id  :", r["transaction_id"])
print("    unit            :", r["asset_code"], "-", r["asset_name"])
print("    route           :", " -> ".join(map(str, rt.get("hospital_ids", []))), "hospital ids,",
      rt.get("total_minutes"), "min via", rt.get("source"),
      "(cold chain ok)" if rt.get("cold_chain_satisfied") else "")
print("    wait-die stamp  :", r["wait_die_ts"])'
  TXN=$(echo "$RES" | $PY -c 'import json,sys; print(json.load(sys.stdin).get("transaction_id",""))')
  note "After — asset locked to RESERVED, version bumped by trigger, transfer + audit rows written:"
  sql "SELECT asset_code, status, version FROM emergency_assets WHERE asset_id = $AID;
SELECT transaction_id, status, requesting_hospital_id, supplying_hospital_id, eta_minutes, route_path, route_source
FROM transfer_transactions WHERE transaction_id = $TXN;"
  pause
  note "Dispatch and deliver via stored procedures (sp_dispatch_transfer, sp_complete_transfer):"
  api -X POST "$API/api/transactions/$TXN/dispatch" >/dev/null
  api -X POST "$API/api/transactions/$TXN/complete" >/dev/null
  sql "SELECT log_id, action, old_status, new_status, log_time FROM audit_logs WHERE transaction_id = $TXN ORDER BY log_id;
SELECT a.asset_code, a.status, h.hospital_name AS now_owned_by
FROM emergency_assets a JOIN hospitals h USING (hospital_id) WHERE a.asset_id = $AID;"
  say "Ownership moved to Katpadi. Every state change was logged by a trigger, not by the app."
}

step5() {
  step 5 "Race condition: two hospitals, last O-negative unit, same instant"
  $PY scripts/concurrency_demo.py race 2>&1 | grep -v "^Lock compat\|^   [SX]:"
}

step6() {
  step 6 "Row-level locking inside PostgreSQL (two real sessions)"
  AID=$(sql_quiet "SELECT asset_id FROM emergency_assets WHERE status='AVAILABLE' AND expiry_date > now() + interval '2 days' AND blood_group='O-' ORDER BY asset_id LIMIT 1")
  say "Session A locks unit $AID with SELECT … FOR UPDATE and holds it for 6 s."
  say "Session B tries to lock the same row 1 s later."
  A_SQL="BEGIN;
SELECT 'A has the lock' AS a, asset_code, status FROM emergency_assets WHERE asset_id = $AID FOR UPDATE;
SELECT pg_sleep(6);
UPDATE emergency_assets SET status = 'RESERVED' WHERE asset_id = $AID;
COMMIT;"
  B_SQL="BEGIN;
SELECT 'B started waiting' AS b, to_char(clock_timestamp(), 'HH24:MI:SS') AS at;
SELECT 'B got the row' AS b, asset_code, status, to_char(clock_timestamp(), 'HH24:MI:SS') AS at
FROM emergency_assets WHERE asset_id = $AID FOR UPDATE;
ROLLBACK;"
  printf '%sSession A:%s\n' "$B" "$RST"; show_code "$A_SQL"
  printf '%sSession B:%s\n' "$B" "$RST"; show_code "$B_SQL"
  printf '%s\n' "$A_SQL" | $PSQL -q -P pager=off >/tmp/mm_a.out 2>&1 &
  PA=$!; sleep 1
  printf '%s\n' "$B_SQL" | $PSQL -q -P pager=off >/tmp/mm_b.out 2>&1 &
  PB=$!; sleep 1.5
  note "While B is stuck — who is blocking whom (pg_stat_activity):"
  sql "SELECT pid, wait_event_type, wait_event, pg_blocking_pids(pid) AS blocked_by, left(query, 50) AS query
FROM pg_stat_activity
WHERE datname = 'medmatch' AND pid <> pg_backend_pid() AND state <> 'idle';"
  note "Waiting for A to commit…"
  wait $PA $PB
  printf '%sSession A output:%s\n' "$B" "$RST"; sed 's/^/    /' /tmp/mm_a.out
  printf '%sSession B output:%s\n' "$B" "$RST"; sed 's/^/    /' /tmp/mm_b.out
  say "B waited ~5 s, then saw RESERVED — the committed value. No lost update."
  sql_quiet "UPDATE emergency_assets SET status='AVAILABLE' WHERE asset_id=$AID AND status='RESERVED'" >/dev/null
  note "(unit $AID reset to AVAILABLE)"
}

step7() {
  step 7 "Deadlock: detection (PostgreSQL) vs prevention (Wait-Die)"
  $PY scripts/concurrency_demo.py deadlock 2>&1 | grep -v "^Lock compat\|^   [SX]:"
  sql "SHOW deadlock_timeout;"
}

step8() {
  step 8 "Triggers and constraints refusing bad data (each should FAIL)"
  sql "DELETE FROM audit_logs WHERE log_id = 1;"
  sql "UPDATE emergency_assets SET status = 'RESERVED'
WHERE asset_id = (SELECT min(asset_id) FROM emergency_assets WHERE status = 'EXPIRED');"
  sql "UPDATE emergency_assets SET status = 'AVAILABLE'
WHERE asset_id = (SELECT min(asset_id) FROM emergency_assets WHERE status = 'DELIVERED');"
  sql "INSERT INTO hospitals (hospital_code, hospital_name, facility_type, email, phone, address, city, state, latitude, longitude)
VALUES ('X1', 'Test', 'PRIVATE', 'not-an-email', '123', 'a', 'b', 'c', 12, 79);"
  say "Append-only audit, expiry guard, status state machine, CHECK constraint."
}

step9() {
  step 9 "Views and aggregation"
  sql "SELECT hospital_name, category, available, reserved, expiring_48h, o_negative_available
FROM v_hospital_inventory WHERE category = 'BLOOD' ORDER BY o_negative_available DESC LIMIT 5;"
  sql "SELECT asset_type_name, blood_group, units, earliest_expiry
FROM v_scarcity_report WHERE blood_group = 'O-' AND city IS NULL ORDER BY units;"
  sql "SELECT transaction_id, status, asset_type_name, supplying_hospital, requesting_hospital, eta_minutes
FROM v_transfer_dashboard ORDER BY request_time DESC LIMIT 5;"
}

step10() {
  step 10 "Indexes and query optimization"
  sql "SELECT indexname, indexdef FROM pg_indexes WHERE tablename = 'emergency_assets' AND indexname LIKE 'idx%';"
  Q="SELECT asset_id, hospital_id, expiry_date FROM emergency_assets
WHERE status = 'AVAILABLE' AND blood_group = 'O-' AND expiry_date > now()
ORDER BY expiry_date LIMIT 20;"
  note "With indexes:"
  sql "EXPLAIN ANALYZE $Q"
  note "Same query with index scans disabled for this session:"
  sql "SET enable_indexscan = off; SET enable_bitmapscan = off;
EXPLAIN ANALYZE $Q"
  pause
  note "Full benchmark (drop / recreate each index, median of 7 runs):"
  $PY scripts/benchmark_indexes.py 2>&1 | grep -v "^Report written"
}

step11() {
  step 11 "Graph database: transport network in Neo4j"
  if [ "$NEO" = 1 ]; then
    cypher "MATCH (h:Hospital) WITH count(h) AS hospitals
MATCH ()-[r:ROUTE_TO]->() RETURN hospitals, count(r) AS road_and_air_routes;"
    cypher "MATCH (a:Hospital {code:'AMB01'}), (b:Hospital {code:'CHN02'})
CALL apoc.algo.dijkstra(a, b, 'COLD_ROUTE>', 'eta_minutes') YIELD path, weight
RETURN [n IN nodes(path) | n.code] AS fastest_cold_chain_route,
       [r IN relationships(path) | r.mode] AS modes, weight AS minutes;"
    cypher "MATCH p = shortestPath((a:Hospital {code:'AMB01'})-[:ROUTE_TO*]->(b:Hospital {code:'CHN02'}))
RETURN [n IN nodes(p) | n.code] AS fewest_hops_route,
       reduce(t = 0, r IN relationships(p) | t + r.eta_minutes) AS minutes;"
    say "Ambur → Chennai South: fewest hops is 2 legs but slower; Dijkstra finds a 4-leg route that is faster."
    say "Fewest hops ≠ fastest: that is why edges are weighted by eta_minutes."
    cypher "MATCH (h:Hospital)-[r:ROUTE_TO]->() RETURN h.code AS hospital, h.city AS city, count(r) AS routes
ORDER BY routes DESC LIMIT 5;"
    note "Visual version: http://localhost:7474  →  MATCH (a)-[r:ROUTE_TO]->(b) RETURN a,r,b"
  else
    note "Neo4j is down; the app answers from its in-memory fallback:"
  fi
  SRC=$(sql_quiet "SELECT hospital_id FROM hospitals WHERE hospital_code='AMB01'")
  DST=$(sql_quiet "SELECT hospital_id FROM hospitals WHERE hospital_code='CHN02'")
  note "Same question through the app (GET /api/route):"
  api "$API/api/route?src=$SRC&dst=$DST&cold_chain=true" | $PY -c '
import json,sys; r=json.load(sys.stdin)
print("    path   :", " -> ".join(r["hospital_names"]))
print("    legs   :", ", ".join(f"{m} {t} min" for m, t in zip(r["modes"], r["leg_minutes"])))
print("    total  :", r["total_minutes"], "min, solved by", "Neo4j" if r["source"] == "neo4j" else "built-in fallback")'

}

step12() {
  step 12 "Wrap-up"
  api "$API/api/stats" | pretty
  say "Covered: schema & 3NF · functions/procedures/triggers · views & aggregation ·"
  say "2PL with SELECT FOR UPDATE · Wait-Die · B+ tree / hash / partial indexes · Neo4j routing."
  ok "Demo complete. The web app is at $API"
  if [ "$STARTED_SERVER" = 1 ] && [ "$AUTO" = 0 ]; then
    printf '%sThe web server stops when this script exits. Press Enter to stop it.%s' "$DIM" "$RST"
    read -r _ </dev/tty
  fi
}

preflight
pause
i=$START
while [ "$i" -le "$TOTAL" ]; do
  "step$i"
  [ "$i" -lt "$TOTAL" ] && pause
  i=$((i + 1))
done
