#!/usr/bin/env bash
# Unified runner for the full Round 4 Hermit verification & reviewer suite.
#
# Run as the regular benchmark user (NOT via sudo) from the repository root
# after `sudo -v`. Matrix drivers that need root (pebs_perf_matrix.py) invoke
# sudo internally; workload matrices and granularity sweeps run as the user so
# output directories remain user-owned.
#
# Usage:
#   ./tools/rdma/run_round4_suite.sh [options]
#
# Options:
#   --out DIR             output root (default: ~/hermit-results/round4-YYYYMMDD-HHMMSS)
#   --steps "LIST"        space/comma-separated subset of steps, or "all" (default: all)
#                         order: exp1 exp2b exp2a exp3a exp3b exp2c exp2d exp4a exp4b exp4c exp4d exp4e
#   --resume              skip steps already marked done in --out/.done
#   --dry-run             validate all matrix plans and print commands without host changes
#   --user USER           benchmark user for Memcached (default: current user)
#   --redis PATH          redis-server binary (default: ~/redis/src/redis-server)
#   --ycsb PATH           ycsb executable (default: ~/ycsb-0.17.0/bin/ycsb)
#   --gapbs DIR           GAPBS source directory (default: ~/pebs-apps-20261031/gapbs-1.5)
#   --gapbs-scale N       GAPBS Kronecker scale (default: 24)
#   --repeats N           repeats for main matrices (default: 5)
#   --slo-qps "LIST"      fixed QPS points for exp2d (default: "30000 80000 140000 200000")
#   --split-min-samples N policy_split_min_samples for exp3a/exp3b (default: 4)
#   --pebs-heat-ms N      pebs_heat_ms half-life for exp3a/exp3b (default: 2000)
#   -h, --help            show this help
set -euo pipefail

SCRIPT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
REPO_ROOT=$(cd "$SCRIPT_DIR/../.." && pwd)
DEBUG_DIR=/sys/kernel/debug/hermit

ALL_STEPS=(exp1 exp2b exp2a exp3a exp3b exp2c exp2d exp4a exp4b exp4c exp4d exp4e)

usage() {
  sed -n '2,28p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'
}

die() {
  printf 'run_round4_suite: %s\n' "$*" >&2
  exit 1
}

log() {
  printf '[%s] %s\n' "$(date +%H:%M:%S)" "$*"
}

OUT=${OUT:-"$HOME/hermit-results/round4-$(date +%Y%m%d-%H%M%S)"}
STEPS_ARG=all
RESUME=0
DRY_RUN=0
BENCH_USER=${BENCH_USER:-$(id -un)}
REDIS_BIN=${REDIS_BIN:-"$HOME/redis/src/redis-server"}
YCSB_BIN=${YCSB_BIN:-"$HOME/ycsb-0.17.0/bin/ycsb"}
GAPBS_DIR=${GAPBS_DIR:-"$HOME/pebs-apps-20261031/gapbs-1.5"}
GAPBS_SCALE=${GAPBS_SCALE:-24}
REPEATS=${REPEATS:-5}
SLO_QPS=${SLO_QPS:-"30000 80000 140000 200000"}
SPLIT_MIN_SAMPLES=${SPLIT_MIN_SAMPLES:-4}
PEBS_HEAT_MS=${PEBS_HEAT_MS:-2000}

while [ $# -gt 0 ]; do
  case "$1" in
    --out) OUT=$2; shift 2 ;;
    --steps) STEPS_ARG=$2; shift 2 ;;
    --resume) RESUME=1; shift ;;
    --dry-run) DRY_RUN=1; shift ;;
    --user) BENCH_USER=$2; shift 2 ;;
    --redis) REDIS_BIN=$2; shift 2 ;;
    --ycsb) YCSB_BIN=$2; shift 2 ;;
    --gapbs) GAPBS_DIR=$2; shift 2 ;;
    --gapbs-scale) GAPBS_SCALE=$2; shift 2 ;;
    --repeats) REPEATS=$2; shift 2 ;;
    --slo-qps) SLO_QPS=$2; shift 2 ;;
    --split-min-samples) SPLIT_MIN_SAMPLES=$2; shift 2 ;;
    --pebs-heat-ms) PEBS_HEAT_MS=$2; shift 2 ;;
    -h|--help) usage; exit 0 ;;
    *) die "unknown option: $1" ;;
  esac
done

