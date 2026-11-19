#!/usr/bin/env bash
# MEASUREMENT ONLY (throwaway draft PR, never merged): cold vs prewarmed
# collection of backend shard 1, with and without --cov.
set -uo pipefail
PINS=${PINS:?}
WORKERS=${WORKERS:?}
COV_ARGS=(--cov=kiro_crew --cov=sage_lib --cov=src/kiro_crew/apps/builtins/aws_control/crew/packaging --cov=src/kiro_crew/apps/builtins/code_review_sage/tests)
SHARD=(-p scripts.ci_file_shards --file-shards 8 --file-shard 1 --file-shard-pins "$PINS")
CACHE=$(python -c "import os; print(os.path.join(os.environ.get('XDG_CACHE_HOME') or os.path.join(os.path.expanduser('~'), '.cache'), 'kirocrew', 'pycache'), end='')")
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
wipe() { rm -rf "$CACHE"; echo "wiped $CACHE (exists: $([ -e "$CACHE" ] && echo yes || echo no))"; }
for cov in nocov cov; do
  if [ "$cov" = cov ]; then C=("${COV_ARGS[@]}"); else C=(--no-cov); fi
  wipe
  probe "cold-$cov" python -m pytest -q -n "$WORKERS" "${SHARD[@]}" "${C[@]}" -k zz_no_such_test_zz
  echo "pyc files after cold: $(find "$CACHE" -name '*.pyc' 2>/dev/null | wc -l)"
  wipe
  probe "prewarm" python -m pytest -q --collect-only -n 0 "${SHARD[@]}" --no-cov
  echo "pyc files after prewarm: $(find "$CACHE" -name '*.pyc' 2>/dev/null | wc -l)"
  probe "warm-$cov" python -m pytest -q -n "$WORKERS" "${SHARD[@]}" "${C[@]}" -k zz_no_such_test_zz
done
wipe
probe "cold-plain-nocov" python -m pytest -q -n "$WORKERS" "${SHARD[@]}" --no-cov --assert=plain -k zz_no_such_test_zz
rm -f .coverage .coverage.*
