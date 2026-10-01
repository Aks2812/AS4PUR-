#!/bin/bash
# ============================================================================
#  Netskope Private App Import — ZTNA PRODUCTION BULK IMPORT
#  Source: Private_Apps_Ready.xlsx
#  Publisher assignment: Publisher DC ONLY (both DC publishers, HA pair)
#    (Publisher DRC intentionally excluded - connectivity issue under
#     active troubleshooting. Add DRC back once that's resolved.)
#
#  Built on top of npa_v1.sh (2-app HA test script). Same execution
#  philosophy: fill in config, test connection, run, review, repeat.
#
#  USAGE:
#    ./netskope_bulk_import.sh                  Normal run - auto batch size
#    ./netskope_bulk_import.sh --batch-size 25   Override batch size for this run
#    ./netskope_bulk_import.sh --dry-run         Build + validate payloads, POST nothing
#    ./netskope_bulk_import.sh --list-publishers Fetch publisher IDs and exit
#    ./netskope_bulk_import.sh --status          Show cumulative progress and exit
# ============================================================================

# ──── STEP 1: REPLACE THESE VALUES ────
TENANT=""
TOKEN=""
PUBLISHER_DC_IDS=("" "")             # e.g. PUBLISHER_DC_IDS=("1001" "1002") for both DC publishers — run --list-publishers to get IDs
EXCEL_FILE=""
# ────────────────────────────────────────

# Batch progression: first run does 10, next run does 20, then 50, then 100,
# then 100 repeating until every row has been successfully imported.
BATCH_SIZES=(10 20 50 100)
SLEEP_BETWEEN_CALLS=2            # seconds between POSTs — rate-limit protection

WORK_DIR="./netskope_import_work_v2"
MANIFEST_FILE="$WORK_DIR/manifest.jsonl"
SKIPPED_LOG="$WORK_DIR/validation_skipped.log"
STATE_FILE="$WORK_DIR/state.csv"                          # append-only, persists across every run
RUN_LOG="$WORK_DIR/run_$(date +%Y%m%d_%H%M%S).log"
NORMALIZER_SCRIPT="$(dirname "$0")/netskope_xlsx_normalize.py"
EXISTING_FETCH_SCRIPT="$(dirname "$0")/netskope_fetch_existing.py"
EXISTING_HOSTS_FILE="$WORK_DIR/existing_destinations.txt"

BASE_URL="https://${TENANT}.goskope.com/api/v2/steering/apps/private"

DRY_RUN=0
BATCH_SIZE_OVERRIDE=""
MODE="run"

for arg in "$@"; do
  case "$arg" in
    --dry-run) DRY_RUN=1 ;;
    --batch-size) NEXT_IS_BATCH_SIZE=1 ;;
    --list-publishers) MODE="list-publishers" ;;
    --status) MODE="status" ;;
    *)
      if [ "$NEXT_IS_BATCH_SIZE" == "1" ]; then
        BATCH_SIZE_OVERRIDE="$arg"
        NEXT_IS_BATCH_SIZE=0
      fi
      ;;
  esac
done

mkdir -p "$WORK_DIR"

# ============================================================================
#  FUNCTION: log
#  Writes a timestamped line to both stdout and the run log file.
# ============================================================================
log() {
  local line="[$(date '+%Y-%m-%d %H:%M:%S')] $1"
  echo "$line" | tee -a "$RUN_LOG"
}

# ============================================================================
#  FUNCTION: check_dependencies
#  Confirms curl and python3 (with openpyxl) are available before doing
#  anything else — fail fast with a clear message rather than partway through.
# ============================================================================
check_dependencies() {
  command -v curl >/dev/null 2>&1 || { echo "ERROR: curl is required but not found."; exit 1; }
  command -v python3 >/dev/null 2>&1 || { echo "ERROR: python3 is required but not found."; exit 1; }
  python3 -c "import openpyxl" 2>/dev/null || {
    echo "ERROR: python3 module 'openpyxl' is missing. Install with:"
    echo "  pip install openpyxl --break-system-packages"
    exit 1
  }
  if [ ! -f "$NORMALIZER_SCRIPT" ]; then
    echo "ERROR: normalizer script not found at $NORMALIZER_SCRIPT"
    echo "  It must be in the same folder as this script."
    exit 1
  fi
  if [ ! -f "$EXISTING_FETCH_SCRIPT" ]; then
    echo "ERROR: existing-destinations helper not found at $EXISTING_FETCH_SCRIPT"
    echo "  It must be in the same folder as this script."
    exit 1
  fi
  if [ ! -f "$EXCEL_FILE" ]; then
    echo "ERROR: Excel file not found at $EXCEL_FILE"
    exit 1
  fi
}

