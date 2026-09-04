#!/usr/bin/env bash
# Boot the GUI user-test target on a CI runner: a private Xvfb display, a seeded
# Kiro Crew gateway on the packaged fake ACP backend, and a plain Chromium window
# pointed at the dashboard. Nothing here needs kiro-cli, a login, or a pod.
#
#   GUI_OUT       directory for logs / pids / target.env      (required)
#   GUI_DISPLAY   X display to create                         (default :99)
#   GUI_SCREEN    Xvfb screen WxHxDEPTH                       (default 1600x1000x24)
#   GUI_SEED      KIROCREW_HOME fixture                       (default rich)
#   GUI_MEMBERS   comma-separated crew member slugs to add    (default nova-sky)
#   GUI_CHROME    browser binary override                     (auto-detected)
#
# On success $GUI_OUT/target.env holds GUI_BASE_URL and GUI_DASHBOARD_TOKEN (the
# gateway's one-time token, mode 0600) and $GUI_OUT/pids lists the process
# groups teardown.sh kills. The token is a throwaway CI value for a gateway that
# dies with the job; it never leaves the runner.
#
# Local reproduction: docs/build/gui-user-test.md.
set -euo pipefail

: "${GUI_OUT:?GUI_OUT must be set}"
GUI_DISPLAY="${GUI_DISPLAY:-:99}"
GUI_SCREEN="${GUI_SCREEN:-1600x1000x24}"
GUI_SEED="${GUI_SEED:-rich}"
GUI_MEMBERS="${GUI_MEMBERS:-nova-sky}"

case "$GUI_DISPLAY" in
  :0|:1|:0.*|:1.*|*:0|*:1)
    echo "::error::GUI_DISPLAY=$GUI_DISPLAY is a real desktop; use a private display such as :99" >&2
    exit 2 ;;
esac

mkdir -p "$GUI_OUT"
PIDS="$GUI_OUT/pids"
: > "$PIDS"
HOME_DIR="$(mktemp -d "${RUNNER_TEMP:-${TMPDIR:-/tmp}}/gui-home.XXXXXX")"
PROFILE_DIR="$(mktemp -d "${RUNNER_TEMP:-${TMPDIR:-/tmp}}/gui-chrome.XXXXXX")"
echo "home=$HOME_DIR" > "$GUI_OUT/target.paths"
echo "profile=$PROFILE_DIR" >> "$GUI_OUT/target.paths"

# ---- 1. virtual display ----------------------------------------------------
Xvfb "$GUI_DISPLAY" -screen 0 "$GUI_SCREEN" -nolisten tcp -ac > "$GUI_OUT/xvfb.log" 2>&1 &
echo "$!" >> "$PIDS"
for _ in $(seq 1 50); do
  if DISPLAY="$GUI_DISPLAY" xdpyinfo > /dev/null 2>&1; then break; fi
  sleep 0.2
done
DISPLAY="$GUI_DISPLAY" xdpyinfo | grep -E '^ *dimensions:' || { echo "::error::Xvfb did not come up on $GUI_DISPLAY" >&2; exit 1; }

# ---- 2. seeded home --------------------------------------------------------
member_args=()
IFS=',' read -r -a members <<< "$GUI_MEMBERS"
for m in "${members[@]}"; do
  [ -n "$m" ] && member_args+=(--member "$m")
done
KIROCREW_HOME="$HOME_DIR" python3 "$(dirname "$0")/seed_home.py" --fixture "$GUI_SEED" "${member_args[@]}"

