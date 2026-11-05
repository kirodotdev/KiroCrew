#!/usr/bin/env bash
# Show what this machine's Tailscale tells the Kiro Crew direct tailnet link.
#
# Usage: scripts/tailnet-direct-check.sh [teammate-tailscale-ip]
#
# Read-only: it runs `tailscale` query commands and installs nothing. On a Mac
# with only the Tailscale app, the CLI inside the app bundle is used.
set -u

APP_CLI="/Applications/Tailscale.app/Contents/MacOS/Tailscale"
peer="${1:-}"

run() {
  printf '\n$ %s\n' "$*"
  "$@" 2>&1 || printf '(exit %s)\n' "$?"
}

printf '$ which tailscale\n'
cli="$(command -v tailscale || true)"
if [ -n "$cli" ]; then
  printf '%s\n' "$cli"
else
  printf '(not on PATH)\n'
  if [ -x "$APP_CLI" ]; then
    cli="$APP_CLI"
    printf 'using the app bundle CLI: %s\n' "$cli"
  fi
fi

if [ -z "$cli" ]; then
  printf 'No tailscale CLI found. The direct link needs Tailscale running on this machine.\n'
  exit 1
fi

run "$cli" version
run "$cli" ip -4
run "$cli" whois --help
if [ -n "$peer" ]; then
  run "$cli" whois "$peer"
  run "$cli" whois --json "$peer"
else
  printf '\nPass a teammate'"'"'s Tailscale IP to also run: %s whois <peer-ip>\n' "$cli"
fi
