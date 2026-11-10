# kirocrew-dev Coder workspace image: the toolchain the Kiro Crew repository's
# PR loop needs (backend tests, tsc, vitest, lint, semgrep), and nothing else.
# main.tf in the directory above builds it; the guide is
# docs/guides/coder-kirocrew-dev-template.md.
#
# Every version below comes from the repository, and
# test/test_coder_dev_template.py fails when one drifts:
#   - Python 3.12: pyproject.toml requires-python (">=3.12"), which every CI
#     lane also runs.
#   - Node 24.19.0: the test checks the major against .nvmrc (24) and the
#     release against website/package.json engines (>=22.12.0). 24.19.0 is the
#     exact release ensure-node.sh and dependency-vulnerability.yml pin; the
#     test does not track those two.
#   - Semgrep 1.78.0: the version code-review.yml installs, from
#     semgrep-requirements.txt (every file hash-pinned).
#
# Supply chain: base images are official Docker Hub images pinned by their
# multi-arch index digest (amd64 and arm64), Debian packages come from apt, and
# Python packages install with --require-hashes. Nothing pipes a download into
# a shell.
#
# No credential, token or gh login is baked in: git reaches GitHub through the
# Kiro Crew gateway's git proxy, and the Coder agent token arrives at run time
# from main.tf. The toolchain lives outside the user's home (/workspace), so
# the home volume (the checkout, .venv, node_modules) never shadows it and a
# restart keeps it.
#
# Works with the classic builder and BuildKit (no RUN --mount, no TARGETARCH).

FROM node:24.19.0-trixie-slim@sha256:ab3eebe934147fee049b5eb83c570f68c849a13c930bdfa482de99fcdfa3b3de AS node

FROM python:3.12-slim-trixie@sha256:5e2bdb1b0d542d5039cffb8181c65930093949194a65c365dfb1b0fe13a4fa5b

# git (repo work), make (`make build`), curl (the Coder agent's init script
# downloads the agent binary with it), ripgrep (agent search), procps (ps for
# the session worker's diagnostics).
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        ca-certificates \
        curl \
        git \
        make \
        procps \
        ripgrep \
    && rm -rf /var/lib/apt/lists/*

# Node and npm from the official image, same paths and links it uses.
COPY --from=node /usr/local/bin/node /usr/local/bin/node
COPY --from=node /usr/local/lib/node_modules /usr/local/lib/node_modules
RUN ln -s ../lib/node_modules/npm/bin/npm-cli.js /usr/local/bin/npm \
    && ln -s ../lib/node_modules/npm/bin/npx-cli.js /usr/local/bin/npx \
    && node --version \
    && npm --version

# Semgrep in its own venv, so its pins never meet the repository's .venv.
# `semgrep` execs `pysemgrep` by name, so both are linked onto PATH; the venv's
# own bin/ stays off PATH so its python3 and pip never shadow the system ones.
COPY semgrep-requirements.txt /tmp/semgrep-requirements.txt
RUN python3 -m venv /opt/semgrep \
    && /opt/semgrep/bin/pip install --no-cache-dir --disable-pip-version-check \
        --require-hashes --only-binary :all: -r /tmp/semgrep-requirements.txt \
    && rm /tmp/semgrep-requirements.txt \
    && ln -s /opt/semgrep/bin/semgrep /usr/local/bin/semgrep \
    && ln -s /opt/semgrep/bin/pysemgrep /usr/local/bin/pysemgrep \
    && semgrep --version

# Non-root user, uid 1000, whose home is main.tf's persistent volume at
# /workspace.
RUN useradd --home-dir /workspace --create-home --uid 1000 --shell /bin/bash coder
USER coder
WORKDIR /workspace
