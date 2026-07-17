---
name: meshclaw-sync
description: How to sync fixes from the upstream MeshClaw mainline into this de-Amazoned KiroCrew fork. Use for porting commits, upstream sync, picking fixes from MeshClaw, daily merge, cherry-pick from internal.
always: false
triggers: meshclaw, upstream, sync, port, cherry-pick, mainline, internal fork, de-amazon, deamazon, pick fixes, merge upstream
---
# Syncing fixes from MeshClaw → KiroCrew

KiroCrew is the **public, de-Amazoned fork** of the internal `MeshClaw` package.
The two repos **share no git history** (KiroCrew was created fresh, not cloned),
so you **cannot** `git cherry-pick`/`git merge`/`git apply` between them. Every
fix must be ported **by content**, path- and symbol-mapped, and re-verified.

This skill is the repeatable daily workflow for picking up upstream fixes
without re-introducing the Amazon-internal couplings the fork deliberately
removed.

## Repo locations

The fork bundles **two** upstream packages into one repo, so a full sync tracks
**both**:

- **Backend upstream (internal):** `/Volumes/workplace/MeshClaw/src/MeshClaw`
  (package `mesh_claw`) → fork `src/kiro_crew/`.
- **Frontend upstream (internal):** `/Volumes/workplace/MeshClaw/src/MeshClawWebsite`
  (the React/Vite SPA, package dir `src/`) → fork `website/src/`. The `mesh_claw`
  backend ships only a server-rendered `static/dashboard.html`; the fork's SPA's
  real upstream is **MeshClawWebsite**, a separate package — sync it too or the
  dashboard silently drifts behind.
- **Fork (this repo):** `/Volumes/workplace/kirocrew`
  (package `kiro_crew` + `website/`).

## Step 1 — Find the candidate commits

There is no merge-base. Both upstreams track **`origin/beta-braveheart`** (beta
lands fixes before mainline). Sync is **incremental**: bound the candidate set
by the last-synced tips in `skills/meshclaw-sync/last-synced.txt` (one SHA per
tracked repo/branch). **Scan BOTH repos every run.**

### Backend (`mesh_claw`) — SHA-range incremental
The fork shares the backend's content lineage, so a plain SHA range works:

```bash
cd /Volumes/workplace/MeshClaw/src/MeshClaw
git fetch -q
STATE=/Volumes/workplace/kirocrew/skills/meshclaw-sync/last-synced.txt
BETA=$(grep '^beta ' "$STATE" | awk '{print $2}')
MAIN=$(grep '^mainline ' "$STATE" | awk '{print $2}')
git log --no-merges --oneline "$BETA"..origin/beta-braveheart      # new beta commits
git log --no-merges --oneline "$MAIN"..origin/mainline             # new mainline-only commits
```

A mainline-only commit (not reachable from beta) is also a candidate — check
`git merge-base --is-ancestor <sha> origin/beta-braveheart`.

### Frontend (`MeshClawWebsite`) — CONTENT window, not a clean SHA range
The fork's `website/` is a **diverged partial content-snapshot** taken ~2026-06-02
(it was hand-built, not cloned — some post-snapshot upstream commits are present,
some pre-snapshot ones are absent). So **do NOT** trust a SHA range to mean
"not yet present." Instead:

```bash
cd /Volumes/workplace/MeshClaw/src/MeshClawWebsite
git fetch -q
FE=$(grep '^frontend-beta ' "$STATE" | awk '{print $2}')
# Candidate window = commits since the last triaged frontend tip:
git log --no-merges --oneline "$FE"..origin/beta-braveheart
# First-ever frontend sync (or to re-baseline): use the snapshot DATE as the lower bound
# git log --no-merges --oneline origin/beta-braveheart --since='2026-06-02 00:00'
```

For **every** frontend candidate, decide ALREADY_PRESENT vs MISSING **by content**
(read the fork file under `website/src/`), never by SHA reachability. The fork
pre-image often differs from upstream's (divergence), so apply intent, not a patch.

For each candidate in either repo, get the touched files: `git show --stat <sha>`.

**At the END of every sync, update `last-synced.txt`** for BOTH repos (`beta`,
`mainline`, `frontend-beta`) to the new tips. (The fork was originally cut from
the backend v2.6.0 release merge `72301c08`; that is history — the state file is
the live boundary.)

## Step 2 — Triage each commit (KEEP vs SKIP)

**SKIP** — anything that only touches Amazon-internal subsystems the fork
removed or stubbed. These have no public-fork equivalent:

| Internal subsystem | Why skip |
|---|---|
| Brazil / `Config` / `AUTOSDE.yaml` / toolbox bundler / `npm-pretty-much` | public build is setuptools + npm/Vite |
| Midway / `mwinit` / MCS / Kerberos / federate / AEA tunnel | auth stubs (`midway.py`, `browser/auth.py`, `tunnel/manager.py`) |
| `builder-mcp` / `arcc` / Quip / Taskei / SIM / mimir | removed integrations |
| `writing_review/` + `dashboard/handlers_writing_review.py` | dir ABSENT in fork (deleted subsystem) |
| `mcp_gateway/` + `promptfarm/` | dirs ABSENT in fork |
| `code_reviewer` / `secretary` / `taskkeeper` | deleted; `sync_aim_packages` is a no-op stub (`return None`) |
| CodeArtifact / vendored `claude-agent-acp` | fork uses **public** `npm i -g @agentclientprotocol/claude-agent-acp` |
| Cognito / RUM ids / AEA | removed identity/telemetry |
| **non-KiroACP providers**: `providers/claude_code.py` (`ClaudeCodeProvider`), `providers/bedrock.py` (`BedrockProvider`), `cc_agent.py`, `mirror.py` | **KiroCrew is KiroACP (kiro-cli) ONLY.** These modules + the config `claude_code`/`bedrock` factory branches, the `cc_*`/`bedrock_*` `AgentConfig` fields, and the `provider` enum beyond `["acp"]` were deleted. Any upstream commit confined to them is SKIP/NA_INTERNAL. |

> **NOT a SKIP — `platform/` (CPP seam + Governance):** `src/kiro_crew/platform/`
> and the two-level governance wiring are **fork-side generic core**, not an
> Amazon coupling. A commit touching them (or colliding security/hooks/sandbox/sel
> files) is KEEP-and-reconcile, never SKIP. See "What stays KEPT → `platform/`"
> below for the reconciliation rules.

### Frontend (`MeshClawWebsite`) SKIP rubric

The SPA mirrors the backend's removals. **SKIP** a frontend commit confined to
any of these (confirm ABSENT by `ls website/src/...`):

| Frontend area | Why skip |
|---|---|
| `apps/code-reviewer/`, `apps/mimir/`, `apps/team-manager/`, `apps/writing-review/`, `apps/auto-research/` | builtin-app dirs ABSENT in fork (their backends are absent/stubbed) |
| `pages/SecretaryPage*`, `pages/writing-review/`, `*Secretary*` slices/tests | Secretary/writing-review absent |
| ANY provider-selection UI: a `ProviderPanel`/Provider settings tab, `meshclaw-ui/` Claude-Code panels, `providers/adapters/claude-code.ts`, `providers/adapters/bedrock*`, `providers/modelRegistry.ts`, the `cc-mirror`/`ccAim` "Migrate to Claude Code" surface, Bedrock image/model UI, "agent picker on the Claude Code backend" | **SKIP_NONKIROACP** — fork is kiro-cli only and has **fully removed** the provider selector. `website/src/providers/` now ships ONLY `acp.ts` (+ the context/registry/types/index seam collapsed to the single `'acp'` `ProviderId`); the Settings + EmbedSettings pages have NO Provider tab; `lib/effort.ts` `REASONING_EFFORT_PROVIDERS` is `{'acp'}`. Do NOT re-add a `ProviderPanel`, a `claude_code`/`bedrock` adapter, a provider `<select>`, or `cc_model`/`bedrock_*` config UI. (Porting a commit that *removes* a provider choice from the UI is KEEP — it aligns the SPA to the backend enum `["acp"]`. A commit that adds one is SKIP_NONKIROACP.) The `model_registry.json` `providers.claude_code` map key is the canonical model-id namespace, NOT a selectable provider — keep it verbatim (parity-tested by `test_model_registry_parity.py`). |
| `McpGatewayCard`/`SharedMcpGatewayToggle`/`McpPoolable*` (Shared MCP gateway UI) | `mcp_gateway/` backend ABSENT |
| GitFarm workspace-sync (`SyncPanel`, `/api/workspace-sync`), AIM auto-update toggle | absent/stubbed subsystems |
| Harmony Artifactory artifact browse/share UI (`/api/artifactory/*`, `/api/artifacts/*/publish`) | absent subsystem |
| Artifact **Iterate** button re-show; **Channels** app un-hide; **Board** app re-add | **SKIP_FORKUX** — fork hides/removes these for launch; see "Fork-initiated UX / feature divergences" below + `left-out.md`. A Polly-only VoicePanel sync must RECONCILE (keep the Piper selector), not drop it. |
| `lcars/` theme, Bikini-Bottom/parody theme refactors, RUM telemetry (`rum.ts` is an inert stub) | cosmetic/internal, no generic core fix |

