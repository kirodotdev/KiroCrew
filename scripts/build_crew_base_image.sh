#!/usr/bin/env bash
# Build the AWS Control crew BASE image from a checkout.
#
# src/kiro_crew/apps/builtins/aws_control/crew/runtime/Dockerfile names this
# script as the producer of the wheel it installs, and the script did not exist:
# the image that merged in #9223 could not be built from a clean checkout at all,
# because `runtime/vendor/` holds only a `.gitkeep` and nothing else in the tree
# builds a wheel into it. Nothing caught that, since the container lane runs the
# image's Python modules and never runs `docker build`.
#
# What this produces is the BASE image: the serving code, with NO crew content.
# One curated crew is added on top as a thin digest-pinned layer by
# scripts/build_crew_image.sh, which is a separate artifact with a separate
# recipe (Dockerfile.crew).
#
# It builds and verifies; it does not publish. Pushing to ECR needs credentials
# and so cannot be verified in the same place the build can, which is why it is
# not folded in here.
#
# Usage:
#   scripts/build_crew_base_image.sh [--tag TAG]
#
#   --tag  image tag to build (default: kirocrew-crew-base:dev)
set -euo pipefail

readonly SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
readonly REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
readonly RUNTIME_DIR="${REPO_ROOT}/src/kiro_crew/apps/builtins/aws_control/crew/runtime"
readonly VENDOR_DIR="${RUNTIME_DIR}/vendor"

TAG="kirocrew-crew-base:dev"
WHEEL_OUT=""

die() { echo "build_crew_base_image.sh: $*" >&2; exit 1; }
step() { echo; echo "==> $*"; }
show_help() {
  sed -n '2,/^[^#]/{ /^[^#]/q; s/^# \{0,1\}//; p; }' "${BASH_SOURCE[0]}"
}

while [ $# -gt 0 ]; do
  case "$1" in
    --tag)     [ $# -ge 2 ] || die "--tag needs a value"; TAG="$2"; shift 2 ;;
    -h|--help) show_help; exit 0 ;;
    *)         die "unknown argument: $1 (try --help)" ;;
  esac
done

[ -f "${RUNTIME_DIR}/Dockerfile" ] || die "no Dockerfile at ${RUNTIME_DIR}"
command -v docker >/dev/null 2>&1 || die "docker is not on PATH"

# ── The wheel ────────────────────────────────────────────────────────────────
# Staged into runtime/vendor/ and consumed by the Dockerfile through a BuildKit
# context bind mount, never a COPY: a copied wheel is committed to its own layer
# and a later `rm` can only stack a whiteout on top, so every pull would carry
# ~48MB of dead weight (#5778, ratcheted by test_docker_wheel_layer_contract.py).
#
# The directory is EMPTIED first. The Dockerfile's install step refuses a context
# holding more than one wheel, which is what keeps its glob version-agnostic, so
# a stale wheel from an earlier run would otherwise fail the build with a
# confusing message instead of being replaced.
cleanup() {
  rm -f "${VENDOR_DIR}"/*.whl
  if [ -n "${WHEEL_OUT}" ]; then
    rm -rf -- "${WHEEL_OUT}"
  fi
}
trap cleanup EXIT

mkdir -p "${VENDOR_DIR}"
rm -f "${VENDOR_DIR}"/*.whl

step "Building the Kiro Crew wheel"
# The dashboard SPA is deliberately NOT built first. This image is headless --
# the front process forwards a turn to the backend's API over loopback and
# never serves the interface -- so `make wheel`'s frontend step would add an
# npm toolchain requirement and megabytes of assets nothing in the container
# reads. setup.py's BuildWithFrontend warns and continues when the dist tree is
# absent, which is the behaviour this relies on; a tree that already has one
# ships it, harmlessly.
python3 -c 'import build' 2>/dev/null \
  || die "the 'build' package is missing: pip install build"
WHEEL_OUT="$(mktemp -d)"
( cd "${REPO_ROOT}" && python3 -m build --wheel --outdir "${WHEEL_OUT}" >/dev/null )
built="$(ls "${WHEEL_OUT}"/*.whl 2>/dev/null | head -1 || true)"
[ -n "${built}" ] || die "the wheel build produced no .whl in ${WHEEL_OUT}"
cp "${built}" "${VENDOR_DIR}/"

staged="$(basename "$(ls "${VENDOR_DIR}"/*.whl | head -1)")"
echo "    wheel: ${staged}"

# ── The image ────────────────────────────────────────────────────────────────
# Build context is runtime/, which is what the Dockerfile's relative operands
# (container/, vendor/) are resolved against.
step "Building ${TAG}"
DOCKER_BUILDKIT=1 docker build \
  -f "${RUNTIME_DIR}/Dockerfile" \
  -t "${TAG}" \
  "${RUNTIME_DIR}"

# ── Architecture cross-check ─────────────────────────────────────────────────
# The Dockerfile takes the architecture from the builder and pins it nowhere, on
# purpose: an earlier revision hardcoded arm64 and cost two deploy failures, one
# of them an image that refused to exec on an x86 task. So the config's declared
# architecture and the architecture of the binaries actually inside it are two
# independent facts, and this compares them. A mismatch here means the image
# lies about itself, which is only discoverable later as a task that will not
# start.
step "Verifying architecture"
config_arch="$(docker image inspect "${TAG}" --format '{{.Architecture}}')"
echo "    config architecture: ${config_arch}"
case "${config_arch}" in
  amd64) expect_uname="x86_64" ;;
  arm64) expect_uname="aarch64" ;;
  *) die "unsupported image architecture ${config_arch} (expected amd64 or arm64)" ;;
esac
if ! in_image_arch="$(docker run --rm "${TAG}" uname -m)"; then
  die "cannot run the host-architecture image to verify its binaries"
fi
echo "    in-image architecture: ${in_image_arch}"
[ "${in_image_arch}" = "${expect_uname}" ] || die \
  "architecture mismatch: config says ${config_arch} (expected uname ${expect_uname}) but the image reports ${in_image_arch}. The image lies about its own architecture and will not start on a task of either arch."
# Both kiro-cli binaries, asserted here as well as in the Dockerfile: `kiro-cli
# acp` is a launcher that dispatches to a sibling `kiro-cli-chat`, so shipping
# only the launcher builds fine and fails on the first turn.
docker run --rm --entrypoint /usr/local/bin/kiro-cli "${TAG}" --version >/dev/null \
  || die "kiro-cli is present but not runnable in the image"
echo "    kiro-cli: runnable"

step "Built ${TAG}"
docker image inspect "${TAG}" --format '    id:   {{.Id}}
    size: {{.Size}} bytes
    arch: {{.Architecture}}'
echo
echo "This is the BASE image. Add one crew on top with:"
echo "  scripts/build_crew_image.sh --base <repo@sha256:...> --bundle <bundle-dir>"
