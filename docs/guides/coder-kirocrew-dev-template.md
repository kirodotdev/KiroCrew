# The kirocrew-dev Coder template

A Session Environments chat runs its files and commands in a Coder workspace,
and that workspace is whatever the Coder template gives it. A generic image
usually can't run this repository's checks: it has an older Python than
`requires-python`, no Node for `tsc` and `vitest`, and no Semgrep. The
`kirocrew-dev` template in
[`assets/coder-kirocrew-dev/`](assets/coder-kirocrew-dev/) gives a chat a
workspace that can run the whole PR loop for Kiro Crew itself: backend tests,
`tsc`, `vitest`, lint, the Semgrep rule tests and the prepare-pr scripts.

It is for developing Kiro Crew. For other projects, start from your own
template.

## What it contains

| Tool | Version | Where the version comes from |
|---|---|---|
| Python | 3.12 (official `python:3.12-slim-trixie` image) | `requires-python = ">=3.12"` in `pyproject.toml`; every CI lane runs 3.12 |
| Node and npm | Node 24.19.0 (official `node:24.19.0-trixie-slim` image) | `.nvmrc` (24); 24.19.0 is the release `ensure-node.sh` and `dependency-vulnerability.yml` pin; `website/package.json` asks for `>=22.12.0` |
| Semgrep | 1.78.0, in `/opt/semgrep` | The version `code-review.yml` installs |
| git, make, curl, ripgrep, procps | Debian trixie packages | Debian's apt archive |

`test/test_coder_dev_template.py` fails when any of these stops matching the
repository, so a bump to `.nvmrc`, `requires-python` or CI's Semgrep pin has to
update the template in the same change.

How it is put together:

- Both base images are pinned by digest, and each digest covers amd64 and
  arm64. Debian packages come from apt. Semgrep and its dependencies install
  with `pip --require-hashes` from
  [`build/semgrep-requirements.txt`](assets/coder-kirocrew-dev/build/semgrep-requirements.txt).
  Nothing pipes a download into a shell.
- The workspace runs as the non-root user `coder` (uid 1000) with no `sudo`.
- `/home/coder` is a Docker volume that survives stopping and starting the
  workspace, so a clone, its `.venv` and `node_modules` are still there. The
  toolchain lives in the image under `/usr/local` and `/opt`, outside the
  volume, so a restart always gets the image's versions.
- No credential is in the image or the template. git reaches GitHub through
  the Kiro Crew gateway's git proxy. The only secret in the container is the
  Coder agent token, which Coder passes in when the workspace starts, as in
  Coder's own Docker template. Any process in the workspace can read it, so
  treat it like the workspace itself.
- The image has no systemd, so the Kiro Crew session worker starts under the
  `setsid`/`nohup` fallback described in the
  [template contract](../system-specs/modules/providers.md#template-contract).
  It meets the rest of that contract: a pinned image, an unprivileged user,
  a home that survives restarts, Python 3, a POSIX shell, and no published
  ports.

Not included: `gh`, a GitHub login, Playwright browsers, and the repository's
Python and npm dependencies. The dependencies come from the checkout, so
install them once per workspace with `make build`.

## Push it to your Coder deployment

The template uses Coder's Docker provisioner: the Coder provisioner needs
access to a Docker daemon, as in Coder's own `docker` starter template. From a
checkout of this repository, on a machine where `coder login` has been run:

```bash
coder templates push kirocrew-dev -d docs/guides/assets/coder-kirocrew-dev
```

The first workspace builds the image on the Docker host, which takes a few
minutes. Later workspaces reuse it. Pushing again after `build/` changes
builds a new image tag. The template has no parameters, which Kiro Crew
needs, because it creates workspaces with `coder create --use-parameter-defaults`.

## Select it in Kiro Crew

1. Turn on **Settings > Developer > Feature Previews > Session Environments**.
2. In **Settings > Session Environments**, set **Coder URL** to your
   deployment, then **Add Coder profile**.
3. Pick `kirocrew-dev` as the **Coder template**, and set **Working folder** to
   a folder under the home volume, for example `/home/coder/KiroCrew`.
4. **Save settings**, then **Check Coder connection**.
5. In a new chat, pick the profile in the **Run this chat on** menu before the
   first message.

## First run in a workspace

Ask the chat to clone the repository into the working folder and build it:

```bash
git clone https://github.com/kirodotdev/KiroCrew.git /home/coder/KiroCrew
cd /home/coder/KiroCrew
make build
```

`make build` finds Python 3.12 and Node 24 already on `PATH`, creates `.venv`,
and installs the backend and website dependencies. After that the usual
checks run in the workspace, for example:

```bash
.venv/bin/python -m pytest -q test/test_coder_dev_template.py
(cd website && npx tsc --noEmit -p tsconfig.app.json)
semgrep --test --config semgrep/ semgrep-tests/
```

## Bumping a version

1. Change the version in the repository (`.nvmrc`, `requires-python`, or the
   Semgrep pin in `code-review.yml`).
2. In [`build/Dockerfile`](assets/coder-kirocrew-dev/build/Dockerfile), update
   the image tag and its digest. Read the digest with
   `docker buildx imagetools inspect <image>:<tag>`, and check that the
   output lists both `linux/amd64` and `linux/arm64`.
3. For Semgrep, regenerate `build/semgrep-requirements.txt` with the command
   in its header.
4. Run `test/test_coder_dev_template.py`, build the image, and push the
   template again.
