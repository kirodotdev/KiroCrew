#!/bin/bash
# Delayed gateway restart — run as a disowned process.
# The sleep gives the calling session time to finish responding.
#
# The CLI restart verb verifies the replacement gateway is serving and exits
# non-zero when it is not — but a disowned process discards both its output
# and its exit status. Record them instead: output appends to restart.log and
# the exit status lands in restart-status, both under the crew logs dir, so
# the calling agent can verify the outcome on its next turn (see SKILL.md
# "Verify the outcome").
LOG_DIR="${KIROCREW_HOME:-$HOME/.kiro/crew}/logs"
# Attempt-specific when the scheduler passes one (SKILL.md step 3 generates a
# per-attempt path), so overlapping restart attempts cannot overwrite each
# other's verdict; the shared default serves a lone attempt.
#
# CONFINEMENT INVARIANT: every attempt artifact lives inside $LOG_DIR under
# the restart-status. prefix, with no nested path and no traversal. This
# script runs detached from any agent sandbox, so an unvalidated caller-
# supplied path would let it delete and overwrite arbitrary user files; a
# non-conforming path falls back to the shared default instead.
STATUS_FILE="$LOG_DIR/restart-status"
if [ -n "${KIROCREW_RESTART_STATUS_FILE:-}" ]; then
  case "$KIROCREW_RESTART_STATUS_FILE" in
    "$LOG_DIR"/restart-status.*)
      suffix="${KIROCREW_RESTART_STATUS_FILE#"$LOG_DIR"/restart-status.}"
      case "$suffix" in
        */* | *..*) : ;;
        *) STATUS_FILE="$KIROCREW_RESTART_STATUS_FILE" ;;
      esac
      ;;
  esac
fi
# The diagnostic log is correlated with the attempt the same way: derived from
# the (validated) attempt status file, so a failed attempt's verifier never
# quotes another attempt's output or remedy out of a shared log.
if [ "$STATUS_FILE" != "$LOG_DIR/restart-status" ]; then
  LOG_FILE="$STATUS_FILE.log"
else
  LOG_FILE="$LOG_DIR/restart.log"
fi
# Artifact writes never follow links, and each artifact path is resolved
# exactly ONCE: remove whatever sits at the path, then open it under
# noclobber (O_EXCL) directly onto the descriptor the writes use. The single
# open both refuses anything left or re-planted at the path and IS the write
# handle, so there is no window between a validating create and a separate
# reopen for a link swap to win. A failed exclusive open drops the artifact
# rather than writing through a link.
umask 077
mkdir -p "$LOG_DIR"
rm -f -- "$LOG_FILE"
set -C
{ exec 3>"$LOG_FILE"; } 2>/dev/null || exec 3>/dev/null
set +C
# Remove the previous outcome BEFORE the delay: while the file is absent a
# restart attempt is pending; once it exists it names the exit status of the
# most recent attempt. A stale success left in place would be read as this
# attempt's verdict.
rm -f -- "$STATUS_FILE"
sleep "${KIROCREW_RESTART_DELAY:-10}"
# The restart verb prints a fresh dashboard token URL on success; the skill
# tells the resumed agent to quote this log into the conversation, so redact
# the bearer token on the way in. PIPESTATUS keeps the restart's own exit
# status — plain $? would report sed's.
kirocrew restart 2>&1 | sed -E 's/([?&]token=)[^[:space:]]+/\1REDACTED/g' >&3
status="${PIPESTATUS[0]}"
# Same open-once discipline as the log: the exclusive open is the write
# handle. A refused open (something re-planted at the path) drops the verdict.
rm -f -- "$STATUS_FILE"
set -C
if { exec 4>"$STATUS_FILE"; } 2>/dev/null; then
  printf '%s\n' "$status" >&4
  exec 4>&-
fi
set +C
exit "$status"
