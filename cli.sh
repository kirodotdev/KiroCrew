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
# Python: uses the system interpreter (>=3.10) when one exists; otherwise — or
# always, with --managed-python — provisions a python-build-standalone CPython
# via a SHA-256-pinned uv into a user-owned directory. No package manager, no
# sudo, works on old-glibc distros (CentOS 7).
#
# Options / env:
#   --channel <nightly|insider|stable>   (default: stable; env KIROCREW_CHANNEL)
#   --version <X.Y.Z>                    pin an exact version, verified against
#                                        its immutable signed CLI manifest
#   --cdn <base-url>                     (default CloudFront; env KIROCREW_CDN_BASE)
#   --managed-python                     skip the system interpreters and run on
#                                        a uv-provisioned Python instead (sticky:
#                                        later runs and updates keep the choice;
#                                        opt out with --system-python)
#                                        (env KIROCREW_MANAGED_PYTHON=1)
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
# Three states: "" = undecided (fall back to the persisted python-mode marker,
# then to system), "1" = managed, "0" = system. An explicit env value or flag
# always outranks the marker, so an operator can override a sticky choice.
MANAGED_PYTHON="${KIROCREW_MANAGED_PYTHON:-}"

# Pinned uv release used to provision a managed Python interpreter when the
# system has none (or when --managed-python asks for one). uv is only ever
# fetched as a tarball and verified against these SHA-256 digests — the
# astral.sh install script is never piped into a shell, keeping the signed
# installer's no-unsigned-third-party-script invariant. Bump the version and
# all four digests together (each uv release publishes <asset>.tar.gz.sha256
# files beside the tarballs). Linux pins the musl builds: they are fully
# static, so they run on any glibc age — old distros are the whole point of
# this path.
UV_VERSION="0.10.11"
UV_PYTHON_SERIES="cpython-3.12"
UV_SHA_LINUX_X64="d78246139dc6cf3ed6d03c84da762686bced7ad1de67977ee372a45b95a1f6d0"
UV_SHA_LINUX_ARM64="5d80a7f6343d2676dfde1e5126582070a2bbc62df6f60d5527a169be3788532a"
UV_SHA_MACOS_X64="ff90020b554cf02ef8008535c9aab6ef27bb7be6b075359300dec79c361df897"
UV_SHA_MACOS_ARM64="437a7d498dd6564d5bf986074249ba1fc600e73da55ae04d7bd4c24d5f149b95"

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
    --managed-python) MANAGED_PYTHON=1; shift ;;
    --system-python) MANAGED_PYTHON=0; shift ;;
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
  --managed-python                     skip the system interpreters and run on a
                                       uv-provisioned Python instead (sticky: later
                                       runs and updates keep the choice)
  --system-python                      opt back out of a recorded managed-python
                                       choice and use the system interpreter
                                       (env KIROCREW_MANAGED_PYTHON=1)
  KIROCREW_VENV                        override the managed venv location
  KIROCREW_PYTHON_DIR                  override where uv-provisioned interpreters
                                       are stored (default: beside the data home,
                                       ~/.kiro/crew-python)
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
if command -v sha256sum >/dev/null 2>&1; then SHA_CMD="sha256sum"
elif command -v shasum  >/dev/null 2>&1; then SHA_CMD="shasum -a 256"
else err "need sha256sum or shasum to verify the download"; fi

TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT INT TERM

# KiroCrew needs Python >=3.10 at runtime (contextlib.aclosing, etc.) even
# though older published wheels' METADATA claimed >=3.9 -- pip would install
# fine on 3.9 and then crash on first run. Prefer the newest interpreter the
# project builds and tests on (3.12 is the CI target); 3.13 is untested and
# only a last resort before bare python3, which itself only counts if >=3.10.
# A version-manager shim (mise, pyenv, asdf) can wedge instead of answering --
# notably when HOME does not hold the config the shim expects -- and an
# unbounded probe then hangs the whole install on its FIRST candidate,
# python3.12, leaving a spinning orphan behind. Bound it so a wedged candidate
# fails over to the next one. A healthy interpreter answers in well under a
# tenth of a second, so 5s is generous and caps the whole ladder at ~25s.
# `timeout` is absent from a stock macOS, so its absence must leave the probe
# working rather than fail every candidate.
_PY_PROBE_TIMEOUT=""
if command -v timeout >/dev/null 2>&1; then
  _PY_PROBE_TIMEOUT="timeout 5"