# ============================================================================
#  FUNCTION: validate_config
#  Confirms the top-of-script variables were actually filled in.
# ============================================================================
validate_config() {
  if [ -z "$TENANT" ] || [ -z "$TOKEN" ]; then
    echo "ERROR: TENANT and TOKEN cannot be empty. Edit the top of this script."
    exit 1
  fi
  if [ "$MODE" != "list-publishers" ] && [ "${#PUBLISHER_DC_IDS[@]}" -eq 0 ]; then
    echo "ERROR: PUBLISHER_DC_IDS is empty. Run: ./$(basename "$0") --list-publishers"
    exit 1
  fi
}

# ============================================================================
#  FUNCTION: list_publishers
#  Fetches all publishers from the tenant so you can copy the DC publisher_id
#  into the config section above. Equivalent to npa_v1.sh's Stage 1.
# ============================================================================
list_publishers() {
  echo "Fetching publishers from $TENANT..."
  curl -s -X GET "https://${TENANT}.goskope.com/api/v2/infrastructure/publishers?fields=publisher_id,publisher_name,status" \
    -H "Netskope-API-Token: $TOKEN" | python3 -m json.tool
}

# ============================================================================
#  FUNCTION: generate_manifest
#  Runs the Python normalizer against the Excel file. Regenerated on every
#  run so edits to the Excel file are always picked up automatically.
# ============================================================================
generate_manifest() {
  log "Reading and normalizing $EXCEL_FILE ..."
  local pub_ids_csv result
  pub_ids_csv=$(IFS=,; echo "${PUBLISHER_DC_IDS[*]}")
  result=$(python3 "$NORMALIZER_SCRIPT" "$EXCEL_FILE" "$pub_ids_csv" "$MANIFEST_FILE" "$SKIPPED_LOG" 2>&1)
  if [ $? -ne 0 ]; then
    log "FATAL: normalization failed:"
    log "$result"
    exit 1
  fi
  log "$result"

  local skip_count
  skip_count=$(grep -c "^Row .* | " "$SKIPPED_LOG" 2>/dev/null)
  skip_count=${skip_count:-0}
  if [ "$skip_count" -gt 0 ]; then
    log "WARNING: $skip_count row(s) were excluded from the manifest — see $SKIPPED_LOG"
  fi
  if grep -q "WARNINGS" "$SKIPPED_LOG" 2>/dev/null; then
    log "NOTE: some rows have warnings (truncated names, ambiguous hosts, etc.) — review $SKIPPED_LOG before a large batch"
  fi
}

# ============================================================================
#  FUNCTION: test_api_connection
#  Confirms the token and tenant are reachable before spending any of the
#  batch on apps that would all fail for the same root cause.
# ============================================================================
test_api_connection() {
  log "Testing API connection..."
  local response code
  response=$(curl -s -w "\n%{http_code}" -X GET "$BASE_URL" -H "Netskope-API-Token: $TOKEN")
  code=$(echo "$response" | tail -1)
  if [ "$code" != "200" ]; then
    log "ERROR: API connection failed (HTTP $code)"
    log "  HTTP 401: token wrong/expired | HTTP 403: token missing Steering scope | HTTP 000: tenant unreachable"
    exit 1
  fi
  log "API connection OK (HTTP $code)"
}

# ============================================================================
#  FUNCTION: fetch_existing_destinations
#  Pulls every existing Private App's destination host(s) from the tenant
#  BEFORE this run's batch, so candidate rows can be checked for a collision
#  before attempting a POST - not after. A failed fetch halts the run rather
#  than silently proceeding with zero protection (see script header note in
#  netskope_fetch_existing.py on why this must never be treated as "no
#  collisions exist").
# ============================================================================
fetch_existing_destinations() {
  log "Fetching existing private app destinations (collision pre-check)..."
  local result
  result=$(python3 "$EXISTING_FETCH_SCRIPT" "$TENANT" "$TOKEN" "$EXISTING_HOSTS_FILE" 2>&1)
  if [ $? -ne 0 ]; then
    log "FATAL: could not fetch existing destinations - halting rather than running unprotected:"
    log "$result"
    exit 1
  fi
  log "$result"
}

