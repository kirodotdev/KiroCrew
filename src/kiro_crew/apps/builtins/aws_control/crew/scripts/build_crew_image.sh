#!/usr/bin/env bash
# Build (and push) the Share My Crew CREW IMAGE: a thin, digest-pinned layer on
# the maintainer's base image, carrying one curated crew bundle.
#
# Owned by the crew/scripts track (T2, see PACKAGING-CONTRACT.md). Sibling of
# scripts/build_image.sh, which is now the maintainer's BASE-image builder.
#
# What it does, in order:
#   1. Validate inputs: --base must be digest-pinned (repo@sha256:...), the
#      bundle dir must hold the four-entry layout, and the bundle's own
#      manifest.json must name the crew we were asked to build. A mislabeled
#      crew is exactly the failure this whole change exists to prevent, so it
#      fails here rather than at deploy time.
#   2. Build Dockerfile.crew FROM the base + COPY the bundle + OCI labels. It
#      compiles and installs nothing; if it needed a package the base would be
#      wrong and that is a REPORT, not a fix here.
#   3. Compute a tag that names EVERY input (crew, arch, base digest, bundle
#      digest) so it cannot collide across crew edits or base bumps, push ONLY
#      that one registry tag, and REUSE an identical existing tag rather than
#      failing on an immutable repository.
#   4. Emit machine-readable JSON (schema smc-crew-image/v1) the deploy driver
#      consumes. Fields documented in scripts/README.md.
#
# NO AWS CREDENTIALS are used by the author of this script. A real build+push
# needs the caller to be logged in to the target registry first (the deploy
# driver logs in before calling this; a manual run needs its own login). Use
# --dry-run to exercise validation, tag computation and the exact commands
# WITHOUT touching docker or the registry.
set -euo pipefail

# This script sits at crew/scripts/. The crew Dockerfile lives beside it under
# crew/runtime/ (the migration moved the container source there from the app
# root). The docker build CONTEXT for the crew layer is the BUNDLE directory, not
# runtime/, because Dockerfile.crew COPYs the bundle's four entries from it.
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CREW_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
RUNTIME_DIR="${CREW_ROOT}/runtime"
DOCKERFILE_CREW="${RUNTIME_DIR}/Dockerfile.crew"

# --------------------------------------------------------------------------- #
# Inputs (flags override env). --base --bundle --repo --crew are required.
# --------------------------------------------------------------------------- #
BASE_IN="${SMC_BASE:-}"                 # <repo>@sha256:<hex> -- the maintainer base
BUNDLE_DIR_IN="${SMC_BUNDLE_DIR:-}"     # T1 packaging.build output dir
REPO_IN="${SMC_CREW_IMAGE_REPO:-}"      # target RepositoryUri (the owner's ECR)
CREW_IN="${SMC_CREW_NAME:-}"            # crew name; must match the bundle manifest
ARCH_IN="${SMC_ARCH:-$(uname -m)}"      # default: host arch
OUT_JSON="${SMC_CREW_BUILD_OUT:-${RUNTIME_DIR}/build/crew-image-build.json}"
DRY_RUN=false

usage() {
  cat >&2 <<'USAGE'
usage: build_crew_image.sh --base <repo@sha256:...> --bundle <dir> \
           --repo <RepositoryUri> --crew <name> [--arch <X86_64|ARM64>] \
           [--out <path.json>] [--dry-run]

  --base    Maintainer base image, pinned BY DIGEST (repo@sha256:<hex>). Required.
  --bundle  Bundle directory from `python -m packaging.build` (T1). Required.
  --repo    Target repository URI (the owner's ECR repo). Required.
  --crew    Crew name; MUST equal the bundle manifest's crew_name. Required.
  --arch    X86_64|ARM64 (also accepts amd64|arm64|x86_64|aarch64). Default: host.
  --out     Where to write the machine-readable JSON. Default: runtime/build/crew-image-build.json
  --dry-run Validate + compute the tag + print the exact docker commands, but do
            NOT call docker or the registry. Use this without AWS credentials.
USAGE
}

die() { echo "build_crew_image.sh: FATAL: $*" >&2; exit 1; }

while [ $# -gt 0 ]; do
  case "$1" in
    --base)    BASE_IN="$2"; shift 2 ;;
    --bundle)  BUNDLE_DIR_IN="$2"; shift 2 ;;
    --repo)    REPO_IN="$2"; shift 2 ;;
    --crew)    CREW_IN="$2"; shift 2 ;;
    --arch)    ARCH_IN="$2"; shift 2 ;;
    --out)     OUT_JSON="$2"; shift 2 ;;
    --dry-run) DRY_RUN=true; shift ;;
    -h|--help) usage; exit 0 ;;
    *) echo "unknown argument: $1" >&2; usage; exit 2 ;;
  esac
