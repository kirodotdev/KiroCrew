#!/usr/bin/env bash
# Publish the crew BASE image to a durable registry and print a digest-pinned
# reference for it.
#
# WHAT WAS MISSING. cloud/fargate/taskdef.py refuses a movable tag and requires
# `<repository>@sha256:<64 hex>`, because a revision is keyed on the image and a
# tag would let the content behind that key change after registration. A digest
# exists only after a push. The build lane
# (.github/workflows/crew-image-build.yml) does push, but to a throwaway registry
# on 127.0.0.1 that dies with the job -- deliberately, because that needs no
# credential and is what keeps the lane runnable on a fork pull request with no
# secrets at all. So the machinery that PRODUCES a digest is complete and the
# digest it produces is unusable, and no legal task definition can be registered.
# This script is the durable half.
#
# WHY IT IS SEPARATE FROM THE BUILD. scripts/build_crew_base_image.sh states the
# reason in its own header: pushing needs credentials and so cannot be verified
# in the same place the build can. Publishing is therefore a caller of the
# producer, not a step inside it, and this script must never be wired into the
# fork-PR build lane -- doing so would either break that lane on a fork or make
# it skip silently. test_crew_image_publish_contract.py holds that lane to
# naming no secret and not invoking this script.
#
# WHY THE BASE, NOT THE CREW LAYER. One shared image serves every crew (see
# taskdef.py, "One image serves every crew by design"), so what a registry needs
# to hold is the base. scripts/build_crew_image.sh bakes ONE crew's bundle into a
# layer, which is the per-crew model that decision replaces; publishing that
# would put one image per crew in the registry.
#
# WHY THE REPOSITORY HAS NO DEFAULT. The account and namespace belong to whoever
# runs this, and a default would either name someone's account in the tree or
# quietly publish to the wrong one. Absent is refused, not guessed.
#
# Usage:
#   scripts/publish_crew_base_image.sh --repository REPO [--dry-run]
#
#   --repository  registry repository to publish to, with no tag and no digest,
#                 for example public.ecr.aws/<namespace>/kirocrew-crew-base
#   --dry-run     build and report, stopping before the push and before any
#                 other network write
set -euo pipefail

readonly SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

REPOSITORY=""
DRY_RUN=0

die() { echo "error: $*" >&2; exit 1; }
# Progress goes to STDERR, like every other line this script writes except one.
# STDOUT is a machine interface here: the caller captures it with `$(...)` to get
# a reference, so a progress line on stdout is joined to that reference and the
# result is refused at registration -- a failure reported far from this script.
step() { echo "==> $*" >&2; }

#: The tag this invocation builds, pushes and inspects. UNIQUE per run, and that
#: is the point: the digest is read back by inspecting a tag, so a tag another
#: process can move between the build and the read is a tag that can hand this
#: run somebody else's image and its digest.
#:
#: Time and pid alone are not enough. Two containers sharing one Docker daemon
#: have independent pid spaces, so they can start in the same second holding the
#: same pid and derive the same tag -- which is the collision this name exists to
#: prevent. The nonce comes from the kernel's entropy source, so agreement
#: requires coincidence in 2^32 rather than merely in scheduling.
readonly PUBLISH_NONCE="$(od -An -tx1 -N4 /dev/urandom | tr -d ' \n')"
[ -n "${PUBLISH_NONCE}" ] || die "could not read a nonce for the publish tag"
readonly PUBLISH_TAG="publish-$(date -u +%Y%m%dT%H%M%SZ)-$$-${PUBLISH_NONCE}"

while [ $# -gt 0 ]; do
  case "$1" in
    --repository) [ $# -ge 2 ] || die "--repository needs a value"; REPOSITORY="$2"; shift 2 ;;
    --dry-run)    DRY_RUN=1; shift ;;
    -h|--help)    sed -n '/^# Usage:/,/^set -euo/p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//;$d'; exit 0 ;;
    *)            die "unknown argument: $1" ;;
  esac
done

[ -n "${REPOSITORY}" ] || die \
  "--repository is required and has no default. Pass the repository to publish to, with no tag and no digest, for example public.ecr.aws/<namespace>/kirocrew-crew-base"

# A repository carrying a tag or a digest is refused rather than trimmed: the two
# readings of `repo:1.2` (a tag, or a port on a registry host) are not
# distinguishable here, and silently dropping the wrong half publishes somewhere
# the caller did not name.
case "${REPOSITORY}" in
  *@*) die "--repository must not carry a digest: ${REPOSITORY}" ;;
