#!/bin/sh
# ──────────────────────────────────────────────────────────────────────
# KiroCrew CLI installer (channel / wheel based).
#
#   curl -fsSL https://download.crew.kiro.dev/cli.sh | sh
#   curl -fsSL https://download.crew.kiro.dev/cli.sh | sh -s -- --channel nightly
#
# Installs the prebuilt `kirocrew` wheel for a release channel. It resolves the
# channel feed, verifies its RSA-SHA256 signature against the public key pinned
# below, downloads the wheel over HTTPS from CloudFront, verifies its SHA-256
# against the signed digest, then installs it (pipx if available, else a managed
# venv). No unsigned/checksum-only fallback exists. Unlike install.sh (which
# builds from a git clone), this pulls the published wheel.
#
# Options / env:
#   --channel <nightly|insider|stable>   (default: stable; env KIROCREW_CHANNEL)
#   --version <X.Y.Z>                    pin an exact version, verified against
#                                        its immutable signed CLI manifest
#   --cdn <base-url>                     (default CloudFront; env KIROCREW_CDN_BASE)
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

# Offline trust root for CLI artifact manifests. These two values are replaced
# together during the operational KMS-key enablement documented in
# packaging/signing/README.md, and must stay in lockstep with
# packaging/signing/cli-manifest-public.pem -- KEY_ID is the SHA-256 of that
# PEM's DER encoding, so a mismatched pair fails closed before any network I/O.
# Never edit these in place to rotate: schema v1 pins exactly one key, so an
# in-place swap breaks every already-deployed installer. Rotation requires the
# dual-trust sequence in packaging/signing/README.md.
CLI_MANIFEST_KEY_ID="sha256:d3a83f0c1ff84a2cbee6bd34d889d8725af34358148a6c18ed3ecbbbcceec06b"
CLI_MANIFEST_PUBLIC_KEY_B64="LS0tLS1CRUdJTiBQVUJMSUMgS0VZLS0tLS0KTUlJQm9qQU5CZ2txaGtpRzl3MEJBUUVGQUFPQ0FZOEFNSUlCaWdLQ0FZRUF0MnR0NnZ3ZFZ4Z0tWbTRGQVdkeApwZjZFckx3Y2ljUHlHUGh2SXdXRTRqNmg1YjlwMzFiaktMaWlEakxvK3VpQUJPL21vUjdJUUtoaUNSaXY0d0dTCk1mYnd2ZnNhLy8xNlVBbkNURkRDb1pId0IwVm93cTRYWjZ1NHBrdTFqNlBlRXBMNjVqRXZvcjd1a29HS2xiOVMKQlBva01aN0VtYlpWbmJiSWJBVXYrZ0NWajRCWDRpam5GWkJEMmNPcmtkQWdGR3UraU9jRHVlRDNqTExicXVhUwp0K0tLWXltQ2VxaitPazZ0OFBMQ2VRZmYrWVc4YS9wRU03Wm1tMTJ0Y3BRdEF0OHVCSVdkZE9qaTN1c3BhVlA3CkZJUlhzNnJIajIwTDd0dE9kMGpmKzRWQ0ZtV09FWE4rNWc0YS8rNkcrc3lxeDk4VlR2RVF5cDZVdWZnb0FoQkMKLzFVNG5XajdmMVRFQkV4dXBSRXFUK1lmUmp6aFJUR2NGN0czRUp3MmZjUU1taElIdFpVanM3endVY3NmblhDMwpGQzJBR3pBZnExSGV0WHU5amFOQWZSdjdLZXYxT2hvVmMzYUlONEd3UkpZRDNPNUFSQk5SRGpQUVFWUHBaVW5rCjB1WVdpZExSVDVRUVZMYnlSLzJFKytqTWFyRXBkVXRkZGY1anlwZW5pbFhUQWdNQkFBRT0KLS0tLS1FTkQgUFVCTElDIEtFWS0tLS0tCg=="

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
channel feed, verifies its signature against the installer-pinned public key,
downloads the wheel over HTTPS, verifies its SHA-256 against the signed digest,
then installs it (pipx if available, else a managed venv BESIDE the data home —
"$KIROCREW_HOME"-venv or ~/.kiro/crew-venv, never inside the data home itself).
Records the channel in the data home. There is no unsigned fallback.

