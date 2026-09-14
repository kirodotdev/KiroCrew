# The Share My Crew deployment image: three processes, one task.
#
# What runs here is Kiro Crew's own backend, headless, plus a thin front process
# and a backup sidecar. Headless means no interface is served to anyone, the
# owner included: the owner's control plane stays on the owner's own machine and
# no part of it is in this image.
#
# Base image: bookworm ships glibc 2.36. The floor for the kiro-cli gnu builds is
# 2.30, not the 2.39 the installer claims, so slim is new enough.
#
# Pinned by manifest-list digest, not only by tag: a tag can move, and nothing
# would detect that the built image differs from the reviewed one. Not
# hypothetical -- while adding this pin, bare `3.12-slim` had already moved to
# trixie (Debian 13), a different release from the bookworm this file's glibc
# reasoning was reviewed against. The tag names the bookworm variant explicitly
# so the digest and the human-readable name cannot disagree. The digest names
# the multi-arch INDEX, so the builder's platform still selects the right
# per-arch manifest (and sets TARGETARCH). To bump:
# `docker buildx imagetools inspect python:3.12-slim-bookworm` and update the
# digest, keeping tag and digest in step.
FROM python:3.12-slim-bookworm@sha256:782412e85d0f0984994c290652577d4018aff08145c85b262bb63dc0c7522254

# Architecture comes from the builder, it is NEVER pinned here. An earlier build
# hardcoded arm64 in two places and cost two separate deploy failures: the image
# refused to exec on an x86 task, and then a task definition kept an arm64
# RuntimePlatform while accepting an x86 parameter that nothing referenced. The
# deploy must cross-check image arch against task arch; this file's job is only
# to not lie about it.
ARG TARGETARCH

# Pinned deliberately: this is the version whose ACP behaviour was verified, down
# to agentCapabilities.loadSession and the shape of its tool events.
ARG KIRO_VERSION=2.20.2

# The expected sha256 of each arch's tarball for KIRO_VERSION. The host does
# publish these (<url>.sha256 and stable/latest/manifest.json), but a checksum
# served by the same host it protects verifies transport, not review: the values
# are recorded HERE so the build fails if the published artifact ever differs
# from what was reviewed. Bumping KIRO_VERSION means updating both in the same
# change (cross-check the hand-computed sha against the published .sha256).
# ARG defaults are overridable, so never pass these as --build-arg: an override
# silently defeats the pin.
ARG KIRO_SHA256_X86_64=232ca9498ba7640ec205b1e2ad02553448c897e63e0a89cb01f60056ae255a59
ARG KIRO_SHA256_AARCH64=49395d0491e6d9e46ce9b900f5e819573bd313751a5b0b6906899a92b438ee3a

RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates curl xz-utils \
    && rm -rf /var/lib/apt/lists/*

# `kiro-cli acp` is a thin launcher that dispatches to a SIBLING `kiro-cli-chat`,
# resolved relative to its own path and through PATH. Shipping only the launcher
# builds fine and then fails on the first turn, so both go in and both are
# asserted here. The install is unconditional on purpose: an earlier revision
# made it opt-in via a build arg, the deploy path never passed the arg, and the
# build "succeeded" by skipping the download.
RUN set -eux; \
    case "${TARGETARCH:-amd64}" in \
      amd64) KIRO_ARCH=x86_64;  KIRO_SHA256="${KIRO_SHA256_X86_64}" ;; \
      arm64) KIRO_ARCH=aarch64; KIRO_SHA256="${KIRO_SHA256_AARCH64}" ;; \
      *) echo "unsupported TARGETARCH: ${TARGETARCH}" >&2; exit 1 ;; \
    esac; \
    mkdir -p /tmp/kiro; \
    curl -fsSL -o /tmp/kiro/kirocli.tar.xz \
      "https://prod.download.cli.kiro.dev/stable/${KIRO_VERSION}/kirocli-${KIRO_ARCH}-linux.tar.xz"; \
    echo "${KIRO_SHA256}  /tmp/kiro/kirocli.tar.xz" | sha256sum -c -; \
    tar -xJf /tmp/kiro/kirocli.tar.xz -C /tmp/kiro; \
    install -m 0755 /tmp/kiro/kirocli/bin/kiro-cli      /usr/local/bin/kiro-cli; \
    install -m 0755 /tmp/kiro/kirocli/bin/kiro-cli-chat /usr/local/bin/kiro-cli-chat; \
    rm -rf /tmp/kiro; \
    test -x /usr/local/bin/kiro-cli; \
    test -x /usr/local/bin/kiro-cli-chat; \
    command -v kiro-cli-chat; \
    kiro-cli --version

# The running crew has no reason to hold a download tool.
RUN apt-get purge -y curl xz-utils && apt-get autoremove -y && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Kiro Crew is installed as a real package because the backend is RUN here, not
# imported as a library. scripts/build_image.sh builds the wheel from a checkout
# and drops it in vendor/; the wheel is a build artifact and is not committed.
COPY vendor/*.whl /tmp/wheels/
COPY container/requirements.txt ./container/requirements.txt
RUN pip install --no-cache-dir -r container/requirements.txt /tmp/wheels/*.whl \
    && rm -rf /tmp/wheels

# ONLY the container package. The Kiro Crew app's own backend and UI are in this
# repository too, and they must never be copied in: that code runs inside the
# OWNER's gateway with the owner's AWS credentials, and this image is the
# customer-facing surface. Keeping the COPY narrow is what stops a control-plane
# credential from riding into a customer's container.
COPY container/ ./container/

# Non-root: this container terminates a surface that eventually faces customers,
# and nothing it does needs root.
#
# The state directories are created HERE, in the image, on purpose. A fresh
# volume mounted over a path is initialised from the image's directory at that
# mount point, ownership included, so declaring them owned by `crew` is what
# lets a non-root container write to a mounted transcript store. Without it the
# container starts and dies on a PermissionError that reads like a code fault.
RUN useradd --create-home --home-dir /var/lib/crew --shell /usr/sbin/nologin crew \
    && mkdir -p /var/lib/kirocrew/sessions/archive \
                /var/lib/kirocrew/artifacts \
                /var/lib/kirocrew/run \
                /var/lib/crew/.kiro/agents \
    && chown -R crew:crew /var/lib/kirocrew /var/lib/crew

# The three processes read their configuration only through container.common,
# so these names are the whole environment contract.
#
# SMC_CONFIG_DIR EQUALS SMC_DATA_HOME, and that is not an oversight. Verified
# against a running gateway: Kiro Crew's config_dir() and data_home() resolve to
# the same directory, so session_map.json and open_slots.json live at the home
# root rather than under a config/ subdirectory. Setting them apart makes the
# sidecar back up every transcript and neither of those files, which yields a
# backup that looks healthy and a restore with no resume and no conversation
# list. The supervisor refuses to start if they disagree.
ENV HOME=/var/lib/crew \
    KIROCREW_HOME=/var/lib/kirocrew \
    SMC_DATA_HOME=/var/lib/kirocrew \
    SMC_CONFIG_DIR=/var/lib/kirocrew \
    SMC_BACKEND_RUN_DIR=/var/lib/kirocrew/run \
    SMC_BACKEND_PORT=8765 \
    SMC_FRONT_PORT=8080 \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

# Supplied at run time, never baked in: KIRO_API_KEY, SMC_CONTROL_SECRET,
# SMC_ROUTE_PREFIX, SMC_CREW_NAME, SMC_BACKUP_BUCKET, SMC_BACKUP_PREFIX.
USER crew

# 8080 is the front process, the only listener the network reaches. The Kiro Crew
# backend listens on 8765 bound to 127.0.0.1 and is deliberately NOT exposed;
# adding it here would defeat the design's only trust boundary.
EXPOSE 8080

# No HEALTHCHECK on purpose. A docker-level check that proves the port is bound
# would report healthy on a container where every turn fails, which is exactly
# what an invalid model credential produces: a present credential is not a
# working one. Readiness belongs to the front process's own endpoint, which can
# gate on a real turn having succeeded.
CMD ["python", "-m", "container.supervisor"]
