#!/usr/bin/env bash
set -Eeuo pipefail

# Run the real-input qualification in a single, resumable process.  This
# driver is intentionally conservative: it never enables dominance, never
# starts a Winter experiment, and skips a dependent branch after a hard
# failure rather than selecting a favourable subset of results.

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd -- "$SCRIPT_DIR/.." && pwd)"
RUNNER="$SCRIPT_DIR/benchmark_temporal_dominance_real.py"
C_P01_PYTHON="${C_P01_PYTHON:-/root/my_project/work_package_c/.venv/bin/python}"
if [[ ! -x "$C_P01_PYTHON" ]]; then
    C_P01_PYTHON="${C_P01_PYTHON_FALLBACK:-python3}"
fi
export PYTHONPATH="$REPO_ROOT/src${PYTHONPATH:+:$PYTHONPATH}"

EXPERIMENT_ROOT="${P01_M15_EXPERIMENT_ROOT:-/root/my_project/.runtime/experiments/c-p01-m15-real-qualification-20260827-r1}"
LOCK_PATH="${P01_M15_LOCK_PATH:-/root/my_project/.runtime/experiments/.c-p01-m15-real-qualification.lock}"
DRIVER_LOG="$EXPERIMENT_ROOT/driver.log"
STAGE_FILE="$EXPERIMENT_ROOT/.current-stage"
HEARTBEAT_FILE="$EXPERIMENT_ROOT/heartbeat.json"
STATE_FILE="$EXPERIMENT_ROOT/driver-state.json"
CPU="${P01_M15_CPU:-0}"
NO_NEW_WORKERS_AFTER_SECONDS="${P01_M15_NO_NEW_WORKERS_AFTER_SECONDS:-26100}"
START_EPOCH="$(date +%s)"

HOLDOUT_COMMIT="/root/my_project/.runtime/experiments/winter-b-validation-holdout-total-20260826/risk-store/commits/risk-window-sha256-115ad3ab6d7034fabc9428f91c14099b02dff8bb2443569a8d3947187fbb5ff9.json"
HOLDOUT_PLAN="/root/my_project/.runtime/experiments/winter-c-validation-holdout-total-20260826/winter-four-layer-route-plan-set-v3.json"
DEVELOPMENT_COMMIT="/root/my_project/.runtime/experiments/winter-b-validation-development-total-20260826/risk-store/commits/risk-window-sha256-bdfd7964df96ffcad7dd78d9830394a0a91d7fbbfde16c0649d2ba2fb68a00ab.json"
DEVELOPMENT_PLAN="/root/my_project/.runtime/experiments/winter-c-validation-development-total-20260826/winter-four-layer-route-plan-set-v3.json"
CONFIG_ROOT="$REPO_ROOT/configs"

mkdir -p "$EXPERIMENT_ROOT" "$(dirname -- "$LOCK_PATH")"
exec 9>"$LOCK_PATH"
if ! flock -n 9; then
    echo "another P0.1-M1.5 driver already holds $LOCK_PATH" >&2
    exit 2
fi

if [[ "${SKIP_CLEAN_CHECK:-0}" != "1" ]]; then
    if [[ -n "$(git -C "$REPO_ROOT" status --porcelain --untracked-files=all)" ]]; then
        echo "refusing evidence run from a dirty implementation worktree" >&2
        exit 2
    fi
fi

rm -f "$EXPERIMENT_ROOT/ALL_DONE" "$EXPERIMENT_ROOT/STOPPED_HARD"
printf '%s\n' "starting" > "$STAGE_FILE"
touch "$DRIVER_LOG"

