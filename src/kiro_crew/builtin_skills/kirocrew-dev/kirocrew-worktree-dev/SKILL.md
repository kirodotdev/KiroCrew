---
name: kirocrew-worktree-dev
description: "HARD RULE for developing the Kiro Crew source repo ITSELF (not users' projects): develop, build and verify in a git worktree, never against the live gateway. Covers environment setup, build/dist ordering, feature flags, isolated previews, cleanup and publication authorization; prepare-pr owns the PR workflow."
triggers: kirocrew worktree, kirocrew build gate, kirocrew dev, kirocrew source, contribute to kirocrew, kirocrew repo
repo_scope: src/kiro_crew
---

# Kiro Crew worktree development

This skill applies ONLY to the Kiro Crew source repository or a worktree of it.
Ignore it for other projects. One feature uses one dedicated worktree containing
both `src/kiro_crew/` and `website/`, including backend-only changes.
Never edit the live checkout or start/stop the live gateway from a feature session.

## Worktree and environment

```bash
# From the main clone; skip creation when already in the assigned worktree.
git fetch origin main
git worktree add ../kirocrew-wt-<name> -b feat/<name> origin/main
cd ../kirocrew-wt-<name>
python3 -m venv .venv
.venv/bin/pip install -e ".[voice]" --group dev
cd website && npm ci && cd ..
git worktree list
```