Options / env:
  --channel <nightly|insider|stable>   (default: stable; env KIROCREW_CHANNEL)
  --version <X.Y.Z>                    pin an exact version, verified against
                                       its immutable signed CLI manifest
  --cdn <base-url>                     (default CloudFront; env KIROCREW_CDN_BASE)
  KIROCREW_VENV                        override the managed venv location
EOF
      exit 0 ;;
    *) echo "kirocrew-install: unknown argument '$1'" >&2; exit 2 ;;
  esac
done
FEED_BASE="${FEED_BASE%/}"
ARTIFACT_BASE="${ARTIFACT_BASE%/}"

err() { echo "kirocrew-install: $*" >&2; exit 1; }

# The channel name IS the storage path segment: publish-cli.yml writes
# feed/<channel>/latest-cli.json and cli/<channel>/<version>/ using the literal
# channel, and "beta" was renamed to "insider" everywhere including the path
# segment (docs/build/release.md -> "Deliberately not built"). So there is
# no name-to-prefix mapping. Reject anything outside the known set here: a
# typo'd channel otherwise reaches the CDN and surfaces as an opaque 403.
case "$CHANNEL" in
  nightly|insider|stable) ;;
  *) err "unknown channel '$CHANNEL' (expected one of: nightly, insider, stable)" ;;
esac
CHANNEL_PATH="$CHANNEL"

# Canonical physical path of an EXISTING directory (symlinks and `..` resolved),
# or empty output when it cannot be resolved. Used to compare two directory
# paths for identity rather than string equality. Kept POSIX (`cd` + `pwd -P`)
# because `realpath`/`readlink -f` are not portable to macOS's base install.
_canon_dir() {
  ( cd "$1" 2>/dev/null && pwd -P ) 2>/dev/null || printf ''
}

# True when canonical path $1 IS $2 or is nested beneath it. Used to reject any
# overlap between the old and new venv trees before removing one of them:
# equality alone is not enough, because a nested override (KIROCREW_VENV pointing
# INSIDE the old venv) leaves the paths unequal while making `rm -rf` on the
# parent destroy the new installation. The prefix strip is quoted so the
# comparison stays literal rather than glob-matching a path with metacharacters.
_is_within() {
  [ "$1" = "$2" ] && return 0
  _within_rest="${1#"$2"/}"
  [ "$_within_rest" != "$1" ]
}

[ "$CLI_MANIFEST_KEY_ID" != "UNCONFIGURED" ] \
  && [ "$CLI_MANIFEST_PUBLIC_KEY_B64" != "UNCONFIGURED" ] \
  || err "manifest signing trust root is not configured; refusing unsigned installation"

command -v curl    >/dev/null 2>&1 || err "curl is required"
command -v openssl >/dev/null 2>&1 || err "openssl is required to verify the signed manifest"
# KiroCrew needs Python >=3.10 at runtime (contextlib.aclosing, etc.) even
# though older published wheels' METADATA claimed >=3.9 -- pip would install
# fine on 3.9 and then crash on first run. Prefer the newest interpreter the
# project builds and tests on (3.12 is the CI target); 3.13 is untested and
# only a last resort before bare python3, which itself only counts if >=3.10.
_py_usable() {
  command -v "$1" >/dev/null 2>&1 || return 1
  "$1" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3,10) else 1)' 2>/dev/null
}

# Resolve the newest supported interpreter into PY (left empty if none found).
_resolve_python() {
  for _c in python3.12 python3.11 python3.10 python3.13 python3; do
    if _py_usable "$_c"; then PY="$_c"; return 0; fi
  done
  return 1
}

# Privilege prefix for a package-manager install: empty when already root or
# when no sudo exists; otherwise `sudo`. Two sudo shapes by context:
#   - A real terminal on stdin (`sh cli.sh` run interactively): use a normal
#     `sudo`, which may prompt for a password the user can actually type. A
#     non-interactive `sudo -n` here would fail on any password-protected sudo
#     and drop the whole distro-install path (a fresh Ubuntu never installs
#     python3-venv, then the venv step aborts).
#   - No controlling TTY (the documented `curl ... | sh` pipe): only a
#     passwordless `sudo -n` can work, so gate on it and otherwise leave SUDO
#     empty and let the install attempt fail through to the guidance below.
SUDO=""
if [ "$(id -u)" != "0" ] && command -v sudo >/dev/null 2>&1; then
  # A terminal on EITHER stdin or stderr means a password prompt can be shown
  # and answered -- true for `sh cli.sh` and also for `curl ... | sh` in a normal
  # shell, where stdin is the pipe but stderr is still the tty. Only when
  # neither is a terminal (cron, CI, fully redirected) do we require a
  # passwordless `sudo -n` and otherwise leave SUDO empty.
  if [ -t 0 ] || [ -t 2 ]; then
    SUDO="sudo"
  elif sudo -n true 2>/dev/null; then
    SUDO="sudo -n"
  fi
