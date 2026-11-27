# KiroCrew gateway image — headless dashboard + channel bots (Slack/Discord/
# Telegram/WeCom/Webex) in a container.
#
# Build contract: the image consumes the CI-BUILT WHEEL from dist/ (the same
# artifact pip users install — frontend staged in, version stamped), never a
# raw source tree. Building from source here would duplicate build-wheel.yml
# and let the Docker bytes drift from the pip bytes for the same version.
#   docker build -f docker/Dockerfile .        (requires dist/kirocrew-*.whl
#                                                and BuildKit — the default
#                                                builder since Docker 23; on
#                                                older engines prefix with
#                                                DOCKER_BUILDKIT=1)
#
# Base: Amazon Linux 2023 from its canonical registry, public.ecr.aws (anyone
# can pull it anonymously), pinned by the multi-arch INDEX digest so the
# builder's platform still selects the per-arch manifest. AL2023 ships glibc
# 2.34. kiro-cli's gnu binaries reference nothing newer than GLIBC_2.34 on
# x86_64 and GLIBC_2.30 on aarch64 (read from the 2.37.1 binaries' symbol
# versions), so 2.34 covers both. Its install.sh refuses aarch64 below 2.39,
# which is why the binaries are installed directly below rather than through
# it. Python 3.12 comes from the AL2023 repos. The wheel itself is
# py3-none-any — multi-arch needs no compilation. To bump:
# `docker buildx imagetools inspect public.ecr.aws/amazonlinux/amazonlinux:2023`
# and update the digest.
#
# Security posture:
#   - Non-root user (kirocrew, uid 1000). All state lives under
#     /home/kirocrew so a single volume mount persists everything: gateway
#     home (~/.kiro/crew), kiro-cli credentials, agents, skills.
#   - KIROCREW_BIND=0.0.0.0 binds the container's network namespace only;
#     nothing reaches the host unless the operator publishes the port
#     (-p 5476:5476). Token auth is always mounted server-side (see
#     dashboard/origin.py bind_address_for), CSRF + Host checks apply to
#     every request except the three token-free liveness probes
#     (origin.PROBE_PATHS), and config writes stay restricted to
#     direct-local sessions (docker exec).
#   - kiro-cli binaries install to /usr/local/bin (outside the volume) so a
#     user-mounted home can never shadow them with stale or tampered copies.
#     The release is the one pinned in packaging/kiro-cli-version and
#     sha256-verified fail-closed against packaging/kiro-cli-sha256, the same
#     two files the desktop build reads; the version is recorded at
#     /usr/local/share/kirocrew/kiro-cli-version and (when the lane passes
#     KIRO_CLI_VERSION) in the dev.kirocrew.kiro-cli.version OCI label.

# tini, built from source in a throwaway stage: AL2023 has no tini package.
# Only the binary is carried into the final image; the compiler and the
# source stay here.
#
# Pinned like every other fetched input. The release is krallin/tini v0.19.0
# (https://github.com/krallin/tini/releases/tag/v0.19.0, the project's latest
# release, tag commit de40ad007797e0dcd8b7126f27bb87401d224240). The release
# page publishes checksums for its prebuilt binaries only, not for the source
# archive, so the sha256 below was computed from two separate downloads of the
# tag's source archive and its unpacked tree was diffed against a clone of the
# tag commit, with no difference. Bumping TINI_VERSION means recomputing the
# sha256 the same way in the same change. ARG defaults are overridable, so
# never pass these as --build-arg: an override silently defeats the pin.
FROM public.ecr.aws/amazonlinux/amazonlinux:2023@sha256:5c38fd85b00ce8c094315d9b71f671f880052314a22bd25f1a027821bf61d9d7 AS tini-builder

ARG TINI_VERSION=0.19.0
ARG TINI_SRC_SHA256=0fd35a7030052acd9f58948d1d900fe1e432ee37103c5561554408bdac6bbf0d

