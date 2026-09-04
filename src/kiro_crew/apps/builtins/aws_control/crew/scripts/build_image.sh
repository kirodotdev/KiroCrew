#!/usr/bin/env bash
# Build the Share My Crew deployment BASE image.
#
# NOTE (crew-in-image change): the OWNER no longer runs this script. It is now
# the MAINTAINER's BASE-image builder: it produces the base image (serving code +
# kiro-cli, no crew) that scripts/build_crew_image.sh then layers a crew bundle
# onto. Its interface is unchanged. When a public base image is published, that
# base's digest is passed to build_crew_image.sh --base and this script is not
# run at all. See PACKAGING-CONTRACT.md ("T2 -- the crew image layer").
#
# Owned by the crew/scripts track. This is the ONLY way the Kiro Crew wheel
# appears in runtime/vendor/ (it is gitignored, a ~57MB build artifact), and it
# is the only sanctioned way the base image is built.
#
# What it does, in order:
#   1. Build the Kiro Crew wheel from a source checkout into runtime/vendor/,
#      recording the exact commit it came from (baked into the image as OCI
#      labels, so the running bytes trace back to a commit).
#   2. Build the image for a requested architecture and reference it by DIGEST,
#      never by a mutable tag -- a stale tag reads as a working feature.
#   3. Cross-check the built image's architecture against what was asked for, as
#      a HARD failure. Architecture cost two separate deploy failures; the image
#      half asserts here so the deploy driver can compare too.
#   4. Assert BOTH kiro-cli binaries are present and executable in the FINAL
#      image -- `kiro-cli acp` is a launcher that dispatches to a sibling
#      `kiro-cli-chat`, and a later layer could remove one.
#   5. Emit machine-readable JSON (digest, architecture, wheel source commit) the
#      deploy driver consumes. Format is documented in scripts/README.md.
#
# It does NOT push by default and does NOT touch AWS. Pass --repo <registry/repo>
# to also push and capture the registry (repo) digest that becomes ImageUri.
set -euo pipefail

# --------------------------------------------------------------------------- #
# Locations. This script sits at crew/scripts/; the container image source
# (Dockerfile, vendor/, container/) lives beside it under crew/runtime/, which
# is the docker BUILD CONTEXT. The standalone tree kept all of these at the app
# root next to scripts/; the migration moved them one directory over into
# runtime/, so the context is runtime/ rather than the script's parent.
# --------------------------------------------------------------------------- #
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CREW_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
RUNTIME_DIR="${CREW_ROOT}/runtime"                 # the build context (Dockerfile + vendor/ + container/)
DOCKERFILE="${RUNTIME_DIR}/Dockerfile"
VENDOR_DIR="${RUNTIME_DIR}/vendor"

# --------------------------------------------------------------------------- #
# Inputs (flags override env override defaults)
# --------------------------------------------------------------------------- #
ARCH_IN="${SMC_ARCH:-$(uname -m)}"                 # default: host arch
# The Kiro Crew source checkout the wheel is built from. There is no defensible
# fixed default -- it is one maintainer's checkout path -- so it must be named
# explicitly with --kirocrew-src or $KIROCREW_SRC. The driver never runs this
# script without the maintainer choosing that path, and hard-coding a developer's
# home directory here both breaks on every other machine and trips the repo's
# scrub gate.
KIROCREW_SRC="${KIROCREW_SRC:-}"
KIRO_VERSION_IN="${SMC_KIRO_VERSION:-}"            # empty -> take the Dockerfile default
IMAGE_REPO="${SMC_IMAGE_REPO:-}"                   # set -> push + capture repo digest
LOCAL_TAG="${SMC_IMAGE_TAG:-smc-image:build}"      # ephemeral load handle only; digest is authoritative
OUT_JSON="${SMC_BUILD_OUT:-${RUNTIME_DIR}/build/image-build.json}"

usage() {
  cat >&2 <<'USAGE'
usage: build_image.sh [--arch X86_64|ARM64|amd64|arm64|x86_64|aarch64]
                      [--kiro-version <v>] --kirocrew-src <path>
                      [--repo <registry/repo>] [--tag <name:tag>]
                      [--out <path.json>]
--kirocrew-src (or $KIROCREW_SRC) is required: the Kiro Crew checkout the wheel is
built from. Other flags are optional; see scripts/README.md. Defaults: arch=host,
kiro-version=Dockerfile default.
USAGE
}