write_state() {
    local status="$1"
    local stage="$2"
    local return_code="${3:-}"
    "$C_P01_PYTHON" - "$STATE_FILE" "$HEARTBEAT_FILE" "$status" "$stage" "$return_code" "$$" <<'PY'
from __future__ import annotations

import json
import os
import sys
import tempfile
from datetime import UTC, datetime
from pathlib import Path

state_path, heartbeat_path, status, stage, return_code, pid = sys.argv[1:]
payload = {
    "schema_version": "c.p0.1-temporal-real-qualification.driver.v1",
    "status": status,
    "stage": stage,
    "return_code": int(return_code) if return_code else None,
    "pid": int(pid),
    "updated_at": datetime.now(UTC).isoformat(timespec="seconds"),
}
for raw_path in (state_path, heartbeat_path):
    path = Path(raw_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_name, path)
    finally:
        try:
            os.unlink(temporary_name)
        except FileNotFoundError:
            pass
PY
}

write_skip() {
    local path="$1"
    local stage="$2"
    local reason="$3"
    "$C_P01_PYTHON" - "$path" "$stage" "$reason" <<'PY'
from __future__ import annotations

import json
import os
import sys
import tempfile
from datetime import UTC, datetime
from pathlib import Path

path = Path(sys.argv[1])
payload = {
    "schema_version": "c.p0.1-temporal-real-qualification.driver.v1",
    "status": "SKIPPED_PREREQUISITE",
    "stage": sys.argv[2],
    "reason": sys.argv[3],
    "updated_at": datetime.now(UTC).isoformat(timespec="seconds"),
}
path.parent.mkdir(parents=True, exist_ok=True)
fd, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
try:
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2, sort_keys=True)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary_name, path)
finally:
    try:
        os.unlink(temporary_name)
    except FileNotFoundError:
        pass
PY
}

heartbeat_loop() {
    while true; do
        local stage="heartbeat"
        if [[ -r "$STAGE_FILE" ]]; then
            stage="$(<"$STAGE_FILE")"
        fi
        write_state "RUNNING" "$stage"
        sleep 60
    done
}

HEARTBEAT_PID=""
FINAL_STATUS="STOPPED_HARD"

on_signal() {
    FINAL_STATUS="STOPPED_HARD"
    trap - INT TERM HUP
    exit 143
}

on_exit() {
    local return_code="$?"
    if [[ -n "$HEARTBEAT_PID" ]]; then
        kill "$HEARTBEAT_PID" 2>/dev/null || true
        wait "$HEARTBEAT_PID" 2>/dev/null || true
    fi
    if [[ "$FINAL_STATUS" == "ALL_DONE" && "$return_code" -eq 0 ]]; then
        touch "$EXPERIMENT_ROOT/ALL_DONE"
        write_state "ALL_DONE" "finished" 0
    else
        touch "$EXPERIMENT_ROOT/STOPPED_HARD"
        write_state "STOPPED_HARD" "driver-exit" "$return_code"
    fi
}

trap on_exit EXIT
trap on_signal INT TERM HUP

if command -v systemd-run >/dev/null 2>&1 \
    && systemd-run --scope --quiet true >/dev/null 2>&1; then
    SYSTEMD_AVAILABLE=1
else
    SYSTEMD_AVAILABLE=0
fi

run_stage() {
    local stage="$1"
    local deadline_seconds="$2"
    shift 2
    local elapsed=$(( $(date +%s) - START_EPOCH ))
    if (( elapsed >= NO_NEW_WORKERS_AFTER_SECONDS )); then
        write_state "SKIPPED_DEADLINE" "$stage" 75
        echo "[$(date -Is)] skip $stage: no new workers after ${NO_NEW_WORKERS_AFTER_SECONDS}s" >> "$DRIVER_LOG"
        return 75
    fi
    printf '%s\n' "$stage" > "$STAGE_FILE"
    write_state "RUNNING" "$stage"
    echo "[$(date -Is)] start $stage" >> "$DRIVER_LOG"
    set +e
    if (( SYSTEMD_AVAILABLE == 1 )); then
        timeout --signal=TERM --kill-after=30s "$deadline_seconds" \
            systemd-run --scope --quiet \
            --property=MemoryMax=4G \
            --property=MemorySwapMax=0 \
            --property=OOMPolicy=stop \
            "$@" >> "$DRIVER_LOG" 2>&1
    else
        timeout --signal=TERM --kill-after=30s "$deadline_seconds" "$@" >> "$DRIVER_LOG" 2>&1
    fi
    local return_code="$?"
    set -e
    if [[ "$return_code" -eq 0 ]]; then
        write_state "COMPLETED" "$stage" 0
        echo "[$(date -Is)] complete $stage" >> "$DRIVER_LOG"
    else
        write_state "BRANCH_FAILED" "$stage" "$return_code"
        echo "[$(date -Is)] failed $stage rc=$return_code; continuing" >> "$DRIVER_LOG"
    fi
    return "$return_code"
}