# ============================================================================
#  FUNCTION: find_colliding_host
#  Checks a comma-separated host string against EXISTING_HOSTS_FILE. Prints
#  the first colliding host if found (empty output = no collision).
# ============================================================================
find_colliding_host() {
  local host_field="$1"
  local tok
  IFS=',' read -ra tokens <<< "$host_field"
  for tok in "${tokens[@]}"; do
    tok=$(echo "$tok" | xargs)  # trim whitespace
    if grep -Fxq "$tok" "$EXISTING_HOSTS_FILE" 2>/dev/null; then
      echo "$tok"
      return
    fi
  done
}

# ============================================================================
#  FUNCTION: count_status
#  Counts how many rows in STATE_FILE currently have the given status.
# ============================================================================
count_status() {
  local status="$1"
  [ -f "$STATE_FILE" ] || { echo 0; return; }
  awk -F',' -v s="$status" '$3==s' "$STATE_FILE" | wc -l | tr -d ' '
}

# ============================================================================
#  FUNCTION: count_currently_failed
#  Counts rows whose most recent outcome is FAILED with no later SUCCESS -
#  NOT the same as total failure events, since a row can fail once and then
#  succeed on retry (its earlier FAILED line stays in the append-only log).
# ============================================================================
count_currently_failed() {
  [ -f "$STATE_FILE" ] || { echo 0; return; }
  awk -F',' '
    $3=="SUCCESS" { success[$1]=1 }
    $3=="FAILED"  { failed[$1]=1 }
    END {
      count=0
      for (r in failed) if (!(r in success)) count++
      print count
    }
  ' "$STATE_FILE"
}

# ============================================================================
#  FUNCTION: determine_batch_size
#  Walks the BATCH_SIZES progression by RUN COUNT, not by cumulative success
#  count - run 1 = 10, run 2 = 20, run 3 = 50, run 4 = 100, run 5+ = 100.
#  A handful of individual app failures within a batch does not stall the
#  progression (that's what skip-and-continue is for) - only a manual
#  --batch-size override changes the size for a given run.
# ============================================================================
BATCH_COUNTER_FILE="$WORK_DIR/batch_counter"

determine_batch_size() {
  if [ -n "$BATCH_SIZE_OVERRIDE" ]; then
    echo "$BATCH_SIZE_OVERRIDE"
    return
  fi
  local run_index=0
  [ -f "$BATCH_COUNTER_FILE" ] && run_index=$(cat "$BATCH_COUNTER_FILE")
  if [ "$run_index" -lt "${#BATCH_SIZES[@]}" ]; then
    echo "${BATCH_SIZES[$run_index]}"
  else
    echo "${BATCH_SIZES[-1]}"
  fi
}

# ============================================================================
#  FUNCTION: advance_batch_counter
#  Called once a batch has actually been attempted (not dry-run, not empty),
#  so the next invocation moves to the next tier in BATCH_SIZES.
# ============================================================================
advance_batch_counter() {
  local run_index=0
  [ -f "$BATCH_COUNTER_FILE" ] && run_index=$(cat "$BATCH_COUNTER_FILE")
  echo "$((run_index + 1))" > "$BATCH_COUNTER_FILE"
}

# ============================================================================
#  FUNCTION: select_batch
#  Picks the next N rows to process: anything not already SUCCESS, in
#  original spreadsheet order. Previously FAILED rows are retried naturally
#  before moving further down the list. Populates the global array BATCH.
# ============================================================================
select_batch() {
  local size="$1"
  local -A done_rows
  if [ -f "$STATE_FILE" ]; then
    while IFS=',' read -r row_no _ status _ _; do
      [ "$status" == "SUCCESS" ] && done_rows["$row_no"]=1
      [ "$status" == "SKIPPED_EXISTS" ] && done_rows["$row_no"]=1
    done < "$STATE_FILE"
  fi

  BATCH=()
  while IFS= read -r line; do
    local row_no
    row_no=$(echo "$line" | python3 -c "import sys,json; print(json.loads(sys.stdin.read())['row_no'])")
    if [ -z "${done_rows[$row_no]}" ]; then
      BATCH+=("$line")
      [ "${#BATCH[@]}" -ge "$size" ] && break
    fi
  done < "$MANIFEST_FILE"
}