RUN set -eux; \
    dnf install -y --setopt=install_weak_deps=False cmake gcc gzip make tar; \
    curl --proto '=https' --tlsv1.2 -fsSL \
        "https://github.com/krallin/tini/archive/refs/tags/v${TINI_VERSION}.tar.gz" \
        -o /tmp/tini.tar.gz; \
    echo "${TINI_SRC_SHA256}  /tmp/tini.tar.gz" | sha256sum -c -; \
    tar -xzf /tmp/tini.tar.gz -C /tmp; \
    cmake -S "/tmp/tini-${TINI_VERSION}" -B /tmp/tini-build -DCMAKE_BUILD_TYPE=Release; \
    make -C /tmp/tini-build tini; \
    install -m 0755 /tmp/tini-build/tini /usr/local/bin/tini; \
    /usr/local/bin/tini --version

FROM public.ecr.aws/amazonlinux/amazonlinux:2023@sha256:5c38fd85b00ce8c094315d9b71f671f880052314a22bd25f1a027821bf61d9d7

# TARGETARCH is set by buildx: amd64 | arm64. Mapped to kiro-cli's
# x86_64 / aarch64 artifact names below.
ARG TARGETARCH

# Version stamped into OCI labels by the publish lane (dev builds: "dev").
ARG KIROCREW_VERSION=dev
ARG VCS_REF=unknown

# kiro-cli version the lane expects, for the OCI label. The build installs
# the version pinned in packaging/kiro-cli-version; a lane that passes a
# different one fails the build, so the label can never lie. Empty (the
# default) skips that cross-check.
ARG KIRO_CLI_VERSION=

# Source repository for the OCI label. The publish lane passes the actual
# repository (forks label their own images correctly instead of
# masquerading as the canonical repo).
ARG SOURCE_REPO_URL="https://github.com/kirodotdev/KiroCrew"

LABEL org.opencontainers.image.source="${SOURCE_REPO_URL}" \
      org.opencontainers.image.description="KiroCrew gateway — personal AI agent crew (dashboard + Slack/Discord/Telegram/WeCom/Webex)" \
      org.opencontainers.image.licenses="MIT" \
      org.opencontainers.image.version="${KIROCREW_VERSION}" \
      org.opencontainers.image.revision="${VCS_REF}" \
      dev.kirocrew.kiro-cli.version="${KIRO_CLI_VERSION}"

# Runtime toolset for agent sessions: git (repo work), curl (healthcheck +
# fetches), ripgrep (agent search), unzip (kiro-cli archive). tini (PID 1:
# reap zombies from agent subprocess trees + forward SIGTERM so
# `docker stop` shuts the gateway down gracefully instead of SIGKILL after
# the timeout) comes from the tini-builder stage, copied in just below.
#
# AL2023 additions: python3.12 + pip (the base python3 is 3.9, so python3,
# python and pip are linked to 3.12 for the layers below and the entrypoint),
# shadow-utils (useradd), util-linux (runuser in the KAS step), and
# findutils/procps-ng/tar/gzip, which Debian slim carried and the AL2023 base
# does not. curl-minimal is already in the base and is named to keep this list
# the whole runtime toolset.
RUN dnf install -y --setopt=install_weak_deps=False \
        python3.12 \
        python3.12-pip \
        ca-certificates \
        curl-minimal \
        findutils \
        git \
        gzip \
        procps-ng \
        ripgrep \
        shadow-utils \
        tar \
        unzip \
        util-linux \
    && dnf clean all \
    && ln -sf /usr/bin/python3.12 /usr/local/bin/python3 \
    && ln -sf /usr/bin/python3.12 /usr/local/bin/python \
    && ln -sf /usr/bin/pip3.12 /usr/local/bin/pip

# tini from the builder stage above. The entrypoint execs it after the
# credential scrub (see the ENTRYPOINT note at the end of this file).
COPY --from=tini-builder /usr/local/bin/tini /usr/local/bin/tini

