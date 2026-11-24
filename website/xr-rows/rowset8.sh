#!/bin/sh
# Run browser rows one at a time on the dev gateway (6777) WITHOUT touching it: the build under test is read by the
# tab from its own folder (XR_DIST, chatcard-lib's `codeFromDir`), so the gateway's dist link stays as it is and no
# other tab on it loads again. Nothing in the shared folders is put back or removed: new files are only copied to OUT.
# usage: rowset7.sh <dist> <out-dir> <label>:<env>:<tool args> ...   (env: words joined by ',')
S=${XR_SCRATCH:?set XR_SCRATCH}
I=${XR_UI:?set XR_UI}/iterations/iter-15
DIST=$1; OUT=$2; shift 2
mkdir -p $OUT
stamp=$OUT/.stamp; touch $stamp
# The dev gateway's lock (LIVE-CANDIDATE-16.md part 7): one row holds it, its guard switch included, then lets go.
# A waiter gives up after 20 minutes and names the holder. The row's own command does not inherit it (8>&-).
L=${XR_UI:?set XR_UI}/tools/.dev-gateway-6777.lock
for job in "$@"; do
  label=${job%%:*}; rest=${job#*:}; envs=${rest%%:*}; args=${rest#*:}
  exec 8>>"$L"
  # A waiter says so in the waiters folder beside the lock (one file a waiter, its pid), and takes its name out once in.
  mkdir -p "$L.waiters"; echo "${LINE:-android-xr} $label" > "$L.waiters/$$"
  if ! flock -w 1200 8; then echo "6777 is held: $(cat "$L")" >&2; echo "$label held-out" >> $OUT/sum.txt; rm -f "$L.waiters/$$"; exec 8>&-; continue; fi
  rm -f "$L.waiters/$$"
  echo "pid $$ ${LINE:-android-xr} $label since $(date -u +%H:%M:%S)" > "$L"
  load0=$(cut -d" " -f1 /proc/loadavg)
  # M190: the row tool's lines as they run are kept beside the row's proof, and named by their sum in its log.
  T=$(dirname "$L"); tool=${args%% *}
  mkdir -p $OUT/tools; [ -f "$T/$tool" ] && cp "$T/$tool" "$OUT/tools/$label-$tool" && toolsum=$(sha256sum "$T/$tool" | cut -c1-16)
  ( export XR_DIST=$DIST XR_DEV=gateway; for e in $(echo "$envs" | tr ',' ' '); do export "$e"; done; sh $S/run9.sh $label $args 8>&- )
  : > "$L"; flock -u 8; exec 8>&-
  # flock is not first come, first served: with another line waiting, let it in before taking the lock again.
  [ -n "$(ls -A "$L.waiters" 2>/dev/null)" ] && sleep 2
  load1=$(cut -d" " -f1 /proc/loadavg); echo "load at the start $load0, at the end $load1" >> $S/run9/$label.log
  echo "row tool $tool sha256 ${toolsum:-none}, kept as $OUT/tools/$label-$tool" >> $S/run9/$label.log
  echo "$label $(grep -c '^PASS' $S/run9/$label.log)/$(grep -c '^PASS\|^FAIL' $S/run9/$label.log) not-judged $(grep -c '^NOT-JUDGED' $S/run9/$label.log) not-run $(grep -c '^NOT-RUN' $S/run9/$label.log) load $load0-$load1" >> $OUT/sum.txt
done
for d in rows rooms taps2; do
  [ -d $I/$d ] || continue
  (cd $I/$d && find . -type f -newer $stamp -print) | while read f; do mkdir -p "$(dirname "$OUT/$d/$f")"; cp "$I/$d/$f" "$OUT/$d/$f"; done
done
echo all-done >> $OUT/sum.txt