fi

_py_usable() {
  command -v "$1" >/dev/null 2>&1 || return 1
  if [ -n "$_PY_PROBE_TIMEOUT" ]; then
    # Unquoted on purpose: expands to two words, or to nothing when unavailable.
    # shellcheck disable=SC2086
    $_PY_PROBE_TIMEOUT "$1" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3,10) else 1)' 2>/dev/null
    return $?
  fi
  # No `timeout` binary (stock macOS): emulate the same 5s bound with a POSIX
  # watchdog, so a wedged version-manager shim still fails over to the next
  # candidate instead of hanging the install on its first probe.
  "$1" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3,10) else 1)' >/dev/null 2>&1 &
  _py_pid=$!
  (
    # On TERM from the fast-exit path below, kill our own in-flight `sleep`
    # before exiting: a plain kill on this subshell cannot reach its child,
    # which would otherwise linger for up to a second. kill -9 is untrappable,
    # so the parent must TERM us for this cleanup to run.
    trap 'kill "${_wd_sleep:-}" 2>/dev/null || true; exit 0' TERM
    _i=0
    while [ "$_i" -lt 5 ]; do
      sleep 1 & _wd_sleep=$!
      wait "$_wd_sleep" 2>/dev/null || true
      kill -0 "$_py_pid" 2>/dev/null || exit 0
      _i=$((_i + 1))
    done
    kill -9 "$_py_pid" 2>/dev/null || true
  ) &
  _watchdog_pid=$!
  # Capture the probe's real exit status: a plain `wait ... || true` would
  # overwrite $? and report every candidate as usable. 137 (killed by the
  # watchdog) and 1 (version too old) must both read as "not usable".
  _py_status=0
  wait "$_py_pid" 2>/dev/null || _py_status=$?
  kill "$_watchdog_pid" 2>/dev/null || true
  wait "$_watchdog_pid" 2>/dev/null || true
  return "$_py_status"
}

# Resolve the newest supported interpreter into PY (left empty if none found).
_resolve_python() {
  for _c in python3.12 python3.11 python3.10 python3.13 python3; do
    if _py_usable "$_c"; then PY="$_c"; return 0; fi
  done
  return 1
}