while [ $# -gt 0 ]; do
  case "$1" in
    --arch) ARCH_IN="$2"; shift 2 ;;
    --kiro-version) KIRO_VERSION_IN="$2"; shift 2 ;;
    --kirocrew-src) KIROCREW_SRC="$2"; shift 2 ;;
    --repo) IMAGE_REPO="$2"; shift 2 ;;
    --tag) LOCAL_TAG="$2"; shift 2 ;;
    --out) OUT_JSON="$2"; shift 2 ;;
    -h|--help) usage; exit 0 ;;
    *) echo "unknown argument: $1" >&2; usage; exit 2 ;;
  esac
done

die() { echo "build_image.sh: FATAL: $*" >&2; exit 1; }

[ -n "${KIROCREW_SRC}" ] || { usage; die "--kirocrew-src (or \$KIROCREW_SRC) is required: the Kiro Crew checkout the wheel is built from"; }

# --------------------------------------------------------------------------- #
# Normalise architecture into the four forms the pipeline needs.
#   CFN_ARCH        -> CpuArchitecture parameter value (X86_64 | ARM64)
#   DOCKER_PLATFORM -> buildx --platform (linux/amd64 | linux/arm64); this alone
#                      populates the Dockerfile's predefined TARGETARCH arg.
#   MACHINE_EXPECT  -> platform.machine() as seen INSIDE the image (x86_64|aarch64)
# --------------------------------------------------------------------------- #
case "$(echo "${ARCH_IN}" | tr '[:upper:]' '[:lower:]')" in
  x86_64|amd64|x86-64) CFN_ARCH=X86_64; DOCKER_PLATFORM=linux/amd64; MACHINE_EXPECT=x86_64 ;;
  arm64|aarch64)       CFN_ARCH=ARM64;  DOCKER_PLATFORM=linux/arm64; MACHINE_EXPECT=aarch64 ;;
  *) die "unsupported --arch '${ARCH_IN}' (use X86_64|ARM64|amd64|arm64|x86_64|aarch64)" ;;
esac
HOST_MACHINE="$(uname -m)"

# --------------------------------------------------------------------------- #
# Kiro CLI version: take the Dockerfile default unless overridden, so this
# script never silently ships a different pinned version than the image declares.
# --------------------------------------------------------------------------- #
if [ -n "${KIRO_VERSION_IN}" ]; then
  KIRO_VERSION="${KIRO_VERSION_IN}"
else
  KIRO_VERSION="$(sed -n 's/^ARG KIRO_VERSION=\([^ ]*\).*/\1/p' "${DOCKERFILE}" | head -1)"
  [ -n "${KIRO_VERSION}" ] || die "could not read ARG KIRO_VERSION default from ${DOCKERFILE}"
fi

command -v docker >/dev/null 2>&1 || die "docker not found on PATH"
[ -f "${DOCKERFILE}" ] || die "Dockerfile not found at ${DOCKERFILE}"
[ -d "${KIROCREW_SRC}/.git" ] || die "Kiro Crew source checkout not a git repo: ${KIROCREW_SRC}"

# --------------------------------------------------------------------------- #
# Step 1: build the wheel, record the source commit.
# --------------------------------------------------------------------------- #
WHEEL_COMMIT="$(git -C "${KIROCREW_SRC}" rev-parse HEAD)"
if git -C "${KIROCREW_SRC}" diff --quiet && git -C "${KIROCREW_SRC}" diff --cached --quiet; then
  WHEEL_DIRTY=false
else
  WHEEL_DIRTY=true
  echo "build_image.sh: WARNING: ${KIROCREW_SRC} has uncommitted changes; wheel will not match ${WHEEL_COMMIT} exactly" >&2
fi

