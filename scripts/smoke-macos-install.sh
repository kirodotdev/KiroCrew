#!/usr/bin/env bash
# Install the macOS desktop DMG the way a user does -- mount it, copy the app
# out -- and prove what only the installed product can show: the app launches,
# its own supervisor boots the packaged gateway, and that gateway is handed the
# bundled kiro-cli.
#
# WHY A SCRIPT AND NOT INLINE YAML: same reason as scripts/smoke-linux-packages.sh
# and scripts/smoke-windows-install.ps1 -- one set of assertions for every caller
# of build-desktop.yml, so the lane that matters cannot drift from the others.
#
# WHAT IS ASSERTED, and why nothing else could:
#
#   * The DMG carries exactly one .app and it copies out whole (`ditto` keeps
#     resource forks and permissions the way Finder's drag does).
#   * The universal layout the shell resolves at launch (find-bin.js): a
#     per-arch backend tree for THIS runner's arch, with an executable launcher.
#   * The staged kiro-cli-chat is in the installed tree and reports the pinned
#     release from packaging/kiro-cli-version -- checked here in the INSTALLED
#     copy, not the build's staging directory, under an explicit credential-free
#     environment.
#   * The REAL app, launched from its own executable, boots the packaged gateway
#     and that gateway answers /api/health. This is the only place the Electron
#     supervisor's bundled-kiro-cli hand-off (gateway-env.js: stat, run
#     `--version`, then export KIROCREW_BUNDLED_KIRO_DIR + KIRO_NO_AUTO_UPDATE)
#     executes against a shipped .app. The gateway child's environment is read
#     back to prove the hand-off happened, and the launch log must not carry the
#     probe's fall-through line.
#   * The installed `kirocrew doctor` resolves that same bundled copy on its
#     kiro-cli row.
#
# WHAT IS DELIBERATELY NOT ASSERTED: code signature and notarization. This job
# runs on the UNSIGNED build (the sign job is a later, release-only stage), so
# `spctl` would reject it by design; the launch exercises the binary, not
# Gatekeeper.
#
# Usage: smoke-macos-install.sh <dist-dir>
#   BUNDLE_KIRO_CLI=0 skips the bundled-kiro-cli assertions (the build's own
#   documented opt-out).
set -euo pipefail

DIST_DIR="${1:?usage: smoke-macos-install.sh <dir containing the built .dmg>}"
REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"

if [ "$(uname -s)" != "Darwin" ]; then
  echo "ERROR: this smoke installs a macOS DMG and must run on macOS" >&2
  exit 1
fi

# Bounded, never slept toward. Sized for a cold macos runner where the first
# Electron launch pays for dyld caching and the packaged interpreter's imports.
MAX_HEALTH_SECONDS=180
MAX_CLI_SECONDS=120

# Run a command under a ceiling and print its combined output. macOS ships no
# coreutils `timeout`, so the bound is a background run plus a poll; a command
# that overruns is killed and reported by name instead of hanging the job.
bounded() {
  local limit="$1" what="$2"; shift 2
  local out pid waited=0
  out="$(mktemp "$WORK/bounded.XXXXXX")"
  "$@" >"$out" 2>&1 &
  pid=$!
  while kill -0 "$pid" 2>/dev/null; do
    if [ "$waited" -ge "$limit" ]; then
      kill -9 "$pid" 2>/dev/null || true
      echo "ERROR: $what exceeded its ${limit}-second ceiling" >&2
      return 124
    fi
    sleep 1; waited=$((waited + 1))
  done
  wait "$pid" || true
  cat "$out"
}

# Processes the launched app spawned, found by the data home they were given
# (`ps eww` prints each process's environment on macOS).
smoke_gateway_pids() {
  ps -axo pid= | while read -r pid; do
    if ps eww -p "$pid" 2>/dev/null | grep -q "KIROCREW_HOME=$HOME_DIR"; then echo "$pid"; fi
  done
}