# ── Managed-Python provisioning (uv + python-build-standalone) ──────────────
# Provision a CPython interpreter without touching the system: uv is a static,
# dependency-free binary, and the interpreters it installs are prebuilt
# python-build-standalone archives that unpack into a user-owned directory —
# no package manager, no sudo, and they run on old-glibc distros (CentOS 7)
# whose base repos never reach 3.10. An installed `uv` on PATH is used as-is
# (the user already made that trust decision); otherwise the pinned uv release
# is downloaded and verified against the SHA-256 digests above before it runs.
# Sets PY on success. Network/platform failures return 1 so the caller can
# print guidance; a digest mismatch is a security stop and errs immediately.
_provision_python_via_uv() {
  _uv_bin=""
  if command -v uv >/dev/null 2>&1; then
    _uv_bin="$(command -v uv)"
    echo "Using installed uv ($_uv_bin) to provision Python ($UV_PYTHON_SERIES) ..."
  else
    # GNU tar's -z shells out to gzip, so both must exist before downloading.
    for _uv_tool in tar gzip; do
      if ! command -v "$_uv_tool" >/dev/null 2>&1; then
        echo "kirocrew-install: $_uv_tool is required to unpack uv" >&2
        return 1
      fi
    done
    case "$(uname -s)/$(uname -m)" in
      Linux/x86_64)              _uv_target="x86_64-unknown-linux-musl";  _uv_sha="$UV_SHA_LINUX_X64" ;;
      Linux/aarch64|Linux/arm64) _uv_target="aarch64-unknown-linux-musl"; _uv_sha="$UV_SHA_LINUX_ARM64" ;;
      Darwin/x86_64)             _uv_target="x86_64-apple-darwin";        _uv_sha="$UV_SHA_MACOS_X64" ;;
      Darwin/arm64)              _uv_target="aarch64-apple-darwin";       _uv_sha="$UV_SHA_MACOS_ARM64" ;;
      *)
        echo "kirocrew-install: no pinned uv build for $(uname -s)/$(uname -m)" >&2
        return 1 ;;
    esac
    echo "Downloading uv $UV_VERSION ($_uv_target) ..."
    # -L: GitHub release assets redirect to a storage host; --proto-redir keeps
    # every hop on HTTPS (curl's redirect default would also allow plain http).
    curl -fsSL --proto '=https' --proto-redir '=https' \
      "https://github.com/astral-sh/uv/releases/download/$UV_VERSION/uv-$_uv_target.tar.gz" \
      -o "$TMP/uv.tar.gz" || return 1
    _uv_got="$($SHA_CMD "$TMP/uv.tar.gz" | awk '{print $1}')"
    [ "$_uv_got" = "$_uv_sha" ] \
      || err "uv download SHA-256 mismatch (expected $_uv_sha, got $_uv_got) — refusing to continue"
    ( cd "$TMP" && tar -xzf uv.tar.gz ) || return 1
    _uv_bin="$TMP/uv-$_uv_target/uv"
    [ -x "$_uv_bin" ] || return 1
  fi
  # The interpreter store lives BESIDE the data home, like the managed venv and
  # for the same blast-radius reason: no data-home-wide operation may ever
  # reach the interpreter that the venv's shebangs point at.
  _uv_data_home="${KIROCREW_HOME:-$HOME/.kiro/crew}"
  _uv_py_dir="${KIROCREW_PYTHON_DIR:-${_uv_data_home%/}-python}"
  UV_PYTHON_INSTALL_DIR="$_uv_py_dir" "$_uv_bin" python install "$UV_PYTHON_SERIES" \
    || return 1
  # only-managed: resolve the interpreter just installed, never a system one
  # that happens to satisfy the series (matters under --managed-python, where
  # a usable system 3.12 may exist but was explicitly opted out of).
  _cand="$(UV_PYTHON_INSTALL_DIR="$_uv_py_dir" UV_PYTHON_PREFERENCE=only-managed \
    "$_uv_bin" python find "$UV_PYTHON_SERIES" 2>/dev/null || true)"
  if [ -n "$_cand" ] && _py_usable "$_cand"; then
    PY="$_cand"
    echo "Provisioned managed Python: $_cand"
    return 0
  fi
  return 1
}

PY=""
# The interpreter choice is STICKY: a completed install records its mode in
# the data home (next to `channel`), and a later run without an explicit flag
# or env value reuses it. Without this, every re-run of the one-liner -- most
# importantly the one `kirocrew update` performs -- would silently flip a
# --managed-python install back onto whatever system interpreter it finds.
# Opt back out explicitly with --system-python (or KIROCREW_MANAGED_PYTHON=0).
_DATA_HOME="${KIROCREW_HOME:-$HOME/.kiro/crew}"
# The marker is agent-writable state, so the READ is guarded like the write:
# only a plain regular file counts (a planted symlink -- e.g. to /dev/zero --
# or a FIFO would wedge an unbounded read or spoof the mode), and the read is
# bounded to the first bytes rather than slurping the file.
_py_mode_file="$_DATA_HOME/python-mode"
if [ -z "$MANAGED_PYTHON" ] && [ -f "$_py_mode_file" ] && [ ! -L "$_py_mode_file" ] \
    && [ "$(head -c 16 "$_py_mode_file" 2>/dev/null || true)" = "managed" ]; then
  echo "Reusing the recorded managed-python choice (override with --system-python)."
  MANAGED_PYTHON=1
fi
[ -n "$MANAGED_PYTHON" ] || MANAGED_PYTHON=0

if [ "$MANAGED_PYTHON" = "1" ]; then
  echo "managed-python: skipping system interpreters."
  _provision_python_via_uv \
    || err "could not provision a managed Python via uv. Check the network connection, or install Python >=3.10 yourself and re-run without --managed-python."
else
  _resolve_python || true
  if [ -z "$PY" ]; then
    echo "No system Python >=3.10 found; provisioning one via uv ..."
    _provision_python_via_uv || true
  fi
