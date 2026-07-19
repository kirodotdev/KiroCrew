# MeshClaw → KiroCrew — Left-Out Commit Provenance

> Companion to [`last-synced.txt`](./last-synced.txt). That file records the sync
> **boundary** (what has been triaged); this file records, per commit, **what was
> deliberately NOT ported and why**. Compiled 2026-06-14 from the shipped batch
> CR descriptions + their "Full per-commit provenance" comments + sync commit
> bodies.

## What this is

This repo's current history was produced by two separate events, each of which
"left out" commits. This document records **both** populations:

1. **The MeshClaw→KiroCrew content syncs** (batches 1–18) — the bulk of this doc.
   The fork shares **no git history** with its upstreams
   ([MeshClaw](https://code.amazon.com/packages/MeshClaw) backend +
   [MeshClawWebsite](https://code.amazon.com/packages/MeshClawWebsite) frontend);
   every fix is ported **by content**, so a "left-out" commit is one a sync batch
   triaged and consciously declined to port.
2. **The 2026-06-14 history replacement** — the original 49-commit KiroCrew
   package history was wiped and replaced with the de-Amazoned fork's history.
   Those 49 commits are enumerated in
   [The 49 wiped original-KiroCrew commits](#the-49-wiped-original-kirocrew-commits).

This is the consolidated record so the decisions (and their reasons) survive even
if the source CRs or the backup branch are pruned.

### Artifacts-page exact-mirror batch (2026-07-17) — resolves several standing DEFERs

A dedicated "exact-mirror the artifacts page" batch (branch
`sync/artifacts-mirror-2026-07-17`) deliberately REVERSED a cluster of previously
deferred/skipped decisions, because the user asked for an exact mirror of the
MeshClaw artifacts page (with two carve-outs — see below). What changed:

- **Durable local-comment subsystem — NOW PORTED** (was DEFER/SKIP_INTERNAL:
  `475146ca`, `86315dc`, `de64d07b`, `0000f561`/`f746d60`, `4685d34`, `a6b1fc7`,
  the `affffcff` base). The backend `ArtifactComment` store (comments.json
  sidecar + anchor rescan + lifecycle events) and the full FE stack
  (`CommentsSidebar`, `CommentThreadPopover`, `InlineCommentOverlay`,
  `FileArtifactComments`, `useCommentBridge`, `artifactCommentsSync`,
  comment-bridge `widgetSrcdoc`) landed. Only the **remote-comment sync half**
  (`merge_remote_comments` / `RemoteComment` — Chorus) stays STRIPPED.
- **Masonry Artifacts page — NOW PORTED** (was `cd6730f` DEFER "still open"):
  masonry gallery + grid/table toggle + gallery folder browse, with the new
  `@virtuoso.dev/masonry` dep. The Harmony Artifactory remote-browse surfaces on
  that page (`RemoteArtifactCard`, shared/public/mine sections) stay STRIPPED.
- **Clickable artifact refs + Artifacts side-panel — NOW PORTED** (`4685d34`):
  `ArtifactBody`/`ArtifactPanel` extraction + `MarkdownRenderer` `onArtifactOpen`.
- **Publish/sharing — PORTED BEHIND A SEAM (not a live provider):** the
  vendor-neutral `publish_provider` ABC + registry + `publish_sync` orchestration
  + `ArtifactPublication`/`ForkMetadata` models + publish handlers +
  `ArtifactSharePanel` are now generic core. Concrete **Harmony Artifactory +
  Chorus** providers stay OUT (companion-only) — registered via the new CPP
  `PublishRegistry` seam; whether/where publish is allowed is gated by the new
  `capabilities.publish` governance scope + `publish.allowed_destinations` config.
  Public edition: registry empty → publishing unavailable (503), no UI.

**Still SKIP (unchanged) — the two carve-outs:** the Iterate button stays hidden
(`SHOW_ARTIFACT_ITERATE=false`, fork-UX); and everything on **Harmony Artifactory
browse + Chorus remote** (`RemoteArtifactCard`, `RemoteArtifactDetailPage`,
`UpstreamSyncBanner`, remote comment sync, `artifactory_client/provider.py`)
stays absent as internal infra.

### Independent coverage audit (2026-06-15)

The lists below are sourced from the sync *records* (CRs + commit bodies). To
check whether those records have **silent gaps** — upstream commits that were
never triaged at all, appearing in neither the ported nor the left-out set — an
independent package-diff audit was run against the actual upstream repos:

- **Method:** enumerate every non-merge commit in the upstream universe
  (`MeshClaw` backend `9f5bb6b6..570a9ccf` = 116 commits; `MeshClawWebsite`
  frontend `2026-06-02 snapshot..ecc6e5a5` = 126 commits), then check each
  upstream SHA's 7-char prefix against the union of all ported provenance (fork
  commit bodies) + all left-out records (this doc + `last-synced.txt`).
- **Result: 0 unaccounted, both repos.** Every one of the 242 in-window upstream
  commits is accounted for as either ported or a documented left-out. (Validated:
  no intra-universe 7-prefix collisions, so the short-SHA match is unambiguous.)

So this record is **comprehensive over the full batch-1→18 window**, not just over
what the CRs happened to write down. The residual honesty caveats below
(pre-snapshot, batch-17, the 1 unnamed batch-18 DEFER) stand — they concern
*detail granularity*, not missing commits.

**Un-triaged tail (NOT gaps — newer than the last sync):** since the batch-18
boundary, upstream has moved on. As of 2026-06-15 there are **7 new backend**
commits (`570a9ccf..5de9411e`) and **8 new frontend** commits
(`ecc6e5a5..314a69e`) that no batch has triaged yet — they postdate batch-18.
These are the next sync's input, not omissions. (e.g. backend: Code Review Sage
built-in app, folder emoji, artifact comments, `subagent_auto_max` config;
frontend: ~~folder icon picker~~ (PORTED 2026-07-10 batch-27 follow-up),
paste-token chips, artifact-comments CX, masonry guard.)

### Source of truth & a caveat on completeness

Per the meshclaw-sync skill, exhaustive per-commit provenance was meant to live
in a **CR comment** on each batch CR. In practice that comment was posted for
some batches and **not** others:

| Batch | Own CR | Provenance comment present? | Left-out source used here |
|---|---|---|---|
| 1–3 | [CR-280626986](https://code.amazon.com/reviews/CR-280626986) | ❌ empty | CR description + sync commit body |
| 4–7 | [CR-280672548](https://code.amazon.com/reviews/CR-280672548) | ❌ empty | CR description + boundary commits |
| 8 | [CR-280853988](https://code.amazon.com/reviews/CR-280853988) | ❌ empty | CR description ("Left out: None") |
| 9 | _(none — SKIP-only boundary advance, commit `b62394c`)_ | n/a | sync commit body |
| 10–11 | [CR-280980741](https://code.amazon.com/reviews/CR-280980741) | ✅ 21 KB comment | **full per-commit table** |
| 12 | [CR-281070110](https://code.amazon.com/reviews/CR-281070110) | ❌ (only an AutoSDE bot comment) | CR description + boundary commit |
| 13 | [CR-281120970](https://code.amazon.com/reviews/CR-281120970) | ✅ comment | full per-commit table |
| 14 | [CR-281228622](https://code.amazon.com/reviews/CR-281228622) | — (no-ports batch) | boundary commit `7b1530a` |
| 15 | [CR-281319232](https://code.amazon.com/reviews/CR-281319232) | ✅ comment | nothing left out (8/8 ported) |
| 16 | [CR-281392951](https://code.amazon.com/reviews/CR-281392951) | ✅ comment | full per-commit table |
| 17 | [CR-281529650](https://code.amazon.com/reviews/CR-281529650) | **ABANDONED / unmerged** | re-ported in batch-18 |
| 18 | [CR-281902310](https://code.amazon.com/reviews/CR-281902310) | ❌ empty | CR description + boundary file |

**Gaps to be honest about:**
- **Pre-2026-06-02 is undocumented.** The fork began as a hand-built content
  snapshot on ~2026-06-02; whatever upstream history predates that snapshot was
  never enumerated. This document starts at the first post-snapshot sync (batch 1–3).
- **Batches 12 and 18** intended an exhaustive comment that was never posted, so
  their left-out lists here are the description-level summaries (high-signal, but
  not guaranteed to name every sub-hunk).
- **Batch 17** was abandoned before merge; its candidates were re-triaged from
  scratch in batch 18, so it has no independent left-out record.

## Verdict categories

| Verdict | Meaning |
|---|---|
| **SKIP_INTERNAL** | Touches a subsystem **absent** from the de-Amazoned fork (mcp_gateway, secretary, writing-review, auto-research, promptfarm, GitFarm/Cloud-Sync, AIM, team_manager, RUM telemetry, Harmony Artifactory, LCARS/Bikini-Bottom themes, etc.). 100% confined — nothing generic to salvage. |
| **SKIP_NONKIROACP** | Specific to a deleted LLM provider (Bedrock / Claude Code). Meaningless under the fork's single-provider `agent.provider` enum `["acp"]`. |
| **SKIP** | Other deliberate skip (e.g. Brazil-`Config`-only hunk that the setuptools build ignores; a Midway-stub-only change; a generic helper with zero fork consumers per the anti-miss check). |
| **SKIP_FORKUX** | A fork-initiated **intentional UX / product divergence** — an upstream surface the public fork deliberately hides or removes for launch (e.g. the artifact **Iterate** button, the **Channels** app store listing, the **Board** app). NOT an Amazon coupling; a product choice. Porting a commit that *hides/removes* the surface = KEEP (it aligns the fork); a commit that *re-shows/re-adds* it = SKIP_FORKUX (or PARTIAL if mixed). See SKILL.md Step 2 → "Fork-initiated UX / feature divergences". |
| **ALREADY_PRESENT** | The fork already carries the change's post-image (often a restore/revert of a regression the fork never had). Porting = no-op. |
| **DEFER** | Technically portable but deliberately held for a separate scoped change or human review. **See the [Human-decision section](#human-decision-items-defer--flagged) — these are the live ones.** |
| **NA_INTERNAL** | Early-batch label equivalent to SKIP_INTERNAL (Amazon-internal-only dependency). |

---

## Human-decision items (DEFER / flagged)

**These are the commits that were NOT skipped on a clear rule — they need a
human call.** Everything in the [SKIP catalogue below](#full-skip-catalogue) is
mechanically out of scope (absent subsystem / wrong provider / already present);
the items here are portable-in-principle and were held back, or flagged as
judgement calls.

### Still open (pending a decision)

| Upstream SHA | Repo | Batch | Status | What it is / why it needs a human |
|---|---|---|---|---|
| [`cd6730f`](https://code.amazon.com/packages/MeshClawWebsite/commits/cd6730f) | frontend | 18 | **DEFER — open** | Artifacts page **masonry layout** rewrite: +572 lines on a hard-diverged 253-line page + a new `@virtuoso.dev/masonry` dependency, with **zero fix/security value** (pure layout). Batch-17's human review also deferred it. Decision needed: adopt the masonry rewrite (and the new dep) onto the fork's diverged Artifacts page, or drop permanently. |
| [`6181474a`](https://code.amazon.com/packages/MeshClaw/commits/6181474a) | backend | 18 | **SKIP_INTERNAL — flagged** | SharePoint/Loop **redaction carve-out**. Skipped because it targets Amazon-corp M365 hosts only (precedent: `e62422ae`), but it was **explicitly flagged for human review** rather than cleanly out of scope — a reviewer should confirm the fork wants no SharePoint/Loop redaction path. |
| [`08e28b8a`](https://code.amazon.com/packages/MeshClawWebsite/commits/08e28b8a7648ddfb739bb33522c1d8043b826401) | frontend | 40 | **DEFER — blocked on PR #14** | Electron shell redesign (native-tab shell → SidePanel + BaseWindow menu rework). The new `SidePanel.tsx` hard-imports `src/components/ArtifactPanel.tsx` — a file the fork does NOT have yet; it is added by still-OPEN [fork PR #14](https://github.com/kirodotdev/KiroCrew/pull/14) (artifacts-mirror). Porting now breaks `tsc -b` or collides with that PR; fork pre-images are also heavily diverged (main.js/App.tsx/ChatPage/index.css hundreds of diff-lines vs upstream pre-image). PARTIAL-extracting the real BaseWindow reload/devtools no-op fix was considered and REJECTED (entangled with redesign-only code: positionTrafficLights, zoomItem, the Cmd+Shift+R accelerator reshuffle; would pre-mutate the viewMenu region the post-PR-#14 full port must rewrite). **Note: fork Cmd+R / DevTools menu items likely no-op today on the BaseWindow shell — schedule this promptly as a dedicated batch immediately after PR #14 merges.** All other deps (useIsMobile, useListboxKeyboard, useTouchedFiles, safeSetItem, extractChatLinks, countLines, framer-motion@^12) are already in the fork. No upstream revert. |

### Inherited-upstream findings (fix in BOTH repos — do NOT diverge the fork unilaterally)

PR #88 (batch-33/34) drew four Codex findings whose flagged code is byte-identical to / a faithful port of the current MeshClaw beta. They are **latent gaps in upstream**, not fork regressions. Fixing them fork-only would diverge from upstream and be re-conflicted by the next sync, so they are tracked here to be fixed **upstream-first**, then flow back via a normal sync batch.

| Upstream origin | Fork file:sym | Sev | Finding | Fix shape (upstream-first) |
|---|---|---|---|---|
| [`7cc8217d`](https://code.amazon.com/packages/MeshClaw/commits/7cc8217d) | `security.py` `StreamRedactor.feed` | HIGH | After a >4096-char credential-anchored tail is dropped (buffer cleared + `_REDACTED_CREDENTIAL_TAG`), a later chunk of the SAME token streams raw — the discard state isn't retained across `feed()` calls. | Add a discard-until-delimiter state flag retained across chunks; test a chunked token exceeding `_STREAM_HOLDBACK_JWT_MAX`. |
| [`56fbb774`](https://code.amazon.com/packages/MeshClaw/commits/56fbb774) | `mcp_gateway/gatewayd.py` `_apply_claim` | HIGH | Any same-UID process can submit a `claim` and replace another connection's session identity; ancestor PID is client-supplied. This is the documented uid-`0700`-socket trust model, ported verbatim. | Authenticate claims with a gateway-only capability (or verify peer is the gateway process) and verify ancestry server-side. Design change — upstream first. |
| [`82560fb7`](https://code.amazon.com/packages/MeshClaw/commits/82560fb7) | `security.py` `_BASH_EXFIL_PATTERNS` | MED | `curl -Ffile=@secret` (no space after `-F`) and `curl --form=file=@secret` (equals form) bypass the `-F *=@`/`--form *=@` globs, which require a space. Present verbatim upstream. | Add no-space + equals glob variants, or tokenize curl args structurally; cover with tests. |
| [`a7736388`](https://code.amazon.com/packages/MeshClaw/commits/a7736388) | `context.py` `build_session_replay` | MED | A single newest message larger than the (window-scaled) `replay_budget` is emitted whole (the `and lines` guard admits the first line unconditionally), dominating a small model's context. Faithful port. | Truncate the first oversized line to `replay_budget`. |

PR #92 (batch-35) drew a further set of Codex findings, again all byte-identical / faithful ports of the current MeshClaw beta (Windows shim `eaf62582`, Talos `383eae45`/`1b0585bb`, frontend `340cfa9`). Same disposition — fix upstream-first, flow back via sync.

> **Batch-36 update:** the batch-36 addendum folded three upstream fixes into the same PR that partially address this table: `f9b37d8c` makes the macOS-26 branch use sandbox-exec (that host is no longer forced onto the fail-closed path), `ff921945` stops a single app's fail-closed sandbox error from crashing gateway boot, and `a6c1680e` adds env-scrub on the agent-spawn chokepoint. The **Windows** row below is NOT resolved — a fresh Windows install still has no sandbox backend, so kiro-cli chat needs `sandbox_allow_unsandboxed_exec=true` until a Windows backend/setup-default lands. The blocking-`taskkill`/`icacls` rows remain Windows-only-open.

| Upstream origin | Fork file:sym | Sev | Finding | Fix shape (upstream-first) |
|---|---|---|---|---|
| [`eaf62582`](https://code.amazon.com/packages/MeshClaw/commits/eaf625828a1757fc2c649a9b43a67778b14715ae) | `sandbox.py` `wrap_argv` + `detect_backend` | HIGH | Windows has no sandbox backend (`detect_backend`->`none`), and the fail-closed `_allow_unsandboxed_exec` gate (from `383eae45`) defaults False, so a fresh Windows install cannot spawn `kiro-cli` — core chat can't start. The user-facing Windows support this batch enabled needs a usable default. | Provide a Windows sandbox backend, OR have `kirocrew setup` set `sandbox_allow_unsandboxed_exec=true` on Windows + document it in WINDOWS_INSTALL.md. Combination-gap between two faithful commits; fix in both. |
| [`eaf62582`](https://code.amazon.com/packages/MeshClaw/commits/eaf625828a1757fc2c649a9b43a67778b14715ae) | `platform_compat.py` `kill_process_tree` | HIGH (Windows-only) | On Windows `kill_process_tree` runs blocking `taskkill /T` (up to 5s) synchronously; several async callers (`acp/client.py`, `apps/backend.py`, `hooks.py`, etc.) invoke it directly, which would block the event loop **on Windows only** (POSIX uses fast non-blocking `killpg`, unchanged). `runtime.py` already offloads via `run_in_executor`. | Offload the Windows `taskkill` branch via `subprocess_executor()` at every async call site, or make an async wrapper. POSIX path is fine as-is. |
| [`eaf62582`](https://code.amazon.com/packages/MeshClaw/commits/eaf625828a1757fc2c649a9b43a67778b14715ae) | `platform_compat.py` `restrict_to_owner` / `_current_user_sid` | HIGH (Windows-only) | On Windows these run `whoami`/`icacls` synchronously (up to ~15s) from lazy-auth/request paths (e.g. `token_secret.py`), blocking the event loop **on Windows only**. | Resolve permissions off-loop (executor) or at startup. |
| [`1b0585bb`](https://code.amazon.com/packages/MeshClaw/commits/1b0585bb) | `apps/manifest.py` `signing_payload` | HIGH | The App-Kit admission signature covers only name/version/signer/permissions, so a valid signature survives changes to install scripts / backend entryPoint / MCP servers / source. | Sign a canonical full manifest **plus** a package/tree (or pinned-commit) digest; verify the checked-out content before build/script execution. |
| [`1b0585bb`](https://code.amazon.com/packages/MeshClaw/commits/1b0585bb) | `apps/registry.py` `install_from_registry` / `_clone_build_app` | HIGH | Admission verifies a separately-fetched manifest, then clones + builds mutable repo content without verifying it matches (no pinned commit / digest check). | Pin a commit and verify the cloned package digest before any build/install script runs. |
| [`1b0585bb`](https://code.amazon.com/packages/MeshClaw/commits/1b0585bb) | `apps/manager.py` `register_external_app` | HIGH | Does not require the signed manifest name/version to equal the request, so a valid signature can be replayed for a different registration. | Enforce manifest-vs-request identity equality before admission. |
| [`383eae45`](https://code.amazon.com/packages/MeshClaw/commits/383eae45) | `sandbox.py` launcher hardlink scan (~L618) | HIGH | Protected inodes are recorded **after** sensitive paths are replaced by empty bind mounts, so pre-existing hardlinks to the original credentials are never detected. Byte-identical to upstream. | Capture protected inodes **before** mounting; fail closed if the scan limit is exceeded; add a hardlink regression test. |
| [`1b0585bb`](https://code.amazon.com/packages/MeshClaw/commits/1b0585bb) | `apps/admission.py` `from_dict` | MED | Structural policy errors (e.g. non-object `trust_keys`) escape `from_dict()` instead of the documented fail-closed policy. | Validate schema in the guarded load path; deny on any invalid field. |
| [`340cfa9`](https://code.amazon.com/packages/MeshClawWebsite/commits/340cfa9) | `store/chatSlice.ts` `sseSlots` | MED | An authoritative **empty** slots snapshot skips all cache pruning (treated as reconnect no-op), so closing the last session retains every inactive transcript. | Distinguish reconnect-init from a valid empty snapshot; prune non-active caches for the latter. |
| [`3c290bb`](https://code.amazon.com/packages/MeshClawWebsite/commits/3c290bb) | `scripts/settingsExtract.ts` | MED | Duplicate-setting IDs follow alphabetical file order, not rendered component order, so a deep-link highlight can target the wrong control (e.g. VoicePanel Polly-vs-STT). | Use explicit stable setting IDs in the DOM, or derive real composition order. |

> **Batch-37 late-add note (2026-07-17):** three keeper commits initially dropped (their triage-skeptic agents hit API errors and the pipeline nulled them) were re-triaged by diff and ported on top of `sync/beta-2026-07-17`. Two carry provenance worth recording here:
>
> - [`1fee24ce`](https://code.amazon.com/packages/MeshClaw/commits/1fee24ce43b012d585149d47e72c462bd823a84d) (backend, KEEP — [CR-289825986](https://code.amazon.com/reviews/CR-289825986)) closes **Talos bdf0d7e5 / V2285983353** (agent-spawn resource-exhaustion / fork-bomb DoS). Ported in full as generic-core security (per-process RLIMIT floor + default-on cgroup v2 scope wired into every fork routed-spawn site). DROPPED the upstream `providers/claude_code.py` / `taskkeeper.py` / `apps/builtins/code_reviewer/*` spawn-wiring (absent in this fork) and the pure-black-reformat churn (conftest.py, security.py slice-spacing). Renamed the systemd slice `meshclaw-agents.slice` -> `kirocrew-agents.slice`. Nothing left un-fixed; recorded for the security-finding trail.
> - [`b493aa40`](https://code.amazon.com/packages/MeshClaw/commits/b493aa409cd50fcc4184bd524659c17a9a82e0bc) (backend, **PARTIAL** — [CR-289037039](https://code.amazon.com/reviews/CR-289037039) / [Mesh-2837](https://taskei.amazon.dev/tasks/Mesh-2837)) — ported ONLY the generic ungated refactor (`chat_runner.drain_pending_context` + shared `state._MAX_PENDING_CONTEXT`). Its **headline slack thread-backfill half is DELIBERATELY DROPPED**: `_backfill_thread_context` + its `chat_slack.py` hunk fire exclusively on the challenge-and-redirect auto-link flow this fork removes (see "Fork-initiated UX / feature divergences" + the `send_channel_challenge` / `_CHALLENGE_REDIRECT_ENABLED` SKILL rule). This is a **do-not-reintroduce** fork divergence, not an upstream-first gap — a future sync must NOT restore the backfill helper. The dropped upstream security tests (grant-mismatch confused-deputy, credential-straddle) guard code that does not exist in the fork.

PR #5 (batch-37) drew four more Codex findings whose flagged code is byte-identical to / a faithful port of the current MeshClaw beta — same disposition (fix upstream-first, flow back via sync; fixing fork-only would diverge + re-conflict). The fork-INTRODUCED defects Codex + `/code-review max` found (artifact DELETE 500, cron model `.strip()` 500, `registry.py` `wrap_argv` bypass, dropped cron start-refresh, the `available_models("claude_code")` fork-divergence gate) were all fixed fork-side in commits `4ea586f`/`caad3df` and are NOT in this table.

| Upstream origin | Fork file:sym | Sev | Finding | Fix shape (upstream-first) |
|---|---|---|---|---|
| [`1fee24ce`](https://code.amazon.com/packages/MeshClaw/commits/1fee24ce43b012d585149d47e72c462bd823a84d) | `acp/client.py` (+ every wired spawn) `preexec_fn=resource_limit_preexec()` | HIGH | `preexec_fn` on an asyncio `create_subprocess_exec` is documented as deadlock-prone in a multithreaded process. Ported verbatim from upstream, which applies RLIMIT this way at every routed spawn. The preexec only calls `resource.setrlimit` (no locks/allocation), so the practical risk is low, but it is a real upstream design question. | Apply RLIMIT via an exec wrapper/launcher (like `wrap_argv`'s launcher script) instead of `preexec_fn`, upstream-first. |
| [`1fee24ce`](https://code.amazon.com/packages/MeshClaw/commits/1fee24ce43b012d585149d47e72c462bd823a84d) | `security.py` `apply_resource_limits` (~L2771) | MED | Clamps a requested rlimit only against the inherited **hard** limit, then sets both soft+hard to it — so a request above an operator's lower **soft** ulimit raises that soft limit. Byte-identical to upstream (whose intent is to tighten to the ceiling, not preserve operator soft caps). | If preserving operator soft limits is desired, clamp `min(requested, soft, hard)` w/ `RLIM_INFINITY` handling — upstream-first (it is arguably by-design). |
| [`192b5c5c`](https://code.amazon.com/packages/MeshClaw/commits/192b5c5c4bab02e222bcabc7346412fea2407ca3) | `slack/gateway.py` `_acquire_with_model_fallback` (~L1484) | MED | A cron job's model override routed through a warm-pool session with no configured pool agent skips `set_model`, so the job silently runs with the default model and reports no downgrade. Faithful port of the upstream fallback. | Switch a claimed pool session when an explicit model is requested, or bypass the pool when its model is unknown — upstream-first. |
| [`3b93b1f3`](https://code.amazon.com/packages/MeshClaw/commits/3b93b1f3a9e43591c59dbd48c23411e209fb93c2) | `workflows/__init__.py` `WorkflowContext.phase/log/nudge` | MED | The runtime now returns a context manager but the `WorkflowContext` Protocol still annotates `-> None`, so the type contract is stale. Identical to upstream (the CM fix touched `runner.py`/`validate.py`, not the Protocol). | Update the Protocol return types to the CM type + refresh the workflow spec — upstream-first. |

PR #18 (batch-38 + batch-39 follow-up) drew a fresh set of Codex findings. **None are fork-introduced** — each flagged line is byte-identical to the current MeshClaw beta it was ported from (verified by `git show <sha>:<path>`), so all are upstream-first. Recorded below; do NOT diverge the fork unilaterally.

| Upstream origin | Fork file:sym | Sev | Finding | Fix shape (upstream-first) |
|---|---|---|---|---|
| [`24c320f6`](https://code.amazon.com/packages/MeshClaw/commits/24c320f6efbfe14867efab281105d14bf4f7ec22) | `sandbox.py` launcher `if PID_NAMESPACE:` (~L676) | ~~HIGH~~ **RESOLVED (batch-40)** | ~~If `sandbox_pid_namespace` is enabled but `unshare(CLONE_NEWPID)` fails, the launcher logs and continues WITHOUT pid isolation (fail-open) rather than aborting.~~ **Upstream reverted the ENTIRE pid-namespace feature in [`14fb9442`](https://code.amazon.com/packages/MeshClaw/commits/14fb944242c77047bcdef564493cbf1b0a4e884d) (byte-clean revert of `24c320f6`); the fork ported the revert this batch (batch-40), removing the flagged fail-open branch along with the feature.** The finding is moot until upstream re-lands a redesigned pid-ns. | n/a — resolved by upstream revert (ported batch-40). |
| [`ed2f65b0`](https://code.amazon.com/packages/MeshClaw/commits/ed2f65b0fbd0900c471ee6ce0f71d0bc945e61d4) | `cron_script.py` `_resolve_safe_pgid`/`_kill_proc_group` (~L109-165) | MED (Windows-only) — **still OPEN after batch-40** (cron-cancel was NOT part of the `14fb9442` pidns revert) | Cron-cancel uses raw `os.getpgid`/`os.killpg`/`signal.SIG*` (POSIX) with `proc.terminate()`/`proc.kill()` fallbacks; the killpg branch is not routed through `platform_compat`, so on Windows the pgid path errors (the fallback still fires). Byte-identical to upstream `cron_script.py:109-165`. | Route the process-group kill through `platform_compat.kill_process_tree` and use portable creation flags — upstream-first (POSIX path is correct as-is). |
| [`0acf7f8f`](https://code.amazon.com/packages/MeshClaw/commits/0acf7f8f) | `mcp_gateway/pool.py` (~L43-79) + `config/loader.py` (~L1897-1913, L2921-2922) `read_buffer_limit_bytes` / `response_spill_threshold_bytes` | MED | The two config keys are parsed into `KiroCrewConfig` (`config/loader.py:2921-2922`, with `max(1024, ...)`/`max(0, ...)` clamps) but never read by the pool — only the `KIROCREW_MCP_READ_LIMIT` / `KIROCREW_MCP_SPILL_THRESHOLD` env vars take effect (`pool.py:56/79`), so a `config.json` setting is a silent no-op. Additionally a configured/env `0` threshold would spill EVERY response instead of disabling spilling (the loader's `max(0, ...)` clamp permits it). Faithful port of the batch (config-vs-env wiring gap is upstream). Re-verified at batch-40: still unwired at origin/beta-braveheart tip. | Wire the loaded config into the gateway pool and require a positive threshold before spilling (treat `0` as "disabled") — upstream-first. |
| [`ed2f65b0`](https://code.amazon.com/packages/MeshClaw/commits/ed2f65b0fbd0900c471ee6ce0f71d0bc945e61d4) | `cron.py` `cancel()` vs `_run_job_isolated` finally (~L634-734 / L1108-1130) | MED | Cancel race — a job that completes NATURALLY while `cancel()` is running (between the `_executing` check and the task cancel) can be mislabeled: `cancel()` adds the id to `_cancelled_jobs`, sets `last_status="error"` / "Cancelled by user", and records a `cancelled` history entry, while `_run_job_isolated`'s finally sees `cancelled=True` and SKIPS `_merge_job_result` + the real success history record — so the completed run's result is dropped and reported as cancelled. Upstream-verbatim (fork `cron.py` cancel/finally are a faithful port); verified present at origin/beta-braveheart tip. | Distinguish "task actually cancelled" from "task finished before cancel landed" (e.g. check `task.done()` before mutating state / recording history) — upstream-first. |
| [`ed2f65b0`](https://code.amazon.com/packages/MeshClaw/commits/ed2f65b0fbd0900c471ee6ce0f71d0bc945e61d4) | `cron.py` `cancel()` re-entrancy (~L634-720) | LOW | Two concurrent `cancel()` calls for the same job both pass the `job_id in self._executing` gate before either discards it, so BOTH record a `cancelled` history entry (duplicate history rows + double SEL audit). Upstream-verbatim; verified at origin/beta-braveheart tip. | Make cancel idempotent (discard from `_executing` up front, or gate on `_cancelled_jobs`) — upstream-first. |
| [`0acf7f8f`](https://code.amazon.com/packages/MeshClaw/commits/0acf7f8f) | `mcp_gateway/spill.py` `_spill_dir()` (~L41-44, L141-143) | MED (by-design, flagged by Codex) | All spilled oversize tool responses for ALL sessions of the same OS user land in ONE shared `~/.kirocrew/mcp_spill/` dir (created `0o700`, filenames are server-name + id). The dir is deliberately agent-readable (the agent is told the spill path to `read` back the full response), so any session's agent can read another session's spilled response — cross-SESSION disclosure within the same user, flagged by Codex on PR #18. Same-user-only (0o700 blocks other OS users); faithful port, upstream-verbatim at beta tip. | Per-session spill subdirs (or embed the session key in the filename + gate reads), upstream-first — arguably by-design for a single-operator tool. |
| (taskrunner port) | `git_coord.py` `setup_run_git` (~L32-48) | LOW | When `run.work_dir` is a NESTED SUBDIRECTORY of a git repo, worktree setup resolves the repo root via `--show-toplevel`, creates the worktree from the ROOT, and then sets `run.work_dir = wt_dir` (the worktree ROOT) — the original subpath under the repo is lost, so the task executes at the repo root instead of the intended subdir. Byte-identical to upstream (from the original taskrunner port); verified at origin/beta-braveheart tip. | Re-append the `orig_dir`-relative-to-`repo_root` subpath onto `wt_dir` when setting `run.work_dir` — upstream-first. |
| [`0acf7f8f`](https://code.amazon.com/packages/MeshClaw/commits/0acf7f8f) | `mcp_gateway/spill.py` (~L152) truncation budget | MED | Large-response truncation caps each text item independently, so many sub-16-KiB blocks (or large non-text content) stay fully inline and defeat the spill memory/context protection. Faithful port. | Enforce one cumulative inline budget across all content types + multi-block/image tests — upstream-first. |
| [`ebc75e30`](https://code.amazon.com/packages/MeshClaw/commits/ebc75e30) | `dashboard/handlers/discover.py` skill overwrite (~L303) | HIGH | On overwrite, an existing skill-dir **symlink** is followed and containment is checked relative to its (possibly external) target, so a crafted symlinked skill root could let bundle files land outside the skills dir; and the existing skill is removed before the replacement bundle is validated (malformed bundle → data loss). Faithful port of the skills.sh browser. | Reject/unlink symlink destinations, validate against the canonical `_skills_dir()`, and stage-then-atomically-replace — upstream-first. |
| [`ebc75e30`](https://code.amazon.com/packages/MeshClaw/commits/ebc75e30) | `skill_providers/skillsh.py` redirect handling (~L287) | HIGH | The SSRF guard validates the initial host but follows redirects to arbitrary hostnames without re-resolving/allowlisting, so a DNS name resolving to a private address (or a redirect-based rebinding) can bypass the private-IP block. Faithful port. | Re-validate resolved addresses on every hop + restrict redirects to explicit HTTPS hosts (anti-DNS-rebinding) — upstream-first. |

> **Batch-39 fork-introduced check:** unlike batch-37 (which had real fork-introduced defects fixed in `4ea586f`/`caad3df`), the batch-39 follow-up introduced **no** fork-side defects. The only fork-side changes this round were CI-hygiene, not logic: isort on ported files, a justified `# nosemgrep` on `sandbox._ensure_run_dir`'s `0o700` chmod (owner-only is intentional), an inclusive-language reword of a non-inclusive branch-name term in a skills.sh comment, and the `origin/main` merge resolution (took main's superset auth-parity version of the re-ported `#11`, de-duped the doubled `TestApiServerAuth`).

> **Batch-40 fork-side review fixes (exception to "upstream-first" — each is fork-appropriate):** three PR #18 review findings WERE fixed fork-side in `fix(review): address PR #18 review findings`, because each restores a rule/pattern the fork (or upstream itself) already mandates rather than diverging:
>
> 1. `sandbox.py` cleanup sweep (Codex HIGH, repeated): the legacy-launcher sweep's raw `os.kill(int(pid_str), 0)` liveness probe was replaced with `platform_compat.pid_exists()`. This is the fork's own CLAUDE.md/AGENTS.md platform rule — `os.kill(pid, 0)` TERMINATES the target process on Windows, and every POSIX-only liveness probe must route through the `platform_compat` shim. Upstream (Linux/macOS-only) has no such rule, so this stays a deliberate fork divergence; a future upstream sweep edit must be re-applied on top of the shim call.
> 2. `AutoNudgePopover.tsx` save button `text-white` -> `text-accent-fg` (Claude LOW): NOT a unilateral divergence — upstream itself established the `text-accent-fg` token in `15b1f9c` and accidentally regressed it to `text-white` in the `729e09d` shadcn migration. The fork restores upstream's own intended pattern; if upstream fixes it later, the next sync sees ALREADY_PRESENT.
> 3. `TagManagerList.tsx` inline-rename `<input>` a11y (Claude LOW): added `aria-label={`Rename tag ${t.name}`}` — an additive accessible-name fix consistent with the fork's a11y conventions; same ALREADY_PRESENT convergence if upstream adds one later.

PR #14 (artifacts-page exact-mirror) drew Codex + CodeQL findings across several
re-runs. **Fork-INTRODUCED defects (all fixed fork-side on-branch):** (1) the new
`capabilities.publish` chokepoint (`dashboard/handlers/artifacts.py`
`_publish_governance_denied`) degraded-to-permit on an unexpected
governance-eval error — changed to **fail closed** (deny) since publish is an
exfil authorization decision (stricter than the messaging/cron chokepoints by
design); (2) the new `publish.allowed_destinations` config field was never
parsed in `KiroCrewConfig.load()` nor emitted in `to_dict()`, so the allowlist
was inert and erased on save — wired both + round-trip test; (3) the
`capabilities.publish` gate covered `api_artifact_publish` but NOT
`api_artifact_update_sharing`, so an already-published artifact could be widened
to PUBLIC bypassing the gate — added the same fail-closed gate to the sharing
mutation; (4) CodeQL flagged the new-to-fork `api_artifact_relocate` path
expression — reordered so the traversal + `is_sensitive_path` guards run BEFORE
any `os.path.exists`/`isdir` stat (sanitize-before-use). The remaining
**upstream-verbatim findings** (faithful ports of the current MeshClaw beta) —
same disposition as prior batches (fix upstream-first, flow back via sync;
fixing fork-only would diverge + re-conflict):

| Upstream origin | Fork file:sym | Sev | Finding | Fix shape (upstream-first) |
|---|---|---|---|---|
| MeshClaw `publish_sync.py` | `publish_sync.py` `_write_tempfile` / `os.unlink` (~L260/352) | HIGH | Synchronous `tempfile.mkstemp`/write/`unlink` for artifacts up to 25 MiB run on the asyncio gateway loop. Byte-identical to upstream (modulo the `kc-artifact-` prefix rename). | Offload tempfile create/write/delete via `asyncio.to_thread` / the subprocess executor — upstream-first. |
| MeshClaw `dashboard/handlers/artifacts.py` | `api_artifact_update` (~L787) | MED | A content update never calls `push_version_by_slug()`, so an `auto_sync=True` publication doesn't push the new version. Faithful port — upstream's own update handler also omits this (count 0 both sides). | Trigger a best-effort provider push after a successful version bump — upstream-first. |
| MeshClaw `dashboard/handlers/artifacts.py` | `api_artifact_delete` (~L816) | MED | Deleting a published artifact skips `publish_sync.delete_for_artifact()`, orphaning the remote. Faithful port — upstream's delete handler also omits the cleanup. | Call `delete_for_artifact(prev)` before the local delete — upstream-first. |
| MeshClaw `publish_sync.py` | `pull_upstream` (~L860) | MED | Live/CRDT pulls compare the version counter, which `upstream_status()` documents advances merely on view → phantom pulls. Identical to upstream. | Use the remote-content-hash comparison for live publications too — upstream-first. |

The Claude Review gate (`[BLOCK-MERGE]`) on PR #14 confirmed the design and
surfaced the same upstream-verbatim HIGH (`_write_tempfile` blocking I/O, already
tracked above) plus two more **fork-fixed** items: the masonry artifact card was a
bare `div onClick` with no keyboard semantics (blocking `accessible-interactive-
elements` rule) → added `role="button"`/`tabIndex`/Enter-Space `onKeyDown` to
match the sibling `FolderCard`; and `types/index.ts` declared `CommentAnchor`/
`ArtifactComment` twice (a merge artifact from the transcript-replay recovery) →
de-duplicated. One MED it raised — `scope=shared` comment/reply provider pushes
(`artifacts.py` `api_artifact_*comment`) are outbound egress that skip the
`capabilities.publish` gate — is **upstream-verbatim** comment-handler code and
inert in the public edition (no provider registered); tracked as a consistency
follow-up (gate provider-comment egress too, upstream-first).

**CodeQL `py/path-injection` (3 HIGH) — suppressed as guarded false-positives**
(user chose suppress over confine-to-root, to preserve upstream's any-file
relocate behavior). The new-to-fork `api_artifact_relocate` handler is
byte-identical to upstream and intentionally accepts any non-sensitive local
file; its two `os.path.exists`/`isdir` stat calls run AFTER the `..`-traversal
guard + the `is_sensitive_path` denylist, but CodeQL's taint tracker only accepts
a fixed-root `is_relative_to` containment barrier (the `files.py` pattern) as a
sanitizer, not the denylist. The third alert (`security.py:711`) is inside
`is_sensitive_path` itself — the sanitizer's own `Path(...).resolve()` candidate
build — surfaced only because relocate is a new caller. All three carry an inline
`# lgtm[py/path-injection]` suppression with a justification comment; none is a
real vulnerability (a fixed-root confine remains the upstream-first hardening if
the any-file behavior is ever narrowed).

**PR #14 review round 2 (2026-07-18, rebased onto `main` @ `1f6b478f`).** A second
Codex/Claude/CodeQL pass drew 14 live inline threads. Triaged against this ledger:
- **Fork-INTRODUCED, fixed fork-side on-branch** (fork-original `capabilities.publish`
  code with no upstream counterpart — fixing can't diverge or re-conflict):
  (a) `_publish_governance_denied` still **failed OPEN** for the case it was
  written for: `governance_permits` swallows its OWN internal errors and returns a
  permissive "no opinion" Decision, so the handler's fail-closed `except` was
  unreachable for a real governance-eval error. Added an opt-in `fail_closed=True`
  to `governance_permits` (denies + audits `failed_closed` at the point the error
  is caught) and passed it from the publish gate. (b) The gate checked the
  **requested/default** provider, but `publish_sync.publish()` re-dispatches an
  already-published artifact to `publication.provider` — a re-publish with no
  explicit provider gated on `artifactory` while pushing to a possibly-denied
  existing destination. Now resolves the **effective** provider before the gate
  (mirrors `api_artifact_update_sharing`). (c) `ArtifactStore.update_comment`
  mass-assigned any dataclass attr via `setattr`; added a `_MUTABLE_COMMENT_FIELDS`
  allowlist (`status`/`body`/`anchor_orphaned`) matching `update_publication` /
  `update_fork_metadata`. Regression tests added in `test_governance_chokepoints.py`
  (`test_internal_resolve_error_fails_closed`, `test_governance_permits_fail_closed_flag`,
  `test_republish_gates_on_existing_provider`). Spec: `governance.md` publish
  section updated.
- **Already fixed by an earlier on-branch commit** (thread predated the fix): the
  masonry `LocalCardBody` a11y (`role`/`tabIndex`/`onKeyDown` — commit "dedupe
  comment types") and the duplicate `CommentAnchor`/`ArtifactComment` TS interfaces.
- **Upstream-verbatim — DEFER unchanged** (faithful MeshClaw ports; inert in the
  public fork's empty `PublishRegistry`; fixing fork-only would diverge + get
  re-conflicted by the next sync): `publish_sync._write_tempfile`/`_render_content`
  blocking I/O on the loop (already tracked above), the comment/publish
  handlers' inline blocking store IO (`store.get`/`list_comments`/… — the reviewer
  notes it MIRRORS the pre-existing `api_artifact_detail` inline pattern, so
  offloading only the new handlers would be *inconsistent*; a module-wide
  offload is the upstream-first fix), and the relocate arbitrary-local-file read
  (same any-file behavior the 3 CodeQL suppressions above cover — a fixed-root
  confine is the upstream-first hardening if the behavior is ever narrowed).

**PR #14 review round 3 (2026-07-18) — CI AI-review gate (Codex `Severity: HIGH`
+ Claude `[BLOCK-MERGE]`) re-flagged the deferred items on the squashed HEAD.**
Branch protection has NO required status checks (only human review), so the AI
gates are advisory — but per the maintainer directive ("make CI green"), the
gate-blocking HIGHs were promoted from DEFER to **fork-side fixes** where the fix
is additive + low-risk (and still upstream-mergeable, i.e. won't re-conflict):
- **`publish_sync._redact_untrusted` `manual`-source exemption** (Codex HIGH):
  `source` is set once at create and NOT re-derived on a later agent `update`, so
  a `manual`-labelled artifact can carry LLM bytes by publish time and reach a
  provider unscanned. Made redaction **unconditional** (dropped the `manual`
  bypass) — a false redaction is far cheaper than an exfiltration miss. Test:
  `test_redact_untrusted_scans_every_source`.
- **`scope=shared` comment post/reply/edit provider egress** (Codex + Claude
  HIGH; was DEFER above): now gated through `_publish_governance_denied` (the same
  `capabilities.publish` chokepoint as artifact publish) before the provider
  dispatch — a denied destination keeps the comment/reply/edit LOCAL
  (`local_only` / `remote_synced=False`) rather than pushing. Test:
  `test_edit_gated_by_publish_governance`.
- **HTTP envelope cap** (Codex + Claude MEDIUM): `_MAX_BODY_BYTES` was pinned at
  2 MiB while the store/validation cap rose to 25 MiB, so valid 2–25 MiB MCP
  saves failed at the HTTP boundary. Raised to `MAX_CONTENT_BYTES` + 8 MiB
  envelope headroom and corrected the inverted "store enforces a stricter cap"
  comment.

Still DEFER (upstream-first, MEDIUM/non-gate-blocking — do NOT fix fork-only):
the two remaining MEDIUMs (`api_artifact_update` omits `push_version_by_slug`;
`api_artifact_delete` omits `delete_for_artifact`) are faithful upstream ports
(count 0 both sides) and inert in the public fork; the relocate any-file read
stays upstream-first as above.

**PR #14 review round 3b/3c (2026-07-18) — additional fork-side hardening.** The
CI AI-review gate is empirically **non-convergent**: four consecutive Codex runs
over the same ~11k-line diff each surfaced a DISJOINT set of 2–5 `Severity: HIGH`
findings (run1: wrong-provider/fail-open; run2: manual-redaction/comment-egress;
run3: blocking-IO/mark_review-delete-gate/tags-redaction/SEL-audit; run4:
`wrap_widget_html` CSP-idempotency/`unpublish` best-effort-clear/store-IO). None
overlapped. Branch protection requires NO status checks (human review only), so
the gates are advisory. Fixed fork-side every finding that is additive +
low-risk + still upstream-mergeable:
- **`publish_sync` blocking I/O** (promoted from DEFER): `_render_content` +
  `_write_tempfile` + unlink in `publish()`/`push_version()` now offload via
  `asyncio.to_thread` (+ `_safe_unlink` helper).
- **LLM tags unredacted on publish**: `publish()` now redacts each tag via
  `_redact_untrusted` (title/summary already were).
- **mark_review / delete provider egress** now pass `_publish_governance_denied`
  (same `capabilities.publish` gate) before the provider dispatch.
- **SEL audit on comment-handler denials**: post/reply/mark_review/resolve/
  reopen/delete restricted-session (and reply validation) denials now emit
  `_audit(outcome="denied")`.
- **`author` / anchor strings echoed unredacted**: `_redact_text` + length caps
  applied to the LLM/agent-influenced `author` (256) and anchor quote/prefix/
  suffix (2000) in post/reply.
- **`add_comment` unbounded**: added `MAX_COMMENTS_PER_ARTIFACT = 500` FIFO cap
  (drops oldest WHOLE threads, never orphans a reply). Tests in
  `test_artifact_comment_store.py::TestCommentRetentionCap`.
- **CLAUDE.md violation — Amazon terminology in generic core**: genericized
  `ArtifactSharePanel` ("Every Amazon employee" → "Everyone in your
  organization", `alice@amazon.com` example → `alice@example.com`). This one is a
  real de-Amazon-fork requirement, fixed regardless of the gate.

Remaining DEFER (upstream-verbatim, inert in the empty-registry public fork,
design-intentional — a fork-only fix would diverge + re-conflict and STILL not
make the non-convergent gate green): `wrap_widget_html` returning a `<!DOCTYPE`
document unchanged (deliberate no-double-wrap idempotency; the sandbox null-origin
iframe already contains it), `unpublish()` clearing local metadata on a
best-effort provider-delete failure (documented behavior), and the comment-handler
inline store reads (mirror `api_artifact_detail`; module-wide offload is
upstream-first).

**PR #14 review round 4 (2026-07-18) — CodeQL (the ONE deterministic gate).**
Unlike the non-convergent Codex/Claude reviews, GitHub-native CodeQL reports the
SAME 3 `py/path-injection` HIGH alerts every run (`artifacts.py` relocate stat
calls + `security.py:717` inside `is_sensitive_path`). The inline
`# lgtm[py/path-injection]` comments do NOT suppress them — that is legacy
Semmle/LGTM syntax; GitHub CodeQL ignores it. **Reversed the earlier
"suppress over confine-to-root" decision** (it never actually suppressed) and
implemented the real fix the reviewers asked for: `api_artifact_relocate` now
CONFINES `source_path` to the user's home dir via a resolved-`is_relative_to`
barrier (the sanitizer CodeQL recognizes), with an operator
`publish.relocate_roots` config allowlist to widen to extra absolute roots; the
`..` guard runs first and `is_sensitive_path` still applies inside every root.
This closes the CodeQL alerts AND the agent-reachable arbitrary-file read
(finding #5 above). The `security.py:717` alert's taint path from relocate is
severed by the upstream containment (other callers already pass vetted paths).
Tests: `test_artifacts_handlers.py::TestRelocate` (home allowed, outside-home
denied, configured extra-root allowed, traversal denied). Spec: `artifacts.md`
Security → "Relocate root confinement". This is a deliberate, documented
divergence from upstream's any-file relocate (the public fork is
security-hardened here); a MeshClaw sync must NOT revert it.

**PR #14 review round 5 (2026-07-18) — CodeQL green (4 FPs dismissed), Claude
green, Codex down to 2 HIGH; both fixed fork-side.** After the relocate
containment landed, GitHub-native CodeQL STILL flagged the `is_relative_to`
guard + stat calls (its default `py/path-injection` query does not model
`Path.is_relative_to` as a sanitizer). The code is correct (home-confined +
denylist + `TestRelocate`), so the 4 PR-introduced alerts (#311–314) were
DISMISSED via the code-scanning API as guarded false-positives with written
justifications — CodeQL now passes. Claude's latest run reports "no CRITICAL or
HIGH". Codex's latest 2 HIGH were both fixed:
- **`ArtifactSharePanel.tsx` `view_url` used as `href`** (real XSS surface even
  though inert without a provider): added `safeHttpUrl()` — a provider-controlled
  `view_url` is rendered as a link ONLY when it parses as http(s); a
  `javascript:`/`data:` scheme yields a disabled affordance. Tests:
  ArtifactSharePanel.test.tsx (http href rendered / javascript: neutralized).
- **`publish_sync` `store.get()` ≤25 MiB sync reads on the loop**: the hot
  `publish()` / `push_version()` reads now `await asyncio.to_thread(store.get, …)`
  (joining the tempfile/render offload already done in round 3b). The
  clone/fork/pull/overwrite reads stay inline (separate inert ops, upstream-first).

**PR #14 review round 6 (2026-07-18).** Codex's next run flagged (a) the agent
delete `reason` persisted to the SEL audit + activity feed unredacted — fixed:
`reason` now passes through `_redact_text` before audit/persistence (same
treatment as comment body/author/anchors); (b) more `publish_sync` store
reads/writes in the clone/fork/pull/overwrite paths — left inline (upstream-verbatim,
inert without a registered provider; the hot publish/push paths are already
offloaded). Confirms the Codex gate remains non-convergent (6 runs, disjoint
sets); the legitimate in-scope findings are fixed as they surface.

**PR #14 review round 7 (2026-07-18) — Claude [BLOCK-MERGE], both findings real
fork bugs, fixed:**
- **`publish.relocate_roots` never parsed in `from_dict`** (MEDIUM but a genuine
  bug I introduced in round 4): the field was declared + consumed by the relocate
  handler but `KiroCrewConfig.from_dict` only parsed `allowed_destinations`, so an
  operator's configured extra roots were silently dropped (permanently `[]`) and
  lost on round-trip. Added the parse (blank-filtered). Test:
  `test_config_loader.py::test_publish_relocate_roots_parsed_and_round_trips`.
- **blocking store IO in the REACHABLE publish handlers** (blocking-rule HIGH):
  `update_sharing` / `unpublish` / `refresh_publication` / `push_version_by_slug`
  called `store.get`/`update_publication`/`clear_publication` synchronously on the
  loop (a ≤25 MiB read under a lock) — these three+ are wired handler paths the
  author had missed when offloading `publish()`/`push_version()` in round 3b. Now
  `await asyncio.to_thread(...)` like their siblings. (The still-unwired
  clone/fork/pull/overwrite reads stay inline — upstream-verbatim, not reachable.)
Claude's same run verified everything else clean (frontend XSS/a11y, keystone,
governance fail-closed, path traversal, redaction, SEL audit).

**PR #14 review round 8 (2026-07-18) — Claude down to ONE blocking finding,
fixed.** The `push_version()` `live_dirty` pre-push snapshot
(`publish_sync.py:445` `fresh = store.update(slug, snapshot=True)`) was "the one
unwrapped store call among 8 deliberately offloaded siblings" — now
`await asyncio.to_thread(...)`. Claude verified everything else sound (keystone,
path-traversal, governance, never-trust-LLM redaction, SEL audit, frontend
XSS/a11y incl. the `safeHttpUrl` guard). This is the convergent signal: Claude
(the precise reviewer) went large-clean → one line → (expected) clean, unlike
the non-convergent Codex gate whose 7+ runs each re-sample a disjoint HIGH set
of the same inert-in-fork publish-path store calls.

**PR #14 review round 9 (2026-07-18) — comment/relocate handler blocking IO
offloaded (previously DEFER, now fixed to clear the Claude gate).** Claude's next
run named a SPECIFIC, finite set of on-loop store calls in the new async
handlers (`api_artifact_comments`, the comment mutators post/reply/mark_review/
resolve/reopen/delete/edit, and `api_artifact_relocate`) — `store.get`
(≤25 MiB), `list_comments`, `add_comment` (load-append-rewrite), `update_comment`,
`delete_comment`, `record_comment_event`, `relocate`. These were the ones I'd
deferred as "mirror the pre-existing `api_artifact_detail` inline pattern." Since
Claude's ask is bounded (a named list, not Codex's ever-expanding "every store
call"), routed all of them through the existing `_run_off_loop(...)` executor
helper (the same one the folder handlers use). The 25× `MAX_CONTENT_BYTES` bump
made the deferral no longer defensible. Handler tests (92) still pass — the
offload is transparent to the MagicMock-request harness. This clears the
blocking-IO category across the whole artifacts handler module, not just the
publish seam.

**PR #14 review round 10 (2026-07-18) — user merged main into the branch (broke
the single-commit hygiene gate); rebased back to one commit + cleared Claude's
next precise batch.** The branch was manually merged with `main` (#16 nightly CLI
wheel), creating a 2-commit history that fails PR Hygiene — rebased my single
commit onto the advanced `main` (`ca5bc359`) to restore the invariant. Claude's
review then named a specific batch, all fixed: (a) HIGH — the publish/sharing HTTP
handlers (`api_artifact_publish` ×2, `update_sharing`, `unpublish`,
`refresh_publication`) called `get_default_store().get(slug)` (≤25 MiB) directly
on the loop → routed through `_run_off_loop`; (b) MEDIUM — `publish_sync.py`
re-publish branch `store.update_publication` on the loop → `asyncio.to_thread`;
(c) LOW — `_publish_governance_denied` used `getattr(decision, "permitted", True)`
(fail-OPEN default) on an exfil chokepoint → defaulted to `False` (fail-closed).
Each subsequent Claude batch is precise + converges; Codex's parallel run again
objected to the home-confinement itself + the unwired clone/fork/pull store reads
(non-convergent, advisory).

### Resolved (deferred earlier, later ported — recorded for the audit trail)

| Upstream SHA | Repo | Deferred in | Resolution |
|---|---|---|---|
| [`38864fd9`](https://code.amazon.com/packages/MeshClaw/commits/38864fd98f4fc7fabd81487b6e91ae6a49f0ebf1) (+ `d17306e1`) | backend | batch 1–3 (DEFERRED: 4331-line multi-instance SSH tunnels, no UI consumer) | **PORTED (PARTIAL) in batch-10** — kept the generic multi-instance registry / port-allocator / plain-OpenSSH tunnel manager + UI; **dropped** the Midway SSH-cert watchdog (`instances/midway.py`) and its `~/.ssh` carve-out (forbidden by `MIGRATION_PLAN.md`). |
| [`b490c7e8`](https://code.amazon.com/packages/MeshClaw/commits/b490c7e8) | backend | batch 6–7 (DEFERRED: dynamic sub-agent concurrency cap; depended on absent `mcp_gateway.pool`) | **PORTED in batch-8** — relocated the ~50-line stdlib `/proc`-subtree RSS/CPU helpers into `subagent.py`; the absent-import objection was overcome. |
| [`96c39b8`](https://code.amazon.com/packages/MeshClawWebsite/commits/96c39b8) | frontend | batch 18 (initially DEFER) | **RESCUED to KEEP in batch-18** — its backend pair `7b66e2e3` (MLX Whisper STT) was a keeper the same batch, so the UI was ported backend-first. |
| [`ed984a05`](https://code.amazon.com/packages/MeshClaw/commits/ed984a05edc17e3d740c4feb32c0a6ada026184c) | backend | batch 33 (DEFER: 16-file, −2409-line legacy-HTML-dashboard removal landed after the batch-33 verdicts were verified) | **PORTED in batch-34 addendum (PR #88)** — the fork still shipped the legacy `dashboard.html`/`dashboard.js`/`purify.min.js`/`dashboard.css`/`cli-mode.css` + `_HTML_PATH` wiring + XSS test, so the generic-core XSS-surface reduction applied. `index()` now serves `dist/index.html` only (guidance page on missing bundle); `/static` route, theme assets, `kirocrew-logo.png`, and `_BASE_CSP` preserved. Talos V2285871874 / CR-289374220. |

---

## Full SKIP catalogue (mechanical — absent subsystem / provider / already present)

These required no judgement: each targets something that does not exist in the
fork, belongs to a deleted provider, or is already present. Grouped by batch.

### Batch 1–3 — [CR-280626986](https://code.amazon.com/reviews/CR-280626986) (24 ported, 3 left out)

| Upstream SHA | Repo | Verdict | Reason |
|---|---|---|---|
| [`cf5f017a`](https://code.amazon.com/packages/MeshClaw/commits/cf5f017a) | backend | NA_INTERNAL | PromptFarm skills — depends on Midway `McsRequestsHook` auth + a CodeArtifact-only dep + a hardcoded `*.prompt-farm.payments.amazon.dev` endpoint. |
| [`a7a03199`](https://code.amazon.com/packages/MeshClaw/commits/a7a03199) | backend | NA_INTERNAL | Writing-review scanner sync — `writing_review/` dir absent in the fork. |
| `38864fd9` | backend | DEFER → later ported | See [Human-decision / resolved](#resolved-deferred-earlier-later-ported--recorded-for-the-audit-trail). |

### Batch 4–7 — [CR-280672548](https://code.amazon.com/reviews/CR-280672548) (8 ported, 3 left out)

| Upstream SHA | Repo | Verdict | Reason |
|---|---|---|---|
| [`ea6348d5`](https://code.amazon.com/packages/MeshClaw/commits/ea6348d5) | backend | SKIP | `fix(midway): cache midway_status` — fork `midway.py` is a no-op OSS stub; no real-mwinit path to cache. (Also the held boundary for the `b490c7e8` deferral.) |
| [`63ee7fde`](https://code.amazon.com/packages/MeshClaw/commits/63ee7fde) | backend | SKIP | `config`→`configuration` rename motivated by a Brazil `Config` case-collision; the fork has no Brazil build, so no analog. |
| `b490c7e8` | backend | DEFER → later ported | See [Human-decision / resolved](#resolved-deferred-earlier-later-ported--recorded-for-the-audit-trail). |

### Batch 8 — [CR-280853988](https://code.amazon.com/reviews/CR-280853988) (3 ported, 0 newly left out)

Cleared the deferral backlog (`b490c7e8` ported). The only non-ported edge
items: `11973f4c` (ALREADY_PRESENT — ported in batch 7) and `ea6348d5` (SKIP —
midway stub, already past the boundary).

### Batch 9 — _(no CR; SKIP-only boundary advance, commit `b62394c`)_

| Upstream SHA | Repo | Verdict | Reason |
|---|---|---|---|
| [`e62422ae`](https://code.amazon.com/packages/MeshClaw/commits/e62422ae) | backend | SKIP_INTERNAL | `fix(writing-review): send …` — writing-review subsystem absent. Sole candidate; boundary advanced with no port. |

### Batch 10–11 — [CR-280980741](https://code.amazon.com/reviews/CR-280980741) (67 ported, **36 left out** — full table below)

103 upstream commits triaged → 67 ported, 36 left out (28 SKIP_INTERNAL · 6
ALREADY_PRESENT · 2 SKIP_NONKIROACP). `[b11]` = batch-11 cron run.

| Upstream SHA | Repo | Verdict | Reason |
|---|---|---|---|
| [`1408ca73`](https://code.amazon.com/packages/MeshClaw/commits/1408ca73) | backend | SKIP_INTERNAL | Real tunnel-probe machinery the fork omits entirely. |
| [`33658c12`](https://code.amazon.com/packages/MeshClaw/commits/33658c12) | backend | SKIP_INTERNAL | `app-registry.json` entry pointing to internal GitFarm repo MeshClawApp-SystemMonitor. |
| [`437262df`](https://code.amazon.com/packages/MeshClaw/commits/437262df) | backend | SKIP_INTERNAL | Entirely Midway/internal-SSH connectivity docs + scripts. |
| [`599d6f64`](https://code.amazon.com/packages/MeshClaw/commits/599d6f64) | backend | SKIP_INTERNAL | `api_skill_publish_to_promptfarm` handler — promptfarm absent. `[b11]` |
| [`5c7b2c93`](https://code.amazon.com/packages/MeshClaw/commits/5c7b2c93) | backend | SKIP_INTERNAL | Changes only `test_sync_module.py` (GitProvider) — sync module absent. |
| [`37ca5898`](https://code.amazon.com/packages/MeshClawWebsite/commits/37ca5898) | frontend | ALREADY_PRESENT | Fork already at post-image + its prereq `202e3224` (CSS Highlight API find). |
| [`9fa9b796`](https://code.amazon.com/packages/MeshClawWebsite/commits/9fa9b796) | frontend | ALREADY_PRESENT | Pure deletions stripping Cloud Sync entry points the fork never had. |
| [`a673571`](https://code.amazon.com/packages/MeshClawWebsite/commits/a673571) | frontend | ALREADY_PRESENT | Fork ChatPage already routes `m.role=="mcp_oauth"`. `[b11]` |
| [`d3daac16`](https://code.amazon.com/packages/MeshClawWebsite/commits/d3daac16) | frontend | ALREADY_PRESENT | Fork carries the restore target (post-image), not the reverted regression. |
| [`dfbc99cd`](https://code.amazon.com/packages/MeshClawWebsite/commits/dfbc99cd) | frontend | ALREADY_PRESENT | Both restored features present + wired (embed mode + Browser settings tab). |
| [`eec9c679`](https://code.amazon.com/packages/MeshClawWebsite/commits/eec9c679) | frontend | ALREADY_PRESENT | Fork SettingsPage already has the substance this restores (Provider tab). |
| [`47934af9`](https://code.amazon.com/packages/MeshClawWebsite/commits/47934af9) | frontend | SKIP_INTERNAL | Confined to absent mcp_gateway (`McpPoolableServers` → `/api/mcp-gateway/*`). |
| [`4bb769ff`](https://code.amazon.com/packages/MeshClawWebsite/commits/4bb769ff) | frontend | SKIP_INTERNAL | Pure CloudWatch RUM telemetry; fork `rum.ts` is an inert stub. |
| [`4be5e549`](https://code.amazon.com/packages/MeshClawWebsite/commits/4be5e549) | frontend | SKIP_INTERNAL | Gated on absent mcp_gateway backend (`SharedMcpGatewayToggle`/`McpGatewayCard`). |
| [`58c73651`](https://code.amazon.com/packages/MeshClawWebsite/commits/58c73651) | frontend | SKIP_INTERNAL | Confined to `apps/code-reviewer/` — absent. |
| [`5948404c`](https://code.amazon.com/packages/MeshClawWebsite/commits/5948404c) | frontend | SKIP_INTERNAL | New `lcars/` theme subsystem — absent; cosmetic. |
| [`6fd9ba51`](https://code.amazon.com/packages/MeshClawWebsite/commits/6fd9ba51) | frontend | SKIP_INTERNAL | Bikini-Bottom parody-theme refactor — cosmetic. |
| [`7342c6e`](https://code.amazon.com/packages/MeshClawWebsite/commits/7342c6e) | frontend | SKIP_INTERNAL | `SecretaryPage.tsx` only — secretary absent. `[b11]` |
| [`79785150`](https://code.amazon.com/packages/MeshClawWebsite/commits/79785150) | frontend | SKIP_INTERNAL | PromptFarm (SkillsTab remote-skill install) — absent. |
| [`7b68031f`](https://code.amazon.com/packages/MeshClawWebsite/commits/7b68031f) | frontend | SKIP_INTERNAL | writing-review (`WritingReviewPage` + `wrScanners*` api) — absent. |
| [`7e1d2a1c`](https://code.amazon.com/packages/MeshClawWebsite/commits/7e1d2a1c) | frontend | SKIP_INTERNAL | AIM auto-update (`/api/settings/aim-update`) — absent. |
| [`7eb7d6c`](https://code.amazon.com/packages/MeshClawWebsite/commits/7eb7d6c) | frontend | SKIP_INTERNAL | `SyncPanel.tsx` (Cloud Sync/GitFarm) — absent. `[b11]` |
| [`97ed5548`](https://code.amazon.com/packages/MeshClawWebsite/commits/97ed5548) | frontend | SKIP_INTERNAL | Secretary (`SecretaryPage` + advance-on-dismiss) — absent. |
| [`a0b29564`](https://code.amazon.com/packages/MeshClawWebsite/commits/a0b29564) | frontend | SKIP_INTERNAL | Absent `apps/auto-research` (`ResearchLabPage`/`GrillTree`). |
| [`a232e5bc`](https://code.amazon.com/packages/MeshClawWebsite/commits/a232e5bc) | frontend | SKIP_INTERNAL | Iterates the absent `apps/auto-research/ResearchLabPage`. |
| [`a4207ecd`](https://code.amazon.com/packages/MeshClawWebsite/commits/a4207ecd) | frontend | SKIP_INTERNAL | Wholly in absent `apps/auto-research/` (`grillTree`/`GrillTree`). |
| [`aaf7cfe3`](https://code.amazon.com/packages/MeshClawWebsite/commits/aaf7cfe3) | frontend | SKIP_INTERNAL | GitFarm/Bindle workspace-sync — absent. |
| [`cdf5566c`](https://code.amazon.com/packages/MeshClawWebsite/commits/cdf5566c) | frontend | SKIP_INTERNAL | Secretary — absent. |
| [`d2d2b110`](https://code.amazon.com/packages/MeshClawWebsite/commits/d2d2b110) | frontend | SKIP_INTERNAL | writing-review (`WritingReviewPage` + WR* types/api) — absent. |
| [`d3b5fcb2`](https://code.amazon.com/packages/MeshClawWebsite/commits/d3b5fcb2) | frontend | SKIP_INTERNAL | Secretary (`SecretaryPage`/`secretarySlice`) — absent. |
| [`e06b2dd7`](https://code.amazon.com/packages/MeshClawWebsite/commits/e06b2dd7) | frontend | SKIP_INTERNAL | Harmony Artifactory share UI (`/api/artifacts/*/publish`) — absent. |
| [`e83c0bd7`](https://code.amazon.com/packages/MeshClawWebsite/commits/e83c0bd7) | frontend | SKIP_INTERNAL | Harmony Artifactory browse/fork (`/api/artifactory/*`) — absent. |
| [`ed7c87f`](https://code.amazon.com/packages/MeshClawWebsite/commits/ed7c87f) | frontend | SKIP_INTERNAL | PromptFarm publish UI in SkillsTab — absent. `[b11]` |
| [`ff8b9047`](https://code.amazon.com/packages/MeshClawWebsite/commits/ff8b9047) | frontend | SKIP_INTERNAL | `McpGatewayCard` (Shared MCP gateway) — mcp_gateway absent. |
| [`d7271865`](https://code.amazon.com/packages/MeshClawWebsite/commits/d7271865) | frontend | SKIP_NONKIROACP | Bedrock-specific image-limit downscaling (`BEDROCK_IMAGE_LIMITS`); not generic. |
| [`e42cb331`](https://code.amazon.com/packages/MeshClawWebsite/commits/e42cb331) | frontend | SKIP_NONKIROACP | Flips `agentTemplates` on the Claude Code adapter — CC provider surface. |

### Batch 12 — [CR-281070110](https://code.amazon.com/reviews/CR-281070110) (4 ported, 2 left out)

| Upstream SHA | Repo | Verdict | Reason |
|---|---|---|---|
| [`527459a0`](https://code.amazon.com/packages/MeshClaw/commits/527459a0) | backend | SKIP_INTERNAL | config-baseline regen. The CR description says "only promptfarm + AIM schema", but the actual diff also adds `instances.*` (multi-instance SSH schema), `slack.show_thinking`, and a `/api/status`→`/api/health` probe help-text fix. Skipping the whole generated baseline is still correct (it mirrors loader.py changes that were themselves not ported), but the description **understates** the commit's contents. |
| [`641a6f0c`](https://code.amazon.com/packages/MeshClaw/commits/641a6f0c) | backend | SKIP_NONKIROACP | Per-agent provider override — re-introduces the deleted multi-provider dispatch factory (`_build_provider_factory`/`_resolve_agent_provider` with bedrock/claude_code branches); meaningless under `enum ["acp"]`. (Mesh-1766) |

### Batch 13 — [CR-281120970](https://code.amazon.com/reviews/CR-281120970) (7 ported, 1 full + 1 partial skip)

| Upstream SHA | Repo | Verdict | Reason |
|---|---|---|---|
| [`39d22f8a`](https://code.amazon.com/packages/MeshClaw/commits/39d22f8a) | backend | SKIP_INTERNAL | mcp_gateway backend-lifecycle hardening — 100% confined to absent `src/mesh_claw/mcp_gateway/` + its tests. |
| [`3a017786`](https://code.amazon.com/packages/MeshClaw/commits/3a017786) (Brazil `Config` hunk only) | backend | SKIP (partial) | The Brazil `Config` hunk of the python-docx floor bump; public build is setuptools. The generic `python-docx>=1,<2` floor was ported to `setup.cfg` (commit PARTIAL, not fully left out). |

### Batch 14 — [CR-281228622](https://code.amazon.com/reviews/CR-281228622) (0 ported — SKIP-only boundary advance)

| Upstream SHA | Repo | Verdict | Reason |
|---|---|---|---|
| [`80c564f5`](https://code.amazon.com/packages/MeshClaw/commits/80c564f5) | backend | SKIP_INTERNAL | team-manager standup-cron leak fix — `apps/builtins/team_manager/` absent; `reconcile_schedule_crons` has no fork analogue. |
| [`7c25d7ef`](https://code.amazon.com/packages/MeshClaw/commits/7c25d7ef) | backend | SKIP | v2.7 docs + AGENTS (33 files) — bulk doc snapshot for absent subsystems; fork keeps diverged specs. (Anti-miss: ran fork's 4 `TestDoctorOllamaDocker` → pass.) |
| [`78f8f60c`](https://code.amazon.com/packages/MeshClaw/commits/78f8f60c) | backend | ALREADY_PRESENT | `validate_enterprise` test patch — fork's `validate_enterprise` is default-OPEN, tests already green by a more fundamental divergence. (Mesh-2072) |
| [`9099180`](https://code.amazon.com/packages/MeshClawWebsite/commits/9099180) | frontend | SKIP | frontend AGENTS.md v2.7 — diverged convention doc; fork's `website/AGENTS.md` is its own de-Amazoned voice. |

### Batch 15 — [CR-281319232](https://code.amazon.com/reviews/CR-281319232) (8 ported, **0 left out**)

The largest real-port batch — all 8 candidates (7 backend + 1 frontend) ported.

### Batch 16 — [CR-281392951](https://code.amazon.com/reviews/CR-281392951) (9 ported, 3 left out)

| Upstream SHA | Repo | Verdict | Reason |
|---|---|---|---|
| [`ebee95e7`](https://code.amazon.com/packages/MeshClaw/commits/ebee95e7) | backend | SKIP_INTERNAL | macOS gateway **service** installer — confined to absent `scripts/darwin-gateway-service/` (Midway-gateway LaunchAgent + FDA Swift helper + own DMG builder). Not the fork's `packaging/build-desktop.sh` DMG flow. |
| [`b9036ac4`](https://code.amazon.com/packages/MeshClaw/commits/b9036ac4) | backend | SKIP_INTERNAL | Mocks `summarize_standup` in standup async tests — team_manager/standup absent. |
| [`1ae3c85`](https://code.amazon.com/packages/MeshClawWebsite/commits/1ae3c85) | frontend | SKIP | Generic `useVisibilityInterval` hook, but its only consumer is the absent midway-ttl topbar countdown; anti-miss grep found no other fork caller → would be dead code. |

### Batch 21 — CR pending (3 ported, 2 SKIP_INTERNAL)

Window: backend `b35c496b..59ec6e1d`, frontend `fdfe158b..ca99bb4`. Ported:
`59ec6e1d` (loopback-WS, → `2092347`), `d10750e2` (app-config self-heal, →
`5891deb`), `ca99bb4` (voice prewarm — the batch-20 DEFER straggler, → `d6856a2`).

| Upstream SHA | Repo | Verdict | Reason |
|---|---|---|---|
| [`5d99a8d4`](https://code.amazon.com/packages/MeshClaw/commits/5d99a8d4198a5904f91eacff87b02380bf781bcb) | backend | SKIP_INTERNAL | feat(security) MCS-Jail Midway AgentContext (Mesh-1517) — Midway/MCS coupling; `jail.py` absent, MCS-Jail Brazil dep. Its one generic hunk (`sandbox.py` `userns_available()` public alias) has **no fork consumer** — only the absent `jail.py` calls it (anti-miss (b); same precedent as batch-16 `useVisibilityInterval`). `cli.py --no-jail`, `agent.jail` enum, cli_doctor jail status, config-baseline all confined to the jail. |
| [`afed9312`](https://code.amazon.com/packages/MeshClaw/commits/afed93127c82625d7287735202eef6d449ee01da) | backend | SKIP_INTERNAL | fix(mcp-gateway) raise pooled-backend cap 20→64 — 100% confined to `mcp_gateway/` (`manager.py` `GatewaySpec` + `McpGatewayConfig` in loader.py). Fork has **no `McpGatewayConfig`** (grep empty) — no anchor. |

### Batch 29 — GitHub PR (16 ported, 5 left out) — dual-repo sync 2026-07-14

Window: backend `e870fed4..3b679515` (11 candidates) + frontend
`20f18813..2a70682` (10) = **21 triaged** via a 42-agent adversarial workflow
(analyzer + skeptic per candidate, decide by content; unanimous). **16 ported
(7 backend KEEP + 1 RFC doc + 7 frontend KEEP + 2 PARTIAL); 5 left out** (3
ALREADY_PRESENT, 2 DEFER — the comment-lifecycle pair). Full per-verdict detail:
the batch-29 block of [`last-synced.txt`](./last-synced.txt).

| Upstream commit | Repo | Verdict | Reason left out |
|---|---|---|---|
| [`38fa6701`](https://code.amazon.com/packages/MeshClaw/commits/38fa6701a8a8acd09adbb692ababc0317318fb54) | backend | ALREADY_PRESENT | `accepts_no_extension` in `/api/knowledge/config` — present at `knowledge.py` via fork PR #61. |
| [`d2b685f0`](https://code.amazon.com/packages/MeshClaw/commits/d2b685f0f51cb81787f2fb61bcba1fd2d520218e) | backend | ALREADY_PRESENT | acp session_handle review nits (dead clause, single-shot probe flag) — already in the fork. |
| [`3db7ede`](https://code.amazon.com/packages/MeshClawWebsite/commits/3db7ede) | frontend | ALREADY_PRESENT | Knowledge upload accept filter driven from backend config — present at `knowledge/index.tsx` via fork PR #61. |
| [`475146ca`](https://code.amazon.com/packages/MeshClaw/commits/475146caa019888fc5ee6784efe50c469d438452) | backend | **DEFER** | feat(artifacts) comment lifecycle — audited agent delete + orphaned-anchor detection. Builds on a persistent `ArtifactComment` store (comments.json sidecar, provider sync) that the fork does NOT have — the fork uses a session-only `InlineComment`/`CommentOverlay` model. Porting requires the whole comment base first. |
| [`86315dc`](https://code.amazon.com/packages/MeshClawWebsite/commits/86315dc) | frontend | **DEFER** | feat(artifacts) comment lifecycle UI — orphaned-anchor warning + comment activity events. Frontend pair of `475146ca`; depends on the same absent durable-comment subsystem (`CommentsSidebar.tsx` absent). |

### Batch 28 — GitHub PR (41 ported, 17 left out) — dual-repo sync 2026-07-13

Branch `sync/beta-2026-07-13` off `origin/main` `4f942f3`. Window: backend
`5dbb7778..e870fed4` (31 candidates) + frontend `3858e573..20f18813` (20) +
7 batch-27 DEFER re-audits = **58 triaged** via a 116-agent adversarial workflow
(analyzer + skeptic per candidate, decide by content). **41 ported (22 backend +
19 frontend, incl. 9 PARTIAL); 17 left out** (10 SKIP_INTERNAL, 3 ALREADY_PRESENT,
4 DEFER). Batch-27 DEFER rescues ported this batch: `3858e573` (CommandPalette
tab-strip scroll), `5360aa4d` (split-view live tool streaming — the per-slot
substrate now exists in the fork), `3acce399` (RL-v2 UI, PARTIAL — engine chooser
dropped pending PR #57), and `d2240c48` resolved as PARTIAL (generic `_dm_owner`
redacting DM exit point ported; tunnel hunks remain absent). Full per-verdict
detail: the batch-28 block of [`last-synced.txt`](./last-synced.txt).

| Upstream commit | Repo | Verdict | Reason left out |
|---|---|---|---|
| [`0e7322ce`](https://code.amazon.com/packages/MeshClaw/commits/0e7322ce093aa5e1a640e09d9e631a23c65ccc39) | backend | SKIP_INTERNAL | MarkBin alternative publish provider — the artifact publish-provider subsystem (Harmony/Chorus) is absent. |
| [`0fe90c30`](https://code.amazon.com/packages/MeshClaw/commits/0fe90c30e3dd20e921c3313978cba583a9c3ac00) | backend | SKIP_INTERNAL | mimir SIM ticket assignee via assigneeIdentity — mimir integration absent. |
| [`d87672ea`](https://code.amazon.com/packages/MeshClaw/commits/d87672ea977fdbc2f4063f114a13c4d4d7a24593) | backend | SKIP_INTERNAL | File-backed publications surface/pull upstream edits — publications subsystem absent. |
| [`4a41b8a3`](https://code.amazon.com/packages/MeshClaw/commits/4a41b8a3207682592b2989fe75b02229492f48dd) | backend | SKIP_INTERNAL | taskkeeper strip slack-mcp slack-user-content wrapper — taskkeeper absent. |
| [`5cdfb619`](https://code.amazon.com/packages/MeshClaw/commits/5cdfb619fa20f8097bb9f058194b95e2a5e196c5) | backend | SKIP_INTERNAL | Keyword-negation false positives — confined to the absent secretary keyword-matching regex. |
| [`a5979e42`](https://code.amazon.com/packages/MeshClaw/commits/a5979e425c1c6fcef0265a02d47874d714671935) / [`6055397`](https://code.amazon.com/packages/MeshClawWebsite/commits/605539774c78f71645b6aa5a946a82f1ce08d69b) | both | SKIP_INTERNAL | Edit comment body — local + Chorus in-place remote edit (BE+FE pair) — Chorus publish + durable artifact-comment subsystem absent. |
| [`d0a78b07`](https://code.amazon.com/packages/MeshClaw/commits/d0a78b070b7058daad61b603dd19b67a0b295a9a) | backend | SKIP_INTERNAL | Add defusedxml to install_requires — a dead dep in the fork (no consumer). |
| [`7cfeed18`](https://code.amazon.com/packages/MeshClaw/commits/7cfeed18289f14deb72fae285e501ab75772a503) | backend | SKIP_INTERNAL | Work Summary app registry row — `app-registry.json` is `[]` by design. |
| [`1984ccc`](https://code.amazon.com/packages/MeshClawWebsite/commits/1984ccc4707eec0f3b3f04d143a84e17173e7f45) | frontend | SKIP_INTERNAL | Gate Shared on provider supports_shared — publish UI surface absent. |
| [`70898ca5`](https://code.amazon.com/packages/MeshClaw/commits/70898ca54a64dc0a56666e7ae41444767efbc4e0) / [`d5819090`](https://code.amazon.com/packages/MeshClaw/commits/d58190905baf848757d6dd85af91f9984bab73d4) | backend | ALREADY_PRESENT | AcpRuntime teardown termination + age/RSS recycle — the fork did both natively via fork PR [#43](https://github.com/kirodotdev/KiroCrew/pull/43). |
| [`e4fc0ce`](https://code.amazon.com/packages/MeshClawWebsite/commits/e4fc0ce1a18776119e646fb6031beb67a96f2339) | frontend | ALREADY_PRESENT | Follow-up-bar clickable scroll arrows — present via fork PR [#45](https://github.com/kirodotdev/KiroCrew/pull/45). |
| [`3e5d7132`](https://code.amazon.com/packages/MeshClaw/commits/3e5d7132e46c3fd7dd4394dd182fba9f58656025) | backend | **DEFER** | Reject type-unsafe authored workflow scripts — the dynamic-workflows engine is absent from the fork; fork PR [#57](https://github.com/kirodotdev/KiroCrew/pull/57) (workflows engine) carries all three fixes by content. Resolves when that PR lands. |
| [`f8383887`](https://code.amazon.com/packages/MeshClaw/commits/f8383887d8dec0f3583a3cbcf674468172e3dd45) | backend | **DEFER** | Hero SVGs for deploy_web/workflows — needs the `fd633154` builtin-ui materialization substrate. |
| [`0000f561`](https://code.amazon.com/packages/MeshClawWebsite/commits/0000f561ecc8f750b51203290279dcdb9ee9d6a7) / [`f746d60`](https://code.amazon.com/packages/MeshClawWebsite/commits/f746d60d6beeb1b9219bcbcb0322986bb05a3793) | frontend | **DEFER** | Comment-anchor offset fix / collapse empty comment sidebar — the anchored/durable artifact-comment subsystem is absent; port when the `affffcff` base lands. |

### Batch 27 — GitHub PR (73 ported, 62 left out) — FIRST batch on the public GitHub fork

**First sync run in the public GitHub checkout** (`kirodotdev/KiroCrew`, ships via PR, not
CRUX). The boundary file was **stale** (recorded batch-25) but the fork had since done batch-26
(PR #18) plus a large **GitHub-native porting wave** (PRs #10–#38) that did not cite upstream SHAs —
so the fork is **bidirectional** with MeshClaw, not strictly downstream. Every candidate was triaged
**by content** against the fork file (SHA reachability is meaningless here). Window: backend
`6cf3aae0..5dbb7778` (88) + frontend `9b512ca..3858e573` (47) = 135, via a 270-agent adversarial
Workflow (analyzer + skeptic per commit; 2 skeptics API-errored, hand-confirmed KEEP).
**73 ported (58 KEEP + 15 PARTIAL: 47 backend + 26 frontend); 62 left out** (22 SKIP_INTERNAL,
4 SKIP_NONKIROACP, 29 ALREADY_PRESENT, 7 DEFER). Branch `sync/beta-2026-07-10`. Full per-verdict
detail: the batch-27 block of [`last-synced.txt`](./last-synced.txt) + the PR provenance comment.

| Upstream commit | Repo | Verdict | Reason left out |
|---|---|---|---|
| [`7608b99f`](https://code.amazon.com/packages/MeshClaw/commits/7608b99f3c0faef38e808e2f37922199dd6eaf43) | backend | SKIP_INTERNAL | Route WritingReview via backend MCP — `writing_review/` dir absent. |
| [`7bb86a44`](https://code.amazon.com/packages/MeshClaw/commits/7bb86a44045ada061db55f858ca85d6d16449f1a) | backend | SKIP_INTERNAL | mcp-gateway respawn dead backend — `mcp_gateway/` daemon absent. |
| [`8b5351f1`](https://code.amazon.com/packages/MeshClaw/commits/8b5351f1170d89193b51d8ccfd7ec1e795379363) | backend | SKIP_INTERNAL | Bot-message filter confined to `secretary.py` `SecretaryPoller` — secretary absent. |
| [`9a696e65`](https://code.amazon.com/packages/MeshClaw/commits/9a696e650caa3d8c8a853695478752f6673e70c8) | backend | SKIP_INTERNAL | ChorusProvider + multi-provider publishing — artifact publish/Chorus (Quip-replacement via AIM) absent. |
| [`b944c74d`](https://code.amazon.com/packages/MeshClaw/commits/b944c74de36a223aea965a736e5bb08a0bb77403) | backend | SKIP_INTERNAL | Tunnel health-probe in-process TLS — `tunnel/manager.py` is a stub. |
| [`a179b9e0`](https://code.amazon.com/packages/MeshClaw/commits/a179b9e0d3b479b44c6f2aa11b035ab4ae0ccb06) | backend | SKIP_INTERNAL | DM owner when AEA tunnel off — AEA-tunnel subsystem absent. |
| [`cec38579`](https://code.amazon.com/packages/MeshClaw/commits/cec385796075d437a2bcd2464d559d0d22ecf8fe) | backend | SKIP_INTERNAL | security-assistance recommendation engine — Amazon-internal builder-mcp primitive. |
| [`9bbcb048`](https://code.amazon.com/packages/MeshClaw/commits/9bbcb048b1c84c1d4e3d266a2539afbbeb239c00) | backend | SKIP_INTERNAL | Bundle CHANGELOG into toolbox package — `setup.py` ToolboxBundlerCommand (toolbox absent). |
| [`cb6f8f19`](https://code.amazon.com/packages/MeshClaw/commits/cb6f8f19a5e540543a7997a6dfbfc43e15b85ece) | backend | SKIP_INTERNAL | Pin launcher site-packages to ABI — Amazon toolbox launcher `configuration/toolbox/bin/meshclaw` absent. |
| [`c637016b`](https://code.amazon.com/packages/MeshClaw/commits/c637016bd0c1d0bb5672fa5e8e2442fe696f0a64) | backend | SKIP_INTERNAL | Skip lxml/pdfplumber tests — a Brazil-version-set-only failure; the fork installs these normally. |
| [`7f86241d`](https://code.amazon.com/packages/MeshClaw/commits/7f86241d12a0fa572b5741192dfc3b5bfc53b6b7) | backend | SKIP_INTERNAL | `.mailmap` for internal empty-email committers — no OSS relevance. |
| [`4f753ed0`](https://code.amazon.com/packages/MeshClaw/commits/4f753ed06525b83813fb18fc44814d7d97d24b8f) | backend | SKIP_INTERNAL | mwinit modal TTL cache reset — Midway/`midway.py` is a stub. |
| [`1f6b7982`](https://code.amazon.com/packages/MeshClaw/commits/1f6b79827607a2924142cbea5100a29602f29e2e) | backend | SKIP_INTERNAL | Register auto-improvement app — `app-registry.json` is `[]` by design. |
| [`81d19cef`](https://code.amazon.com/packages/MeshClaw/commits/81d19cef7c962acc00f2d84aaf10f2b071a4ee9d) | backend | SKIP_INTERNAL | Add Papyrus to App Store — internal app-registry data row. |
| [`0e9c9ffc`](https://code.amazon.com/packages/MeshClaw/commits/0e9c9ffc512804e54f24a3c04f660c887b626f65) / [`d53b497b`](https://code.amazon.com/packages/MeshClaw/commits/d53b497ba84e5f898d86c6ea1180da4c362e4c69) | backend | SKIP_INTERNAL | Version bumps 3.1.1→3.2.0→3.2.1 + changelog — fork is 0.1.0. |
| [`de64d07b`](https://code.amazon.com/packages/MeshClaw/commits/de64d07bcaa29cf936e5949549612d1f0f734ebb) | backend | SKIP_INTERNAL | artifact_get_comments full body — artifact-comments subsystem absent. |
| [`321f9fbc`](https://code.amazon.com/packages/MeshClawWebsite/commits/321f9fbcd6aa45b09d326c7a839546cac7ca8899) / [`eed2773d`](https://code.amazon.com/packages/MeshClawWebsite/commits/eed2773df99fd7a4d713bc1a5d517356372e67f6) | frontend | SKIP_INTERNAL | WritingReview context dialogs / resume scan — `WritingReviewPage` absent. |
| [`3a182608`](https://code.amazon.com/packages/MeshClawWebsite/commits/3a1826087667b36e8afc3b56984d77b1f3b46597) | frontend | SKIP_INTERNAL | code-reviewer workspace pin/drag — `apps/code-reviewer/CodeReviewerPage` absent. |
| [`5e13d8b7`](https://code.amazon.com/packages/MeshClawWebsite/commits/5e13d8b75f5b06d3d75e3f3ed4f4ed05ccdb82c6) | frontend | SKIP_INTERNAL | Provider-aware share panel + LIVE mode — Harmony Artifactory + Chorus publish absent. |
| [`92104fcc`](https://code.amazon.com/packages/MeshClawWebsite/commits/92104fccaa5364243a21fbabcf5ae752d84e1d1d) | frontend | SKIP_INTERNAL | Tunnel status tile — AEA-tunnel subsystem absent. |
| [`3997c2ae`](https://code.amazon.com/packages/MeshClaw/commits/3997c2aefb4856fb1c7bff77c34702ccd074f142) / [`7b74179b`](https://code.amazon.com/packages/MeshClaw/commits/7b74179b3065e830f8bf927b66188b4c6fb0a683) | backend | SKIP_NONKIROACP | `/api/usage/cost` + per-turn token tracking — the deleted `claude_code` provider spend surface. |
| [`c6bc2744`](https://code.amazon.com/packages/MeshClawWebsite/commits/c6bc2744182f901c3e31c74c5e1747a392ff4184) / [`f6e38b3b`](https://code.amazon.com/packages/MeshClawWebsite/commits/f6e38b3b625d46f35be0dcfcc844f490ed79c9d2) | frontend | SKIP_NONKIROACP | Claude $-spend top-bar pill / model-label dedup — provider-spend + provider-selection surface. |
| [`3e5d7132`](https://code.amazon.com/packages/MeshClaw/commits/3e5d7132e46c3fd7dd4394dd182fba9f58656025) | backend | DEFER (still open) | Reject type-unsafe authored workflow scripts — the dynamic-workflows engine is absent from the fork. Re-audited in batch-28: still DEFER — fork PR #57 carries all three fixes by content. |
| [`f8383887`](https://code.amazon.com/packages/MeshClaw/commits/f8383887d8dec0f3583a3cbcf674468172e3dd45) | backend | DEFER (still open) | Hero images for deploy_web/workflows — asset-only, low value; workflows page absent. Re-audited in batch-28: still DEFER — needs the `fd633154` builtin-ui materialization substrate. |
| ~~[`d2240c48`](https://code.amazon.com/packages/MeshClaw/commits/d2240c48bbd72de6e7141fbc313c1390f9087760)~~ | backend | ~~DEFER~~ → **PORTED (PARTIAL) in batch-28** | Was: tunnel-off Slack-link warning hardening. Batch-28 rescued the generic `_dm_owner`/`_dispatch_owner_dm` redacting DM exit point (fork `5d66a45`); the tunnel `resolve_tunnel_enabled` hunks remain absent-subsystem drops. |
| [`0000f561`](https://code.amazon.com/packages/MeshClawWebsite/commits/0000f561ecc8f750b51203290279dcdb9ee9d6a7) | frontend | DEFER (still open) | Comment-anchor highlight offset — persisted file-artifact comment subsystem absent (same as batch-22/23 defers). Re-audited in batch-28: still DEFER (with the new `f746d60`). |
| ~~[`3acce399`](https://code.amazon.com/packages/MeshClawWebsite/commits/3acce39988b768a7c8e6dfa15b5bc5ab8ce0d52b)~~ | frontend | ~~DEFER~~ → **PORTED (PARTIAL) in batch-28** | Was: Research-Lab v2 UI — FE deemed absent. Batch-28 re-audit found the fork DOES ship `apps/auto-research/` FE; ported multi-line inputs + research-only notice (fork `ff41856`), dropped the engine chooser pending PR #57. |
| ~~[`5360aa4d`](https://code.amazon.com/packages/MeshClawWebsite/commits/5360aa4d0e8b2133de9263878f87e219bab81a87)~~ | frontend | ~~DEFER~~ → **PORTED in batch-28** | Was: rich tool streaming in split-view panes — substrate absent. The per-slot substrate (slot-aware `sseToolActivity`/`sseToolResult` in `chatSlice.ts`) landed since; ported as fork `7631aaf`. |
| ~~[`3858e573`](https://code.amazon.com/packages/MeshClawWebsite/commits/3858e57307ea4d0261ac7e56b2813a3ab40aebcf)~~ | frontend | ~~DEFER~~ → **PORTED in batch-28** | Was: CommandPalette tab-strip horizontal scroll (follow-up to `12bfe897`). Ported as fork `005cc26`. |

> The 15 PARTIALs (generic hunks kept, internal/absent dropped) are keepers — their dropped
> sub-hunks are recorded in the batch-27 block of [`last-synced.txt`](./last-synced.txt) and each
> port commit's body. Notable: `d759d3b3` (kept the 3 `asyncio.to_thread` SEL-offload wraps, bulk
> ALREADY_PRESENT), `2e960fe2` (kept the dashboard steer wiring; ACP-layer already present),
> `41ad420c` (dropped the `mcp_gateway` overlay kwargs), `9daac30f` (research-only mode), `12bfe897`
> (Command Palette — dropped absent-endpoint providers), the Fable-5 pair (model-registry only).

### Batch 25 — CR pending (9 ported, 6 left out)

Window: backend `bfa7b4e8..6cf3aae0` (13), frontend `0f2c062..9b512ca` (2). Stacked on the
batch-22+23+24 branch after rebasing all 141 commits onto the advanced `origin/mainline`
(`55d0ccb` = fork-native "Pentest round 3": fail-closed SEL audit, REST input validation,
legacy dashboard XSS). Triaged via a 30-agent adversarial Workflow (analyzer + skeptic per
commit + synthesis; 1 frontend analyzer hit the StructuredOutput retry cap → triaged by hand),
**zero skeptic verdict-flips**. **9 ported (6 KEEP + 3 PARTIAL: 8 backend + 1 frontend)**;
**6 left out**. Ported keepers (not in this table): `4aba97e9` app-MCP health-gate, `284af3ea`
cookie-Secure via X-Forwarded-Proto, `b0140b0c` Permissions-Policy clipboard header, `f4acf37e`
ACP keepalive watchdog, `210baed2` pytest-xdist cap, `0d9797e` (FE) per-tab remote host; PARTIALs
`99041753` (bulk ALREADY_PRESENT via fork-native `1c8f980`+`aef7a86`; ported the 2 residual
edit-mode redactor one-liners + 1 test), `bf9663df` (workspace_root realpath; dropped the deleted
CC-encoder slug test), `76cfc2e0` (rescued the 2 subagent-path redaction/content.text fixes; the
3900-line multiplex rewrite DEFERRED). See the batch-25 block of [`last-synced.txt`](./last-synced.txt)
and the CR comment for full port provenance.

| Upstream commit | Repo | Verdict | Reason left out |
|---|---|---|---|
| [`6cf3aae0`](https://code.amazon.com/packages/MeshClaw/commits/6cf3aae0ad1ec28e5e5d443a17d24daf99e33457) | backend | SKIP_INTERNAL | docs: document the Shared MCP Gateway (kiro-cli only) — docs-only (+189 lines/3 files) for the **absent** `mcp_gateway/` subsystem (ls-confirmed absent; the only fork refs are lineage comments in `env.py`/`subagent.py`). No `mcp_gateway.*` config keys in the fork, no generic prose bundled in — nothing to rescue. |
| [`66c022fc`](https://code.amazon.com/packages/MeshClaw/commits/66c022fc8a438a112a35b1394b5e0df12835050b) | backend | SKIP_INTERNAL | chore(autosde): flag SDK bypass in builtin apps — single hunk in `AUTOSDE.yaml`, **absent** in the fork (Brazil/AutoSDE internal build tooling). No code, no test. |
| [`21087188`](https://code.amazon.com/packages/MeshClaw/commits/21087188660c2c83ffdb17caa6e15a9cb2c9ff15) | backend | SKIP_INTERNAL | feat: register planning app — single data append of a `MeshClawApp-Planning` row to `app-registry.json`; the fork ships `[]` by design and the load-bearing value **is** the internal repo pointer. No rename rescues it. |
| [`a4a35008`](https://code.amazon.com/packages/MeshClaw/commits/a4a35008582cd807d68bc074ed4604b6dda6f64d) | backend | SKIP_INTERNAL | Warn when Slack auth window is about to expire — new `slack/expiry_warning.py` GATED on the fork-**removed** `challenge_window` subsystem (`challenge_window_grants().remaining_secs`); no `challenge_window.py`/`expiry_warning.py` in the fork (find-confirmed absent). The `extract_options` hoist is a verified no-op. |
| [`2bbf2e0b`](https://code.amazon.com/packages/MeshClaw/commits/2bbf2e0b2f5e655efdd563484da994fadb543815) | backend | **DEFER** | feat(pod): `meshclaw pod` isolated worktree test instances — 2231-line brand-new QA harness (not a fix/security patch) coupled to `brazil-build`/`brazil-recursive-cmd`/`~/.toolbox/bin`/`~/.aim` + a Bedrock-shaped `AWS_*` env-scrub. De-Amazoning is a **re-architecture, not a rename** (setuptools+npm build, public worktree layout, kiro-cli-only env). Legitimate DEFER per the SKILL cron rule — leave for human review. |
| [`9b512ca`](https://code.amazon.com/packages/MeshClawWebsite/commits/9b512cab7597ec2b791da50921b10128099fff52) | frontend | SKIP_INTERNAL | chore(autosde): flag SDK bypass in dashboard app pages — single 50-line rule in `website/AUTOSDE.yaml` (a stale Brazil leftover not consumed by the Vite build), scoped to the **absent** `src/pages/apps/**`. No runtime code, no test. |

### Batch 24 — CR pending (53 ported, 25 left out)

Window: backend `1926f16f..bfa7b4e8` (47), frontend `a6b1fc7..0f2c062` (31). Stacked on the
batch-22+23 branch (rebased onto the advanced mainline first). Triaged via a 156-agent adversarial
Workflow + 1 manual (`8a6bfbc0` API-flake → KEEP). **53 ported (36 KEEP + 17 PARTIAL: 32 backend +
21 frontend).** The fork base already did instances auto-reconnect + pentest auth NATIVELY, so those
upstream commits are ALREADY_PRESENT (verified by content).

| Upstream SHA | Repo | Verdict | Reason |
|---|---|---|---|
| [`247aa7d8`](https://code.amazon.com/packages/MeshClaw/commits/247aa7d8) | backend | SKIP_INTERNAL | Add `pensieve` to App Store registry — internal app data. |
| [`da953198`](https://code.amazon.com/packages/MeshClaw/commits/da953198) | backend | SKIP_INTERNAL | mcp-gateway singleton flock — `mcp_gateway/` absent. |
| [`ffee2086`](https://code.amazon.com/packages/MeshClaw/commits/ffee2086) | backend | SKIP_INTERNAL | Add `standup-zen` to app registry — internal app data. |
| [`7ae12831`](https://code.amazon.com/packages/MeshClaw/commits/7ae12831) | backend | SKIP_INTERNAL | Slack sliding-idle + session-ceiling auth window — internal auth surface (also the `fcee75b9` v3.1.1 mainline hotfix). |
| [`0cf627ea`](https://code.amazon.com/packages/MeshClaw/commits/0cf627ea) | backend | SKIP_INTERNAL | Test-only forward-port of a `create=True` stub to an internal v3.1.1 hotfix. |
| [`79868424`](https://code.amazon.com/packages/MeshClaw/commits/79868424) | backend | SKIP_NONKIROACP | code-review-sage selectable model/effort — subsystem absent + a provider surface. |
| [`ca927a74`](https://code.amazon.com/packages/MeshClaw/commits/ca927a74) | backend | SKIP_INTERNAL | TOOLBOX_PUBLISH.md docs — toolbox bundler absent in setuptools build. |
| [`1f4ae9f7`](https://code.amazon.com/packages/MeshClaw/commits/1f4ae9f7) | backend | SKIP_INTERNAL | v3.1.1 version bump + CHANGELOG — fork is 0.1.0. |
| [`ed2f53b6`](https://code.amazon.com/packages/MeshClaw/commits/ed2f53b6) | backend | ALREADY_PRESENT | Instances auto-reconnect/sticky/error-aware tabs — fork did it natively (`c033f4a`+`aef7a86`). |
| [`e8a1010b`](https://code.amazon.com/packages/MeshClaw/commits/e8a1010b) | backend | SKIP_INTERNAL | code-review-sage CR-title redaction — subsystem absent. |
| [`2e3e7ed4`](https://code.amazon.com/packages/MeshClaw/commits/2e3e7ed4) | backend | ALREADY_PRESENT | Pentest auth hardening (per-session logout, app-token scope, link-to-session exchange) — fork did it natively (`5700059`+`24e94b9`). |
| [`37aa5567`](https://code.amazon.com/packages/MeshClaw/commits/37aa5567) | backend | SKIP_INTERNAL | mcp-gateway warm-pool prewarming + hit-rate metric — `mcp_gateway/` absent. |
| [`328d704e`](https://code.amazon.com/packages/MeshClaw/commits/328d704e) | backend | SKIP_INTERNAL | secretary Slack-permalink inbox field — secretary backend absent. |
| [`3d466e6c`](https://code.amazon.com/packages/MeshClaw/commits/3d466e6c) | backend | SKIP_NONKIROACP | Auto approval mode — VERIFIED hard-gated on the claude_code backend + native afk classifier binary. |
| [`a8224f18`](https://code.amazon.com/packages/MeshClaw/commits/a8224f18) | backend | SKIP_NONKIROACP | Miami Vice 2080 theme logo asset — cosmetic internal theme. |
| [`16c5375`](https://code.amazon.com/packages/MeshClawWebsite/commits/16c5375) | frontend | SKIP_INTERNAL | Clear composer on Slack challenge-handoff — targets the deleted `send_channel_challenge` surface (`9d606bd`). |
| [`ef92034`](https://code.amazon.com/packages/MeshClawWebsite/commits/ef92034) | frontend | ALREADY_PRESENT | Instances auto-reconnect FE — fork did it natively (`c033f4a`+`8603ed7`). |
| [`7c75dec`](https://code.amazon.com/packages/MeshClawWebsite/commits/7c75dec) | frontend | SKIP_INTERNAL | tui Ctrl+O command palette — `website/tui/` absent in fork. |
| [`d331022`](https://code.amazon.com/packages/MeshClawWebsite/commits/d331022) | frontend | ALREADY_PRESENT | Reconnect tab on click when tunnel dropped — fork did it natively. |
| [`e3641e2`](https://code.amazon.com/packages/MeshClawWebsite/commits/e3641e2) | frontend | SKIP_INTERNAL | taskkeeper editable descriptions — taskkeeper backend + app dir absent. |
| [`056aa42`](https://code.amazon.com/packages/MeshClawWebsite/commits/056aa42) | frontend | SKIP_NONKIROACP | Miami Vice 2080 theme — cosmetic internal theme refactor. |
| [`744f5ad`](https://code.amazon.com/packages/MeshClawWebsite/commits/744f5ad) | frontend | SKIP_INTERNAL | mwinit modal keydown bubbling — mwinit is a stubbed internal auth surface. |
| [`2b91d0a`](https://code.amazon.com/packages/MeshClawWebsite/commits/2b91d0a) | frontend | SKIP_INTERNAL | Render Slack permalink in secretary detail panel — secretary subsystem absent. |
| [`42ebd21`](https://code.amazon.com/packages/MeshClawWebsite/commits/42ebd21) | frontend | SKIP_NONKIROACP | Auto approval mode segment in chat UI — provider-selection surface, hard-gated on claude_code. |
| [`3da3f45`](https://code.amazon.com/packages/MeshClawWebsite/commits/3da3f45) | frontend | SKIP_INTERNAL | MCP gateway prewarm hit-rate on overview card — `mcp_gateway/` backend absent. |

> **Note:** the 17 PARTIALs (`8b05b3e4` drop AUTOSDE, `fd633154`/`0951476` drop .meshclaw-dev app
> manifests+hero assets, `e5210223`/`9586ad6` drop mcp_gateway hunks, `dd27186` drop tui Message.tsx,
> `b1edc01e` drop Brazil Config, `372329d1` drop Java/Brazil doctor hints, `4ab933e` drop absent-file
> storage sites, etc.) are KEEPERS — their dropped internal sub-hunks are recorded in the batch-24 block
> of [`last-synced.txt`](./last-synced.txt) + the CR comment.

### Batch 23 — CR pending (4 ported, 2 SKIP_INTERNAL)

Window: backend `58e0abb0..1926f16f` (4), frontend `0e1f442..a6b1fc7` (2). Stacked
on the batch-22 worktree branch (one coherent CR). Ported: `eb32b8a2` (recent-projects
cap 100, → `e2d58b2`), `6eaab8cb` (self-contained final-message rule, → `cfadb4a`),
`c6fa6489` (knowledge auto-dedup, → `72ef8df`), `384a5d1` (ProjectPicker searchable
Recent + drill-on-slash, → `c2eb9f6`).

| Upstream SHA | Repo | Verdict | Reason |
|---|---|---|---|
| [`1926f16f`](https://code.amazon.com/packages/MeshClaw/commits/1926f16f957d7ee8c517b9ecc364917b81c87b48) | backend | SKIP_INTERNAL | fix(browser) detect Playwright MCP via `aim mcp list -i -o JSON` instead of a substring check. The fork's `is_playwright_installed()` / `ensure_playwright_installed()` are de-Amazoned OSS **no-op stubs** (the `aim` package manager doesn't exist in OSS — the very coupling the fork removed); the fix targets the `aim mcp` call path, which has no fork anchor. |
| [`a6b1fc7`](https://code.amazon.com/packages/MeshClawWebsite/commits/a6b1fc714ff2ae831c5b56a98963f19a6635885b) | frontend | SKIP_INTERNAL | fix: anchor widget comment to the selected occurrence (Range offset vs `indexOf`). The anchored-comment bridge it patches (`COMMENT_BRIDGE_BODY` / `selectionContext` / `FileArtifactComments`) is **absent** in the fork's diverged `widgetSrcdoc.ts` (332 vs 560 lines) — the same absent subsystem as the batch-22 DEFER `4685d34`. Anti-miss: the file exists, but the changed code does not. |

### Batch 22 — CR pending (80 ported, 25 left out)

Window: backend `59ec6e1d..58e0abb0` (68 candidates), frontend `ca99bb4..0e1f442`
(37). Triaged via a 211-agent adversarial Workflow (analyzer + skeptic per commit +
synthesis critic). 80 keepers (68 KEEP + 12 PARTIAL: 49 backend + 31 frontend).
Notable ports: `b40982aa` OpenAI-compatible `/v1/chat/completions`; `a636b1ba`
`set_project` MCP tool; `ce4f6c49` per-spawn basename child-tracking; `38173673`
NL search recall; `6d8008a` Radix sidebar menus; `8b6807c` electron wedged-backend
auto-recover; `cca1191` project-agent UI (unblocked by BE `7519a5d9`). FOLD-INs:
`8e24a0fc`→`4425bd52`, `64599424`→`f8e5b36f` (one commit each).

| Upstream SHA | Repo | Verdict | Reason |
|---|---|---|---|
| [`54f01fc0`](https://code.amazon.com/packages/MeshClaw/commits/54f01fc0) | backend | SKIP_INTERNAL | feat(writing-review) publish review as Artifactory HTML report — `writing_review/` + Harmony/Artifactory publish absent. |
| [`bc92b8b5`](https://code.amazon.com/packages/MeshClaw/commits/bc92b8b5) | backend | SKIP_INTERNAL | fix(writing-review) oversized-report test 25 MiB cap — subsystem absent. |
| [`25feb233`](https://code.amazon.com/packages/MeshClaw/commits/25feb233) | backend | SKIP_INTERNAL | feat bidirectional artifact sync (clone/pull/snapshot push) via Harmony/Artifactory — absent subsystem. |
| [`6ecb54d2`](https://code.amazon.com/packages/MeshClaw/commits/6ecb54d2) | backend | SKIP_INTERNAL | fix(code-review-sage) self-heal resolved_paths — `code_reviewer` backend absent. |
| [`3a1ea4d5`](https://code.amazon.com/packages/MeshClaw/commits/3a1ea4d5) | backend | SKIP_INTERNAL | fix(mcp-gateway) guard empty EXTRA under set -u — `mcp_gateway/` absent. |
| [`e08f3e4b`](https://code.amazon.com/packages/MeshClaw/commits/e08f3e4b) | backend | SKIP_INTERNAL | fix(mcp-gateway) pre-flight backend spawn — `mcp_gateway/` absent. |
| [`fc6a8c12`](https://code.amazon.com/packages/MeshClaw/commits/fc6a8c12) | backend | SKIP_INTERNAL | fix(mcp-gateway) disambiguate pooled backend by args — `mcp_gateway/` absent. |
| [`0d978546`](https://code.amazon.com/packages/MeshClaw/commits/0d978546) | backend | SKIP_INTERNAL | fix: stop leaking PYTHONPATH/PYTHONHOME into mcp — confined to the absent `mcp_gateway` spawn path. |
| [`16fa88d2`](https://code.amazon.com/packages/MeshClaw/commits/16fa88d2) | backend | SKIP_INTERNAL | fix(jail) skip MCS-Jail under WSL — `jail.py` absent in fork. |
| [`71ac5fcc`](https://code.amazon.com/packages/MeshClaw/commits/71ac5fcc) | backend | SKIP_INTERNAL | fix POST→GET downgrade on Midway redirect in Taskei GraphQL client — both internal. |
| [`803af9de`](https://code.amazon.com/packages/MeshClaw/commits/803af9de) | backend | SKIP_INTERNAL | feat(cli) pluggable preflight checks with midway refresh — Midway absent. |
| [`938a147d`](https://code.amazon.com/packages/MeshClaw/commits/938a147d) | backend | SKIP_INTERNAL | feat(taskkeeper) PATCH task-edit endpoint — taskkeeper backend absent. |
| [`20ae2b81`](https://code.amazon.com/packages/MeshClaw/commits/20ae2b81) | backend | SKIP_INTERNAL | Add weblab-radar to app registry — internal App Store entry. |
| [`cf093d08`](https://code.amazon.com/packages/MeshClaw/commits/cf093d08) | backend | SKIP_INTERNAL | feat(apps) add mindcraft to App Store registry — internal app entry. |
| [`48f9627e`](https://code.amazon.com/packages/MeshClaw/commits/48f9627e) | backend | SKIP_INTERNAL | fix: pin 24/7 service to auto-updating toolbox build — Brazil/toolbox absent. |
| [`9dca095`](https://code.amazon.com/packages/MeshClawWebsite/commits/9dca095) | frontend | SKIP_INTERNAL | feat(workflows) Workflows tab + chat progress tree — workflows subsystem absent (4 generic guard-fixes ride backend `a48502a7` PARTIAL). |
| [`cdd68ecb`](https://code.amazon.com/packages/MeshClaw/commits/cdd68ecb) | backend | SKIP_NONKIROACP | fix(claude_code) resolve 1M context for opus-4.8 — confined to `providers/claude_code.py` (non-KiroACP provider). |
| [`d5dc40e`](https://code.amazon.com/packages/MeshClawWebsite/commits/d5dc40e) | frontend | SKIP_NONKIROACP | feat(writing-review) Publish-to-Artifactory button — absent app + internal publish surface. |
| [`186aeaa`](https://code.amazon.com/packages/MeshClawWebsite/commits/186aeaa) | frontend | SKIP_NONKIROACP | feat bidirectional artifact sync UI (upstream banner + remote artifacts) — absent Harmony/Artifactory. |
| [`823ec07`](https://code.amazon.com/packages/MeshClawWebsite/commits/823ec07) | frontend | SKIP_NONKIROACP | feat(secretary) auto-dismiss-on-reply toggle UI — SecretaryPage absent (generic config field rides backend `0e139747` PARTIAL). |
| [`4ff05ccf`](https://code.amazon.com/packages/MeshClaw/commits/4ff05ccf) | backend | ALREADY_PRESENT | Revert of `f63dfe53`'s auto-improvement app — fork content already equals each hunk's post-revert state. |
| [`4685d34`](https://code.amazon.com/packages/MeshClawWebsite/commits/4685d34) | frontend | DEFER (held) | feat(chat) clickable artifact refs + Artifacts tab — prereq `affffcff` (artifact-comments CX, `useCommentBridge`/`FileArtifactComments`) is NOT in this batch and absent in fork. Port after that base lands. |

> **Note:** `8e24a0fc` and `64599424` were DEFER→FOLDED into their prereqs
> (`4425bd52`, `f8e5b36f`) as single commits, and `cca1191` was DEFER→ported
> (unblocked by backend `7519a5d9` the same batch) — so they are KEEPERS, not
> left out. The PARTIAL drops (internal sub-hunks dropped inside ported commits:
> jail, workflows engine, `usage_upload`/`_patch`, `rewrite_agents` overlay,
> internal-subsystem doc prose, `providers/claude_code.py`) are recorded in the
> batch-22 block of [`last-synced.txt`](./last-synced.txt) and the CR comment.

> **Batches 19 & 20 were not back-filled into this table** — their exhaustive
> per-commit left-out provenance lives in the published CR comments on
> [CR-282682422](https://code.amazon.com/reviews/CR-282682422) (batch-19) and
> [CR-283464369](https://code.amazon.com/reviews/CR-283464369) (batch-20, rev 2).

### Batch 18 — [CR-281902310](https://code.amazon.com/reviews/CR-281902310) (44 ported, 2 DEFER + 13 SKIP_INTERNAL)

> ⚠️ **The exhaustive provenance comment was NEVER posted for this batch**
> (`allComments` is empty), and the CR description/commit bodies **name none of
> the 13 SKIP_INTERNAL SHAs** — they list only categories. The SHA list below is
> therefore reconstructed from the boundary file (`last-synced.txt`) and is the
> **only surviving record** of these SHAs. The 2 DEFER/flagged items are in the
> [Human-decision section](#human-decision-items-defer--flagged).

| Upstream SHA(s) | Repo | Verdict | Reason |
|---|---|---|---|
| `2b95f6ac`, `fc06e9ab` | — | SKIP_INTERNAL | AIM auto_update — absent. |
| `6d07a290`, `45821ff` | — | SKIP_INTERNAL | writing-review — absent. |
| `24745b38` | frontend | SKIP_INTERNAL | oncall-radar App Store entry — `app-registry.json` is `[]` by design. |
| `231cb2dc` | backend | SKIP_INTERNAL | code-approvers — Brazil infra. |
| `a6a7c2db` | frontend | SKIP_INTERNAL | AppSenseAIUsage telemetry — RUM stub. |
| `24f23968` | frontend | SKIP_INTERNAL | Knight Rider *world* — parody theme (like LCARS), absent substrate. |
| `7c9c140f`, `92a1d3bb`, `94196d1a`, `fedf6e4d` | — | SKIP_INTERNAL | changelog/version bumps to 3.x — fork is 0.1.0. |
| [`6181474a`](https://code.amazon.com/packages/MeshClaw/commits/6181474a) | backend | SKIP_INTERNAL (**flagged**) | SharePoint/Loop redaction carve-out — see [Human-decision](#still-open-pending-a-decision). |
| [`3396e112`](https://code.amazon.com/packages/MeshClaw/commits/3396e112) | backend | SKIP | **(recovered on re-audit, not in the original 13)** Byte-identical twin of the ported `3ef2bdbc` (same upstream CR-281616797); intentionally not ported to avoid a duplicate. |
| [`cd6730f`](https://code.amazon.com/packages/MeshClawWebsite/commits/cd6730f) | frontend | **DEFER** | artifacts masonry — see [Human-decision](#still-open-pending-a-decision). |
| _(unnamed)_ | — | **DEFER** | ⚠️ The CR claims **2 DEFER** but names only `cd6730f`. The second DEFER SHA is named nowhere in the CR or boundary file and is **unrecoverable** from existing sources. |

**Also in batch-18: 16 dropped sub-hunks inside PARTIAL ports.** These are not
standalone left-out commits — each is a deliberately-dropped piece of an
otherwise-ported upstream commit, recorded in the port commit's own body. Most
drop a CHANGELOG/version-bump hunk, an absent-subsystem hunk
(`writing_review`/`mcp_gateway`/`cc_session`), or a KIROCREW-branding/placeholder
hunk the fork overrides. Examples: `570a9ccf` dropped its `acp-client.md` spec
hunk; `73fb9dd0` dropped `writing_review` hunks + tests; `b674cd5a` dropped 3
theme hunks (kiro-dark/light, bikini-bottom); `e7730da7` dropped the
`validate_enterprise` removals. The full per-port table lives in the individual
commit bodies (`git show <port-sha>`) — not duplicated here.

---

## Recurring reasons at a glance

Almost every mechanical SKIP traces to a subsystem the de-Amazoning **deleted or
stubbed**. If you're wondering why a class of upstream commit never lands:

- **mcp_gateway** (shared MCP pool) — deleted.
- **secretary**, **writing-review**, **research-lab**, **team_manager /
  standup**, **code-reviewer**, **oncall-radar** — builtin apps absent. The fork
  ships only `auto_research` + `file_explorer` under `apps/builtins/`, and
  `apps/app-registry.json` is `[]` by design. (Note: the upstream `auto-research`
  *frontend* `ResearchLabPage`/`GrillTree` UI is absent even though the backend
  `auto_research` app exists — those frontend commits are still SKIP_INTERNAL.)
- **promptfarm** (skill publish) — internal, deleted.
- **GitFarm / Cloud-Sync / Bindle workspace-sync**, **AIM auto-update**,
  **Harmony Artifactory** — internal infra, deleted.
- **RUM / AppSenseAIUsage telemetry** — inert stub.
- **Midway** (mwinit, gateway service, SSH-cert watchdog) — stubbed; `~/.ssh`
  carve-outs forbidden by `MIGRATION_PLAN.md`.
- **Bedrock / Claude Code** provider surfaces — `agent.provider` is fixed to
  `["acp"]`; multi-provider dispatch was deleted (the dormant `ACP_BACKEND_CLAUDE`
  seam is intentionally kept but not re-wired).
- **LCARS / Bikini-Bottom / Knight-Rider-world themes** — cosmetic parody
  subsystems, absent.
- **Brazil `Config`** hunks — public build is setuptools; the root `Config` was
  dropped entirely.
- **3.x changelog/version bumps** — fork is at `0.1.0`.

See [`../../CLAUDE.md`](../../CLAUDE.md) ("do not re-introduce Amazon-internal
couplings") and `DEAMAZON_REPORT.md` for the authoritative deleted/stubbed list.

---

## Fork-initiated UX / feature divergences (do-not-reintroduce)

These are **not** sync left-outs and **not** Amazon-coupling removals — they are
deliberate public-launch product choices where the fork hides or removes a
surface that MeshClaw keeps. Verdict for a commit that re-introduces one:
**SKIP_FORKUX** (port the rest of a mixed commit as PARTIAL). The enforcing sync
rule is SKILL.md Step 2 → "Fork-initiated UX / feature divergences"; this is the
durable record of WHAT and WHY. Guarding on the exact mechanism matters — a note
that only says "hidden" is unenforceable when upstream ships the same default.

| Surface | Fork mechanism | Upstream (watch for re-add) | Task |
|---|---|---|---|
| Artifact **Iterate** button + all its entry points | `website/src/pages/ArtifactDetailPage.tsx` module const `SHOW_ARTIFACT_ITERATE = false` gates the header button, inline comment creation (`commentable`), the pending-comments "Submit All" path, and the "click Iterate" tips. `iterateWithAgent`/`buildPromptForChat` + the `iterated` lifecycle event stay. | Upstream keeps the button visible (icon-only `Sparkles`), and its `CommentsSidebar.tsx` `onAskAgent` ("Ask agent to address") + `ArtifactPanel.tsx` SubmitBar are additional iterate triggers absent from the fork — strip those too if that comment stack is ever ported. Symbols don't rename, so a sync will treat them as directly portable. | P472753393 |
| **Channels** app store listing | `src/kiro_crew/apps/manager.py` `_BUILTIN_APPS` "channels" entry carries `"hidden": True`; `website/src/pages/AppsPage.tsx` Browse grid filters `!(a.manifest as any)?.hidden`. Code/routes/`ChannelPage` stay (opt-in via `kirocrew app enable channels`). This MIRRORS upstream MeshClaw CR-289326017, so it should be at parity. | `defaultEnabled:False` is parity, NOT the divergence — the guard is the `hidden:True` flag + the AppsPage filter. Don't drop either on sync. | P472750613 |
| **Board** app (fully removed) | Deleted `website/src/pages/BoardPage.tsx`, the `/board` `builtinRegistry.ts` route, the `_BUILTIN_APPS` "board" entry, the `KanbanSquare` nav icon, the Alt+B / KeyB shortcut, the `MigrationCheck` prefix, and the Board tests. MIRRORS upstream CR-289326017 (which also removed Board). | If a pre-CR-289326017 upstream commit re-adds Board, DROP it. | (CR-289326017) |
| Voice/TTS **Piper** provider UI | `website/src/pages/settings/VoicePanel.tsx` adds a Piper/Polly provider selector + Piper fields; `dashboard/chat_voice.py` exposes/persists `provider` + `piper_*`. Upstream VoicePanel is Polly-only. | This is fork-AHEAD (a public feature upstream lacks). A sync of upstream's Polly-only VoicePanel must NOT drop the Piper selector — reconcile, keep both providers. | P472753900-adjacent |

**De-Amazon feature removals** (the `/tk` command, Secretary/TaskKeeper config,
`taskkeeper_complete`, the `HEARTBEAT_SAFE_TOOLS` Amazon tool names, the
`amazon_dev_story` eval, Secretary `.mjs` scripts) are recorded above under
"Recurring reasons at a glance" + the SKIP tables, and in `DEAMAZON_REPORT.md`.
Note specifically: `HEARTBEAT_SAFE_TOOLS` in `slack/gateway.py` was TRIMMED to
generic + kirocrew-core reads — the old rubric line calling those names
"inert, keep verbatim" no longer applies (see SKILL.md Step 2).

---

## The 49 wiped original-KiroCrew commits

Separate from the sync left-outs above: on **2026-06-14** the `KiroCrew` Brazil
package's original 49-commit history was **replaced** with the de-Amazoned public
fork's 207-commit history (the package took ownership of the fork's content). The
original 49 commits were a *different, earlier* codebase that happened to share
the `KiroCrew` name — an internal kiro-cli **agent-pool** architecture (Slack ↔
ACP pool, FastAPI + MCP task scheduler, Taskei orchestrator, Ralph loop), not the
fork's setuptools `kiro_crew` package.

**Why wiped:** the two codebases are unrelated — the original was an ancestor
prototype, the fork is the current product. Rather than merge, the package was
repointed wholesale to the fork (your explicit decision). The original
architecture (ralph, taskei orchestrator, bang-command system, `agent_manager`/
`agent_instance` pool) is **confirmed absent** from the current tree — the fork
uses different concepts (`subagent.py`, `cron.py`/`taskrunner.py`, handler
keywords + MCP tools, memory/vector_memory).

**Where they survive:** local branch `backup/pre-external-migration` (tip
`557a9ba`) and the `backup/bolichen` remote namespace. Recoverable via
`git log backup/pre-external-migration`. They are **not** reachable from the
current `mainline`.

### The 49 commits (oldest → newest)

Grouped by theme; all dated 2026-03-08 → 2026-05-02.

**Bootstrap / scaffold (2):** `93cd428` BuilderHub Create · `93e777a` initial commit

**Slack / ACP chat core (6):** `9f79f96` Slack images as ACP vision blocks ·
`91ffa04` INTROSPECTION prompt section · `9f54b05` persistent memory file + fix
load_session stuck · `2307e47` task completion Slack notification + bot-echo fix ·
`78e9d7e` spawn agent in new Slack thread + msg_too_long fix · `22d0c66`
app_mention handler + channel gate + PostStreamer fallback

**Bang commands (2):** `233c572` exec + task bang handlers · `877ccd6` `!context`
bang command + `/context` ACP schema doc

**Task / session plumbing (7):** `25a76b5` thread session_key through MCP/tasks/
bang · `fa22221` task result retrieval + stream completions · `2e45eec` periodic
cleanup of history/tasks/jobs/sessions · `1fbd430` move session tracking into
AgentManager · `f310f63` task list last_run/created_at · `fbfc33a` job list API +
mrkdwn task output · `f7d2dc0` `kirocrew reset` command

**CLI / packaging (3):** `9610ae9` unified CLI with uv · `13ac86c` auto-discover
agent-browser skill from npm · `6148f7d` bundle agent-browser wrapper + PATH

**Ralph autonomous loop (3):** `005f4c6` ralph skill (PRD-driven loop) · `941ef61`
ralph tmux management (start/stop/status/logs/attach) · `50b74fb` +`557a9ba`
ralph-executor prompt updates (default model claude-opus-4.6, loops forever)

**Taskei orchestrator (6):** `bc5afe2` Taskei coordinator (FSM, orchestrator, MCP
client) · `65acb69` orchestrator→agent via TaskScheduler · `7759ce7` `!taskei`
bang command + multi-room + session routing · `a8cd8d6` concurrency control +
unified recovery + immediate stop · `c273da1` per-task workspace dirs + context.md
· `938afcd` bootstrap taskei skill + taskei-planner agent spec

**Bootstrap refactor — `.ralph` US-001…US-010 (13):** `50ce66d` extract
`_build_skills_section` · `42ced52` US-001 path constants · `4d6d756` US-002 tool/
MCP constants · `28d3079` US-003 AgentDef + AGENTS registry · `e116979` US-004
non-destructive skill copy · `6801054` US-005 skill source registry · `f48cea9`
US-006 agent→skill mapping · `faf0a88` US-007 prompt templates to files · `a2bae05`
US-008 generic write_agent · `7a045b8` US-009 unit tests · `0964426` US-010
integration test · `f7d2dc0`/`f48cea9` (above) · plus `b69aa5c` MCP merge order +
ralph improvements

**Type/quality (3):** `207113a` resolve all mypy errors (22 files) · `966855e`
type annotations · `d600256` AGENTS.md ralph/runtime-layout docs

**Later features (4):** `ae6c317` cancel button to stop agent mid-response ·
`6911d6f` ACP proxy for TUI event interception · `d7f9a0a` synchronous spawn_agent
mode for subagent delegation · `0a4a6f9` deep-research skill + agent-pool race fix

> Note: a few SHAs span themes (e.g. `f7d2dc0`, `f48cea9`); counts above are
> thematic groupings of the same 49 commits, not additive. Authoritative list:
> `git log --reverse --oneline backup/pre-external-migration ^mainline`.