run_fifo() {
    local stage="$1" output="$2" commit="$3" route_plan="$4" segment="$5" timeout_seconds="$6"
    run_stage "$stage" "$timeout_seconds" \
        "$C_P01_PYTHON" "$RUNNER" \
        --mode fifo-scan \
        --risk-window-commit "$commit" \
        --route-plan-set "$route_plan" \
        --config-root "$CONFIG_ROOT" \
        --segment "$segment" \
        --output-dir "$output" \
        --worker-timeout-seconds 600 \
        --cpu "$CPU" \
        --resume
}

run_resource() {
    local stage="$1" output="$2" commit="$3" route_plan="$4" segment="$5" repetitions="$6" worker_timeout="$7" deadline="$8"
    run_stage "$stage" "$deadline" \
        "$C_P01_PYTHON" "$RUNNER" \
        --mode resource-frontier \
        --risk-window-commit "$commit" \
        --route-plan-set "$route_plan" \
        --config-root "$CONFIG_ROOT" \
        --segment "$segment" \
        --output-dir "$output" \
        --repetitions "$repetitions" \
        --worker-timeout-seconds "$worker_timeout" \
        --cpu "$CPU" \
        --resume
}

resource_status() {
    local summary="$1"
    if [[ ! -f "$summary" ]]; then
        return 1
    fi
    "$C_P01_PYTHON" - "$summary" <<'PY'
import json
import sys
from pathlib import Path

document = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
print(document.get("status", "MISSING"))
PY
}

run_input() {
    local name="$1" commit="$2" route_plan="$3"
    local six_root="$EXPERIMENT_ROOT/$name/executable_0_6h"
    local long_root="$EXPERIMENT_ROOT/$name/rolling_0_24h"
    mkdir -p "$six_root" "$long_root"

    run_fifo "$name/executable_0_6h/fifo-scan" "$six_root/fifo" "$commit" "$route_plan" executable_0_6h 1800 || true
    run_resource "$name/executable_0_6h/resource-frontier" "$six_root/resource" "$commit" "$route_plan" executable_0_6h 2 600 4200 || true

    local six_status="MISSING"
    six_status="$(resource_status "$six_root/resource/comparison-summary.json" 2>/dev/null || true)"
    if [[ "$six_status" == "RESOURCE_FRONTIER_PASS" ]]; then
        run_fifo "$name/rolling_0_24h/fifo-scan" "$long_root/fifo" "$commit" "$route_plan" rolling_0_24h 1800 || true
        run_resource "$name/rolling_0_24h/resource-frontier" "$long_root/resource" "$commit" "$route_plan" rolling_0_24h 1 900 5400 || true
    else
        write_skip "$long_root/comparison-summary.json" rolling_0_24h "6h resource frontier status=$six_status"
        write_state "SKIPPED_PREREQUISITE" "$name/rolling_0_24h" 75
    fi
}

write_state "RUNNING" "preflight"
heartbeat_loop &
HEARTBEAT_PID="$!"

run_input holdout "$HOLDOUT_COMMIT" "$HOLDOUT_PLAN"
run_input development "$DEVELOPMENT_COMMIT" "$DEVELOPMENT_PLAN"

FINAL_STATUS="ALL_DONE"
exit 0
