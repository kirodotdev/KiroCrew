#!/usr/bin/env bash
# Build one crew image: the base image plus exactly one curated crew bundle.
#
# Dockerfile.crew names this script as the caller that always supplies its
# digest-pinned BASE, and the script did not exist. Same gap as
# scripts/build_crew_base_image.sh, one layer up.
#
# The crew layer compiles nothing and installs nothing. It adds a read-only
# payload and provenance labels, so that ONE artifact pins both the serving code
# (the base, by digest) and the crew content (the bundle, by digest). A bundle
# delivered separately is a thing the container can fail to read while every gate
# reports green.
#
# Usage:
#   scripts/build_crew_image.sh --base REPO@sha256:... --bundle DIR [--tag TAG]
#
#   --base    the base image, PINNED BY DIGEST. A tag is refused: see below.
#   --bundle  the bundle directory produced by the crew packaging build. It is
#             also the docker build context, which is what the Dockerfile's
#             relative COPY operands resolve against.
#   --tag     override the derived tag. The default embeds the bundle digest so
#             an image and its crew content cannot silently disagree.
set -euo pipefail

readonly SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
readonly REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
readonly RUNTIME_DIR="${REPO_ROOT}/src/kiro_crew/apps/builtins/aws_control/crew/runtime"

BASE=""
BUNDLE=""
TAG=""
BUNDLE_SNAPSHOT=""

die() { echo "build_crew_image.sh: $*" >&2; exit 1; }
step() { echo; echo "==> $*"; }
cleanup() {
  if [ -n "${BUNDLE_SNAPSHOT}" ]; then
    rm -rf -- "${BUNDLE_SNAPSHOT}"
  fi
}
trap cleanup EXIT
show_help() {
  sed -n '2,/^[^#]/{ /^[^#]/q; s/^# \{0,1\}//; p; }' "${BASH_SOURCE[0]}"
}

while [ $# -gt 0 ]; do
  case "$1" in
    --base)   [ $# -ge 2 ] || die "--base needs a value";   BASE="$2";   shift 2 ;;
    --bundle) [ $# -ge 2 ] || die "--bundle needs a path";  BUNDLE="$2"; shift 2 ;;
    --tag)    [ $# -ge 2 ] || die "--tag needs a value";    TAG="$2";    shift 2 ;;
    -h|--help) show_help; exit 0 ;;
    *) die "unknown argument: $1 (try --help)" ;;
  esac
done

[ -n "${BASE}" ]   || die "--base is required (repo@sha256:...); there is no sane default"
[ -n "${BUNDLE}" ] || die "--bundle is required"
[ -f "${RUNTIME_DIR}/Dockerfile.crew" ] || die "no Dockerfile.crew at ${RUNTIME_DIR}"
command -v docker >/dev/null 2>&1 || die "docker is not on PATH"

# A digest, never a tag. The whole point of this layer is that one artifact pins
# both halves of what runs, and a tag is a mutable pointer: `base:latest` rebuilt
# tomorrow silently changes the serving code under a crew image whose own digest
# did not move, so `docker inspect` would trace back to a base that no longer
# exists. Refused rather than repaired, because there is no correct guess.
case "${BASE}" in
  *@sha256:*) ;;
  *) die "--base must be pinned by digest (repo@sha256:...), got '${BASE}'. A tag is mutable, so it cannot pin what this image serves." ;;
esac

BUNDLE="$(cd "${BUNDLE}" 2>/dev/null && pwd)" || die "no bundle directory at ${BUNDLE}"

# ── The bundle layout contract ───────────────────────────────────────────────
# The Dockerfile's four explicit COPY sources ARE the layout contract, and a
# missing one already fails the build. Checking the source first preserves the
# clearer refusal that names the missing bundle member. `skills/` may be empty
# and `mcp.json` may be `{}`, but both must exist.
step "Checking the bundle source at ${BUNDLE}"
for required in manifest.json agent.json mcp.json; do
  [ -f "${BUNDLE}/${required}" ] || die "bundle is missing ${required} (required by Dockerfile.crew)"
done
[ -d "${BUNDLE}/skills" ] || die "bundle is missing the skills/ directory (may be empty, must exist)"

# This selective snapshot defines the checked set: exactly the four members
# Dockerfile.crew copies into /app/crew-bundle. It is therefore byte-identical
# to the in-image bundle, so the in-image digest function is correct on this
# tree by construction. Every later check and Docker COPY reads this snapshot.
BUNDLE_SNAPSHOT="$(mktemp -d)"
cp -a -- \
  "${BUNDLE}/manifest.json" \
  "${BUNDLE}/agent.json" \
  "${BUNDLE}/mcp.json" \
  "${BUNDLE}/skills" \
  "${BUNDLE_SNAPSHOT}/" \
  || die "could not snapshot the bundle at ${BUNDLE}"

# python3 rather than jq: the repo already requires python3 everywhere and jq is
# not a declared dependency of anything here.
read_manifest() {
  python3 - "$1" "$2" <<'PY'
import json, sys
with open(sys.argv[1], encoding="utf-8") as fh:
    doc = json.load(fh)
value = doc.get(sys.argv[2])
if value is None or str(value).strip() == "":
    sys.exit(1)
print(str(value).strip())
PY
}

