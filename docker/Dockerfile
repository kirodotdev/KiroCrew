# KiroCrew gateway image — headless dashboard + channel bots (Slack/Discord/
# Telegram/WeCom/Webex) in a container.
#
# Build contract: the image consumes the CI-BUILT WHEEL from dist/ (the same
# artifact pip users install — frontend staged in, version stamped), never a
# raw source tree. Building from source here would duplicate build-wheel.yml
# and let the Docker bytes drift from the pip bytes for the same version.
#   docker build -f docker/Dockerfile .        (requires dist/kirocrew-*.whl)
#
# Base: python:3.12-slim-trixie is pinned (not bare -slim) because kiro-cli's
# gnu builds enforce a glibc floor of 2.34 on x86_64 and 2.39 on aarch64;
# trixie ships 2.41 so ONE base covers both architectures. The wheel itself is
# py3-none-any — multi-arch needs no compilation.
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
#     The download is resolved through the release manifest and
#     sha256-verified fail-closed; the resolved version is recorded at
#     /usr/local/share/kirocrew/kiro-cli-version and (when the lane passes
#     KIRO_CLI_VERSION) in the dev.kirocrew.kiro-cli.version OCI label.

FROM python:3.12-slim-trixie

# TARGETARCH is set by buildx: amd64 | arm64. Mapped to kiro-cli's
# x86_64 / aarch64 artifact names below.
ARG TARGETARCH

# Version stamped into OCI labels by the publish lane (dev builds: "dev").
ARG KIROCREW_VERSION=dev
ARG VCS_REF=unknown

# kiro-cli version the build MUST resolve to. The lanes resolve this from
# the release manifest before the build (so it lands in an OCI label and a
# moved manifest fails the build); "latest" accepts whatever the manifest
# names at build time — bytes are checksum-verified either way.
ARG KIRO_CLI_VERSION=latest

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
# fetches), ripgrep (agent search), unzip (kiro-cli archive), tini (PID 1:
# reap zombies from agent subprocess trees + forward SIGTERM so
# `docker stop` shuts the gateway down gracefully instead of SIGKILL after
# the timeout).
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        ca-certificates \
        curl \
        git \
        ripgrep \
        tini \
        unzip \
    && rm -rf /var/lib/apt/lists/*

# kiro-cli — the agent runtime KiroCrew drives over ACP; chat sessions
# require it. Global install (Q_INSTALL_GLOBAL) places kiro-cli,
# kiro-cli-chat, kiro-cli-term into /usr/local/bin; Q_SKIP_SETUP skips
# dotfile/shell integrations that are meaningless in a container.
#
# Supply-chain discipline (image inputs must be verifiable):
#   1. Resolve the version + per-arch sha256 from the release manifest
#      (latest/manifest.json) — the manifest names VERSIONED download paths.
#   2. If the lane pinned KIRO_CLI_VERSION, a manifest that moved between
#      resolution and build FAILS the build (label can never lie).
#   3. Download the VERSIONED zip and verify its sha256 fail-closed before
#      executing anything from it.
#   4. Record the resolved version at a fixed path for runtime audit
#      (docker run --rm <image> cat /usr/local/share/kirocrew/kiro-cli-version).
RUN set -eux; \
    case "${TARGETARCH}" in \
        amd64) KIRO_ARCH="x86_64" ;; \
        arm64) KIRO_ARCH="aarch64" ;; \
        *) echo "unsupported TARGETARCH: ${TARGETARCH}" >&2; exit 1 ;; \
    esac; \
    curl --proto '=https' --tlsv1.2 -fsSL \
        "https://desktop-release.q.us-east-1.amazonaws.com/latest/manifest.json" \
        -o /tmp/kirocli-manifest.json; \
    RESOLVED="$(python3 -c 'import json;print(json.load(open("/tmp/kirocli-manifest.json"))["version"])')"; \
    if [ "${KIRO_CLI_VERSION}" != "latest" ] && [ "${KIRO_CLI_VERSION}" != "${RESOLVED}" ]; then \
        echo "kiro-cli manifest moved: pinned ${KIRO_CLI_VERSION}, manifest now ${RESOLVED}" >&2; exit 1; \
    fi; \
    SHA256="$(KIRO_ARCH="${KIRO_ARCH}" python3 -c 'import json,os; m=json.load(open("/tmp/kirocli-manifest.json")); a=os.environ["KIRO_ARCH"]; print(next(p["sha256"] for p in m["packages"] if p["fileType"]=="zip" and p["architecture"]==a and "musl" not in p["download"]))')"; \
    curl --proto '=https' --tlsv1.2 -fsSL \
        "https://desktop-release.q.us-east-1.amazonaws.com/${RESOLVED}/kirocli-${KIRO_ARCH}-linux.zip" \
        -o /tmp/kirocli.zip; \
    echo "${SHA256}  /tmp/kirocli.zip" | sha256sum -c -; \
    unzip -q /tmp/kirocli.zip -d /tmp; \
    Q_INSTALL_GLOBAL=1 Q_SKIP_SETUP=1 /tmp/kirocli/install.sh; \
    rm -rf /tmp/kirocli /tmp/kirocli.zip /tmp/kirocli-manifest.json; \
    mkdir -p /usr/local/share/kirocrew; \
    printf '%s\n' "${RESOLVED}" > /usr/local/share/kirocrew/kiro-cli-version; \
    kiro-cli --version

# The CI-built wheel (build-wheel.yml artifact). The glob keeps the Dockerfile
# version-agnostic; the lane stages exactly one wheel into dist/.
COPY dist/*.whl /tmp/wheels/
RUN set -eux; \
    WHEEL_COUNT=$(ls /tmp/wheels/*.whl | wc -l); \
    [ "${WHEEL_COUNT}" -eq 1 ] || { echo "expected exactly one wheel, found ${WHEEL_COUNT}" >&2; exit 1; }; \
    pip install --no-cache-dir /tmp/wheels/*.whl; \
    rm -rf /tmp/wheels; \
    kirocrew --version

# Non-root runtime user. HOME holds ALL persistent state; users mount a
# named volume at /home/kirocrew (recommended over a bind mount: named
# volumes inherit the image's ownership on first use).
RUN useradd --create-home --uid 1000 --shell /bin/bash kirocrew
COPY --chmod=755 docker/entrypoint.sh /usr/local/bin/kirocrew-entrypoint
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