fi
[ -n "$PY" ] || err "Python >=3.10 is required and could not be found or provisioned. Install Python 3.10+ yourself (your distro's packages, or https://www.python.org/downloads/), then re-run."

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
  # interpreter in the data home would put the runtime and the user's data in
  # one blast radius: any home-wide operation (a bulk delete, a backup restore,
  # a relocation) could reach the live interpreter — a non-relocatable venv with
  # absolute shebangs — and leave a dangling ~/.local/bin/kirocrew and no working
  # CLI. Keeping the venv out of the data home means no home-wide operation can
  # ever reach the interpreter.
  _DATA_HOME_FOR_VENV="${KIROCREW_HOME:-$HOME/.kiro/crew}"
  VENV="${KIROCREW_VENV:-${_DATA_HOME_FOR_VENV%/}-venv}"
  _OLD_VENV="${_DATA_HOME_FOR_VENV%/}/venv"
  echo "Installing into managed venv at $VENV ..."
  # Debian/Ubuntu ship the base `python3` WITHOUT the venv/ensurepip module (it
  # lives in the separate `python3-venv` / `python3.X-venv` package), so
  # `python3 -m venv` there dies with "ensurepip is not available" and, under
  # `set -eu`, aborts the whole install with a raw stack trace. Rather than
  # driving the system package manager with sudo, fall back to a managed
  # interpreter: python-build-standalone bundles venv and pip, so the re-probe
  # always passes on it.
  if ! "$PY" -c 'import ensurepip, venv' >/dev/null 2>&1; then
    echo "$PY cannot create a virtual environment (venv/ensurepip missing); provisioning a managed Python via uv instead ..."
    _provision_python_via_uv \
      || err "$PY cannot create a virtual environment (the venv/ensurepip module is missing) and a managed Python could not be provisioned. On Debian/Ubuntu install the module with 'sudo apt-get install python3-venv', then re-run."
  fi
  # Rebuilding over an EXISTING venv must not keep the old interpreter: the
  # venv module rewrites pyvenv.cfg but leaves an existing bin/python* symlink
  # in place, producing a hybrid that CLAIMS the new interpreter while running
  # the old one -- silently defeating --managed-python for an existing install
  # and dying with a dangling shebang the day the old interpreter disappears.
  # Remove ONLY the interpreter links so the venv rebuild recreates them
  # against $PY. Never `--clear`: that empties site-packages and the
  # entrypoint too, so a download failure in the pip step below would leave
  # the user with NO working install where a plain re-run used to keep the
  # old one. The links are the only stale asset, and they are regenerated by
  # the very next command. Guards: pyvenv.cfg proves the target IS a venv (a
  # mis-pointed KIROCREW_VENV at a plain directory is never touched), and a
  # symlinked venv root (trailing slash stripped so `-L` sees the link
  # itself) is left as-is -- the venv module refuses a symlink root anyway,
  # and removing links inside its target first would break the linked venv
  # before that refusal.
  if [ -f "$VENV/pyvenv.cfg" ] && [ ! -L "${VENV%/}" ]; then
    rm -f "$VENV/bin/python" "$VENV/bin/python3" "$VENV/bin"/python3.* 2>/dev/null || true
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
# Atomic, symlink-proof marker writes. A plain `>` redirection FOLLOWS a
# pre-planted symlink at the destination -- the data home is agent-writable,
# so a hostile `ln -sf ~/.bashrc .../python-mode` would turn the next install
# run into an arbitrary-file overwrite. mktemp creates a fresh regular file
# (O_EXCL, never a symlink); a pre-existing symlink at the destination is
# removed first (mv would REPLACE a symlink-to-file, but would move the temp
# file INSIDE a symlink-to-directory's target); a real directory at the
# marker path is corrupt state and is refused loudly -- mv would otherwise
# move the temp file inside it and every later read would silently miss the
# marker. After those guards the destination is absent or a regular file, so
# the rename is atomic and can never land outside the data home.
_write_marker() {
  _marker_dest="$_DATA_HOME/$1"
  [ -L "$_marker_dest" ] && rm -f "$_marker_dest"
  [ -d "$_marker_dest" ] \
    && err "refusing to record $1: a directory occupies $_marker_dest -- remove it and re-run"
  _marker_tmp="$(mktemp "$_DATA_HOME/.marker.XXXXXX")"
  printf '%s\n' "$2" > "$_marker_tmp"
  mv -f "$_marker_tmp" "$_marker_dest"
}
_write_marker channel "$CHANNEL"
# Record the interpreter mode so the next run -- including the re-run that
# `kirocrew update` performs -- keeps the same choice without the flag.
if [ "$MANAGED_PYTHON" = "1" ]; then
  _write_marker python-mode managed
else
  _write_marker python-mode system
fi

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