# kiro-cli — the agent runtime KiroCrew drives over ACP; chat sessions
# require it. The three binaries the release ships (kiro-cli, kiro-cli-chat,
# kiro-cli-term) are installed straight into /usr/local/bin, outside the home
# volume. That is all the release's install.sh does for a global install with
# setup skipped, but install.sh also refuses aarch64 hosts below glibc 2.39,
# and AL2023 has 2.34 while the aarch64 binaries need only 2.30. `kiro-cli acp`
# dispatches to kiro-cli-chat as a sibling on PATH, so both are asserted.
#
# Supply-chain discipline (image inputs must be verifiable):
#   1. Read the version from packaging/kiro-cli-version and the per-arch
#      sha256 from packaging/kiro-cli-sha256 (keyed `<version>/<file>`), the
#      pin the desktop build shares. Both are bind-mounted from the build
#      context, so neither enters a layer; a rebuild of a tag reproduces it.
#   2. If the lane passed KIRO_CLI_VERSION, a pin that disagrees FAILS the
#      build (label can never lie).
#   3. Download that version's zip and verify its sha256 fail-closed before
#      executing anything from it. A pin with no sha line fails the build.
#   4. Record the version at a fixed path for runtime audit
#      (docker run --rm <image> cat /usr/local/share/kirocrew/kiro-cli-version).
RUN --mount=type=bind,source=packaging,target=/tmp/kiro-cli-pin \
    set -eux; \
    case "${TARGETARCH}" in \
        amd64) KIRO_ARCH="x86_64" ;; \
        arm64) KIRO_ARCH="aarch64" ;; \
        *) echo "unsupported TARGETARCH: ${TARGETARCH}" >&2; exit 1 ;; \
    esac; \
    RESOLVED="$(tr -d '[:space:]' < /tmp/kiro-cli-pin/kiro-cli-version)"; \
    case "${RESOLVED}" in \
        ''|*[!0-9A-Za-z.+-]*) echo "bad kiro-cli pin: '${RESOLVED}'" >&2; exit 1 ;; \
    esac; \
    if [ -n "${KIRO_CLI_VERSION}" ] && [ "${KIRO_CLI_VERSION}" != "${RESOLVED}" ]; then \
        echo "kiro-cli pin mismatch: lane expects ${KIRO_CLI_VERSION}, pin is ${RESOLVED}" >&2; exit 1; \
    fi; \
    ARTIFACT="${RESOLVED}/kirocli-${KIRO_ARCH}-linux.zip"; \
    SHA256="$(awk -v want="${ARTIFACT}" '$2 == want { print $1 }' /tmp/kiro-cli-pin/kiro-cli-sha256)"; \
    if [ "$(printf '%s' "${SHA256}" | wc -w)" -ne 1 ]; then \
        echo "packaging/kiro-cli-sha256 has no single line for ${ARTIFACT}" >&2; exit 1; \
    fi; \
    curl --proto '=https' --tlsv1.2 -fsSL \
        "https://desktop-release.q.us-east-1.amazonaws.com/${ARTIFACT}" \
        -o /tmp/kirocli.zip; \
    echo "${SHA256}  /tmp/kirocli.zip" | sha256sum -c -; \
    unzip -q /tmp/kirocli.zip -d /tmp; \
    for bin in kiro-cli kiro-cli-chat kiro-cli-term; do \
        install -m 0755 "/tmp/kirocli/bin/${bin}" /usr/local/bin/; \
    done; \
    rm -rf /tmp/kirocli /tmp/kirocli.zip; \
    mkdir -p /usr/local/share/kirocrew; \
    printf '%s\n' "${RESOLVED}" > /usr/local/share/kirocrew/kiro-cli-version; \
    test -x /usr/local/bin/kiro-cli-chat; \
    test -x /usr/local/bin/kiro-cli; \
    kiro-cli --version