# Exactly one DMG: zero means the build dropped it, more than one is ambiguous.
found=()
while IFS= read -r line; do found+=("$line"); done < <(find "$DIST_DIR" -maxdepth 1 -type f -name '*.dmg')
if [ "${#found[@]}" -ne 1 ]; then
  echo "ERROR: expected exactly one .dmg in ${DIST_DIR}, found ${#found[@]}" >&2
  printf '  %s\n' "${found[@]}" >&2
  exit 1
fi
DMG="${found[0]}"
echo "▶ Installer under test: $(basename "$DMG")"

WORK="$(mktemp -d "${RUNNER_TEMP:-${TMPDIR:-/tmp}}/kirocrew-smoke-XXXXXX")"
MOUNT="$WORK/mnt"
APPS="$WORK/Applications"
HOME_DIR="$WORK/home"
LOG_DIR="$WORK/logs"
mkdir -p "$MOUNT" "$APPS" "$HOME_DIR" "$LOG_DIR"

APP_PID=""
cleanup() {
  if [ -n "$APP_PID" ] && kill -0 "$APP_PID" 2>/dev/null; then
    # The app owns the gateway child; TERM lets its supervisor stop the tree.
    kill "$APP_PID" 2>/dev/null || true
    for _ in $(seq 1 30); do
      kill -0 "$APP_PID" 2>/dev/null || break
      sleep 1
    done
    kill -9 "$APP_PID" 2>/dev/null || true
  fi
  # Anything the app left behind under the smoke home is ours to reap.
  for pid in $(smoke_gateway_pids); do kill -9 "$pid" 2>/dev/null || true; done
  hdiutil detach "$MOUNT" -quiet 2>/dev/null || true
}
trap cleanup EXIT

# --- 1. Mount and copy out, the way a user drags the app to Applications. -----
hdiutil attach "$DMG" -nobrowse -readonly -mountpoint "$MOUNT" >/dev/null
apps=()
while IFS= read -r line; do apps+=("$line"); done < <(find "$MOUNT" -maxdepth 1 -type d -name '*.app')
if [ "${#apps[@]}" -ne 1 ]; then
  echo "ERROR: expected exactly one .app on the DMG, found ${#apps[@]}" >&2
  printf '  %s\n' "${apps[@]}" >&2
  exit 1
fi
APP_NAME="$(basename "${apps[0]}")"
ditto "${apps[0]}" "$APPS/$APP_NAME"
hdiutil detach "$MOUNT" -quiet
APP="$APPS/$APP_NAME"
echo "▶ Installed $APP_NAME into $APPS"

# The executable name comes from the bundle's own Info.plist, never from a
# hardcoded product name: the nightly channel ships as "KiroCrew Nightly".
EXECUTABLE="$(/usr/libexec/PlistBuddy -c 'Print :CFBundleExecutable' "$APP/Contents/Info.plist")"
APP_BIN="$APP/Contents/MacOS/$EXECUTABLE"
test -x "$APP_BIN" || { echo "ERROR: app executable missing: $APP_BIN" >&2; exit 1; }
RESOURCES="$APP/Contents/Resources"

# --- 2. Layout the shell resolves at launch (find-bin.js). --------------------
case "$(uname -m)" in
  arm64) ARCH_SUFFIX=arm64 ;;
  x86_64) ARCH_SUFFIX=x64 ;;
  *) echo "ERROR: unsupported runner arch $(uname -m)" >&2; exit 1 ;;
esac
BACKEND="$RESOURCES/backend-dist/kirocrew-backend-$ARCH_SUFFIX"
LAUNCHER="$BACKEND/bin/kirocrew"
PYTHON="$BACKEND/bin/python3.12"
for required in "$BACKEND" "$LAUNCHER" "$PYTHON"; do
  test -e "$required" || { echo "ERROR: installed backend payload incomplete; missing $required" >&2; exit 1; }