fi

PY=""
_resolve_python || true
if [ -z "$PY" ]; then
  # No system Python >=3.10. Try the distro package manager first, then re-probe.
  # Covers Debian/Ubuntu (apt), Amazon Linux 2023 / RHEL 8+ / CentOS Stream
  # (dnf), and RHEL/CentOS 7 (yum). The apt branch also pulls python3-venv, which
  # Debian splits out of the base interpreter (see the venv step below).
  if command -v apt-get >/dev/null 2>&1; then
    echo "No Python >=3.10 found; attempting: apt-get install python3 python3-venv python3-pip ..."
    $SUDO apt-get update -qq >/dev/null 2>&1 || true
    $SUDO apt-get install -y python3 python3-venv python3-pip >/dev/null 2>&1 || true
  elif command -v dnf >/dev/null 2>&1; then
    echo "No Python >=3.10 found; attempting: dnf install python3.11 ..."
    $SUDO dnf install -y -q python3.11 python3.11-pip >/dev/null 2>&1 || true
  elif command -v yum >/dev/null 2>&1; then
    echo "No Python >=3.10 found; attempting: yum install python3 ..."
    $SUDO yum install -y -q python3 python3-pip >/dev/null 2>&1 || true
  fi
  _resolve_python || true
fi
if [ -z "$PY" ]; then
  # The distro packages could not supply >=3.10: RHEL/CentOS 7 ships 3.6 and
  # older Ubuntu 3.8, and neither has a >=3.10 package in its base repos. If the
  # operator has ALREADY installed mise (its python-build-standalone runs on old
  # glibc and bundles venv/pip), use it. This installer never bootstraps mise
  # itself: piping an unsigned third-party script into `sh` would break the
  # whole point of a signed installer (pinned key + SHA-256-verified wheel, no
  # unsigned fallback). A source build MAY provision mise -- see
  # ensure-python.sh -- but that is an explicit, non-piped path, not this
  # published one-liner.
  MISE="$HOME/.local/bin/mise"
  [ -x "$MISE" ] || MISE="$(command -v mise 2>/dev/null || true)"
  if [ -n "$MISE" ] && [ -x "$MISE" ]; then
    echo "No system Python >=3.10; using your installed mise to provision one ..."
    "$MISE" use -g "python@3.12" >/dev/null 2>&1 || true
    _cand="$("$MISE" which python 2>/dev/null || true)"
    if _py_usable "$_cand"; then PY="$_cand"; fi
  fi
fi
[ -n "$PY" ] || err "Python >=3.10 is required and could not be found. Install it (Debian/Ubuntu: 'sudo apt-get install python3 python3-venv python3-pip'; RHEL/CentOS 8+/Amazon Linux: 'sudo dnf install python3.11'; CentOS 7: install a newer Python via SCL or mise, e.g. 'curl https://mise.run | sh && mise use -g python@3.12'), then re-run."
if command -v sha256sum >/dev/null 2>&1; then SHA_CMD="sha256sum"
elif command -v shasum  >/dev/null 2>&1; then SHA_CMD="shasum -a 256"
else err "need sha256sum or shasum to verify the download"; fi

TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT INT TERM

# Materialize and self-check the embedded trust root. The key id is the SHA-256
# fingerprint of SubjectPublicKeyInfo DER, so an accidental edit to either
# pinned value fails before the network is consulted.
if ! printf '%s' "$CLI_MANIFEST_PUBLIC_KEY_B64" \
    | "$PY" -c 'import base64,sys; open(sys.argv[1], "xb").write(base64.b64decode(sys.stdin.buffer.read(), validate=True))' \
        "$TMP/cli-manifest-public.pem" 2>/dev/null; then
  err "embedded CLI manifest public key is malformed"
fi
if ! openssl pkey -pubin -in "$TMP/cli-manifest-public.pem" \
    -outform DER -out "$TMP/cli-manifest-public.der" 2>/dev/null; then
  err "embedded CLI manifest public key is invalid"
