#!/bin/sh
# ──────────────────────────────────────────────────────────────────────
# KiroCrew CLI installer (channel / wheel based).
#
#   curl -fsSL https://download.crew.kiro.dev/cli.sh | sh
#   curl -fsSL https://download.crew.kiro.dev/cli.sh | sh -s -- --channel nightly
#
# Installs the prebuilt `kirocrew` wheel for a release channel. It resolves the
# channel feed, downloads the wheel over HTTPS from CloudFront, verifies its
# SHA-256 against the feed manifest, then installs it (pipx if available, else a
# managed venv). No Brazil workspace, no Node, no build step. Unlike install.sh
# (which builds from a git clone), this pulls the published wheel.
#
# Options / env:
#   --channel <nightly|insider|stable>   (default: stable; env KIROCREW_CHANNEL)
#   --version <X.Y.Z>                     pin an exact version instead of the
#                                         channel's latest (verified against the
#                                         version's published SHA256SUMS)
#   --cdn <base-url>                      (default CloudFront; env KIROCREW_CDN_BASE)
# ──────────────────────────────────────────────────────────────────────
set -eu

# Isolate the managed venv from any inherited PYTHONPATH/PYTHONHOME. If the
# caller's environment points these at foreign site-packages (e.g. another
# app's interpreter on a different Python version), pip treats those packages
# as already satisfied and silently skips installing our dependencies into the
# venv -- producing a broken install (ImportError: No module named 'aiohttp').
unset PYTHONPATH PYTHONHOME

# The URL contract splits by class: FEED_BASE serves the mutable pointers
# (latest-cli.json), ARTIFACT_BASE serves the bytes (wheels, SHA256SUMS).
# Both are aliases of the same distribution today; --cdn / KIROCREW_CDN_BASE
# overrides BOTH (test / alternate-CDN escape hatch).
FEED_BASE="${KIROCREW_CDN_BASE:-https://updates.crew.kiro.dev}"
ARTIFACT_BASE="${KIROCREW_CDN_BASE:-https://download.crew.kiro.dev}"
CHANNEL="${KIROCREW_CHANNEL:-stable}"
PIN_VERSION=""

while [ $# -gt 0 ]; do
  case "$1" in
    --channel) CHANNEL="${2:?--channel needs a value}"; shift 2 ;;
    --channel=*) CHANNEL="${1#*=}"; shift ;;
    --version) PIN_VERSION="${2:?--version needs a value}"; shift 2 ;;
    --version=*) PIN_VERSION="${1#*=}"; shift ;;
    --cdn) FEED_BASE="${2:?--cdn needs a value}"; ARTIFACT_BASE="$2"; shift 2 ;;
    --cdn=*) FEED_BASE="${1#*=}"; ARTIFACT_BASE="${1#*=}"; shift ;;
    -h|--help)
      cat <<'EOF'
KiroCrew CLI installer (channel / wheel based).

  curl -fsSL https://download.crew.kiro.dev/cli.sh | sh
  curl -fsSL https://download.crew.kiro.dev/cli.sh | sh -s -- --channel nightly

Installs the prebuilt `kirocrew` wheel for a release channel: resolves the
channel feed, downloads the wheel over HTTPS, verifies its SHA-256 against
the published manifest, then installs it (pipx if available, else a managed
venv at ~/.kirocrew/venv). Records the channel to ~/.kirocrew/channel.

Options / env:
  --channel <nightly|insider|stable>   (default: stable; env KIROCREW_CHANNEL)
  --version <X.Y.Z>                    pin an exact version, verified against
                                       that version's published SHA256SUMS
  --cdn <base-url>                     (default CloudFront; env KIROCREW_CDN_BASE)
EOF
      exit 0 ;;
    *) echo "kirocrew-install: unknown argument '$1'" >&2; exit 2 ;;
  esac
done
FEED_BASE="${FEED_BASE%/}"
ARTIFACT_BASE="${ARTIFACT_BASE%/}"

# Users say "insider"; the release pipeline publishes that channel under the
# `beta` prefix (see docs/RELEASE_AUTOMATION.md channel naming). Map the
# user-facing name to the storage prefix; keep the user-facing name for the
# recorded channel file.
case "$CHANNEL" in
  insider) CHANNEL_PATH="beta" ;;
  *) CHANNEL_PATH="$CHANNEL" ;;
esac

err() { echo "kirocrew-install: $*" >&2; exit 1; }

command -v curl    >/dev/null 2>&1 || err "curl is required"
# KiroCrew needs Python >=3.10 at runtime (contextlib.aclosing, etc.) even
# though older published wheels' METADATA claimed >=3.9 -- pip would install
# fine on 3.9 and then crash on first run. Pick the best interpreter, newest
# first; plain python3 only counts if it is itself >=3.10.
PY=""
for c in python3.13 python3.12 python3.11 python3.10 python3; do
  command -v "$c" >/dev/null 2>&1 || continue
  if "$c" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3,10) else 1)' 2>/dev/null; then
    PY="$c"; break
  fi