done
launcher_version="$(bounded "$MAX_CLI_SECONDS" "installed launcher --version" "$LAUNCHER" --version | tail -1)"
echo "▶ Installed launcher reports: $launcher_version"

# --- 3. The bundled kiro-cli, in the INSTALLED tree. --------------------------
BUNDLED_DIR="$RESOURCES/backend-dist/kiro-cli"
BUNDLED="$BUNDLED_DIR/kiro-cli-chat"
PIN="$(tr -d '[:space:]' < "$REPO_ROOT/packaging/kiro-cli-version")"
if [ "${BUNDLE_KIRO_CLI:-1}" = "0" ]; then
  echo "▶ Bundled kiro-cli: skipped (BUNDLE_KIRO_CLI=0)"
else
  test -x "$BUNDLED" || { echo "ERROR: installed tree carries no bundled kiro-cli at $BUNDLED (pin $PIN)" >&2; exit 1; }
  # Explicit, credential-free environment, the same shape the build's probes use.
  kiro_version="$(bounded "$MAX_CLI_SECONDS" "bundled kiro-cli --version" \
    env -i HOME="$HOME_DIR" PATH=/usr/bin:/bin KIRO_NO_AUTO_UPDATE=1 "$BUNDLED" --version | tail -1)"
  echo "▶ Installed bundled kiro-cli reports: $kiro_version"
  case "$kiro_version" in
    *"$PIN"*) ;;
    *) echo "ERROR: bundled kiro-cli reported '$kiro_version' but packaging/kiro-cli-version pins $PIN" >&2; exit 1 ;;
  esac
fi

# --- 4. Launch the REAL app; its supervisor boots the packaged gateway. -------
PORT="$(python3 -c 'import socket; s=socket.socket(); s.bind(("127.0.0.1",0)); print(s.getsockname()[1])')"
APP_OUT="$LOG_DIR/app-stdout.log"
APP_ERR="$LOG_DIR/app-stderr.log"
# KIROCREW_HOME isolates the data home (and the dashboard token) under $WORK;
# KIROCREW_PORT pins the gateway port so /api/health has one address to ask;
# KIROCREW_SKIP_MODEL_DOWNLOAD keeps the ~610MB embeddings download out of a
# smoke test. HOME stays the runner's own: Electron needs its real
# Library/Application Support and Logs directories.
KIROCREW_HOME="$HOME_DIR" KIROCREW_PORT="$PORT" KIROCREW_SKIP_MODEL_DOWNLOAD=1 \
  "$APP_BIN" >"$APP_OUT" 2>"$APP_ERR" &
APP_PID=$!
echo "▶ Launched $EXECUTABLE (pid $APP_PID) with KIROCREW_PORT=$PORT"

healthy=0
started=$(date +%s)
while :; do
  if curl -fsS -m 2 -o /dev/null "http://127.0.0.1:$PORT/api/health" 2>/dev/null; then
    healthy=1
    break
  fi
  if ! kill -0 "$APP_PID" 2>/dev/null; then
    echo "ERROR: the app exited before its gateway answered /api/health" >&2
    break
  fi
  if [ $(( $(date +%s) - started )) -ge "$MAX_HEALTH_SECONDS" ]; then
    echo "ERROR: the installed app's gateway did not answer /api/health within ${MAX_HEALTH_SECONDS}s" >&2
    break
  fi
  sleep 2
done
LAUNCH_LOG="$(find "$HOME/Library/Logs" -maxdepth 2 -name gateway-launch.log 2>/dev/null | head -1)"
if [ "$healthy" -ne 1 ]; then
  echo "app stdout tail:"; tail -n 40 "$APP_OUT" || true
  echo "app stderr tail:"; tail -n 40 "$APP_ERR" || true
  [ -n "$LAUNCH_LOG" ] && { echo "gateway-launch.log tail:"; tail -n 40 "$LAUNCH_LOG"; }
  exit 1