echo "==> Building Kiro Crew wheel from ${KIROCREW_SRC} @ ${WHEEL_COMMIT:0:12} (dirty=${WHEEL_DIRTY})"
mkdir -p "${VENDOR_DIR}"
rm -f "${VENDOR_DIR}"/*.whl          # single source of truth: exactly one wheel

# Prefer the Kiro Crew checkout's venv python (it is the one the wheel is built
# against); fall back to python3. --no-deps: vendor/ holds ONLY the kirocrew
# wheel; the Dockerfile resolves kirocrew's own dependencies at image build time.
PYBIN="${KIROCREW_SRC}/.venv/bin/python"
[ -x "${PYBIN}" ] || PYBIN="$(command -v python3)"
"${PYBIN}" -m pip wheel --no-deps --wheel-dir "${VENDOR_DIR}" "${KIROCREW_SRC}" \
  || die "wheel build failed"

shopt -s nullglob
WHEELS=( "${VENDOR_DIR}"/*.whl )
shopt -u nullglob
[ "${#WHEELS[@]}" -eq 1 ] || die "expected exactly one wheel in ${VENDOR_DIR}, found ${#WHEELS[@]}"
WHEEL_PATH="${WHEELS[0]}"
WHEEL_NAME="$(basename "${WHEEL_PATH}")"
WHEEL_SHA256="$(sha256sum "${WHEEL_PATH}" | cut -d' ' -f1)"
WHEEL_BYTES="$(stat -c%s "${WHEEL_PATH}" 2>/dev/null || wc -c < "${WHEEL_PATH}")"
echo "==> Wheel: ${WHEEL_NAME} (${WHEEL_BYTES} bytes, sha256 ${WHEEL_SHA256:0:12})"

# --------------------------------------------------------------------------- #
# Step 2: build the image. Reference it by DIGEST, never by the mutable tag.
# The commit is baked in as OCI labels so `docker inspect` traces the bytes back.
# --------------------------------------------------------------------------- #
BUILT_AT="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
echo "==> Building image for ${DOCKER_PLATFORM} (CpuArchitecture=${CFN_ARCH}, kiro-cli ${KIRO_VERSION})"

BUILDX_COMMON=(
  buildx build
  --platform "${DOCKER_PLATFORM}"
  --file "${DOCKERFILE}"
  --build-arg "KIRO_VERSION=${KIRO_VERSION}"
  --label "org.opencontainers.image.revision=${WHEEL_COMMIT}"
  --label "org.opencontainers.image.source=${KIROCREW_SRC}"
  --label "org.opencontainers.image.created=${BUILT_AT}"
  --label "dev.sharemycrew.wheel-commit=${WHEEL_COMMIT}"
  --label "dev.sharemycrew.wheel-name=${WHEEL_NAME}"
  --label "dev.sharemycrew.wheel-sha256=${WHEEL_SHA256}"
  --label "dev.sharemycrew.kiro-version=${KIRO_VERSION}"
)
# NOTE: LOCAL_TAG is deliberately NOT in BUILDX_COMMON. `buildx --push` pushes
# EVERY tag on the build, and LOCAL_TAG has no registry component, so docker
# resolves it to docker.io/library/<name> and the push is denied by Docker Hub.
# That reads as an ECR permission problem and is not one. The push build carries
# the registry tag only; the local load build carries the local tag only.

REPO_DIGEST="null"
IMAGE_URI="null"
if [ -n "${IMAGE_REPO}" ]; then
  # The tag must name BOTH inputs to the image: the Kiro Crew wheel's commit and
  # this repository's own commit. A tag derived from the wheel commit alone is the
  # same string after any change to the Dockerfile, container/ or requirements, so
  # two different images compete for it -- and against a repository with immutable
  # tags the second push is refused outright, making the first build from a given
  # Kiro Crew commit the only one that can ever be pushed.
  APP_REV="$(git -C "${CREW_ROOT}" rev-parse --short=12 HEAD 2>/dev/null || echo nogit)"
  if [ -n "$(git -C "${CREW_ROOT}" status --porcelain 2>/dev/null)" ]; then
    # A dirty tree has no commit that identifies it, so no stable tag exists for it.
    APP_REV="${APP_REV}-dirty$(date -u +%Y%m%dT%H%M%SZ)"
  fi
  # The tag must name EVERY input that changes the image, or reuse serves the wrong
  # one. Architecture is such an input and was missing: built on amd64 and pushed,
  # a later arm64 build from the same source resolved this same tag, found the
  # amd64 digest, and reused it -- and because downstream pins the DIGEST rather
  # than the tag, the deployment then ran an amd64 image on an arm64 task. The
  # earlier comment here claimed "the tag pins both inputs", which was true of the
  # two it named and false of the set that matters.
  PUSH_TAG="${IMAGE_REPO}:smc-${CFN_ARCH}-${WHEEL_COMMIT:0:12}-${APP_REV}"

  # Tags are immutable here, so an existing tag is reused rather than treated as a
  # failure. From a clean tree the tag now pins architecture, wheel commit and app
  # revision, so whatever is already there was built from exactly this source FOR
  # THIS ARCHITECTURE, and downstream references the digest rather than the tag,
  # which makes reuse the same deployment.
  EXISTING="$(docker buildx imagetools inspect "${PUSH_TAG}" \
    --format '{{json .Manifest.Digest}}' 2>/dev/null | tr -d '"' || true)"
  if [ -n "${EXISTING}" ]; then
    echo "==> ${PUSH_TAG} is already in the registry; reusing ${EXISTING}"
    REPO_DIGEST="${EXISTING}"
    IMAGE_URI="${IMAGE_REPO}@${EXISTING}"
    docker "${BUILDX_COMMON[@]}" --tag "${LOCAL_TAG}" --load "${RUNTIME_DIR}" \
      || die "buildx local load failed"
  else
    echo "==> Pushing to ${PUSH_TAG}"
    docker "${BUILDX_COMMON[@]}" --tag "${PUSH_TAG}" --push "${RUNTIME_DIR}" \
      || die "buildx build+push failed. A 'denied' here means docker is not authenticated to ${IMAGE_REPO%%/*}; the deploy driver logs in before calling this script, so a manual run needs its own login."
    # Also load locally so the arch/binary asserts run against the same bytes.
    docker "${BUILDX_COMMON[@]}" --tag "${LOCAL_TAG}" --load "${RUNTIME_DIR}" \
      || die "buildx local load failed"
    RD="$(docker buildx imagetools inspect "${PUSH_TAG}" --format '{{json .Manifest.Digest}}' 2>/dev/null | tr -d '"' || true)"
    [ -n "${RD}" ] || die "could not read pushed manifest digest for ${PUSH_TAG}"
    REPO_DIGEST="${RD}"
    IMAGE_URI="${IMAGE_REPO}@${RD}"
  fi

  # Publish the MOVING 'smc-base' tag at whichever digest this run settled on, reused
  # or freshly pushed. The deploy driver resolves --base from this tag when none is
  # given, and nothing produced it, so every deploy failed at the crew-image step with
  # "the ECR repo has no 'smc-base' tag" -- a resolver naming a tag no builder wrote.
  #
  # Deliberately a separate retag rather than a second --tag on the build: buildx
  # --push publishes EVERY tag it is given, so pairing an immutable content tag with a
  # moving pointer means a rebuild of identical content fails on the immutable one and
  # never republishes the pointer. imagetools create retags an existing digest without
  # rebuilding, and it is the operation an immutable repository still permits on a tag
  # that is meant to move.
  #
  # A failure here is a WARNING, not fatal: the image is already pushed and its digest
  # is the authoritative reference, so the run's real output is intact and the deploy
  # can still be given --base explicitly.
  echo "==> Moving the 'smc-base-${CFN_ARCH}' tag to ${REPO_DIGEST}"
  docker buildx imagetools create --tag "${IMAGE_REPO}:smc-base-${CFN_ARCH}" "${IMAGE_REPO}@${REPO_DIGEST}" \
    || echo "WARNING: could not move the 'smc-base-${CFN_ARCH}' tag. The image is pushed; pass --base ${IMAGE_REPO}@${REPO_DIGEST} to the deploy driver." >&2
else
  docker "${BUILDX_COMMON[@]}" --tag "${LOCAL_TAG}" --load "${RUNTIME_DIR}" || die "buildx build failed"
fi

# Local content digest (image ID). Downstream steps reference THIS, not the tag.
IMAGE_ID="$(docker image inspect "${LOCAL_TAG}" --format '{{.Id}}')"
[ -n "${IMAGE_ID}" ] || die "could not resolve built image id"
REF="${IMAGE_ID}"                      # digest-addressed handle for all asserts

# --------------------------------------------------------------------------- #
# Step 3: architecture cross-check -- HARD failure. Two independent signals:
#   (a) the image config's declared arch -- always available, even cross-arch;
#       this is what ECS RuntimePlatform must match.
#   (b) platform.machine() from INSIDE the image -- the check that finally pinned
#       the earlier failure; only runnable when the target arch runs on this host
#       (else it needs qemu/binfmt), so it is asserted when runnable and reported
#       as skipped otherwise. (a) is enough to fail the build on a mismatch.
# --------------------------------------------------------------------------- #
IMG_CFG_ARCH="$(docker image inspect "${REF}" --format '{{.Architecture}}')"   # amd64|arm64
case "${IMG_CFG_ARCH}" in
  amd64) IMG_MACHINE_FROM_CFG=x86_64 ;;
  arm64) IMG_MACHINE_FROM_CFG=aarch64 ;;
  *) IMG_MACHINE_FROM_CFG="${IMG_CFG_ARCH}" ;;
esac
[ "${IMG_MACHINE_FROM_CFG}" = "${MACHINE_EXPECT}" ] \
  || die "ARCH MISMATCH: requested ${CFN_ARCH} (${MACHINE_EXPECT}) but image config is ${IMG_CFG_ARCH}"

RUNNABLE_HERE=false
case "${HOST_MACHINE}:${MACHINE_EXPECT}" in
  x86_64:x86_64|aarch64:aarch64|arm64:aarch64|amd64:x86_64) RUNNABLE_HERE=true ;;
esac

MACHINE_IN_IMAGE="skipped-cross-arch"
if [ "${RUNNABLE_HERE}" = true ]; then
  MACHINE_IN_IMAGE="$(docker run --rm --entrypoint /usr/local/bin/python "${REF}" \
                        -c 'import platform; print(platform.machine())' 2>/dev/null || true)"
  [ "${MACHINE_IN_IMAGE}" = "${MACHINE_EXPECT}" ] \
    || die "ARCH MISMATCH (in-image): expected ${MACHINE_EXPECT}, image reports '${MACHINE_IN_IMAGE}'"
  echo "==> Architecture verified: config=${IMG_CFG_ARCH} in-image machine=${MACHINE_IN_IMAGE}"
else
  echo "==> Architecture: config=${IMG_CFG_ARCH} (matches ${CFN_ARCH}); in-image machine check SKIPPED (host ${HOST_MACHINE} cannot exec ${MACHINE_EXPECT} without qemu/binfmt)" >&2
fi

# --------------------------------------------------------------------------- #
# Step 4: assert BOTH kiro-cli binaries in the FINAL image (a later layer could
# have removed one). Only runnable when the arch runs on this host.
# --------------------------------------------------------------------------- #
KIRO_CLI_OK=false
KIRO_CLI_CHAT_OK=false
if [ "${RUNNABLE_HERE}" = true ]; then
  if docker run --rm --entrypoint sh "${REF}" -c 'test -x /usr/local/bin/kiro-cli'; then
    KIRO_CLI_OK=true; else die "kiro-cli missing or not executable in the final image"; fi
  if docker run --rm --entrypoint sh "${REF}" -c 'test -x /usr/local/bin/kiro-cli-chat && command -v kiro-cli-chat >/dev/null'; then
    KIRO_CLI_CHAT_OK=true; else die "kiro-cli-chat (the sibling acp dispatches to) missing or not executable in the final image"; fi
  echo "==> Binaries verified: kiro-cli and kiro-cli-chat both present and executable"
else
  echo "==> Binary presence check SKIPPED (cross-arch image cannot be run here); relying on the Dockerfile's build-time assert" >&2
fi

# --------------------------------------------------------------------------- #
# Step 5: machine-readable output for the deploy driver. Schema in scripts/README.md.
# --------------------------------------------------------------------------- #
mkdir -p "$(dirname "${OUT_JSON}")"
cat > "${OUT_JSON}" <<JSON
{
  "schema": "smc-image-build/v1",
  "built_at": "${BUILT_AT}",
  "image_uri": $( [ "${IMAGE_URI}" = "null" ] && echo null || echo "\"${IMAGE_URI}\"" ),
  "repo_digest": $( [ "${REPO_DIGEST}" = "null" ] && echo null || echo "\"${REPO_DIGEST}\"" ),
  "image_id": "${IMAGE_ID}",
  "architecture": "${CFN_ARCH}",
  "docker_platform": "${DOCKER_PLATFORM}",
  "image_config_arch": "${IMG_CFG_ARCH}",
  "platform_machine": "${MACHINE_IN_IMAGE}",
  "kiro_version": "${KIRO_VERSION}",
  "kiro_cli_present": ${KIRO_CLI_OK},
  "kiro_cli_chat_present": ${KIRO_CLI_CHAT_OK},
  "wheel_name": "${WHEEL_NAME}",
  "wheel_sha256": "${WHEEL_SHA256}",
  "wheel_source_repo": "${KIROCREW_SRC}",
  "wheel_source_commit": "${WHEEL_COMMIT}",
  "wheel_source_dirty": ${WHEEL_DIRTY}
}
JSON

echo "==> Wrote ${OUT_JSON}"
echo "SMC_IMAGE_BUILD_JSON=${OUT_JSON}"
cat "${OUT_JSON}"