content_digest() {
  PYTHONDONTWRITEBYTECODE=1 PYTHONPATH="${RUNTIME_DIR}" python3 - "$1" <<'PY'
import sys
from pathlib import Path
from container.supervisor.bundle import _content_digest

print(_content_digest(Path(sys.argv[1])))
PY
}

valid_repository_component() {
  python3 - "$1" <<'PY'
import re
import sys

repository_component = r"[a-z0-9]+(?:(?:[._]|__|[-]+)[a-z0-9]+)*"
sys.exit(0 if re.fullmatch(repository_component, sys.argv[1]) else 1)
PY
}

CREW_NAME="$(read_manifest "${BUNDLE_SNAPSHOT}/manifest.json" crew_name)" \
  || die "manifest.json has no usable crew_name"
BUNDLE_DIGEST="$(read_manifest "${BUNDLE_SNAPSHOT}/manifest.json" digest)" \
  || die "manifest.json has no usable digest"
BUNDLE_VERSION="$(read_manifest "${BUNDLE_SNAPSHOT}/manifest.json" bundle_version || true)"
[ -n "${BUNDLE_VERSION}" ] || BUNDLE_VERSION="unknown"
RECOMPUTED_DIGEST="$(content_digest "${BUNDLE_SNAPSHOT}")" \
  || die "could not recompute the bundle content digest"
[ "${RECOMPUTED_DIGEST}" = "${BUNDLE_DIGEST}" ] || die \
  "bundle content digest mismatch: manifest has '${BUNDLE_DIGEST}', but the bundle content recomputes to '${RECOMPUTED_DIGEST}'. Rebuild the bundle before building its image."

echo "    crew:   ${CREW_NAME}"
echo "    digest: ${BUNDLE_DIGEST}"
echo "    version:${BUNDLE_VERSION}"

# The bundle digest goes in the tag so the image and its crew content cannot
# silently disagree -- the property Dockerfile.crew's LABEL block exists for,
# made visible without a `docker inspect`.
#
# Strip the algorithm prefix by cutting at the LAST colon rather than filtering
# for hex characters: "sha256:" is itself almost all hex, so a character filter
# leaves "a256" glued to the front of the digest and produces a tag that looks
# plausible and matches nothing.
if [ -z "${TAG}" ]; then
  digest_hex="${BUNDLE_DIGEST##*:}"
  case "${digest_hex}" in
    ""|*[!0-9a-fA-F]*) die "bundle digest '${BUNDLE_DIGEST}' is not <alg>:<hex>; pass --tag" ;;
  esac
  derived_repository="kirocrew-crew-${CREW_NAME}"
  valid_repository_component "${derived_repository}" || die \
    "crew name '${CREW_NAME}' cannot form a valid default Docker repository '${derived_repository}'; pass --tag explicitly"
  TAG="${derived_repository}:$(printf '%s' "${digest_hex}" | cut -c1-12)"
fi

step "Building ${TAG}"
echo "    base: ${BASE}"
DOCKER_BUILDKIT=1 docker build \
  -f "${RUNTIME_DIR}/Dockerfile.crew" \
  -t "${TAG}" \
  --build-arg "BASE=${BASE}" \
  --build-arg "CREW_NAME=${CREW_NAME}" \
  --build-arg "BUNDLE_DIGEST=${BUNDLE_DIGEST}" \
  --build-arg "BUNDLE_VERSION=${BUNDLE_VERSION}" \
  --build-arg "BUILT_AT=$(date -u +%Y-%m-%dT%H:%M:%SZ)" \
  "${BUNDLE_SNAPSHOT}"

# Read the provenance back out of the built image rather than trusting that the
# build args arrived. They are expanded by the Dockerfile, and an ARG that is out
# of scope where it is used expands to an empty string with no error -- which is
# exactly how the base label shipped empty until Dockerfile.crew re-declared BASE
# inside the stage. A label nobody reads back is a label that can be silently
# absent, so the two facts this image exists to pin are asserted here.
step "Verifying provenance"
label() { docker image inspect "${TAG}" --format "{{index .Config.Labels \"$1\"}}"; }
got_base="$(label org.opencontainers.image.base.name)"
got_digest="$(label dev.sharemycrew.bundle-digest)"
got_crew="$(label dev.sharemycrew.crew-name)"
[ "${got_base}" = "${BASE}" ] || die \
  "the built image records base '${got_base}' but was built on '${BASE}'; its provenance does not trace back to what it serves"
[ "${got_digest}" = "${BUNDLE_DIGEST}" ] || die \
  "the built image records bundle-digest '${got_digest}' but the bundle's is '${BUNDLE_DIGEST}'"
[ "${got_crew}" = "${CREW_NAME}" ] || die \
  "the built image records crew '${got_crew}' but the bundle's is '${CREW_NAME}'"
echo "    base, crew and bundle-digest all match the inputs"

step "Built ${TAG}"
docker image inspect "${TAG}" --format '    id:   {{.Id}}
    base: {{index .Config.Labels "org.opencontainers.image.base.name"}}
    crew: {{index .Config.Labels "dev.sharemycrew.crew-name"}}
    bundle-digest: {{index .Config.Labels "dev.sharemycrew.bundle-digest"}}'