A commit that adds a **generic SPA mechanism** (a surface, a hook, a renderer)
plus an absent-app wiring line is **PARTIAL**: port the generic part, drop the
absent-app hunk (e.g. a `builtinRegistry.ts` change — port only the lines for
apps the fork HAS, like `/file-explorer`).

**Confirm ABSENT by `ls`, not memory** — a commit confined to an absent dir is
SKIP/NA_INTERNAL. A commit that merely *mentions* an internal name in a
docstring/comment is still KEEP — the literal is inert in OSS. **EXCEPTION —
`HEARTBEAT_SAFE_TOOLS` (`slack/gateway.py`) was TRIMMED** (P472753900) to the
generic + kirocrew-core reads (`Read`/`Grep`/`Glob`/`WorkspaceSearch` +
`learn_list`/`cron_list`/`spawn_list`/`spawn_status`/`artifact_*`/
`local_knowledge_search`). The Amazon-internal names (`TaskeiGetTask`,
`ReadInternalWebsites`, `search_arcc`, `recall`, `BrazilBuildAnalyzerTool`,
CRUX/Apollo/SAS/pipeline reads, …) were REMOVED — do NOT re-add them on sync
(SKIP_FORKUX for the allowlist hunk; port the rest as PARTIAL). This reverses the
old "copy the allowlist verbatim" guidance.

**KEEP** — generic core fixes: provider/ACP logic, session/cron/memory, Slack
gateway + dashboard, security controls (deny patterns, redaction, trust
matching), token auth, model handling. These are the daily bread of a sync.

**PARTIAL** — a commit that mixes both. Port only the generic hunks; drop the
internal ones. Examples:
- The upstream `send_channel_challenge` / challenge-and-redirect flow is
  **DELIBERATELY REMOVED in this fork** (Amazon-internal-only posture; external
  Slack messages reach the agent inline). **DROP any upstream hunk that adds or
  modifies `send_channel_challenge`, a `_CHALLENGE_REDIRECT_ENABLED` gate, or
  the per-message challenge block in `slack/events.py::_route_message`.** Do NOT
  port it back. The generic token-claims helpers in `token_auth.py`
  (`generate_token(prompt=..., extra=...)`, `extract_claims_from_token`) are
  retained and may still receive upstream fixes — port those. Also KEEP the
  fork's `get_tunnel_url() if cfg.slack.use_tunnel_url else ""` gate in
  `send_dashboard_link` (tunnel is deliberately opt-in here).
- A new `_install_<x>_agent()` that pulls `builder-mcp` into a dedicated agent
  JSON — **de-Amazon it to `kirocrew-core`-only**, matching how the fork already
  rewrote `_install_research_agent` / `_install_knowledge_agent` (see
  `MIGRATION_PLAN.md`). Port the generic *mechanism* (dedicated agent, dynamic
  `tools`-from-resolved-`mcpServers`, prompt), drop `builder-mcp` from the pull
  tuple, and soften any internal-tool prose in the system prompt. Then **adapt
  the tests** that assert the builder-mcp behavior to the kirocrew-core reality.
- A hunk anchored on a fork stub with no upstream pre-image (e.g. the
  `sync_aim_packages` iterdir loop the fork replaced with `return None`) has
  **no anchor — drop it.**

### Fork-initiated UX / feature divergences (intentional hides — DROP re-adds)

Beyond the Amazon-coupling and provider removals, this fork **deliberately hides
or removes** some upstream product surfaces for the public launch. These are NOT
Amazon couplings and NOT provider issues — they are intentional product choices.
Verdict for a commit that re-shows/re-adds one: **SKIP_FORKUX** (port the rest of
a mixed commit as PARTIAL). Porting a commit that *hides/removes* the surface is
KEEP — it aligns the fork. The durable record + exact mechanisms live in
[`left-out.md`](left-out.md) → "Fork-initiated UX / feature divergences"; guard
the MECHANISM stated there, not just the feature name.

- **Artifact "Iterate" button** — hidden behind `SHOW_ARTIFACT_ITERATE = false`
  in `website/src/pages/ArtifactDetailPage.tsx` (gates the header button, inline
  comment creation, the comments "Submit All" path, and the tips). DROP any
  upstream hunk that re-shows it or widens its render gate; if the upstream
  comment stack (`CommentsSidebar.tsx` `onAskAgent`, `ArtifactPanel.tsx`
  SubmitBar) is ever ported, strip its iterate triggers too. Keep the `iterated`
  lifecycle event + `iterateWithAgent`/`buildPromptForChat` (dormant, for the
  one-line re-enable). (P472753393)