done
if [ -z "$PY" ] && command -v dnf >/dev/null 2>&1; then
  # Amazon Linux 2023 ships python3 = 3.9; a 3.10+ interpreter is one dnf away.
  echo "No Python >=3.10 found; attempting: dnf install python3.11 ..."
  if [ "$(id -u)" = "0" ]; then
    dnf install -y -q python3.11 >/dev/null 2>&1 || true
  elif command -v sudo >/dev/null 2>&1 && sudo -n true 2>/dev/null; then
    sudo dnf install -y -q python3.11 >/dev/null 2>&1 || true
  fi
  command -v python3.11 >/dev/null 2>&1 && PY="python3.11"
fi
[ -n "$PY" ] || err "Python >=3.10 is required (KiroCrew uses 3.10+ stdlib features). On Amazon Linux: sudo dnf install python3.11"
if command -v sha256sum >/dev/null 2>&1; then SHA_CMD="sha256sum"
elif command -v shasum  >/dev/null 2>&1; then SHA_CMD="shasum -a 256"
else err "need sha256sum or shasum to verify the download"; fi

TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT INT TERM

if [ -n "$PIN_VERSION" ]; then
  # Pinned install: skip the feed; fetch the exact version's wheel and verify
  # against the SHA256SUMS published beside it at release time.
  VER="$PIN_VERSION"
  WHEEL_NAME="kirocrew-${VER}-py3-none-any.whl"
  WHEEL_URL="$ARTIFACT_BASE/cli/$CHANNEL_PATH/$VER/$WHEEL_NAME"
  echo "Resolving KiroCrew $VER ($CHANNEL channel, pinned) ..."
  curl -fsSL "$ARTIFACT_BASE/cli/$CHANNEL_PATH/$VER/SHA256SUMS" -o "$TMP/SHA256SUMS" \
    || err "version '$VER' not found on the $CHANNEL channel (no $ARTIFACT_BASE/cli/$CHANNEL_PATH/$VER/SHA256SUMS)"
  SHA="$(awk -v w="$WHEEL_NAME" '$2==w{print $1}' "$TMP/SHA256SUMS")"
  [ -n "$SHA" ] || err "SHA256SUMS for $VER does not list $WHEEL_NAME"
else
  FEED="$FEED_BASE/feed/$CHANNEL_PATH/latest-cli.json"
  echo "Resolving KiroCrew ($CHANNEL channel) ..."
  curl -fsSL "$FEED" -o "$TMP/feed.json" \
    || err "channel '$CHANNEL' has no feed at $FEED (try: --channel nightly)"

  # Parse the manifest with python3 (portable; no jq dependency). Read the path
  # from argv so no shell value is interpolated into the program text.
  read_field() { "$PY" -c 'import json,sys; print(json.load(open(sys.argv[1]))[sys.argv[2]])' "$TMP/feed.json" "$1"; }
  WHEEL_URL="$(read_field wheel_url)"
  SHA="$(read_field sha256)"
  VER="$(read_field version)"
  [ -n "$WHEEL_URL" ] && [ -n "$SHA" ] || err "malformed feed manifest"
fi

WHL="$TMP/$(basename "$WHEEL_URL")"
echo "Downloading kirocrew $VER ..."
curl -fsSL "$WHEEL_URL" -o "$WHL" || err "failed to download wheel from $WHEEL_URL"

GOT="$($SHA_CMD "$WHL" | awk '{print $1}')"
[ "$GOT" = "$SHA" ] || err "SHA-256 mismatch (expected $SHA, got $GOT) — refusing to install"
echo "Verified SHA-256."

if command -v pipx >/dev/null 2>&1; then
  echo "Installing with pipx ..."
  pipx install --force --python "$PY" "$WHL" >/dev/null
  BIN="$(pipx environment --value PIPX_BIN_DIR 2>/dev/null || echo "$HOME/.local/bin")"
else
  VENV="$HOME/.kirocrew/venv"
  echo "Installing into managed venv at $VENV ..."
  "$PY" -m venv "$VENV"
  "$VENV/bin/pip" install --quiet --upgrade pip >/dev/null 2>&1 || true
  "$VENV/bin/pip" install --quiet "$WHL"
  mkdir -p "$HOME/.local/bin"
  ln -sf "$VENV/bin/kirocrew" "$HOME/.local/bin/kirocrew"
  BIN="$HOME/.local/bin"
fi

mkdir -p "$HOME/.kirocrew"
printf '%s\n' "$CHANNEL" > "$HOME/.kirocrew/channel"

echo ""
echo "Installed kirocrew $VER (channel: $CHANNEL)."
case ":$PATH:" in
  *":$BIN:"*) echo "Run: kirocrew --help" ;;
  *) echo "Add $BIN to your PATH, then run: kirocrew --help" ;;
esac