# ---- 3. gateway ------------------------------------------------------------
# Same shape as kiro_crew.testing.harness.spawn_feature_gateway (the E2E job's
# boot): isolated data + agent homes, the packaged fake ACP backend as the
# "kiro-cli", yolo approval so the tester never meets an approval prompt, no
# crons, and --test-mode (= --port auto --no-open --json-ready) so the READY
# line carries the port and the one-time token.
FAKE_BACKEND="$(python3 "$(dirname "$0")/seed_home.py" --print-fake-backend)"
GATEWAY_LOG="$GUI_OUT/gateway.log"
env \
  KIROCREW_HOME="$HOME_DIR" \
  KIRO_HOME="$HOME_DIR/kiro" \
  KIROCREW_FAKE_ACP_TEST_MODE=1 \
  KIROCREW_KIRO_BIN="$FAKE_BACKEND" \
  KIROCREW_SKIP_MODEL_DOWNLOAD=1 \
  PYTHONUNBUFFERED=1 \
  setsid python3 -m kiro_crew gateway --test-mode --approval yolo --no-crons \
  > "$GATEWAY_LOG" 2> "$GUI_OUT/gateway.err" &
GW_PID="$!"
echo "$GW_PID" >> "$PIDS"

READY_LINE=""
for _ in $(seq 1 300); do
  if ! kill -0 "$GW_PID" 2> /dev/null; then
    echo "::error::gateway exited before READY; see gateway.err" >&2
    tail -n 40 "$GUI_OUT/gateway.err" >&2 || true
    exit 1
  fi
  READY_LINE="$(grep -m1 '^KIROCREW_READY:' "$GATEWAY_LOG" || true)"
  [ -n "$READY_LINE" ] && break
  sleep 1
done
[ -n "$READY_LINE" ] || { echo "::error::gateway did not print KIROCREW_READY within 300s" >&2; tail -n 40 "$GUI_OUT/gateway.err" >&2 || true; exit 1; }

# Parse port + token in Python (jq is not guaranteed); write target.env 0600 and
# mask the token in the job log before anything else can echo it.
umask 077
printf '%s\n' "${READY_LINE#KIROCREW_READY:}" | python3 -c '
import json, sys
ready = json.loads(sys.stdin.read())
port, token = int(ready["port"]), str(ready["token"])
print(f"GUI_BASE_URL=http://127.0.0.1:{port}")
print(f"GUI_DASHBOARD_TOKEN={token}")
' > "$GUI_OUT/target.env"
umask 022
# shellcheck disable=SC1091
. "$GUI_OUT/target.env"
if [ -n "${GITHUB_ACTIONS:-}" ]; then
  echo "::add-mask::$GUI_DASHBOARD_TOKEN"
fi
echo "gateway ready at $GUI_BASE_URL"

# ---- 4. browser ------------------------------------------------------------
CHROME="${GUI_CHROME:-}"
if [ -z "$CHROME" ]; then
  for candidate in google-chrome google-chrome-stable chromium chromium-browser; do
    if command -v "$candidate" > /dev/null 2>&1; then CHROME="$(command -v "$candidate")"; break; fi
  done
fi
if [ -z "$CHROME" ]; then
  # Playwright's bundled Chromium (the E2E job caches it).
  CHROME="$(ls -1d "${HOME}"/.cache/ms-playwright/chromium-*/chrome-linux*/chrome 2> /dev/null | sort | tail -n 1 || true)"
fi
[ -n "$CHROME" ] && [ -x "$CHROME" ] || { echo "::error::no Chromium/Chrome binary found; set GUI_CHROME" >&2; exit 1; }