- **Channels app** — hidden from the App Store Browse grid via `"hidden": True`
  on its `_BUILTIN_APPS` entry (`apps/manager.py`) + the `!manifest.hidden` filter
  in `AppsPage.tsx`. This MIRRORS upstream CR-289326017, so it is at parity —
  keep the `hidden` flag + filter; note `defaultEnabled:False` is parity, not the
  guard. (P472750613)
- **Board app** — fully removed (`BoardPage.tsx`, `/board` route, `_BUILTIN_APPS`
  entry, `KanbanSquare` icon, Alt+B shortcut, tests), mirroring CR-289326017. DROP
  a pre-CR upstream hunk that re-adds Board.
- **Voice Piper provider UI** — the fork's `VoicePanel.tsx` adds a Piper/Polly
  selector + `provider`/`piper_*` in `chat_voice.py`; upstream is Polly-only. This
  is fork-AHEAD: when syncing upstream's Polly-only VoicePanel, RECONCILE (keep the
  Piper selector), do not drop it.

### Anti-miss: a NAME is not a verdict (the #1 cause of wrongly-dropped fixes)

Every wrongly-SKIPped commit we have found was dropped by reading the
**commit title, file path, or symbol name** instead of the **diff**. Before any
SKIP/ALREADY_PRESENT, you MUST open the diff and apply these checks:

1. **Is the change GATED on the internal thing, or merely NAMED for it?** A
   commit titled "…for Bedrock" / "fix(secretary): …" / "fix(promptfarm): …"
   often contains a generic, ungated hunk wired into a SHARED choke point.
   Real misses we shipped late:
   - `d7271865` "downscale images for **Bedrock**" → the resize was at the
     single `uploadFiles()` choke point with **no provider gate**; it helps the
     kiro-cli image path too. Was KEEP. (Renamed `*ForBedrock`→`*ForModel`.)
   - `599d6f64` "fix(**promptfarm**): …" → bundled a generic `SkillsLoader`
     cache-invalidation bug (mutators didn't call `_invalidate_iter_cache()`,
     so the dashboard's skill CRUD showed stale lists). PARTIAL: port the
     generic hunk, drop the promptfarm handler.
   - `7342c6e` "fix(**secretary**): …" → first hunk was a guard in the SHARED
     `timeAgo()` helper (ts=0 → "~20602d"), reached by `ArtifactsPage`. PARTIAL.
2. **Does a SHARED helper / choke point change?** `utils/`, `api/client.ts`
   `uploadFiles`, `skills.py` mutators, `security.py`, `hooks.py`, a renderer —
   a change here is almost never confined to the internal feature that occasioned
   it. Grep the fork for the helper's other callers before dropping.
3. **ALREADY_PRESENT means the BEHAVIOR is present, not a similar name.** Read
   the cited fork file and confirm the actual change. And a commit can be
   ALREADY_PRESENT in its production code yet still carry a **missing generic
   test** worth porting (e.g. `dfbc99cd`: feature present, regression-guard
   tests absent → PARTIAL, test-only).
4. **Provenance ≠ port.** A fork commit *citing* an upstream SHA may be a
   `chore(meshclaw-sync)` boundary-advance that records it as SKIPPED, not a
   real port. When auditing history, only a non-chore feat/fix commit citing the
   SHA proves it was actually ported.

Genuine SKIP still holds when the change is **truly confined** to something the
fork lacks: an endpoint that 404s (`/api/mcp-gateway/*`, `/api/artifactory/*`), a
page/dir that's absent (`SecretaryPage`, `apps/auto-research/`), or a hunk whose
only effect re-adds a forbidden coupling (`e62422ae`'s `_INTERNAL_READ_ALLOWLIST`
existed solely to read `~/.midway/cookie` for the absent `scanner_sync`).

If unsure whether a fix is already in the fork, check by **content**, not SHA:
read the upstream diff, then read the corresponding `kiro_crew` file. Verdicts:
ALREADY_PRESENT / MISSING / PARTIAL / N/A_INTERNAL.

**Scaling the triage:** a triage+verify Workflow (one analyzer + one verifier
per commit) is the right tool for a big batch (dozens of candidates). For a
**small** candidate set (≲8), triaging by reading each diff directly is faster
and more reliable — a `{schema}` Workflow can flake (the analyzer finishes its
reasoning but never calls `StructuredOutput`, so the agent returns empty). If
that happens, don't re-spawn blindly; fall back to reading the diffs. Either
way the verdict MUST be confirmed against fork **content**, not the agent's
say-so.

## Step 3 — Port a KEEP commit

Path map — **backend:** `src/mesh_claw/X` → `src/kiro_crew/X`. **frontend:**
`MeshClawWebsite` `src/X` → fork `website/src/X` (tests: upstream `src/test/` or
`integration/` → fork `website/src/test/` or `website/integration/` — check which
exists). Symbol/string map (apply everywhere, including comments and test bodies):

```
mesh_claw → kiro_crew      MeshClaw → KiroCrew      meshclaw → kirocrew
MESHCLAW_ → KIROCREW_      .meshclaw → .kirocrew    meshclaw-lite → kirocrew-lite
_meshclaw_managed → _kirocrew_managed     CLI `meshclaw` → `kirocrew`
# frontend-specific:
meshclaw-ui → kirocrew-ui  MeshClawNavBridge → KiroCrewNavBridge
source: 'meshclaw' → 'kirocrew'   /api/config/meshclaw → /api/config/kirocrew
# KEEP verbatim (load-bearing literals, NOT brand tokens):
'mc-*' localStorage/postMessage keys (mc-nav, mc-unread-slots, mc-auth-expired),
the 'mc_token_' cookie prefix, and inert tool-name allowlist strings.
```

**Frontend divergence:** the fork's `website/` diverged from a ~2026-06-02
snapshot, so a hot file (e.g. `ChatPage.tsx`) is often hundreds of lines off
upstream. Apply the *intent* by content; for big multi-file frontend features,
port files in chronological commit order so later hunks land on earlier context.

**Source hunks:** read the fork file around each hunk first — the fork's
pre-image often differs from upstream's (de-Amazon edits, prior renames), so
apply the *intent*, not a literal patch. When the context doesn't match, find
the semantically-equivalent location and edit there.

**Test files:** if the fork's test file is byte-identical to upstream's
pre-image (modulo the rename), it's safe to regenerate from the post-image:

```bash
git -C /Volumes/workplace/MeshClaw/src/MeshClaw show <sha>:test/test_x.py \
  | sed 's/mesh_claw/kiro_crew/g; s/MeshClaw/KiroCrew/g; s/meshclaw/kirocrew/g; s/MESHCLAW/KIROCREW/g' \
  > test/test_x.py
```

Otherwise (the fork diverged — e.g. removed an internal-only test, changed an
`ada credentials`→`aws sso` string) **apply only the added hunks**, don't
clobber the fork's divergence.

**New data files** (e.g. `model_registry.json`): add them to **all three**
packaging manifests or they won't ship:
- `setup.cfg` `[options.package_data]`
- `packaging/kirocrew-backend.spec` (the explicit data-file list — separate
  from setup.cfg; the PyInstaller DMG misses files not listed here)
- the frontend copy under `website/src/` if the frontend reads it (+ a parity
  test guarding drift)

## Step 4 — Verify (do NOT trust grep)

Brand renames and "is this already fixed" judgments have burned us before by
relying on grep alone. **Run the tests.**

```bash
# BACKEND per-fix: run the touched test files (override the hardcoded --cov in setup.cfg)
python -m pytest test/test_x.py --override-ini="addopts=" -p no:cacheprovider -q
flake8 src/kiro_crew/<files> test/<files>      # the real gate (NOT black --check)

# FRONTEND per-fix: typecheck + the touched vitest files (from website/)
cd website
npx tsc -b                                     # project refs — the real typecheck (NOT --noEmit)
npx vitest run src/test/<File>.test.tsx        # or integration/<File>.integration.test.tsx
```

Gotchas:
- `setup.cfg` hardcodes `--cov` in `addopts` — always override for fast runs.
- This machine runs **free-threaded CPython 3.13t**; prefix `PYTHON_GIL=0` to
  silence the GIL-re-enable warning. Async tests need `@pytest.mark.asyncio`.
- `tsc -b` (not `--noEmit`) is the real frontend typecheck. Do NOT run
  `prettier`/`eslint --fix` to "clean up" — like black, they churn untouched
  code and are not the gate. A frontend port that adds an import MUST ensure the
  target exists in the fork (port the prerequisite helper in the same wave, e.g.
  `utils/monacoLocal.ts` for the Monaco-local commits).
- **The installed `black` (25.1.0) is NEWER than the repo's formatter** — it
  wants to reformat ~300 untouched files AND upstream's own post-image fails it
  too. So `black --check` is NOT the gate. **Do not run black to "fix"
  anything.** The real gate is **flake8**, which **ignores E501** (line length)
  — so the long verbatim-copied lines you port are fine. Verify your edits are
  clean by: (a) `flake8 <files>`, (b) a `>100`-char scan of *only your added
  lines*, (c) comparing black-`--diff` `+`-line counts main-vs-yours per
  file (equal ⇒ your edits add no new churn). `apps/builtins/*` also ignores E128.
- **The fork's flake8 can be STRICTER than upstream's** — a faithful
  COPY-not-rewrite port can pass upstream yet fail the fork gate. Seen: **F824**
  (`nonlocal x` where `x` is only read, never rebound in that scope) flagged on a
  verbatim-copied closure whose upstream repo didn't enable F824. Fix the ported
  hunk (drop the read-only `nonlocal`), don't disable the check — always run
  `flake8 <your files>` on the post-port image, never assume "upstream passed so
  this passes."
- **isort failures may be pre-existing** — if `isort --check` flags a file you
  only added a field/kwarg to (no import change), confirm it fails on `main`
  too (`git show main:<f> | isort --check -`) and leave it; don't churn.
- **Regenerate-from-pre-image trick** for a test/spec file the commit heavily
  rewrites: if the fork file is byte-identical to the upstream PRE-image (modulo
  the rename), it is safe to regenerate wholesale from the POST-image —
  `diff <(git show <sha>^:path | sed '<rename map>') fork/path` == empty proves
  it, then `git show <sha>:path | sed '<rename map>' > fork/path`. The rename
  map: `s/mesh_claw/kiro_crew/g; s/MeshClaw/KiroCrew/g; s/MESHCLAW/KIROCREW/g; s/meshclaw/kirocrew/g`.
  Watch for load-bearing literals the broad map also rewrites correctly
  (e.g. `meshclaw browse *` → `kirocrew browse *`, `mcp__meshclaw-core__` →
  `mcp__kirocrew-core__`) — grep the result for residual `mesh` tokens.
- **Insert big verbatim blocks with a Python splice**, not Edit, when the block
  is large and clean (e.g. a new function or test class) — extract via
  `git show <sha>:path | awk/sed`, map symbols, then `str.replace(anchor, block
  + "\n\n\n" + anchor, 1)` against a unique anchor. Re-check blank-line spacing
  (flake8 E301/E303) after splicing next to a class member.

## Step 5 — Commit (one fix per commit)

Commit each ported fix separately, citing the upstream SHA in the body so the
provenance is traceable across the history-less boundary:

```
fix(<scope>): <summary>

<what + why>. <Any internal hunks deliberately skipped and why.>

Ported by content from MeshClaw[Website] upstream <short-sha>:
https://code.amazon.com/packages/<Pkg>/commits/<FULL-sha>
Upstream-CR: https://code.amazon.com/reviews/CR-<id>
Task (<Mesh-NNNN>): https://taskei.amazon.dev/tasks/<Mesh-NNNN>
```

### MANDATORY: every reference is a full clickable link

In **commit messages, the PR description, AND the PR provenance comment**, never
write a bare id — always the full `https://` URL. This is non-negotiable; a
reviewer must be one click from every source.

| Reference | Link format |
|---|---|
| upstream commit (internal) | `https://code.amazon.com/packages/<Pkg>/commits/<FULL-40-char-sha>` (NOT the short sha — the full SHA; `Pkg` = `MeshClaw` or `MeshClawWebsite`) |
| upstream CR (internal) | `https://code.amazon.com/reviews/CR-<id>` |
| this fork's PR (GitHub) | `https://github.com/kirodotdev/KiroCrew/pull/<number>` |
| Taskei task | `https://taskei.amazon.dev/tasks/<Mesh-NNNN>` |
| SIM issue (if a commit cites one) | `https://sim.amazon.com/issues/<Mesh-NNNN>` |
| upstream package commit browser (internal) | `https://code.amazon.com/packages/<Pkg>/commits/mainline` |

Pull the upstream CR / Task / SIM trailers straight from the source commit
(`git -C <upstream> log -1 --format=%b <sha>` — they appear as `cr:` / `Task:` /
`Issue:` / `SIM:` lines) and carry them through verbatim as links. Use the FULL
40-char SHA in commit-browser URLs (a short sha 404s less reliably and isn't
copy-paste stable).

Do **not** `git commit`/`push` unless the user asks; push needs separate
explicit approval.

## Step 6 — Final de-Amazon audit before pushing

Scan the cumulative ported diff for couplings that slipped in (LIVE code, not
comments):

```bash
git diff origin/main...HEAD -- 'src/**/*.py' 'src/**/*.json' \
  | grep -iE "^\+" | grep -ivE "^\+\+\+" \
  | grep -iE "midway|mwinit|mcs|kerberos|federate|aea|cognito|codeartifact|builder-mcp|arcc|quip|taskei|brazil|toolbox"
```

Expected: only **comments** and the pre-existing inert `allowed_prefixes`
tuple in `acp/client.py` (`b"arcc"`, `b"builder"`, `b"aim"` — harmless, those
binaries don't exist in OSS). Any **live** new usage is a bug — drop it.

Note: `global.anthropic.claude-*` model ids and `Bedrock` mentions are **not**
couplings — that's the public `claude-agent-acp` adapter's model-id form, used
pre-fork. Keep them.

## What stays KEPT in the fork (never strip these during a sync)

Generic security controls are NOT Amazon-specific — keep them: AKIA/ASIA
credential redaction, destructive-command deny patterns, `~/.aws`/`~/.ssh`
sensitive-path blocking, the SEL HMAC audit log, command-trust matching.

And keep the OSS-flipped defaults: provider **`acp` (kiro-cli, the only
provider)**, Ollama public embeddings, Piper TTS default, Slack enterprise
default-open, lazy boto3/transcribe imports (STT-only; the `[aws]`/Bedrock
extra was removed with the providers).

### `platform/` (CPP seam + Governance) — fork-side core, sync with care

`src/kiro_crew/platform/` (the Composed Platform Providers seam) and the
**two-level Governance model** (`platform/governance.py`,
`platform/governance_profiles.py`, the `security._SENSITIVE_HOME_DIRS` keystone,
the `hooks.on_tool_call` governance check, and the `governance_permits`
chokepoints in `mcp_cron`/`subagent`/`mcp_core`/`sandbox`) are **generic core
security infrastructure that lives in the public fork**. Treat them like the
other KEPT controls. Specifically, when triaging/porting:

- **Do NOT strip or weaken** them during a sync, and do NOT treat them as
  Amazon couplings (they are the opposite — the seam that lets the Amazon
  companion exist WITHOUT the core importing it).
- An upstream commit that touches `security.py`'s sensitive-path /
  deny-pattern / bash-command matchers, `hooks.on_tool_call`, `sandbox.wrap_argv`,
  `sel.py`, or any `mcp_cron`/`subagent`/`mcp_core` tool gate **collides with the
  governance wiring** — port the *fix*, but re-apply it on top of the governance
  edits (the `_governance_denial` call, the threaded `session_key`/`agent` kwargs,
  the `_WRITE_CMDS`/extraction matchers, the `assert_*` boot floors). Verify the
  `test_governance_*.py` suites still pass after porting (Step 4).
- `CONTRACT_VERSION` (`platform/context.py`) is a lockstep marker — never lower it
  on a sync; bump only when a `PlatformContext` field/interface genuinely changes.
- The governance trust-root files (`~/.kirocrew/security_policy.json`,
  `profiles/`, `admission_policy.json`) MUST stay in `_SENSITIVE_HOME_DIRS` — if a
  sync reorders or rewrites that list, re-add them (the keystone; a missing entry
  fails the boot integrity check and the `test_governance_self_protection.py`
  suite).
- Upstream (internal MeshClaw) may carry its OWN, divergent governance/provider
  code — those are **SKIP_NONKIROACP / internal-only** unless the commit is a
  generic fix to the seam itself. When unsure, run the `test_governance_*` +
  `test_cpp_wiring_*` suites and treat a governance behavior change as
  spec-governed: cross-check `docs/system-specs/modules/governance.md` +
  `platform-context.md` before porting.

## Step 7 — Build, verify, and ship (used by the recurring auto-sync)

After porting + the Step 4 verify + the Step 6 audit, a full sync run finishes
with a build and a PR:

1. **Rebuild both macOS DMGs** (the ported backend must ship). Dual-arch from
   one Apple-Silicon Mac via Rosetta — full recipe in `docs/DESKTOP_APP.md`:
   ```bash
   cd website && npm install && npm run build && cp -R dist ../src/kiro_crew/static/dist && cd ..
   SKIP_FRONTEND=1 PYTHON=$PWD/.venv/bin/python bash packaging/build-desktop.sh   # arm64
   # x86_64: arch -x86_64 .venv-x86 (system py3 universal2) + electron-builder --x64,
   #   then RESTORE the arm64 backend into website/electron/backend-dist.
   ```
   Mount-verify each DMG carries the matching backend arch
   (`file …/Resources/backend-dist/kirocrew-backend/kirocrew-backend`) — a
   mismatch crashes on launch. Keep electron `package.json` version at `0.1.0`;
   `rm` stale DMGs (`dist/` is not auto-cleaned). DMGs are gitignored artifacts.
   - `.venv`/`.venv-x86` only carry runtime deps from the editable install —
     `pip install pyinstaller` into each before building.

2. **Commit** each fix separately (Step 5 format) and **update
   `skills/meshclaw-sync/last-synced.txt`** to the new branch tips in the final
   commit.

3. **Open a PR against `main`** on GitHub:
   ```bash
   # Push the sync branch, then open a PR:
   git push -u origin HEAD
   gh pr create --base main --title "[KiroCrew] MeshClaw beta sync <date>: N commits ported" \
     --body "$(cat <<'EOF'
   ## Summary
   ...per-commit triage table here...
   EOF
   )"
   ```
   The PR **title** names the batch (e.g. `[KiroCrew] MeshClaw beta sync
   <date>: N commits ported`). When the batch spans both repos, say so (e.g.
   `... dual-repo sync: N backend + M frontend ported`). The **description MUST
   list, per commit, both what was synced AND what was left out** — every
   KEEP/PARTIAL with its upstream SHA (note backend vs frontend) + one-line
   summary, and every SKIP/NA_INTERNAL/deferred with the reason (writing_review
   absent, builder-mcp internal, mcp_gateway/secretary/auto-research absent,
   SKIP_NONKIROACP, etc.). **Every SHA / CR / Task / SIM id in the description
   and the comment MUST be a full clickable `https://` link** — use the formats
   in the Step 5 link table (keep the PR description concise; put the EXHAUSTIVE
   per-commit provenance — every upstream commit/CR/task as a link — in a PR
   **comment**, which has no size cap; post it with
   `gh pr comment <number> --body "..."`). Provenance across the history-less
   boundary lives entirely in this description + comment.

   Origin = `https://github.com/kirodotdev/KiroCrew.git`. Per the global
   git rule, `commit`/`push`/PR need explicit user authorization — the recurring
   auto-sync cron job **is** that standing authorization; a manual invocation is
   not (ask first).

## Workflow harness (`.claude/workflows/meshclaw-sync.js`)

This skill is also operationalized as a multi-agent Workflow script at
`.claude/workflows/meshclaw-sync.js`, invocable as
`Workflow({name: "meshclaw-sync", args: {mode}})`. The script **defers to this
SKILL** — every agent it spawns reads this file first and the skill wins on any
conflict. It fans out one analyzer + one skeptic per candidate (the adversarial
triage), then gates mutation behind `mode`: `triage` (default, read-only report)
→ `port` (port + verify, no PR) → `full` (+ build DMGs + commit + PR). Paths
come from `args` (`workspaceFork`/`upstreamBackend`/`upstreamFrontend`,
defaulting to the locations above) so it runs on any checkout. The script
parallelizes + enforces the pipeline shape; this skill remains the source of
truth for every verdict, the de-Amazon rubric, and the surgical porting.

## Recurring auto-sync (cron)

A durable cron job runs this whole skill every 6 hours (scan → triage+verify →
port → build → commit → PR). It is the standing authorization for commit/push/PR.
It **scans BOTH repos** (`mesh_claw` backend + `MeshClawWebsite` frontend) each
run. If a run finds **zero** new candidates across both, it does nothing and
exits (no empty commit, no PR). If it hits an ambiguous large/PARTIAL commit it
can't confidently de-Amazon, it ports the clean KEEPs, leaves the ambiguous one
un-ported, and **notes it in the PR description** as deferred-for-human-review
rather than guessing.

## Periodic re-audit of the SKIP backlog

SKIP is reversible — a commit dropped on a name, or one whose owner later clears
an internal feature for external release (e.g. `instances/`), should be caught
without waiting for someone to notice. Periodically (and any time the SKIP rubric
or an absent/stubbed subsystem changes) **re-audit every left-out commit by
content**:

```bash
# Reconstruct the full left-out set across all batches: upstream commits in the
# synced window with NO real (non-chore) fork port commit citing them.
#   window = <first-ever beta sha>..<current beta boundary>
# For each, open the diff and apply the "Anti-miss" checks above. A
# triage+verify Workflow with a SKEPTICAL confirm pass (high bar to add to a
# shipped PR) is the right tool — bias toward rescuing a wrongly-dropped GENERIC
# hunk, but uphold a SKIP that is truly confined to an absent endpoint/dir.
```

Rescues are almost always **PARTIAL** (the headline was correctly internal; a
bundled generic hunk was the miss). Port the generic part with de-Amazon renames
(`*ForBedrock`→`*ForModel`), drop the internal part, and add/adapt the test.
Record the re-audit outcome in the `kirocrew-meshclaw-sync` memory so the backlog
state is durable.