done

# --------------------------------------------------------------------------- #
# Step 1: validate every input BEFORE any docker work.
# --------------------------------------------------------------------------- #
[ -n "${BASE_IN}" ]       || { usage; die "--base is required"; }
[ -n "${BUNDLE_DIR_IN}" ] || { usage; die "--bundle is required"; }
[ -n "${REPO_IN}" ]       || { usage; die "--repo is required"; }
[ -n "${CREW_IN}" ]       || { usage; die "--crew is required"; }

# --base must be digest-pinned. A mutable tag as base means the crew image's
# identity does not actually pin the serving code, which is the whole point.
case "${BASE_IN}" in
  *@sha256:*) : ;;
  *) die "--base must be digest-pinned as <repo>@sha256:<hex>, got '${BASE_IN}'" ;;
esac
BASE_DIGEST="sha256:${BASE_IN##*@sha256:}"          # the sha256:... portion
BASE_SHA_HEX="${BASE_DIGEST#sha256:}"
[ "${#BASE_SHA_HEX}" -ge 12 ] || die "--base digest looks malformed: '${BASE_DIGEST}'"

BUNDLE_DIR="$(cd "${BUNDLE_DIR_IN}" 2>/dev/null && pwd || true)"
[ -n "${BUNDLE_DIR}" ] && [ -d "${BUNDLE_DIR}" ] || die "--bundle dir not found: '${BUNDLE_DIR_IN}'"

# The four-entry layout is the contract. A missing entry fails here (and would
# fail again in the Dockerfile COPY); failing early gives a readable message.
for entry in manifest.json agent.json mcp.json skills; do
  [ -e "${BUNDLE_DIR}/${entry}" ] || die "bundle is missing required entry: ${entry}"
done
[ -d "${BUNDLE_DIR}/skills" ] || die "bundle 'skills' must be a directory"

PYBIN="${KIROCREW_SRC:-}/.venv/bin/python"
[ -x "${PYBIN}" ] || PYBIN="$(command -v python3)"

# Read crew_name, digest and bundle_version straight from the bundle's manifest --
# the digest is the one T1 computed over the bundle content; we do not invent our
# own. read_manifest <field> prints the value or aborts.
read_manifest() {
  "${PYBIN}" - "${BUNDLE_DIR}/manifest.json" "$1" <<'PY'
import json, sys
path, field = sys.argv[1], sys.argv[2]
try:
    with open(path) as fh:
        m = json.load(fh)
except Exception as e:
    sys.stderr.write(f"cannot read manifest {path}: {e}\n"); sys.exit(3)
if field not in m or m[field] in (None, ""):
    sys.stderr.write(f"manifest {path} missing required field '{field}'\n"); sys.exit(3)
sys.stdout.write(str(m[field]))
PY
}

MANIFEST_CREW="$(read_manifest crew_name)"   || die "could not read crew_name from manifest.json"
BUNDLE_DIGEST="$(read_manifest digest)"       || die "could not read digest from manifest.json"
BUNDLE_VERSION="$(read_manifest bundle_version 2>/dev/null || echo unknown)"

# The crew we were told to build MUST be the crew in the bundle. Otherwise the
# image label, the tag and the actual payload could name three different crews.
[ "${MANIFEST_CREW}" = "${CREW_IN}" ] \
  || die "crew mismatch: --crew '${CREW_IN}' but bundle manifest crew_name is '${MANIFEST_CREW}'"
CREW="${CREW_IN}"

# manifest digest shape check: sha256:<hex>
case "${BUNDLE_DIGEST}" in
  sha256:*) : ;;
  *) die "bundle manifest digest is not sha256-form: '${BUNDLE_DIGEST}'" ;;
esac