fi
echo "▶ Installed app's gateway answered /api/health in $(( $(date +%s) - started ))s"

# --- 5. The bundled hand-off actually happened. -------------------------------
if [ "${BUNDLE_KIRO_CLI:-1}" != "0" ]; then
  # The gateway child the supervisor spawned, found by the data home it was
  # given; `ps eww` prints its environment.
  gateway_pid="$(for pid in $(smoke_gateway_pids); do
    if ps -o command= -p "$pid" 2>/dev/null | grep -q "gateway"; then echo "$pid"; break; fi
  done)"
  test -n "$gateway_pid" || { echo "ERROR: no gateway child with KIROCREW_HOME=$HOME_DIR found" >&2; ps -axo pid=,command= | grep -i kiro | head -20 >&2; exit 1; }
  child_env="$(ps eww -p "$gateway_pid")"
  case "$child_env" in
    *"KIROCREW_BUNDLED_KIRO_DIR=$BUNDLED_DIR"*) echo "▶ Gateway child (pid $gateway_pid) carries KIROCREW_BUNDLED_KIRO_DIR=$BUNDLED_DIR" ;;
    *) echo "ERROR: gateway child (pid $gateway_pid) was not handed the bundled kiro-cli directory" >&2
       [ -n "$LAUNCH_LOG" ] && { echo "gateway-launch.log tail:"; tail -n 40 "$LAUNCH_LOG"; }
       exit 1 ;;
  esac
  case "$child_env" in
    *"KIRO_NO_AUTO_UPDATE=1"*) echo "▶ Gateway child carries KIRO_NO_AUTO_UPDATE=1" ;;
    *) echo "ERROR: gateway child lacks KIRO_NO_AUTO_UPDATE=1" >&2; exit 1 ;;
  esac
  if [ -n "$LAUNCH_LOG" ] && grep -q "does not run here" "$LAUNCH_LOG"; then
    echo "ERROR: the shell's runnability probe rejected the bundled kiro-cli:" >&2
    grep "does not run here" "$LAUNCH_LOG" >&2
    exit 1
  fi

  # The installed doctor resolves the same copy on its kiro-cli row. Text is
  # asserted, not the exit code: doctor also reports runner facts it fails on.
  doctor_out="$LOG_DIR/doctor.out"
  KIROCREW_HOME="$HOME_DIR" KIROCREW_BUNDLED_KIRO_DIR="$BUNDLED_DIR" KIRO_NO_AUTO_UPDATE=1 \
    KIROCREW_SKIP_MODEL_DOWNLOAD=1 bounded "$MAX_HEALTH_SECONDS" "installed kirocrew doctor" \
    "$LAUNCHER" doctor >"$doctor_out" 2>&1 || true
  kiro_row="$(grep -E '^\s*kiro-cli:' "$doctor_out" | head -1 || true)"
  echo "▶ Installed doctor kiro-cli row: $kiro_row"
  case "$kiro_row" in
    *"$BUNDLED"*) ;;
    *) echo "ERROR: installed kirocrew doctor did not resolve the bundled kiro-cli at $BUNDLED" >&2
       tail -n 40 "$doctor_out" >&2; exit 1 ;;
  esac
fi

if [ -n "${GITHUB_STEP_SUMMARY:-}" ]; then
  {
    echo "**macOS install-and-launch smoke** ($APP_NAME)"
    echo
    echo "| Step | Result |"
    echo "| --- | --- |"
    echo "| DMG mount + copy | $APP_NAME |"
    echo "| Installed launcher | $launcher_version |"
    echo "| Bundled kiro-cli | ${kiro_version:-skipped (BUNDLE_KIRO_CLI=0)} |"
    echo "| App launch -> /api/health | $(( $(date +%s) - started ))s (ceiling ${MAX_HEALTH_SECONDS}s) |"
  } >> "$GITHUB_STEP_SUMMARY"
fi

echo "macOS install-and-launch smoke passed for '$APP_NAME'."