fi
PINNED_KEY_SHA="$($SHA_CMD "$TMP/cli-manifest-public.der" | awk '{print $1}')"
[ "$CLI_MANIFEST_KEY_ID" = "sha256:$PINNED_KEY_SHA" ] \
  || err "embedded CLI manifest public key fingerprint mismatch"

if [ -n "$PIN_VERSION" ]; then
  case "$PIN_VERSION" in
    *[!A-Za-z0-9._+]*) err "invalid pinned version '$PIN_VERSION'" ;;
  esac
  MANIFEST_URL="$ARTIFACT_BASE/cli/$CHANNEL_PATH/$PIN_VERSION/cli-manifest.json"
  echo "Resolving KiroCrew $PIN_VERSION ($CHANNEL channel, pinned) ..."
else
  MANIFEST_URL="$FEED_BASE/feed/$CHANNEL_PATH/latest-cli.json"
  echo "Resolving KiroCrew ($CHANNEL channel) ..."
fi
# Bound unauthenticated metadata before it reaches disk. curl 7.58+ enforces
# --max-filesize against received bytes even without a Content-Length header.
curl -fsS --proto '=https' --max-filesize 65536 "$MANIFEST_URL" \
  -o "$TMP/cli-manifest.json" \
  || err "signed CLI manifest not found at $MANIFEST_URL"

# The signature covers canonical JSON containing every field except the
# signature itself. Reject duplicate/extra/missing keys, decode into bounded
# files, and only parse artifact metadata AFTER OpenSSL authenticates those
# canonical bytes against the offline key above.
if ! "$PY" - "$TMP/cli-manifest.json" "$TMP/signed-payload.json" \
    "$TMP/manifest-signature.bin" "$CLI_MANIFEST_KEY_ID" <<'PY'
import base64
import json
import sys

manifest_path, payload_path, signature_path, pinned_key_id = sys.argv[1:]
expected = {
    "algorithm", "channel", "key_id", "pub_date", "python_requires",
    "schema", "sha256", "signature", "version", "wheel_url",
}