# ...and the digest must be TRUE, not merely well-shaped. The tag this script looks
# up is derived from it, so a bundle whose content changed while its manifest stayed
# put would hit the tag of the OLD image, reuse that image, and report it as the
# bundle just supplied -- a stale crew deployed under a fresh deploy's name.
#
# Recomputed with the producer's exact rules: sha256 over sorted
# [rel_posix, sha256(bytes)] rows for every file except the top-level manifest.json,
# then sha256 of the compact-JSON payload. This is the same check the container
# already runs at boot (supervisor/bundle.py `_content_digest`, which cites
# crew_export/bundle.py) -- it was simply absent here, on the side that decides
# whether to build at all. Both spellings are load-bearing: the sort is over path
# strings and the JSON separators are compact.
#
# curation-plan.json is excluded for the same reason manifest.json is: it is not in
# the digest the manifest RECORDS. `build_bundle` writes the carried plan into
# staging AFTER taking that digest, so counting it here refused every bundle that
# went through the documented plan/sign/build flow -- a deploy-blocking regression
# introduced by the first version of this check and caught in the same review round.
# `bundle_digest(..., also_skip={PLAN_FILENAME})` in packaging/build.py is the twin
# of this exclusion; the two must name the same file.
RECOMPUTED="$(python3 - "$BUNDLE_DIR" <<'PY' || true
import hashlib, json, sys
from pathlib import Path
root = Path(sys.argv[1])
rows = []
for p in sorted(root.rglob("*")):
    if not p.is_file():
        continue
    rel = p.relative_to(root).as_posix()
    if rel in ("manifest.json", "curation-plan.json"):
        continue
    rows.append([rel, hashlib.sha256(p.read_bytes()).hexdigest()])
payload = json.dumps(rows, ensure_ascii=False, separators=(",", ":"))
sys.stdout.write("sha256:" + hashlib.sha256(payload.encode("utf-8")).hexdigest())
PY
)"
[ -n "${RECOMPUTED}" ] || die "could not recompute the bundle digest from ${BUNDLE_DIR}"
[ "${RECOMPUTED}" = "${BUNDLE_DIGEST}" ] || die "bundle content does not match its manifest digest.
  manifest:    ${BUNDLE_DIGEST}
  recomputed:  ${RECOMPUTED}
The bundle changed after it was built, so the image tag derived from the manifest
would point at a DIFFERENT bundle's image. Re-run the build for this bundle."
BUNDLE_SHA_HEX="${BUNDLE_DIGEST#sha256:}"
[ "${#BUNDLE_SHA_HEX}" -ge 12 ] || die "bundle digest looks malformed: '${BUNDLE_DIGEST}'"

# --------------------------------------------------------------------------- #
# Normalise architecture (same four-form mapping as build_image.sh). The crew
# layer adds no arch-specific bytes, so the image arch is the base's arch; we
# pass --platform so buildx selects/validates the base's matching manifest and
# a base that cannot satisfy the requested arch fails the build.
# --------------------------------------------------------------------------- #
case "$(echo "${ARCH_IN}" | tr '[:upper:]' '[:lower:]')" in
  x86_64|amd64|x86-64) CFN_ARCH=X86_64; DOCKER_PLATFORM=linux/amd64; ARCH_L=amd64 ;;
  arm64|aarch64)       CFN_ARCH=ARM64;  DOCKER_PLATFORM=linux/arm64; ARCH_L=arm64 ;;
  *) die "unsupported --arch '${ARCH_IN}' (use X86_64|ARM64|amd64|arm64|x86_64|aarch64)" ;;
esac

# --------------------------------------------------------------------------- #
# Step 3: the tag. It names EVERY input to the crew image:
#   crew name + arch + base digest (12 hex) + bundle digest (12 hex).
# Any crew edit changes BUNDLE_SHA_HEX; any base bump changes BASE_SHA_HEX; a
# different arch or crew changes its own field -- so two distinct inputs can never
# resolve to the same tag, and the immutable repository never has to refuse a
# second, different push to one tag. An IDENTICAL input reuses the identical tag,
# which is the same content, so reuse is correct rather than a failure.
#
# The tag is only a handle. Everything downstream references the DIGEST
# (repo_digest / image_uri), so even in the impossible event of a 48-bit-per-input
# truncation collision, the deploy still pins the exact bytes by digest.
# --------------------------------------------------------------------------- #
CREW_TAG="$(printf '%s' "${CREW}" | tr -c 'A-Za-z0-9_.-' '-')"   # docker-tag-safe
PUSH_TAG="${REPO_IN}:crew-${CREW_TAG}-${ARCH_L}-b${BASE_SHA_HEX:0:12}-x${BUNDLE_SHA_HEX:0:12}"
BUILT_AT="$(date -u +%Y-%m-%dT%H:%M:%SZ)"