# ============================================================================
#  FUNCTION: import_app
#  Sends one payload to the API, logs the outcome, and records it in
#  STATE_FILE. On failure: logs the error and returns — caller continues
#  the loop rather than aborting the whole batch.
# ============================================================================
import_app() {
  local row_no="$1" payload="$2" app_name="$3"

  if [ "$DRY_RUN" == "1" ]; then
    local dryrun_safe_name="${app_name//,/_}"
    dryrun_safe_name="${dryrun_safe_name//$'\n'/_}"
    log "  [DRY-RUN] Row $row_no ($app_name) — payload built, not sent"
    echo "$row_no,$dryrun_safe_name,DRYRUN,000,$(date '+%Y-%m-%d %H:%M:%S')" >> "$STATE_FILE"
    return
  fi

  local response code body
  response=$(curl -s -w "\n%{http_code}" -X POST "$BASE_URL" \
    -H "Netskope-API-Token: $TOKEN" \
    -H "Content-Type: application/json" \
    -d "$payload")
  code=$(echo "$response" | tail -1)
  body=$(echo "$response" | head -n -1)

  # CSV-safe: strip commas/newlines from app_name before logging to state file
  # (uses bash parameter expansion, not echo|tr, to avoid an extra trailing
  # underscore from the newline echo itself appends)
  local safe_name="${app_name//,/_}"
  safe_name="${safe_name//$'\n'/_}"

  # Netskope can return HTTP 200/201 with an error IN THE BODY (e.g. app name
  # exceeds character limit) - the HTTP code alone is not sufficient to
  # confirm success. Check the body's own "status" field.
  local api_status=""
  if [ "$code" == "200" ] || [ "$code" == "201" ]; then
    api_status=$(echo "$body" | python3 -c "
import sys, json
try:
    d = json.loads(sys.stdin.read())
    print(d.get('status',''))
except Exception:
    print('unparseable')
" 2>/dev/null)
  fi

  if { [ "$code" == "200" ] || [ "$code" == "201" ]; } && [ "$api_status" == "success" ]; then
    log "  Row $row_no ($app_name): SUCCESS (HTTP $code) - response: $body"
    echo "$row_no,$safe_name,SUCCESS,$code,$(date '+%Y-%m-%d %H:%M:%S')" >> "$STATE_FILE"
  else
    log "  Row $row_no ($app_name): FAILED (HTTP $code) — $body"
    echo "$row_no,$safe_name,FAILED,$code,$(date '+%Y-%m-%d %H:%M:%S')" >> "$STATE_FILE"
  fi

  sleep "$SLEEP_BETWEEN_CALLS"
}

# ============================================================================
#  FUNCTION: print_summary
#  Prints this run's results plus cumulative totals across all runs so far.
# ============================================================================
print_summary() {
  local run_success="$1" run_failed="$2" run_total="$3" start_time="$4" run_exists="${5:-0}"
  local end_time elapsed
  end_time=$(date +%s)
  elapsed=$((end_time - start_time))

  local total_manifest total_success total_failed total_skipped total_exists
  total_manifest=$(wc -l < "$MANIFEST_FILE" 2>/dev/null)
  total_manifest=${total_manifest:-0}
  total_success=$(count_status "SUCCESS")
  total_failed=$(count_currently_failed)
  total_exists=$(count_status "SKIPPED_EXISTS")
  total_skipped=$(grep -c "^Row .* | " "$SKIPPED_LOG" 2>/dev/null)
  total_skipped=${total_skipped:-0}

  echo ""
  echo "========================================"
  echo "  THIS RUN"
  echo "  Processed: $run_total | Success: $run_success | Failed: $run_failed | Skipped (exists): $run_exists"
  echo "  Execution time: ${elapsed}s"
  echo "========================================"
  echo "  CUMULATIVE (all runs)"
  echo "  Total apps in source file:     $total_manifest (of $((total_manifest + total_skipped)) rows read)"
  echo "  Successfully imported so far:  $total_success"
  echo "  Currently failed (retryable):  $total_failed"
  echo "  Skipped (destination exists):  $total_exists (see $STATE_FILE, status=SKIPPED_EXISTS)"
  echo "  Excluded by validation:        $total_skipped (see $SKIPPED_LOG)"
  echo "  Remaining to process:          $((total_manifest - total_success - total_exists))"
  echo "========================================"
  if [ "$run_failed" -gt 0 ]; then
    echo "  Some apps failed this run — review $RUN_LOG before continuing."
  fi
  if [ "$((total_manifest - total_success - total_exists))" -gt 0 ] && [ "$DRY_RUN" != "1" ]; then
    echo "  Run this script again to continue with the next batch."
  elif [ "$((total_manifest - total_success - total_exists))" -eq 0 ]; then
    echo "  All apps in the manifest have been successfully imported or skipped (already exists)."
  fi
  echo ""
  echo "  VERIFY IN NETSKOPE UI:"
  echo "  Settings > Security Cloud Platform > App Definition > Private App Segments"
}

# ============================================================================
#  MAIN
# ============================================================================
check_dependencies
validate_config

if [ "$MODE" == "list-publishers" ]; then
  list_publishers
  exit 0
fi

generate_manifest

if [ "$MODE" == "status" ]; then
  print_summary 0 0 0 "$(date +%s)"
  exit 0
fi

test_api_connection
fetch_existing_destinations

BATCH_SIZE=$(determine_batch_size)
select_batch "$BATCH_SIZE"

if [ "${#BATCH[@]}" -eq 0 ]; then
  log "Nothing to do — all rows in the manifest are already SUCCESS."
  print_summary 0 0 0 "$(date +%s)"
  exit 0
fi

log "========================================"
log "  Starting batch: ${#BATCH[@]} app(s) (target batch size: $BATCH_SIZE)"
[ "$DRY_RUN" == "1" ] && log "  DRY-RUN MODE — no API calls will be made"
log "========================================"

START_TIME=$(date +%s)
RUN_SUCCESS=0
RUN_FAILED=0
RUN_EXISTS=0
INDEX=0

for line in "${BATCH[@]}"; do
  INDEX=$((INDEX + 1))
  ROW_NO=$(echo "$line" | python3 -c "import sys,json; print(json.loads(sys.stdin.read())['row_no'])")
  APP_NAME=$(echo "$line" | python3 -c "import sys,json; print(json.loads(sys.stdin.read())['payload']['app_name'])")
  PAYLOAD=$(echo "$line" | python3 -c "import sys,json; print(json.dumps(json.loads(sys.stdin.read())['payload']))")

  log "[$INDEX/${#BATCH[@]}] Row $ROW_NO: $APP_NAME"

  COLLIDING_HOST=$(find_colliding_host "$(echo "$line" | python3 -c "import sys,json; print(json.loads(sys.stdin.read())['payload']['host'])")")
  if [ -n "$COLLIDING_HOST" ]; then
    SAFE_NAME="${APP_NAME//,/_}"
    SAFE_NAME="${SAFE_NAME//$'\n'/_}"
    log "  Row $ROW_NO ($APP_NAME): SKIPPED - destination '$COLLIDING_HOST' already exists on another app"
    echo "$ROW_NO,$SAFE_NAME,SKIPPED_EXISTS,000,$(date '+%Y-%m-%d %H:%M:%S')" >> "$STATE_FILE"
    RUN_EXISTS=$((RUN_EXISTS + 1))
    continue
  fi

  import_app "$ROW_NO" "$PAYLOAD" "$APP_NAME"

  LAST_STATUS=$(tail -1 "$STATE_FILE" | cut -d',' -f3)
  if [ "$LAST_STATUS" == "SUCCESS" ] || [ "$LAST_STATUS" == "DRYRUN" ]; then
    RUN_SUCCESS=$((RUN_SUCCESS + 1))
  else
    RUN_FAILED=$((RUN_FAILED + 1))
  fi
done

[ "$DRY_RUN" != "1" ] && [ -z "$BATCH_SIZE_OVERRIDE" ] && advance_batch_counter

print_summary "$RUN_SUCCESS" "$RUN_FAILED" "${#BATCH[@]}" "$START_TIME" "$RUN_EXISTS"
