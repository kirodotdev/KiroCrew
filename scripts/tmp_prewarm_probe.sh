#!/usr/bin/env bash
# MEASUREMENT ONLY (throwaway draft PR, never merged): cold vs prewarmed
# collection of backend shard 1, with and without --cov.
set -uo pipefail
PINS=${PINS:?}
WORKERS=${WORKERS:?}
COV_ARGS=(--cov=kiro_crew --cov=sage_lib --cov=src/kiro_crew/apps/builtins/aws_control/crew/packaging --cov=src/kiro_crew/apps/builtins/code_review_sage/tests)
SHARD=(-p scripts.ci_file_shards --file-shards 8 --file-shard 1 --file-shard-pins "$PINS")
CACHE=$(python -c "import os; from test.conftest_floor_configure import _shared_cache_root; print(os.path.join(_shared_cache_root(), 'kirocrew', 'pycache'), end='')")
echo "cache dir: $CACHE  workers: $WORKERS  user: $(id -un 2>/dev/null || echo "$USERNAME")  home: $HOME"
now() { python -c 'import time; print(f"{time.time():.1f}", end="")'; }
probe() {
  local label=$1
  shift
  local start end rc
  start=$(now)
  "$@" > "$RUNNER_TEMP/probe-$label.log" 2>&1
  rc=$?
  end=$(now)
  echo "PROBE $label rc=$rc seconds=$(python -c "print(round($end - $start, 1))")"
  tail -n 2 "$RUNNER_TEMP/probe-$label.log"
}
wipe() { rm -rf "$CACHE"; }
for cov in nocov cov; do
  if [ "$cov" = cov ]; then C=("${COV_ARGS[@]}"); else C=(--no-cov); fi
  wipe
  probe "cold-$cov" python -m pytest -q -n "$WORKERS" "${SHARD[@]}" "${C[@]}" -k zz_no_such_test_zz
  wipe
  probe "prewarm" python -m pytest -q --collect-only -p no:xdist "${SHARD[@]}" --no-cov
  probe "warm-$cov" python -m pytest -q -n "$WORKERS" "${SHARD[@]}" "${C[@]}" -k zz_no_such_test_zz
done
wipe
probe "cold-plain-nocov" python -m pytest -q -n "$WORKERS" "${SHARD[@]}" --no-cov --assert=plain -k zz_no_such_test_zz
rm -f .coverage .coverage.*