# Managed browser policy: the model can type anything into the omnibox, so the
# browser itself is pinned to the loopback gateway. Everything else -- file://
# (runner env files, credentials), other hosts, downloads, file pickers,
# devtools, extra profiles -- is refused by the browser, independently of the
# key-chord allowlist in x11.py. Chrome and Chromium read different directories;
# write both. Root is needed for the system policy dir; without it (a dev box)
# the lane still runs but says so loudly.
policy_json="$GUI_OUT/browser-policy.json"
python3 - "$GUI_BASE_URL" > "$policy_json" <<'PYEOF'
import json, sys
from urllib.parse import urlsplit
origin = urlsplit(sys.argv[1])
port = origin.port
# The gateway is bound on loopback and canonicalises the host in redirects
# (127.0.0.1 -> localhost), so every loopback spelling of THIS port is the
# same target; nothing else is reachable.
allowed = [f"{origin.scheme}://{host}:{port}" for host in ("127.0.0.1", "localhost", "[::1]")]
print(json.dumps({
    "URLBlocklist": ["*"],
    "URLAllowlist": allowed + ["about:blank"],
    "AllowFileSelectionDialogs": False,
    "DeveloperToolsAvailability": 2,
    "DownloadRestrictions": 3,
    "IncognitoModeAvailability": 1,
    "BrowserAddPersonEnabled": False,
    "BrowserGuestModeEnabled": False,
    "PasswordManagerEnabled": False,
    "AutofillAddressEnabled": False,
    "AutofillCreditCardEnabled": False,
    "DefaultBrowserSettingEnabled": False,
    "MetricsReportingEnabled": False,
    "PromotionsEnabled": False,
    "ExtensionInstallBlocklist": ["*"],
    "PrintingEnabled": False,
}, indent=2))
PYEOF
policy_installed=""
POLICY_PATHS="$GUI_OUT/policy.paths"
: > "$POLICY_PATHS"
for dir in /etc/opt/chrome/policies/managed /etc/chromium/policies/managed /etc/chromium-browser/policies/managed; do
  if sudo -n mkdir -p "$dir" 2> /dev/null && sudo -n cp "$policy_json" "$dir/gui-user-test.json" 2> /dev/null; then
    policy_installed=1
    # teardown.sh removes exactly these files (and nothing else under /etc).
    echo "$dir/gui-user-test.json" >> "$POLICY_PATHS"
  fi
done
if [ -z "$policy_installed" ]; then
  # Without the policy the browser can reach file:// and any host, and a
  # screenshot of what it reaches goes to the model: never launch it unpinned.
  echo "::error::could not install the managed browser policy (needs non-interactive sudo); refusing to launch an unpinned browser" >&2
  exit 1
fi

# --no-sandbox: hosted runners disable unprivileged user namespaces; the private
#   Xvfb display + throwaway profile are the isolation boundary here.
# --test-type: suppresses the "unsupported command-line flag" infobar that
#   --no-sandbox otherwise adds (40px that shifts every coordinate).
# The window fills the screen but keeps the omnibox: the harness navigates
# between scenarios by typing a URL there, like a person would.
screen_w="${GUI_SCREEN%%x*}"
rest="${GUI_SCREEN#*x}"
screen_h="${rest%%x*}"
DISPLAY="$GUI_DISPLAY" setsid "$CHROME" \
  --user-data-dir="$PROFILE_DIR" \
  --no-sandbox --test-type --disable-gpu --disable-dev-shm-usage \
  --no-first-run --no-default-browser-check --disable-infobars \
  --disable-features=TranslateUI,Translate,MediaRouter \
  --disable-background-networking --disable-component-update --disable-sync \
  --password-store=basic --use-mock-keychain \
  --window-position=0,0 --window-size="${screen_w},${screen_h}" \
  --force-device-scale-factor=1 --lang=en-US \
  "${GUI_BASE_URL}/?token=${GUI_DASHBOARD_TOKEN}" \
  > "$GUI_OUT/chrome.log" 2>&1 &
echo "$!" >> "$PIDS"

# Wait for a visible browser window, focus it, and give the SPA a moment.
win=""
for _ in $(seq 1 60); do
  win="$(DISPLAY="$GUI_DISPLAY" xdotool search --onlyvisible --class 'chrom' 2> /dev/null | head -n 1 || true)"
  [ -n "$win" ] && break
  sleep 0.5
done
[ -n "$win" ] || { echo "::error::browser window never appeared; see chrome.log" >&2; tail -n 40 "$GUI_OUT/chrome.log" >&2 || true; exit 1; }
DISPLAY="$GUI_DISPLAY" xdotool windowactivate --sync "$win" > /dev/null 2>&1 || true
DISPLAY="$GUI_DISPLAY" xdotool windowsize "$win" "$screen_w" "$screen_h" > /dev/null 2>&1 || true
DISPLAY="$GUI_DISPLAY" xdotool windowmove "$win" 0 0 > /dev/null 2>&1 || true
sleep 5
echo "target booted: display $GUI_DISPLAY, $GUI_BASE_URL, browser $CHROME (window $win)"