esac
if printf '%s' "${REPOSITORY}" | grep -qE '(^|/)[^/]+:[^/]+$'; then
  die "--repository must not carry a tag: ${REPOSITORY}"
fi

# The repository must be FULLY QUALIFIED -- its first segment a registry host.
#
# This is a guard rather than a normalization step, and that is the point. Docker
# rewrites a short name into its own familiar form, so `myuser/myrepo` comes back
# out of `RepoDigests` as something this script never received, the exact-prefix
# match below then finds nothing, and the run dies AFTER a successful push --
# reporting failure for work that landed. Matching Docker's rewriting would mean
# keeping a second copy of its reference-parsing rules here, and a second copy of
# somebody else's table is a thing that drifts silently. Refusing the ambiguous
# input instead keeps this script matching strings it was actually given.
#
# The test is Docker's own: a first segment is a registry host when it carries a
# dot or a port, or is exactly `localhost`.
#
# DOCKER HUB IS REFUSED EVEN THOUGH IT PASSES THAT TEST. `docker.io` and
# `index.docker.io` carry a dot, so the accept arm below would take them -- but
# Docker's reference parser reads both as the official registry and DROPS the
# prefix, so `RepoDigests` reports `acme/base@sha256:...` and never the string this
# script was handed. That is the same rewriting a short name gets, reached through a
# host that satisfies the guard, and it ends the same way: the exact-prefix match
# finds nothing and the run dies AFTER a successful push. Refusing it here is what
# keeps the paragraph above true rather than nearly true.
#
# The decided delivery for the shared crew image (#11408) is ECR Public, which is
# always fully qualified and never normalized, so nothing this line is for loses
# anything.
case "${REPOSITORY}" in
  */*) HOST="${REPOSITORY%%/*}" ;;
  *)   HOST="" ;;
esac
case "${HOST}" in
  docker.io|index.docker.io) die "--repository must not name Docker Hub: ${REPOSITORY}. Docker reads ${HOST} as the official registry and drops that prefix, so the digest read-back would look for a string the registry never reports and the run would report failure for a push that succeeded. Publish to a registry that is not rewritten, for example public.ecr.aws/<namespace>/kirocrew-crew-base" ;;
  localhost|*.*|*:*) ;;
  *) die "--repository must be fully qualified, naming a registry host first, for example public.ecr.aws/<namespace>/kirocrew-crew-base. Got ${REPOSITORY}. A short name is rewritten by Docker into a form this script never received, so the digest read-back would find nothing and the run would report failure for a push that succeeded" ;;
esac

# ONE PUBLISH PER CHECKOUT, AND THE TAG ABOVE DOES NOT GIVE YOU THAT.
#
# The per-invocation tag isolates the image NAME and nothing else. The producer
# stages the wheel it builds into one FIXED path in this checkout --
# build_crew_base_image.sh:29, `${RUNTIME_DIR}/vendor` -- and empties that path
# both on entry (:71) and in its own EXIT trap (:62-67). So two publishes from one
# checkout still share it: the second one's `rm` can delete the first one's staged
# wheel between the `cp` that puts it there (:86) and the `docker build` that reads
# it through the bind mount (:95-98). The first build then fails on a missing
# wheel, reported nowhere near the concurrent run that removed it.
#
# REFUSED, NOT QUEUED. Waiting would block on a multi-gigabyte image build with
# nothing to say about how long, and refusing an input it cannot make safe is what
# this script does everywhere else. `mkdir` is the mutex because it is atomic on
# every filesystem this runs on and needs no `flock`, which macOS does not ship.
#
# The lock sits beside the directory it protects, so it is scoped to exactly what
# is shared: one checkout. A run killed outright leaves it behind, which is why the
# refusal names the directory and the one command that clears it.
readonly PUBLISH_STAGING_DIR="${SCRIPT_DIR}/../src/kiro_crew/apps/builtins/aws_control/crew/runtime"
readonly PUBLISH_LOCK="${PUBLISH_STAGING_DIR}/.publish-crew-base-image.lock"
mkdir -p "${PUBLISH_STAGING_DIR}"
if ! mkdir "${PUBLISH_LOCK}" 2>/dev/null; then
  die "another publish is already building in this checkout, and both would stage a wheel into the same directory: ${PUBLISH_STAGING_DIR}/vendor. Wait for it to finish and run this again. If no publish is running, a killed one left the lock behind -- clear it with: rmdir '${PUBLISH_LOCK}'"
fi
# Installed only after the lock is HELD: a trap armed before the acquisition would
# make a refused run delete the lock the run it lost to is still holding.
trap 'rmdir "${PUBLISH_LOCK}" 2>/dev/null || true' EXIT

step "Building the base image through its own producer"
# Invoking the documented producer rather than `docker build` keeps the wheel
# staging and the architecture cross-check in the path, so a published image
# cannot skip the verification a locally built one gets.
#
# Its stdout is folded into stderr for the reason `step` gives: build output on
# stdout would be captured along with the reference.
"${SCRIPT_DIR}/build_crew_base_image.sh" --tag "${REPOSITORY}:${PUBLISH_TAG}" >&2

if [ "${DRY_RUN}" -eq 1 ]; then
  step "Dry run: built ${REPOSITORY}:${PUBLISH_TAG}, stopping before the push"
  echo "    no digest is printed, because a digest exists only after a push" >&2
  exit 0
fi

step "Pushing to ${REPOSITORY}:${PUBLISH_TAG}"
docker push "${REPOSITORY}:${PUBLISH_TAG}" >&2

# The digest is read back from the daemon rather than parsed out of the push
# output, the same way the build lane does it. RepoDigests is empty until a push
# succeeds, so this doubles as the check that the push actually landed.
#
# Read against THIS INVOCATION'S tag, never a shared one: the read is by tag, so
# a tag another process can move between the push and the read would report that
# process's image instead, silently.
#
# The entry is then SELECTED by repository, never taken by position. RepoDigests
# accumulate per image ID across pushes, so an image whose content was already
# pushed to another repository on this daemon can carry that other repository at
# index 0 -- and a shape check alone would pass it, printing a reference that
# names a registry the task cannot pull from. Matching is done with a shell case
# on the exact `<repository>@sha256:` prefix rather than a pattern, so a
# metacharacter in a repository name cannot widen it.
step "Resolving the repository digest"
CANDIDATES="$(docker image inspect "${REPOSITORY}:${PUBLISH_TAG}" --format '{{join .RepoDigests "\n"}}')"
[ -n "${CANDIDATES}" ] || die \
  "no repository digest for ${REPOSITORY}:${PUBLISH_TAG} after the push; RepoDigests is empty, so the push did not land"

REFERENCE=""
while IFS= read -r candidate; do
  [ -n "${candidate}" ] || continue
  case "${candidate}" in
    "${REPOSITORY}@sha256:"*) REFERENCE="${candidate}"; break ;;
  esac
done <<EOF
${CANDIDATES}
EOF

[ -n "${REFERENCE}" ] || die \
  "none of the image's repository digests names ${REPOSITORY}. Present: $(printf '%s' "${CANDIDATES}" | tr '\n' ' ')"

# Held to the shape taskdef.py demands, here rather than at registration: a
# reference that fails there names a refusal far from the push that caused it.
case "${REFERENCE}" in
  *@sha256:*) ;;
  *) die "the resolved reference carries no digest: ${REFERENCE}" ;;
esac
if ! printf '%s' "${REFERENCE}" | grep -qE '^[^[:space:]@]+@sha256:[0-9a-f]{64}$'; then
  die "the resolved reference is not digest-pinned in the form taskdef.py accepts: ${REFERENCE}"
fi

# NO SHARED TAG IS WRITTEN. Not `latest`, not any other fixed name.
#
# A movable name is what every failure mode in this script came back to: used for
# the build and the read-back it lets another run's image be reported, and moved
# afterwards it can still be pointed at the wrong image by a concurrent publish.
# Serializing that move would need a lock across machines sharing one registry,
# which is a lot of machinery for a name nothing reads -- a task definition is
# registered against the digest printed below, and the per-invocation tag above is
# in the registry for anyone browsing. So the convenience is dropped instead, and
# the class of defect goes with it.
step "Published"
# WHAT THIS DIGEST IS, said where it is handed over. It names the BASE image,
# which carries no crew bundle: a task registered against it alone stops at
# install_bundle's fail-closed check before the backend starts, and the bundle
# directory cannot be redirected at runtime because that variable is refused. So
# this reference is the INPUT to composing a crew, not a launchable artifact, and
# the half that delivers a bundle to one shared image is #11408's.
#
# On STDERR, deliberately, and so is everything else this script writes: stdout
# carries exactly one line, the reference, so a caller can capture it with
# `$(...)` without parsing prose out of it. That is why `step` writes to stderr
# and why the producer's and the push's own output is folded there too.
echo "    this names the BASE image, which carries no crew bundle. Compose a crew" >&2
echo "    on top of it before registering a task definition; see the base producer's" >&2
echo "    own closing note for the command" >&2
echo "${REFERENCE}"