BUILDX_ARGS=(
  buildx build
  --platform "${DOCKER_PLATFORM}"
  --file "${DOCKERFILE_CREW}"
  --build-arg "BASE=${BASE_IN}"
  --build-arg "CREW_NAME=${CREW}"
  --build-arg "BUNDLE_DIGEST=${BUNDLE_DIGEST}"
  --build-arg "BUNDLE_VERSION=${BUNDLE_VERSION}"
  --build-arg "BUILT_AT=${BUILT_AT}"
  --tag "${PUSH_TAG}"
  --push
  "${BUNDLE_DIR}"
)
INSPECT_ARGS=( buildx imagetools inspect "${PUSH_TAG}" --format '{{json .Manifest.Digest}}' )

# Print the exact commands so a human (or the deploy driver log) can see/run them.
print_commands() {
  echo "# reuse check (does this exact tag already exist?):" >&2
  echo "docker ${INSPECT_ARGS[*]}" >&2
  echo "# build the crew layer and push ONLY this one registry tag:" >&2
  echo "docker ${BUILDX_ARGS[*]}" >&2
}

REPO_DIGEST="null"
IMAGE_URI="null"
PUSHED=false

if [ "${DRY_RUN}" = true ]; then
  echo "==> DRY RUN: no docker/registry calls made." >&2
  echo "==> Would build ${DOCKER_PLATFORM} crew image for crew='${CREW}'" >&2
  echo "==> base=${BASE_IN}" >&2
  echo "==> bundle_digest=${BUNDLE_DIGEST}" >&2
  echo "==> tag=${PUSH_TAG}" >&2
  print_commands
else
  command -v docker >/dev/null 2>&1 || die "docker not found on PATH"
  [ -f "${DOCKERFILE_CREW}" ] || die "Dockerfile.crew not found at ${DOCKERFILE_CREW}"

  # Reuse an identical existing tag rather than failing on an immutable repo.
  EXISTING="$(docker "${INSPECT_ARGS[@]}" 2>/dev/null | tr -d '"' || true)"
  if [ -n "${EXISTING}" ]; then
    echo "==> ${PUSH_TAG} already in the registry; reusing ${EXISTING}" >&2
    REPO_DIGEST="${EXISTING}"
  else
    echo "==> Building + pushing ${PUSH_TAG}" >&2
    docker "${BUILDX_ARGS[@]}" \
      || die "buildx build+push failed. A 'denied' here usually means docker is not authenticated to ${REPO_IN%%/*}; log in to the registry and retry."
    RD="$(docker "${INSPECT_ARGS[@]}" 2>/dev/null | tr -d '"' || true)"
    [ -n "${RD}" ] || die "could not read pushed manifest digest for ${PUSH_TAG}"
    REPO_DIGEST="${RD}"
  fi
  IMAGE_URI="${REPO_IN}@${REPO_DIGEST}"
  PUSHED=true
fi

# --------------------------------------------------------------------------- #
# Step 4: machine-readable output. Last two stdout lines are
# SMC_CREW_IMAGE_JSON=<path> then the JSON, so the driver never guesses the path.
# --------------------------------------------------------------------------- #
mkdir -p "$(dirname "${OUT_JSON}")"
emit() { [ "$1" = "null" ] && printf 'null' || printf '"%s"' "$1"; }
cat > "${OUT_JSON}" <<JSON
{
  "schema": "smc-crew-image/v1",
  "built_at": "${BUILT_AT}",
  "image_uri": $(emit "${IMAGE_URI}"),
  "repo_digest": $(emit "${REPO_DIGEST}"),
  "base_digest": "${BASE_DIGEST}",
  "base_ref": "${BASE_IN}",
  "architecture": "${CFN_ARCH}",
  "docker_platform": "${DOCKER_PLATFORM}",
  "crew_name": "${CREW}",
  "bundle_digest": "${BUNDLE_DIGEST}",
  "bundle_version": "${BUNDLE_VERSION}",
  "push_tag": "${PUSH_TAG}",
  "pushed": ${PUSHED},
  "dry_run": ${DRY_RUN}
}
JSON

echo "==> Wrote ${OUT_JSON}" >&2
echo "SMC_CREW_IMAGE_JSON=${OUT_JSON}"
cat "${OUT_JSON}"