`dev` is a PEP 735 dependency group, not an extra. `--group` needs pip >= 25.1;
if unsupported, upgrade the worktree's pip with `.venv/bin/pip install -U pip`.
On Windows use `.venv\Scripts\` in place of `.venv/bin/`.

Making a worktree live swaps code behind the same URL and REAL data home,
including DB and sessions. Only one can be live at a time. This is not preview
isolation: get explicit authorization for a live cutover or migration, and return
to the clean baseline afterwards. The default home is `~/.kiro/crew`;
`KIROCREW_HOME` overrides it and legacy `~/.kirocrew` installs auto-migrate.

## Verification

Read `.github/workflows/ci.yml` AND `.github/workflows/fast-gate.yml` for the
canonical blocking gates; CI wins over prose. Never weaken or skip a check to
make it green. For test authoring, flakes, isolation or suite speed, load
`writing-tests` and the owning section of
`docs/system-specs/common/testing-conventions.md` before fixing the test.

During iteration, run `python3 scripts/local-gate.py` from the worktree.
`--dry-run` prints the plan, `--base REF` selects a base and `--full` forces both
surfaces. Do not hand-narrow its classification: meta, mixed or unreadable diffs
fall back to the full gate. The script is an ITERATION gate, never a push gate.
Before publication, MUST load [prepare-pr](../prepare-pr/SKILL.md) and run its
complete resolved `gates[]` floor from the base-ref profile. That skill alone owns
PR preparation, local reviewers, history, dispositions and push-to-green steps.

Minimum manual checks, not a replacement for that floor:

```bash
python -m pytest -q
isort --check-only src/kiro_crew test
flake8 src/kiro_crew test
mypy src/kiro_crew/
cd website
npx tsc -b
npx vitest run
cd ..
```

- Rebuild and stage frontend dist BEFORE backend tests that read static assets.
- Full pytest uses `setup.cfg`'s `-n auto --dist loadgroup` and
  `--max-worker-restart=2`. Keep grouping for `xdist_group` serialization. The
  root conftest budgets workers by free memory and concurrent runs; explicit
  `-n <N>` bypasses it. Check host headroom before a full suite. A scoped
  subagent may use serial targeted tests; the parent owns aggregate gates.
- To omit coverage during iteration without dropping the parallel safeguards:

  ```bash
  python -m pytest -q --override-ini="addopts=--ignore=build/private -n auto --dist loadgroup --max-worker-restart=2"
  ```

  Never a bare `--override-ini=addopts=` for the multi-test gate.
- Capture logs under `$KIROCREW_SCRATCH`; check the command's exit code, not a
  pipe's last command. A green `tail` does not mean a green test.
- Reproduce a failure on a clean `origin/main` worktree before calling it
  pre-existing or flaky. A branch-only failure is yours. Never fix a flake with
  a retry, longer sleep, relaxed assertion or skip; use `writing-tests`.
- To rank suspected flakes, mine CI rather than guessing from a local pass:

  ```bash
  gh run list --workflow=ci.yml --limit 250 --json databaseId,conclusion \
    --jq '.[]|select(.conclusion=="failure")|.databaseId' > "$KIROCREW_SCRATCH/ci-ids"
  ```

  Read candidate runs with `gh run view <id> --log-failed`, rank recurring test
  names, then compare each against main. A ratchet failing on feature branches
  may be a true positive. Check Windows timing/process causes as well as Linux.
- Match mypy's version to the dev-group pin in `pyproject.toml`; install the same
  `.[voice]` plus `--group dev` dependencies as CI. A venv with `faiss-cpu`
  makes imports typed where CI sees `Any`, so it is not a CI mirror. On macOS
  run `mypy --platform linux src/kiro_crew`.

A reusable mypy-only environment can be provisioned from the repo root:

```bash
python3 -m venv ~/.kiro/crew/venvs/mypy-ci
~/.kiro/crew/venvs/mypy-ci/bin/pip install -e ".[voice]" --group dev
# Never add faiss. Run from the worktree, which supplies mypy's config.
~/.kiro/crew/venvs/mypy-ci/bin/mypy src/kiro_crew/
```

## The served frontend is built dist

The gateway serves `src/kiro_crew/static/dist/`, not source TSX or a Vite server.
After creating a worktree or changing frontend code, build and clean-stage:

```bash
cd website && npm ci && npm run build && cd ..
rm -rf src/kiro_crew/static/dist && cp -R website/dist src/kiro_crew/static/dist
```

Only remove that generated dist in the assigned worktree; rebuilding restores it.
`make build` also builds, clean-stages and installs. Copying over an old dist
leaves stale content-hashed assets. Dist is gitignored and does not transfer with
a fetch or new worktree. To inspect a minified bundle, search surviving route,
API or label strings, not React component names.

## Flags and isolated preview

Flags belong in the RUNNING instance's `$KIROCREW_HOME/config.json`, not in code.
The live gateway uses the shared home; `dev-backend.sh` uses `.kirocrew-dev/`;
each pod has its own home. Editing production config cannot enable a preview
flag. Check the right instance's flag before blaming the bundle. Config's live
fingerprint cache picks up edits without a restart; live flags persist across
live-worktree switches.

Green gates are the floor. Preview is optional where unit coverage suffices;
when verifying changed UI, load `web-verify` and inspect the rendered change.
Prefer a disposable isolated pod, built first with venv and dist:

```bash
kirocrew pod up <worktree-name> --json
kirocrew pod up <worktree-name> --seed minimal --json
kirocrew pod down <worktree-name>
```

`kirocrew pod --help` is authoritative. `--provision` runs the build on-ramp.
`--seed` names shipped fixtures (`pod scenarios` lists them); an unknown name is
refused, while a path is the sanitized config-only form. Other boot flags:

- `--approval reads|yolo|interactive` persists the pod's mode; omission inherits
  `agent.approval_mode`. No approval flag grants permission to publish or touch live.
- `--crons` enables its scheduler; the default is `--no-crons`.
- `--no-embeddings` avoids GGUF download and vectors in this pod only; search uses
  keyword fallback, useful for ingestion tests. The real home's model is untouched.
- `--ttl` bounds the dashboard token (default `2h`). Re-up a stopped pod to change
  boot flags. `pod token` re-mints a token; `pod url` prints the base URL without it.

Pods need Linux `systemd --user` or macOS `launchd`; run `kirocrew pod install`
once per host. See [pod platform requirements](../../../pod/README.md).
`pod ls`, `status`, `logs`, `provision`, `prune`, `exec` and `api` cover inspection,
provisioning, removal of gone worktrees, in-pod commands and HTTP probes.
Without a service manager, or for a foreground debugger, run `./dev-backend.sh`
from the worktree. It uses an isolated port/home and `PYTHONPATH=src`; Ctrl+C
stops it, and rerunning picks up code changes. Reclaim its process and dev home
yourself. Never use the live gateway instead.

A healthy pod proves only boot. For behavior proof, follow
`docs/guides/worktree-verification-recipes.md`: select the owning endpoint from
the feature map, assert seeded state, run the pod-e2e Playwright harness and
preserve screenshots. Treat commands marked forthcoming as absent until help
lists them, and a session-control HTTP 403 as a gap, not successful proof.

### Agent/MCP specs are a separate isolation axis

`KIROCREW_HOME` isolates config, DB and sessions, NOT machine-wide
`~/.kiro/agents/*.json`. An ephemeral gateway refuses to rewrite that shared
agent home so its MCP paths cannot outlive its venv. The warning is protection,
not a failure: preview uses the real install's specs and MCP servers. A preview
therefore does NOT exercise changes to `mcp-core`, `mcp-cron` or `mcp-computer`.
Use unit tests; any temporary change to the real spec requires explicit user
authorization and restoration afterwards.

Do not bypass this with `KIRO_HOME`: it also moves sessions, settings, skills
and steering, while remaining host-path readers can break resume. It is not yet
a complete preview-isolation switch.

## Review repair and publication authorization

For Kiro Crew PR CI AI comments, MUST load
[prepare-pr: Review repair routing](../prepare-pr/SKILL.md#review-repair-routing)
BEFORE any repair. Follow that canonical model-pinned delegation procedure;
parent self-fixing is not a substitute. This pointer does not change the profile's
read-only local-review gate.

Committing, pushing and opening a PR each require explicit user authorization.
Permission to commit is not permission to push, and green gates grant neither.
A fix-and-push monitoring scope must be explicit; absent it, confirm each push.
Never push to a protected base branch. For authorized publication, follow
prepare-pr's SHA-pinned force-with-lease protocol and `single_commit` handling,
and AGENTS.md's at-most-two-commit limit; never use an implicit lease or interactive
git. No direct merge: hand back
review-ready work; only explicit ship intent permits prepare-pr's auto-merge path.

## Cleanup and comments

Keep scratch, PR bodies, logs and QA media under `$KIROCREW_SCRATCH`, not the
worktree. Capture scripts' gitignored `temp-screenshots/` is also permitted;
evidence is uploaded as attachments, never committed. Before ending:

```bash
git status --porcelain
git diff -- <regenerated-file>
```

Inspect every change. Restore/remove only residue YOU produced, not user changes
or another agent's work. Discarding uncommitted content is unrecoverable: ask if
ownership is unclear. Leave requested uncommitted implementation intact and report
it; cleanup is not permission to commit or discard it. A merged worktree must be
clean, including untracked files, before Dev Fleet's `Prune merged` can reap it.
After merge and cleanup: `git worktree remove ../kirocrew-wt-<name>`.
Add a genuinely needed ignored scratch pattern to `.gitignore` only within scope.

Comments and docstrings state current behavior and the non-obvious why: invariants,
edge cases, units and security constraints. No task log, PR/CR numbers, review-round
markers, incident dates, milestone tags, SHAs or historical narration. Do not
paraphrase adjacent code. Leave `_vendor/` and semantic pragmas (`type: ignore`,
`noqa`, `pragma`, TypeScript/eslint directives) untouched.
