#!/bin/sh
# Iteration 9: run one browser job through the shared render slot, in the background, with a log and a done file.
# usage: run9.sh <label> <tool.mjs> [args...]      (log: $S/run9/<label>.log, done: $S/run9/<label>.done)
#        run9.sh build                              (rebuild the website the dev gateway serves)
#        run9.sh signin                             (mint a link for the throwaway gateway and sign the tools' browser in)
# No run leaves a core file on the host.
ulimit -c 0
S=${XR_SCRATCH:?set XR_SCRATCH}
T=${XR_UI:?set XR_UI}/tools
mkdir -p "$S/run9"
label=$1
shift
rm -f "$S/run9/$label.done"
cd "$T" || exit 1
# The panels' guard (browser.xr_pages_guarded) for this run. The product starts with it off. The checks whose rows
# are about a refusal of the guard (a grant control, a page for looking at, the list of pages, an address that
# acts) run with it on; every other tool runs with it off. XR_GUARD=on|off says otherwise. XR_GW names the
# gateway's own script (the candidate's has its own).
case "$1" in
  live-check.mjs|many-check.mjs|touch-check.mjs|touch-mouse.mjs|list-check.mjs|note-check.mjs|look-check.mjs) guard=guard ;;
  *) guard=open ;;
esac
[ "$XR_GUARD" = on ] && guard=guard
[ "$XR_GUARD" = off ] && guard=open
case "$label" in build|signin) ;; *) sh "${XR_GW:-$S/gw9.sh}" "$guard" > "$S/run9/$label.guard" 2>&1 ;; esac
case "$label" in
  build) sh "$T/v3-build.sh" > "$S/run9/build.log" 2>&1 ;;
  signin) ./render-slot.sh sh "$S/signin.sh" > "$S/run9/signin.log" 2>&1 ;;
  # M248: the log's first line names the build the row ran on (its dist's own build-id.json).
  *) { printf 'build %s dist %s\n' "$(tr -d ' \n' < "${XR_DIST:-/nonexistent}/build-id.json" 2>/dev/null || echo none)" "${XR_DIST:-none}"; ./render-slot.sh node "$@"; } > "$S/run9/$label.log" 2>&1 ;;
esac
echo "exit $?" > "$S/run9/$label.done"