# The CI-built wheel (build-wheel.yml artifact). The glob keeps the Dockerfile
# version-agnostic; the lane stages exactly one wheel into dist/.
#
# The wheel is BIND-MOUNTED from the build context, never COPYed. Layers are
# append-only: a COPY commits the ~48MB wheel to its own layer, and an `rm`
# in a later instruction can only stack a whiteout on top, so every pull
# still fetched the wheel next to the installed copy of the same code
# (#5778, 5.7% of the published image). The mount exists only for the
# duration of this RUN and commits no wheel bytes to any layer, which makes
# the cleanup `rm` unnecessary rather than merely late -- do not "simplify"
# this back to COPY. The mount is read-only (BuildKit default); pip only
# reads the wheel. .dockerignore's allowlist means the mounted dist/ contains
# exactly the staged *.whl, so the one-wheel guard keeps its meaning.
# Bare `--version` is a pre-dispatch fast-path that answers before
# `kiro_crew.cli` is imported, so it cannot prove the import chain resolves.
# The `import kiro_crew.cli` probe is what catches a runtime import the wheel
# metadata does not declare.
RUN --mount=type=bind,source=dist,target=/tmp/wheels \
    set -eux; \
    WHEEL_COUNT=$(ls /tmp/wheels/*.whl | wc -l); \
    [ "${WHEEL_COUNT}" -eq 1 ] || { echo "expected exactly one wheel, found ${WHEEL_COUNT}" >&2; exit 1; }; \
    pip install --no-cache-dir /tmp/wheels/*.whl; \
    kirocrew --version; \
    python -c 'import kiro_crew.cli'

# Re-stamp the provenance the wheel already carries: this image installs the
# wheel artifact, which is stamped "wheel", so without this every container
# reports the wheel channel and the two are indistinguishable. Overwrite the
# baked module in the installed package rather than setting
# KIROCREW_DISTRIBUTION -- the baked value deliberately outranks the env var,
# so an ENV here would be ignored. Carry over the wheel's baked build lane.
RUN set -eux; \
    PKG_DIR="$(python -c 'import kiro_crew, pathlib; print(pathlib.Path(kiro_crew.__file__).parent)')"; \
    CHANNEL="$(python -c 'from kiro_crew.build_channel import baked_build_channel; print(baked_build_channel())')"; \
    printf '%s\n' \
      '"""Build-time provenance. GENERATED -- do not edit."""' \
      '' \
      'from __future__ import annotations' \
      '' \
      'DISTRIBUTION = "docker"' \
      "BUILD_CHANNEL = \"${CHANNEL}\"" \
      > "${PKG_DIR}/_build_info.py"; \
    python -c 'import sys; from kiro_crew import beacon; from kiro_crew.build_channel import baked_build_channel; assert beacon.distribution() == "docker", beacon.distribution(); assert baked_build_channel() == sys.argv[1], baked_build_channel()' "${CHANNEL}"

# Non-root runtime user. HOME holds ALL persistent state; users mount a
# named volume at /home/kirocrew (recommended over a bind mount: named
# volumes inherit the image's ownership on first use).
RUN useradd --create-home --uid 1000 --shell /bin/bash kirocrew
COPY --chmod=755 docker/entrypoint.sh /usr/local/bin/kirocrew-entrypoint

# Bundled KAS engine: the `agent.kas_bundle` route with no download.
#
# BUNDLE_KAS=1 stages the KAS engine bundle this release PINS
# (kiro_crew/acp/kas_pin.py: KAS_PIN_VERSION + KAS_PIN_MANIFEST_SHA256) at
# /usr/local/share/kirocrew/kas, outside the home volume and root-owned, so a
# mounted home can never shadow or rewrite it. The staging IS the backend's own
# installer (acp.kas_bundle.install_bundle) run from the wheel installed above,
# exactly as packaging/build-desktop.sh stages the desktop app's copy: manifest
# bytes checked against the pin before parsing, every artifact sha256+size-
# pinned by that manifest, tar filter="data", a `node --version` probe, one
# atomic publish. The tree is read back through the reader the gateway uses,
# first as root and then as the kirocrew user, so a tree the gateway would
# ignore fails the build rather than the first prompt. The root holds
# <version>/{bundle,node,install.json}, the layout `kirocrew kas install`
# writes under the data home.
#
# KIROCREW_BUNDLED_KAS_DIR names the root as an image ENV, so every process in
# the container sees it: the gateway, and `docker exec <ctr> kirocrew kas
# status` / `doctor`, which bypass the entrypoint. It is the variable the
# desktop shell exports in gateway-env.js, and installed_bundle() reads it as
# the second root after the data home's own `kirocrew kas install`. The
# directory is created on every build, so on a BUNDLE_KAS=0 image it is an
# empty root-owned directory: the reader finds no version in it, and the
# read-only seal the sandbox puts on this root covers a path that exists.
#
# Off by default: the route itself is still in shadow testing
# (agent.kas_bundle defaults to false, install.sh fetches the engine only with
# --with-kas), and the staged engine adds about 370 MB to every pull of an
# image whose users have not switched the route on.
ARG BUNDLE_KAS=0
ENV KIROCREW_BUNDLED_KAS_DIR=/usr/local/share/kirocrew/kas
RUN set -eux; \
    case "${BUNDLE_KAS}" in \
        0|1) ;; \
        *) echo "BUNDLE_KAS must be 0 or 1, got '${BUNDLE_KAS}'" >&2; exit 1 ;; \
    esac; \
    rm -rf "${KIROCREW_BUNDLED_KAS_DIR}"; \
    mkdir -p "${KIROCREW_BUNDLED_KAS_DIR}"; \
    if [ "${BUNDLE_KAS}" = "0" ]; then \
        echo "BUNDLE_KAS=0: image ships without a bundled KAS engine"; exit 0; \
    fi; \
    python -I -c 'import sys; from pathlib import Path; \
from kiro_crew.acp import kas_bundle; \
from kiro_crew.acp.kas_pin import KAS_PIN_MANIFEST_SHA256, KAS_PIN_VERSION; \
from kiro_crew.config.sections import KAS_BUNDLE_DEFAULT_URL; \
dest = Path(sys.argv[1]); \
manifest, url = kas_bundle.fetch_manifest(KAS_BUNDLE_DEFAULT_URL, version=KAS_PIN_VERSION, sha256=KAS_PIN_MANIFEST_SHA256); \
installed = kas_bundle.install_bundle(manifest, source=url, root=dest); \
again = kas_bundle.installed_bundle(root=dest, version=KAS_PIN_VERSION); \
assert again is not None and again.acp_server == installed.acp_server and again.node_bin == installed.node_bin, "staged tree does not read back"; \
print(f"KAS {KAS_PIN_VERSION} staged, Node v{installed.node_version}")' \
        "${KIROCREW_BUNDLED_KAS_DIR}"; \
    chmod -R u=rwX,go=rX "${KIROCREW_BUNDLED_KAS_DIR}"; \
    runuser -u kirocrew -- python -I -c 'import sys; from pathlib import Path; \
from kiro_crew.acp import kas_bundle; \
from kiro_crew.acp.kas_pin import KAS_PIN_VERSION; \
found = kas_bundle.installed_bundle(root=Path(sys.argv[1]), version=KAS_PIN_VERSION); \
assert found is not None, "the kirocrew user cannot read the staged KAS engine"; \
print(f"kirocrew reads KAS {KAS_PIN_VERSION}: {found.acp_server}")' \
        "${KIROCREW_BUNDLED_KAS_DIR}"

USER kirocrew
WORKDIR /home/kirocrew

# Container networking: published ports map to the bridge interface, so the
# gateway must bind beyond loopback INSIDE the container namespace. This is
# an explicit, validated override (see bind_address_for) — operators can set
# KIROCREW_BIND=127.0.0.1 to force loopback-only (e.g. behind an in-pod
# sidecar proxy).
ENV KIROCREW_BIND=0.0.0.0

EXPOSE 5476

# Probes loopback inside the container namespace: the default 0.0.0.0 bind
# answers there. Operators who narrow KIROCREW_BIND to a specific interface
# address own the corresponding healthcheck override.
HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=3 \
    CMD curl -fsS "http://127.0.0.1:${KIROCREW_PORT:-5476}/api/health" || exit 1

# tini as PID 1: reaps agent subprocess zombies and forwards signals.
# The script (not tini) is the ENTRYPOINT: it must scrub credentials from
# the environment BEFORE any long-lived process image is created, then it
# execs `tini -- kirocrew ...` itself. Wrapping the script IN tini would
# give PID 1 an execve-time environ snapshot containing every credential
# (verified by the docker-smoke environ scan).
ENTRYPOINT ["kirocrew-entrypoint"]
CMD ["gateway"]