[[ "$REPEATS" =~ ^[1-9][0-9]*$ ]] || die "--repeats must be a positive integer"
[[ "$GAPBS_SCALE" =~ ^[1-9][0-9]*$ ]] || die "--gapbs-scale must be a positive integer"
[[ "$SPLIT_MIN_SAMPLES" =~ ^[1-9][0-9]*$ ]] || die "--split-min-samples must be a positive integer"
[[ "$PEBS_HEAT_MS" =~ ^[1-9][0-9]*$ ]] || die "--pebs-heat-ms must be a positive integer"

declare -a SELECTED_STEPS=()
if [ "$STEPS_ARG" = all ]; then
  SELECTED_STEPS=("${ALL_STEPS[@]}")
else
  for token in ${STEPS_ARG//,/ }; do
    valid=0
    for known in "${ALL_STEPS[@]}"; do
      if [ "$token" = "$known" ]; then
        valid=1
        break
      fi
    done
    [ "$valid" = 1 ] || die "unknown step '$token' (valid: ${ALL_STEPS[*]})"
    SELECTED_STEPS+=("$token")
  done
  [ "${#SELECTED_STEPS[@]}" -gt 0 ] || die "no steps selected"
fi

if [ "$DRY_RUN" = 0 ]; then
  [ "$(id -u)" -ne 0 ] || die "run as the benchmark user after 'sudo -v', not via sudo"
  sudo -n true || die "cached sudo credentials required; run 'sudo -v' first"
fi

saved_split_min_samples=
saved_pebs_heat_ms=
keepalive_pid=

restore_split_obs_tunables() {
  if [ -n "$saved_split_min_samples" ]; then
    printf '%s\n' "$saved_split_min_samples" | sudo -n tee "$DEBUG_DIR/policy_split_min_samples" >/dev/null || true
    saved_split_min_samples=
  fi
  if [ -n "$saved_pebs_heat_ms" ]; then
    printf '%s\n' "$saved_pebs_heat_ms" | sudo -n tee "$DEBUG_DIR/pebs_heat_ms" >/dev/null || true
    saved_pebs_heat_ms=
  fi
}

cleanup() {
  local ret=$?
  restore_split_obs_tunables
  if [ -n "$keepalive_pid" ]; then
    kill "$keepalive_pid" 2>/dev/null || true
    wait "$keepalive_pid" 2>/dev/null || true
  fi
  exit "$ret"
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

if [ "$DRY_RUN" = 0 ]; then
  mkdir -p "$OUT/.done"
  # Keep sudo timestamp alive during multi-hour sweeps.
  (while true; do sleep 45; sudo -n true 2>/dev/null || exit 0; done) &
  keepalive_pid=$!
fi

step_done() {
  [ "$RESUME" = 1 ] && [ -f "$OUT/.done/$1" ]
}

mark_done() {
  if [ "$DRY_RUN" = 0 ]; then
    date --iso-8601=seconds > "$OUT/.done/$1"
  fi
}

run_granularity() {
  if [ "$DRY_RUN" = 1 ]; then
    printf '[dry-run] %q' "$REPO_ROOT/tools/rdma/bench/run_swapin_granularity.sh"
    printf ' %q' "$@"
    printf '\n'
    return 0
  fi
  "$REPO_ROOT/tools/rdma/bench/run_swapin_granularity.sh" "$@"
}

run_workloads() {
  if [ "$DRY_RUN" = 1 ]; then
    local out
    out=$(python3 "$REPO_ROOT/tools/rdma/pebs/run_workload_matrix.py" --dry-run "$@")
    python3 -c 'import json,sys; d=json.loads(sys.stdin.read()); cases=sorted({r["case"] for r in d["plan"]}); print("[dry-run] run_workload_matrix: samples=%d adc=%s cases=%s" % (d["samples"], d["adc"], cases))' <<< "$out"
    return 0
  fi
  python3 "$REPO_ROOT/tools/rdma/pebs/run_workload_matrix.py" "$@"
}

run_memcached() {
  if [ "$DRY_RUN" = 1 ]; then
    local out
    out=$(python3 "$REPO_ROOT/tools/rdma/pebs/pebs_perf_matrix.py" --dry-run "$@")
    python3 -c 'import json,sys; d=json.loads(sys.stdin.read()); cases=sorted({r["case"] for r in d["plan"]}); print("[dry-run] pebs_perf_matrix: samples=%d adc=%s cases=%s" % (d["samples"], d["adc"], cases))' <<< "$out"
    return 0
  fi
  sudo -n python3 "$REPO_ROOT/tools/rdma/pebs/pebs_perf_matrix.py" "$@"
}

read_debugfs() {
  sudo -n cat "$DEBUG_DIR/$1"
}

run_with_split_obs() {
  local tag=$1
  shift
  if [ "$DRY_RUN" = 1 ]; then
    log "[dry-run] split-obs[$tag] policy_split_min_samples=$SPLIT_MIN_SAMPLES pebs_heat_ms=$PEBS_HEAT_MS"
    "$@"
    return 0
  fi
  mkdir -p "$OUT/split-obs"
  saved_split_min_samples=$(read_debugfs policy_split_min_samples)
  saved_pebs_heat_ms=$(read_debugfs pebs_heat_ms)
  printf '%s\n' "$SPLIT_MIN_SAMPLES" | sudo -n tee "$DEBUG_DIR/policy_split_min_samples" >/dev/null
  printf '%s\n' "$PEBS_HEAT_MS" | sudo -n tee "$DEBUG_DIR/pebs_heat_ms" >/dev/null

  read_debugfs split_obs_stats > "$OUT/split-obs/${tag}.before.txt"
  read_debugfs pebs_order_stats > "$OUT/split-obs/${tag}.pebs_before.txt"
  "$@"
  read_debugfs split_obs_stats > "$OUT/split-obs/${tag}.after.txt"
  read_debugfs pebs_order_stats > "$OUT/split-obs/${tag}.pebs_after.txt"
  restore_split_obs_tunables

  log "split_obs_stats delta ($tag):"
  python3 - "$OUT/split-obs/${tag}.before.txt" "$OUT/split-obs/${tag}.after.txt" <<'PY' | tee "$OUT/split-obs/${tag}.delta.txt"
import sys
def load(p):
    return {k: int(v) for k, v in (line.split() for line in open(p) if line.strip())}
b, a = load(sys.argv[1]), load(sys.argv[2])
for k in a:
    d = a[k] - b.get(k, 0)
    if d or k.endswith('_total'):
        print(f"{k:24s} {d}")
PY
}

step_exp1() {
  run_granularity \
    --pages "4 16 64 2048" \
    --policy-features "off default fitpte fitpte-medium" \
    --size-gib 8 --ratio 10 \
    --segments "1 4 16 32 64 128" \
    --trials 50 --adc \
    --result-dir "$OUT/gran-adc"
}

step_exp2a() {
  run_workloads \
    --suites anon-1t-d50-high anon-1t-d25-high anon-1t-d25-low \
             anon-1t-d6.25-high anon-1t-d6.25-low anon-1t-d1p-high \
    --pages 4 16 32 64 128 2048 \
    --periods 1999 --repeats "$REPEATS" --adc \
    --policy-features default fitpte fitpte-medium \
    --output "$OUT/density-matrix"
}

step_exp2b() {
  run_workloads \
    --suites redis ycsb \
    --pages 4 16 32 64 128 2048 \
    --periods 1999 --repeats "$REPEATS" --adc \
    --redis "$REDIS_BIN" \
    --ycsb "$YCSB_BIN" \
    --policy-features default fitstart-on fitpte fitpte-medium \
    --output "$OUT/redis-ycsb"
}

step_exp2c() {
  run_workloads \
    --suites gapbs-bfs gapbs-pr \
    --pages 4 16 64 256 2048 \
    --periods 1999 --repeats "$REPEATS" --adc \
    --gapbs "$GAPBS_DIR" --gapbs-scale "$GAPBS_SCALE" \
    --policy-features default fitpte fitpte-medium \
    --output "$OUT/gapbs"
}

step_exp2d() {
  run_memcached \
    --user "$BENCH_USER" --pages 4 64 2048 \
    --server-cpus 0-3 --client-cpus 16-31 --sampler-cpus 0-3 \
    --qps 0 --warmup 30 --duration 120 --repeats "$REPEATS" --adc \
    --cases off,policy-medium \
    --policy-features default fitpte \
    --output "$OUT/memcached-uncapped-4c"

  local q
  for q in $SLO_QPS; do
    run_memcached \
      --user "$BENCH_USER" --pages 4 64 2048 \
      --server-cpus 0-7 --client-cpus 16-31 --sampler-cpus 0-7 \
      --qps "$q" --warmup 30 --duration 120 --repeats 3 --adc \
      --cases off,policy-medium \
      --policy-features default fitpte \
      --output "$OUT/memcached-slo-q${q}"
  done
}

step_exp3a() {
  run_with_split_obs memcached-2m \
    run_memcached \
      --user "$BENCH_USER" --pages 2048 \
      --server-cpus 0-7 --client-cpus 16-31 --sampler-cpus 0-7 \
      --qps 30000 --warmup 30 --duration 120 --repeats 3 \
      --cases policy-high,policy-medium \
      --policy-features split-obs \
      --output "$OUT/split-obs/memcached"
}

step_exp3b() {
  run_with_split_obs workloads-2m \
    run_workloads \
      --suites anon-1t-d6.25-high anon-1t-d25-high redis gapbs-bfs gapbs-pr \
      --pages 64 2048 \
      --periods 1999 --repeats 3 \
      --redis "$REDIS_BIN" \
      --gapbs "$GAPBS_DIR" --gapbs-scale "$GAPBS_SCALE" \
      --policy-features 0x4ca3 \
      --output "$OUT/split-obs/workloads"
}

step_exp4a() {
  run_workloads \
    --suites anon-1t-full anon-1t-d50-high anon-1t-d25-high anon-1t-d25-low gapbs-bfs gapbs-pr \
    --pages 4 64 2048 \
    --periods 1999 --repeats "$REPEATS" --adc \
    --gapbs "$GAPBS_DIR" --gapbs-scale "$GAPBS_SCALE" \
    --policy-features 0x80 0xa0 0xc83 0xca3 0xea3 \
    --output "$OUT/ablation-2x2"

  # Pure read-side granularity decoupling at fixed 64 KiB allocation ceiling.
  run_granularity \
    --pages "64" --remote-order-mask 0x1 \
    --policy-features "off 0xca3 0xea3" \
    --size-gib 8 --ratio 10 \
    --segments "1 4 16 32 64 128" \
    --trials 50 --adc \
    --result-dir "$OUT/gran-read-decoupled-4kwr"

  run_granularity \
    --pages "64" --remote-order-mask 0x11 \
    --policy-features "off 0xca3 0xea3" \
    --size-gib 8 --ratio 10 \
    --segments "1 4 16 32 64 128" \
    --trials 50 --adc \
    --result-dir "$OUT/gran-read-decoupled-64kwr"
}

step_exp4b() {
  run_memcached \
    --user "$BENCH_USER" --pages 4 64 2048 \
    --server-cpus 0-7 --client-cpus 16-31 --sampler-cpus 0-7 \
    --qps 30000 --warmup 30 --duration 120 --repeats "$REPEATS" \
    --cases off,static-medium,policy-medium \
    --policy-features 0xca3 0xca1 \
    --output "$OUT/pebs-overhead-reclaim"

  run_memcached \
    --user "$BENCH_USER" --pages 4 2048 --memory-mb 2048 \
    --server-cpus 0-7 --client-cpus 16-31 --sampler-cpus 0-7 \
    --qps 30000 --warmup 30 --duration 120 --repeats "$REPEATS" \
    --cases off,static-high,static-medium,static-low \
    --output "$OUT/pebs-overhead-resident"
}

step_exp4c() {
  run_workloads \
    --suites anon-1t-full anon-4t-full anon-8t-full anon-16t-full \
    --pages 4 64 2048 \
    --periods 1999 --repeats 3 --adc \
    --policy-features default fitpte fitpte-medium \
    --output "$OUT/scalability"
}

step_exp4d() {
  run_granularity \
    --pages "4 16 64 2048" \
    --policy-features "off default fitpte fitpte-medium" \
    --size-gib 8 --ratio 10 \
    --segments "1 2 4 8 16 32 64 128" \
    --trials 50 --adc \
    --result-dir "$OUT/gran-mixed-segments"

  run_granularity \
    --pages "4 16 64 2048" \
    --policy-features "off default fitpte fitpte-medium" \
    --size-gib 8 --ratio 10 \
    --segments "1 4 16 64" \
    --trials 50 --interleave --max-offset 15 --adc \
    --result-dir "$OUT/gran-interleaved-unaligned"
}

step_exp4e() {
  run_granularity \
    --bypass-swapcache N \
    --pages "4 64 2048" --policy-features "off" \
    --size-gib 8 --ratio 10 \
    --segments "1 4 16 32 64 128" \
    --trials 50 --adc \
    --result-dir "$OUT/baseline-linux-swapcache"

  run_granularity \
    --bypass-swapcache Y \
    --pages "4 64 2048" --policy-features "off fitpte fitpte-medium" \
    --size-gib 8 --ratio 10 \
    --segments "1 4 16 32 64 128" \
    --trials 50 --adc \
    --result-dir "$OUT/baseline-hermit-direct"
}

log "Round 4 suite output: $OUT"
log "Selected steps: ${SELECTED_STEPS[*]}"

for step in "${SELECTED_STEPS[@]}"; do
  if step_done "$step"; then
    log "SKIP $step (already done)"
    continue
  fi
  log "=== START $step ==="
  "step_${step}"
  mark_done "$step"
  log "=== DONE $step ==="
done

log "All requested steps completed: $OUT"
