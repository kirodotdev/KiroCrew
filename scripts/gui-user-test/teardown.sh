#!/usr/bin/env bash
# Stop everything boot.sh started (browser, gateway, Xvfb) and drop the scratch
# home + browser profile. Safe to run twice; never touches anything outside the
# paths boot.sh recorded in $GUI_OUT.
set -uo pipefail
: "${GUI_OUT:?GUI_OUT must be set}"

if [ -f "$GUI_OUT/pids" ]; then
  # Reverse order: browser, gateway, Xvfb. Each was started with setsid or as
  # its own background job, so signal the whole group, then the pid itself.
  tac "$GUI_OUT/pids" | while read -r pid; do
    [ -n "$pid" ] || continue
    kill -TERM -- "-$pid" 2> /dev/null || kill -TERM "$pid" 2> /dev/null || true
  done
  sleep 2
  tac "$GUI_OUT/pids" | while read -r pid; do
    [ -n "$pid" ] || continue
    kill -KILL -- "-$pid" 2> /dev/null || kill -KILL "$pid" 2> /dev/null || true
  done
fi

if [ -f "$GUI_OUT/target.paths" ]; then
  while IFS='=' read -r key path; do
    case "$key" in
      home|profile)
        # Only the two mktemp directories boot.sh created; refuse anything else.
        case "$path" in
          */gui-home.??????|*/gui-chrome.??????) rm -rf -- "$path" ;;
          *) echo "refusing to remove unexpected path for $key: $path" >&2 ;;
        esac ;;
    esac
  done < "$GUI_OUT/target.paths"
fi
rm -f -- "$GUI_OUT/target.env"

# The managed browser policy boot.sh installed under /etc must not outlive the
# run: on a developer box it would keep every later browser session pinned to a
# dead port. Only the exact files boot.sh recorded, and only under the three
# policy directories it writes to.
if [ -f "$GUI_OUT/policy.paths" ]; then
  while IFS= read -r policy; do
    [ -n "$policy" ] || continue
    case "$policy" in
      /etc/opt/chrome/policies/managed/gui-user-test.json|/etc/chromium/policies/managed/gui-user-test.json|/etc/chromium-browser/policies/managed/gui-user-test.json)
        sudo -n rm -f -- "$policy" 2> /dev/null || echo "could not remove $policy (needs sudo); remove it by hand" >&2 ;;
      *) echo "refusing to remove unexpected policy path: $policy" >&2 ;;
    esac
  done < "$GUI_OUT/policy.paths"
  rm -f -- "$GUI_OUT/policy.paths"
fi
echo "gui-user-test target torn down"