def no_duplicates(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise ValueError("duplicate key")
        value[key] = item
    return value

try:
    raw = open(manifest_path, "rb").read(65537)
    if len(raw) > 65536:
        raise ValueError("oversized manifest")
    manifest = json.loads(raw.decode("utf-8"), object_pairs_hook=no_duplicates)
    if not isinstance(manifest, dict) or set(manifest) != expected:
        raise ValueError("unexpected fields")
    if not all(isinstance(value, str) and value for value in manifest.values()):
        raise ValueError("invalid field type")
    if manifest["schema"] != "kirocrew-cli-artifact-manifest-v1":
        raise ValueError("unsupported schema")
    if manifest["algorithm"] != "RSASSA_PKCS1_V1_5_SHA_256":
        raise ValueError("unsupported algorithm")
    if manifest["key_id"] != pinned_key_id:
        raise ValueError("untrusted key id")
    signature = base64.b64decode(manifest.pop("signature"), validate=True)
    if not signature or len(signature) > 1024:
        raise ValueError("invalid signature size")
    canonical = (
        json.dumps(manifest, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
        + "\n"
    ).encode("ascii")
    if len(canonical) > 16384:
        raise ValueError("oversized payload")
    open(payload_path, "xb").write(canonical)
    open(signature_path, "xb").write(signature)
except (OSError, UnicodeError, ValueError, json.JSONDecodeError):
    raise SystemExit(1)
PY
then
  err "malformed signed manifest — refusing to install"
fi

if ! openssl dgst -sha256 -verify "$TMP/cli-manifest-public.pem" \
    -signature "$TMP/manifest-signature.bin" "$TMP/signed-payload.json" \
    >/dev/null 2>&1; then
  err "manifest signature verification failed — refusing to install"
fi
echo "Verified signed manifest."

# Validate the authenticated fields against the request. In particular, the
# wheel URL must be the one canonical URL implied by the selected byte host,
# channel, and signed version; a valid signer cannot redirect this installer to
# an unrelated origin by accident.
if ! "$PY" - "$TMP/signed-payload.json" "$CHANNEL" "$PIN_VERSION" \
    "$ARTIFACT_BASE" <<'PY'
import json
import re
import sys

path, expected_channel, pinned_version, artifact_base = sys.argv[1:]
payload = json.load(open(path, encoding="ascii"))
expected_fields = {
    "algorithm", "channel", "key_id", "pub_date", "python_requires",
    "schema", "sha256", "version", "wheel_url",
}
try:
    if set(payload) != expected_fields:
        raise ValueError
    if payload["channel"] != expected_channel:
        raise ValueError
    version = payload["version"]
    if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._+]{0,127}", version) is None:
        raise ValueError
    if pinned_version and version != pinned_version:
        raise ValueError
    if re.fullmatch(r"[0-9a-f]{64}", payload["sha256"]) is None:
        raise ValueError
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z", payload["pub_date"]) is None:
        raise ValueError
    if len(payload["python_requires"]) > 128 or any(
        ord(char) < 0x20 or ord(char) > 0x7E for char in payload["python_requires"]
    ):
        raise ValueError
    wheel_name = f"kirocrew-{version}-py3-none-any.whl"
    expected_url = f"{artifact_base}/cli/{expected_channel}/{version}/{wheel_name}"
    if payload["wheel_url"] != expected_url:
        raise ValueError
except (KeyError, TypeError, ValueError):
    raise SystemExit(1)
PY
then
  err "signed manifest does not match the requested channel/version/artifact host"
fi

read_field() {
  "$PY" -c 'import json,sys; print(json.load(open(sys.argv[1]))[sys.argv[2]])' \
    "$TMP/signed-payload.json" "$1"
}
WHEEL_URL="$(read_field wheel_url)"
SHA="$(read_field sha256)"
VER="$(read_field version)"
WHEEL_NAME="kirocrew-${VER}-py3-none-any.whl"
WHL="$TMP/$WHEEL_NAME"

echo "Downloading kirocrew $VER ..."
curl -fsS --proto '=https' "$WHEEL_URL" -o "$WHL" || err "failed to download wheel from $WHEEL_URL"

GOT="$($SHA_CMD "$WHL" | awk '{print $1}')"
[ "$GOT" = "$SHA" ] || err "SHA-256 mismatch (expected $SHA, got $GOT) — refusing to install"
echo "Verified SHA-256."

if command -v pipx >/dev/null 2>&1; then
  echo "Installing with pipx ..."
  pipx install --force --python "$PY" "$WHL" >/dev/null
  BIN="$(pipx environment --value PIPX_BIN_DIR 2>/dev/null || echo "$HOME/.local/bin")"
else
  # The managed venv lives BESIDE the data home, never inside it. Nesting the
  # interpreter in the data home put the runtime and the user's data in one
  # blast radius: the one-time ~/.kirocrew -> ~/.kiro/crew data-home migration
  # copied the whole legacy tree and then deleted it, which for a wheel install
  # meant copying a non-relocatable venv (dead shebangs at the destination) and
  # deleting the live interpreter mid-run — leaving a dangling
  # ~/.local/bin/kirocrew and no working CLI. Keeping the venv out of the data
  # home means no home-wide operation can ever reach the interpreter again.
  _DATA_HOME_FOR_VENV="${KIROCREW_HOME:-$HOME/.kiro/crew}"
  VENV="${KIROCREW_VENV:-${_DATA_HOME_FOR_VENV%/}-venv}"
  _OLD_VENV="${_DATA_HOME_FOR_VENV%/}/venv"
  echo "Installing into managed venv at $VENV ..."
  # Debian/Ubuntu ship the base `python3` WITHOUT the venv/ensurepip module (it
  # lives in the separate `python3-venv` / `python3.X-venv` package), so
  # `python3 -m venv` there dies with "ensurepip is not available" and, under
  # `set -eu`, aborts the whole install with a raw stack trace. Probe the
  # capability first; if it is missing, try to apt-install the version-matched
  # venv package, then re-probe. A minimal RHEL/CentOS python bundles venv, so
  # this only bites apt systems.
  if ! "$PY" -c 'import ensurepip, venv' >/dev/null 2>&1; then
    if command -v apt-get >/dev/null 2>&1; then
      # e.g. "3.12" -> python3.12-venv; also install the generic python3-venv so
      # a bare `python3` interpreter is covered too.
      _pyminor="$("$PY" -c 'import sys; print("%d.%d" % sys.version_info[:2])' 2>/dev/null || true)"
      echo "Installing the venv module (python3-venv) ..."
      $SUDO apt-get update -qq >/dev/null 2>&1 || true
      $SUDO apt-get install -y "python${_pyminor}-venv" python3-venv >/dev/null 2>&1 \
        || $SUDO apt-get install -y python3-venv >/dev/null 2>&1 || true
    fi
    "$PY" -c 'import ensurepip, venv' >/dev/null 2>&1 \
      || err "$PY cannot create a virtual environment (the venv/ensurepip module is missing). On Debian/Ubuntu install it with 'sudo apt-get install python3-venv' (or 'python${_pyminor:-3}-venv'), then re-run."
  fi
  "$PY" -m venv "$VENV"
  "$VENV/bin/pip" install --quiet --upgrade pip >/dev/null 2>&1 || true
  "$VENV/bin/pip" install --quiet "$WHL"
  mkdir -p "$HOME/.local/bin"
  ln -sf "$VENV/bin/kirocrew" "$HOME/.local/bin/kirocrew"
  BIN="$HOME/.local/bin"
  # Retire a venv left inside the data home by an earlier version of this
  # script. Three independent conditions must all hold, so this never deletes
  # anything that is not our own managed environment:
  #   1. `pyvenv.cfg` present — proves it IS a virtual environment (the stdlib
  #      venv module always writes it) and not a user directory that merely
  #      happens to be named `venv`, whose contents would otherwise be
  #      recursively deleted by a routine reinstall.
  #   2. `bin/kirocrew` present — proves it is OUR managed environment rather
  #      than some unrelated venv the user parked in the data home.
  #   3. The new environment imports `kiro_crew` — proves the replacement works
  #      before the old one goes away.
  # Plus: not a symlink, and no overlap with the new tree.
  #
  # The old/new comparison is on CANONICAL paths and rejects any OVERLAP of the
  # two trees, not just exact equality: KIROCREW_VENV could name the same
  # directory by a different route (a symlink, or a `..` segment such as
  # $KIROCREW_HOME/../crew/venv), or could point INSIDE the old venv
  # ($KIROCREW_HOME/venv/new) — in which case the paths differ yet `rm -rf` on
  # the old tree deletes the new installation and the ~/.local/bin/kirocrew
  # symlink target with it. Fails CLOSED: if either path cannot be canonicalized
  # we skip the removal rather than guess.
  if [ -d "$_OLD_VENV" ] && [ ! -L "$_OLD_VENV" ] \
     && [ -f "$_OLD_VENV/pyvenv.cfg" ] && [ -f "$_OLD_VENV/bin/kirocrew" ]; then
    _OLD_CANON="$(_canon_dir "$_OLD_VENV")"
    _NEW_CANON="$(_canon_dir "$VENV")"
    if [ -z "$_OLD_CANON" ] || [ -z "$_NEW_CANON" ]; then
      echo "WARNING: could not canonicalize $_OLD_VENV or $VENV; leaving $_OLD_VENV in place." >&2
    elif _is_within "$_NEW_CANON" "$_OLD_CANON" || _is_within "$_OLD_CANON" "$_NEW_CANON"; then
      : # overlapping trees — removing either would damage the new installation
    elif "$VENV/bin/python" -c 'import kiro_crew' >/dev/null 2>&1; then
      echo "Removing the superseded in-data-home venv at $_OLD_VENV ..."
      rm -rf "$_OLD_VENV"
    else
      echo "WARNING: new venv at $VENV failed an import check; leaving $_OLD_VENV in place." >&2
    fi
  fi
fi

_DATA_HOME="${KIROCREW_HOME:-$HOME/.kiro/crew}"
mkdir -p "$_DATA_HOME"
printf '%s\n' "$CHANNEL" > "$_DATA_HOME/channel"

echo ""
echo "Installed kirocrew $VER (channel: $CHANNEL)."
case ":$PATH:" in
  *":$BIN:"*) : ;;
  *) echo "Add $BIN to your PATH first (e.g. add it in your shell profile)." ;;
esac
# Point at the actual next step, not just --help: a user who ran the one-liner
# wants a running gateway. The persistent-service path (systemd/launchd) is
# otherwise buried in the docs, which is the #1 remote-crew onboarding
# complaint. `service install` is the durable path; `gateway` is the foreground
# one for a quick look.
echo ""
echo "Next steps:"
echo "  kirocrew gateway            # start the dashboard now (http://localhost:5476)"
echo "  kirocrew service install    # run it 24/7 as a service (survives logout, restarts on crash)"
echo "  kirocrew --help             # everything else"
echo ""
echo "Need a non-default port (e.g. 5476 is already taken)? Set it at install time;"
echo "it is baked into the service unit:"
echo "  KIROCREW_PORT=5477 kirocrew service install"
