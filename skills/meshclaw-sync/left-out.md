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
| [`6181474a`](https://code.amazon.com/packages/MeshClaw/commits/6181474a) | backend | 18 | ~~**SKIP_INTERNAL — flagged**~~ → **RESOLVED (EXTRACT, G3)** | SharePoint/Loop **redaction carve-out**. Skipped because it targets corp M365 hosts only (precedent: `e62422ae`), but it was **explicitly flagged for human review**. **→ 2026-07-18 governance-seam re-triage:** closed as **EXTRACT (G3)** — the exact-host heuristic exemption now lands generically via `CredentialPolicy.exempt_exact_hosts()` (narrow-only; can never relax the hard-credential floor), with the concrete M365 host list supplied by the companion adapter only. See the 2026-07-18 re-triage section. |
| [`4964a3e`](https://code.amazon.com/packages/MeshClawWebsite/commits/4964a3e) | frontend | 30 | ~~**DEFER — re-audit/close next batch**~~ → **RESOLVED (ALREADY_PRESENT via fork PR #20)** | Usage popup real credit total + overage ([CR-287246889](https://code.amazon.com/reviews/CR-287246889) / [Mesh-2146](https://taskei.amazon.dev/tasks/Mesh-2146)). Was recorded ONLY in the batch-30 block of `last-synced.txt` (never chained into this table — surfaced by the batch-42 open-DEFER sweep). Original blocker (fork text-scrape backend did not emit `credits_overage`/`percentage`) is GONE: fork-native [PR #20](https://github.com/kirodotdev/KiroCrew/pull/20) (`382c9bd5`) added `dashboard/handlers/kiro_usage_api.py` emitting both, and `sessions.py` sets `credits_overage`; the FE post-image is present by content (`App.tsx` computes `used`/`overage` exactly as upstream; `AgentsPage.tsx` reads `credits_overage`). **→ closed as ALREADY_PRESENT by the 2026-07-18 governance-seam re-triage** (see its per-commit table). Residual optional one-liner (adapter prefers backend `percentage` when present) is a nice-to-have, not a blocker. |

### Directive-driven adoptions (batch-47) — divergences future syncs must NOT flag

| Upstream SHA | Repo | Verdict | Record |
|---|---|---|---|
| [`85ff0a87`](https://code.amazon.com/packages/MeshClaw/commits/85ff0a87983d088a1bbfd9abcfe8daad0e5bc299) | backend | **PARTIAL — USER-DIRECTED ADOPT** | In-process embeddings (vendored llama-cpp-python, always-on, `EmbeddingBackend` seam) adopted per explicit user directive, superseding the fork's old OSS-Ollama default. **One structural transport swap** (do not "fix" back on later syncs): upstream downloads the GGUF from its CloudFront CDN with an internal **Gitfarm git+LFS fallback** (`_MODEL_PACKAGE`, `_download_via_gitfarm`, `_ensure_git_lfs`, `_find_gguf`, `_CLONE_TIMEOUT_SECS`, ssh://git.amazon.com — all DELETED in the fork); the fork is **HTTPS-CDN-only** with `_MODEL_URL_ENV = KIROCREW_EMBED_MODEL_URL` (directive name, NOT the plain-map `KIROCREW_MODEL_URL`), a fork-added `memory.embed_model_url` config knob, and `_DEFAULT_MODEL_URL`. Everything transport-agnostic is upstream-faithful (sha256 pin, retry ladder, daemon-thread download, atomic install, Ollama-blob salvage, skip env, keyword degrade, lru cache). Fork-added extras upstream lacks: the doctor HTTPS-reachability probe of the resolved model URL, and `_resolve_model_url()`. Dropped internal hunks: Brazil `Config`, the Gitfarm fallback + its tests (`_fake_git_run_factory`), `test_cc_stop_budget_and_handlers.py` (absent in fork), `docs/design-doc-meshclaw.md` (absent). **Ops RESOLVED (2026-07-20):** the GGUF (sha256 `06507c7b42688469c4e7298b0a1e16deff06caf291cf0a5b278c308249c3e439`) already lives in the SHARED model bucket `meshclaw-models-116101834266` (us-west-2), served by CloudFront `d35dbuobhek1fm` — the SAME object MeshClaw fetches. `_DEFAULT_MODEL_URL` was pointed at that shared distribution (`https://d35dbuobhek1fm.cloudfront.net/qwen3-embedding-0.6b.gguf`, verified HTTP 200 + `x-amz-meta-sha256` == the pin) rather than duplicating the model into a KiroCrew-only `kirocrew-updates/models/` key (which 403'd — never populated). Both products share one canonical artifact; `KIROCREW_EMBED_MODEL_URL` env / `memory.embed_model_url` still override for mirrored/airgapped deploys. |

**Inherited-upstream quirk (batch-47, LOW):** `_download_via_https`'s too-small
branch (`staging.stat().st_size < _GGUF_MIN_BYTES`) unlinks the staging file
BEFORE formatting the error from `staging.stat()`, so the surfaced error is the
generic exception text rather than "downloaded file too small". Byte-faithful
to upstream `85ff0a87` — the safety property (undersized file never installed)
holds. Fix upstream-first; `test_embeddings.py::test_too_small_download_fails`
documents it.

**Severity-framing drift-note (batch-48, [`836644ff`](https://code.amazon.com/packages/MeshClaw/commits/836644ffe2762dbbc7b24944edfff0edc44260ea)):**
the port registers the 6 `workflow_*` tools in `MCP_CORE_SCHEMAS` so their
internal `validate_tool_args()` runs at the guarded outer step. The upstream
commit message frames the pre-fix bug as "kills the whole stdio loop." That
literal server-crash is fully realized only on the **Windows synchronous-dispatch
path** (`mcp_shared.run_mcp_stdio_loop`, unguarded) and on non-kiro-cli hosts. On
**POSIX** the worker-thread `except Exception` backstop in `mcp_shared` (present
in BOTH fork and upstream) catches the escaping `ValidationError` and converts it
to an Error string, so pre-fix POSIX shows a worker-caught generic error + a
"failed" audit rather than a literal crash. The fix still has real value on all
platforms (routes through the intended guarded per-tool validation with proper
SEL logging) and is a genuine crash-fix on Windows. This is a characterization of
**upstream-verbatim** behavior — recorded here per DRIFT-PREVENTION, NOT softened
into the ported fork comment (which stays faithful to upstream's wording). No
fork-side divergence.

### Inherited-upstream findings (fix in BOTH repos — do NOT diverge the fork unilaterally)

PR #88 (batch-33/34) drew four Codex findings whose flagged code is byte-identical to / a faithful port of the current MeshClaw beta. They are **latent gaps in upstream**, not fork regressions. Fixing them fork-only would diverge from upstream and be re-conflicted by the next sync, so they are tracked here to be fixed **upstream-first**, then flow back via a normal sync batch.

| Upstream origin | Fork file:sym | Sev | Finding | Fix shape (upstream-first) |
|---|---|---|---|---|
| [`7cc8217d`](https://code.amazon.com/packages/MeshClaw/commits/7cc8217d) | `security.py` `StreamRedactor.feed` | HIGH | After a >4096-char credential-anchored tail is dropped (buffer cleared + `_REDACTED_CREDENTIAL_TAG`), a later chunk of the SAME token streams raw — the discard state isn't retained across `feed()` calls. | Add a discard-until-delimiter state flag retained across chunks; test a chunked token exceeding `_STREAM_HOLDBACK_JWT_MAX`. |
| [`56fbb774`](https://code.amazon.com/packages/MeshClaw/commits/56fbb774) | `mcp_gateway/gatewayd.py` `_apply_claim` | HIGH → MED (partially mitigated batch-43) | Any same-UID process can submit a `claim` and replace another connection's session identity; ancestor PID is client-supplied. This is the documented uid-`0700`-socket trust model, ported verbatim. **Batch-43 update:** the port of [`2b3c722d`](https://code.amazon.com/packages/MeshClaw/commits/2b3c722d0a0336b97154fdbe5a43b0a6443c4c50) partially mitigates — register-time identity is now resolved SERVER-side from the kernel-attested SO_PEERCRED peer pid (deny-by-default, SEL-audited grant/deny), and a claim matching zero connections is a loud audited `claim-noop` instead of a silent ack. Claim frames themselves remain uid-socket-trusted, so the finding stays open upstream-first at reduced severity. | Authenticate claims with a gateway-only capability (or verify peer is the gateway process) and verify ancestry server-side. Design change — upstream first. |
| [`82560fb7`](https://code.amazon.com/packages/MeshClaw/commits/82560fb7) | `security.py` `_BASH_EXFIL_PATTERNS` | MED | `curl -Ffile=@secret` (no space after `-F`) and `curl --form=file=@secret` (equals form) bypass the `-F *=@`/`--form *=@` globs, which require a space. Present verbatim upstream. | Add no-space + equals glob variants, or tokenize curl args structurally; cover with tests. |
| [`a7736388`](https://code.amazon.com/packages/MeshClaw/commits/a7736388) | `context.py` `build_session_replay` | MED | A single newest message larger than the (window-scaled) `replay_budget` is emitted whole (the `and lines` guard admits the first line unconditionally), dominating a small model's context. Faithful port. | Truncate the first oversized line to `replay_budget`. |

PR #46 (batch-42, Electron shell UX rescue) drew two Codex findings whose flagged code is byte-identical to the upstream post-image — verified by diffing the fork's `TabChip` against `23f5a314:src/pages/chat/SidePanel.tsx` (identical) and the connection-window block against `08e28b8a:electron/main.js` (same shape; upstream has no tracking Set either). Same upstream-first disposition:

| Upstream origin | Fork file:sym | Sev | Finding | Fix shape (upstream-first) |
|---|---|---|---|---|
| [`08e28b8a`](https://code.amazon.com/packages/MeshClawWebsite/commits/08e28b8a7648ddfb739bb33522c1d8043b826401) (via rider [`23f5a314`](https://code.amazon.com/packages/MeshClawWebsite/commits/23f5a314a71197eefe8066bb1622077e967bf613)) | `website/src/pages/chat/SidePanel.tsx` `TabChip` | HIGH | `<div role="tab" onClick>` has no `tabIndex`/keyboard handler — keyboard users cannot focus or activate side-panel tabs (`accessible-interactive-elements` AutoSDE rule). Byte-identical to upstream `TabChip`. | Add roving `tabIndex` + Enter/Space (and ideally arrow-key) handling on the tab chips, or wrap in `Clickable`; flow back on the next sync. |
| [`08e28b8a`](https://code.amazon.com/packages/MeshClawWebsite/commits/08e28b8a7648ddfb739bb33522c1d8043b826401) | `website/electron/main.js` connection-window block (`connWin = new BaseWindow(...)`) | MED | Connection-window reference is a local; Codex argues it may be GC-collected after the async connect completes. Upstream keeps the same local-only shape (its comment says "tracked for menu actions" but no tracking Set exists). Electron retains BaseWindows until `close()`, so severity is disputed — either way the shape is upstream's. | If real: module-level `Set` of connection windows, removed on `closed`. Decide + fix upstream, flow back. |
| [`3f83fa15`](https://code.amazon.com/packages/MeshClaw/commits/3f83fa156392777aeaf22e22e5928ebcb408dee7) | `apps/builtins/file_explorer/server.py` `_h_tree` `.kirocrew` basename exception | HIGH | The basename-only `.kirocrew` special case admits any dir *named* `.kirocrew` under an allowed root — e.g. `/home/user/.ssh/.kirocrew` — bypassing `_safe_path`/`_is_sensitive` for that node (upstream `_h_tree` lines 740-749 are line-identical with `.meshclaw`). Exposure is bounded: `_kirocrew_safe_children` returns only the fixed safe-name allowlist (workspace/uploads/skills), never arbitrary `.ssh` content — but the ancestor check is genuinely missing upstream too. | Reject sensitive ancestors (`is_sensitive_path()` on `resolved.parent`) or restrict the exception to the configured data-dir path, upstream first; flow back. |
| [`efd8988b`](https://code.amazon.com/packages/MeshClaw/commits/efd8988bfbc7acccb23e169260a804046dca8814) | `mcp_shared.py` busy-loop drops concurrent `tools/call` | HIGH | Claude Review: the wedge-detection busy-loop reads stdin while a tool runs but only handles `notifications/cancelled`/`ping`/`tools/list`; a concurrent `tools/call` on the SHARED pooled backend is read and dropped with no response — the caller hangs until the hard-ceiling recycle errors every co-tenant stub. Fork loop is byte-identical to upstream `efd8988b` lines 540-590 incl. the explicit "For now, drop gracefully / MCP spec says servers SHOULD NOT receive new requests while one is in-flight" design comment — upstream's own deliberate scope cut in the same commit that added the stdin-reading loop. | FIFO-queue concurrent `tools/call` (or reply `-32000 server busy` so callers fail fast), upstream first; flow back. Claude's advisory `_cancelled_ids` unbounded-growth note is the same commit's verbatim shape — prune on completion upstream. |
| [`85ff0a87`](https://code.amazon.com/packages/MeshClaw/commits/85ff0a87983d088a1bbfd9abcfe8daad0e5bc299) | `config/loader.py` always-on migration keeps legacy `embedding_dim` | HIGH | Codex: a legacy Ollama config with `embedding_dim=768` (e.g. nomic-embed-text) is coerced to `llama_cpp` (fixed 1024-dim Qwen3 model) but keeps `embedding_dim=768` — VectorMemoryStore builds a 768-dim FAISS index and receives 1024-dim vectors, raising inside FAISS with possible index/db inconsistency. Fork lines are the symbol-mapped image of upstream `85ff0a87` loader lines 3043-3046 (`_coerce_embedding_provider(...)` + `embedding_dim=memory_data.get("embedding_dim", 1024)` — upstream also does NOT force the dim on migration). | Force/derive `embedding_dim=1024` when coercing to `llama_cpp` + rebuild incompatible persisted FAISS indexes, upstream first; flow back. (`vector_memory`'s lazy rebind partially mitigates: embed failures degrade to keyword search rather than crashing the store.) |
| [`7d89725f`](https://code.amazon.com/packages/MeshClaw/commits/7d89725f) | `slack/events.py` queued-message image temps leak on cancel/`!stop` | MED | Codex: the fix defers `_cleanup_image_temps()` to `_dispatch_queued` (correct — else images drop before the queued turn runs), but deleting the queued Slack message or `!stop` removes the queue entry without dispatching, so its `image_temp_paths` are never unlinked (temp-fs leak of attachments on repeated cancels). The deferral NOTE + the missing cancel-path cleanup are both upstream `7d89725f`'s shape (the fork block carries upstream's verbatim NOTE comment). | On queue-entry removal / `_pending_queue` clear, unlink each entry's `kwargs["image_temp_paths"]`, upstream first; flow back. |
| [`08e28b8a`](https://code.amazon.com/packages/MeshClawWebsite/commits/08e28b8a7648ddfb739bb33522c1d8043b826401) | `website/electron/main.js` `zoomItem` | MED | `zoomItem` uses `webContents.getFocusedWebContents()` (zooms DevTools when DevTools has focus) while Reload/Toggle-DevTools use the `focusedDashboardWC()` helper — the exact split upstream `08e28b8a` lines 1527-1535 ship. Faithful port of upstream's deliberate shape. | Use `focusedDashboardWC()` inside `zoomItem` upstream; flow back. |
| [`22fa7042`](https://code.amazon.com/packages/MeshClaw/commits/22fa7042cc19f3999d7d7d1c1d899b260cca1102) | `session.py` pool-discard verify (`pid = getattr(_client, "_pid", None)` … `_sync_kill_provider(provider)`) | MED | The still-alive fallback kill re-reads `provider._client._pid` via `_sync_kill_provider` instead of using the pre-captured `pid` — if shutdown clears client bookkeeping while the OS process survives, the kill finds no PID and the discarded tree leaks. Fork block is line-identical to upstream `22fa7042` lines 1089-1142. | Pass the captured PID to a `kill_process_tree(pid, SIGKILL)` helper upstream; flow back. |
| [`08e28b8a`](https://code.amazon.com/packages/MeshClawWebsite/commits/08e28b8a7648ddfb739bb33522c1d8043b826401) | `website/src/App.tsx` `actSpace` effect | MED | `window.addEventListener('resize', check)` is the only trigger recomputing whether the side panel fits; expanding the top-bar capsule/metrics changes `measureSidePanelReservedW()` without a window resize, so the activity button can stay enabled when the panel no longer fits. Identical to upstream `08e28b8a` `App.tsx` lines 1052-1056. | `ResizeObserver` on the header clusters upstream; flow back. |
| [`08e28b8a`](https://code.amazon.com/packages/MeshClawWebsite/commits/08e28b8a7648ddfb739bb33522c1d8043b826401) (+`23f5a314`) | `website/src/pages/chat/SidePanel.tsx` tab/panel close vs dirty editor | HIGH | Tab close (`onClose={() => closeTab(t.id)}`, incl. middle-click) unmounts `MarkdownPanel` without consulting its unsaved-change guard — dirty edits are lost without confirmation. Byte-identical to upstream `23f5a314` SidePanel lines 222/340. | Hoist dirty state into `PanelTab` (or route external closes through the guarded close) upstream; flow back. |
| [`08e28b8a`](https://code.amazon.com/packages/MeshClawWebsite/commits/08e28b8a7648ddfb739bb33522c1d8043b826401) | `website/src/pages/ChatPage.tsx` `handleFileOpen` slot tagging | MED | `activeSlotRef.current` is read after the async file/diff fetches, so switching chats mid-request tags the tab with the wrong slot (comments then route to the wrong chat). Identical to upstream `08e28b8a` ChatPage lines 1121/1129/1145. | Capture the origin slot before the first `await` upstream; flow back. |
| [`22fa7042`](https://code.amazon.com/packages/MeshClaw/commits/22fa7042cc19f3999d7d7d1c1d899b260cca1102) | `acp/runtime.py` survived-escalation PID stays protected | MED (disputed) | Codex: a PID surviving SIGTERM/SIGKILL stays in `_PROTECTED_PIDS`, so the orphan sweep excludes it — leak until restart. Upstream's comment says leaving it tracked is DELIBERATE ("untracking here would leak the process until reboot"); Codex's fix inverts upstream's design intent. Not a port defect either way. | Upstream to adjudicate the tracked-vs-swept semantics for kill-survivors; flow back whatever lands. |
| [`08e28b8a`](https://code.amazon.com/packages/MeshClawWebsite/commits/08e28b8a7648ddfb739bb33522c1d8043b826401) | `website/src/pages/ChatPage.tsx` `activityTab`-keyed reopen effect | MED | Repeating an explicit activity request for the SAME view (e.g. `/side` twice with the panel closed on another tab) doesn't rerun the `[activityTab]`-keyed effect, so the panel reopens on the wrong tab. Fork effect is identical to upstream `08e28b8a` ChatPage lines 2210-2212. | Track an explicit request nonce in the store and key the effect on it, upstream; flow back. |
| [`53214b50`](https://code.amazon.com/packages/MeshClaw/commits/53214b50d0b0e0f3d9f3ec6951ff5ad8680868c1) | `acp/client.py` + `sandbox.py` `session_host_preexec` replaces `resource_limit_preexec` | MED | The NOFILE-raise preexec REPLACES the configured resource-limit preexec on session hosts, so operator-configured `max_processes`/`max_cpu_seconds`/`max_memory_mb` no longer apply to session hosts. This substitution is exactly upstream `53214b50`'s own diff (`-resource_limit_preexec()` → `+session_host_preexec()`), reflecting its "session hosts are trusted" design note. | Compose the configured limits with the NOFILE override (disable only `max_open_files`) upstream if the trust call is revisited; flow back. |
| [`f2661c8a`](https://code.amazon.com/packages/MeshClaw/commits/f2661c8aa8ec2fa0be552872db0a885495a47cf3) | `sandbox.py` `_probe_unshare` on-loop cold-cache early-return | HIGH | First `wrap_argv` on a running loop with a cold cache returns `False`/`"none"` (fail-closed `RuntimeError`) before the background warm thread populates the cache. Upstream's documented mitigation: `prewarm_backend()` boot hook fills the cache before the first on-loop spawn reaches `detect_backend()`. The on-loop early-return + the prewarm boot hook are both upstream `f2661c8a` lines 284-339 (docstring says "Boot prewarm ensures this path is rarely hit"). | Add an awaited readiness mechanism or resolve the backend off-loop before `asyncio.run` upstream; flow back. |
| [`808f7f74`](https://code.amazon.com/packages/MeshClaw/commits/808f7f746f00e16bdc3e2d28c2cacffb3c26c033) | `session.py` RSS-watchdog idle check (`sess.semaphore.locked()`) | HIGH | Codex: an idle parent chat whose background subagents share its `AcpRuntime` passes the semaphore-only idle check; RSS recycle then resets the parent, killing the co-tenant subagents' in-flight turns. Fork block is byte-identical to upstream `808f7f74` lines 2954-2958 (verified by region diff). Upstream's CR1 "wrap-first" note suggests co-tenancy refinements were deliberately deferred. | Add a co-tenant/runtime-activity check before reset upstream (with regression test); flow back on a later sync. |

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
| [`0acf7f8f`](https://code.amazon.com/packages/MeshClaw/commits/0acf7f8f) | `mcp_gateway/pool.py` (~L43-79) + `config/loader.py` (~L1897-1913, L2921-2922) `read_buffer_limit_bytes` / `response_spill_threshold_bytes` | ~~MED~~ **RESOLVED (config-wiring leg, batch-48)** | ~~The two config keys are parsed into `KiroCrewConfig` but never read by the pool — only the `KIROCREW_MCP_READ_LIMIT` / `KIROCREW_MCP_SPILL_THRESHOLD` env vars take effect, so a `config.json` setting is a silent no-op.~~ **Upstream fixed the config-wiring leg in [`23bf26bb`](https://code.amazon.com/packages/MeshClaw/commits/23bf26bbec507e57dd1b7c4182d0d033bb874e3a) (Upstream-CR [CR-290356251](https://code.amazon.com/reviews/CR-290356251)); the fork ported it this batch (batch-48) — the resolvers now consult `_raw_config()["mcp_gateway"]` between the env check and the default, so precedence is env→config→default and a `config.json` value takes effect.** REMAINING (unchanged upstream, still open): a configured/env `0` threshold spills EVERY response instead of disabling spilling — upstream's `>= 0` guard is a faithful port and does not treat `0` as "disabled". | Remaining leg: require a positive threshold before spilling (treat `0` as "disabled") — upstream-first. |
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

> **Batch-45 inherited-upstream test-ordering fragility (recorded, NOT patched fork-side):** the port of [`873d1130`](https://code.amazon.com/packages/MeshClaw/commits/873d1130bf0fc7d23ab7088856b4a47cfcfd8818) adds `test/test_pip_deps_consistency.py::test_noop_recorder_when_otel_missing`, whose teardown pops `kiro_crew.metrics.provider` from `sys.modules` and re-imports it — creating a FRESH module object. The pre-existing `test/metrics/test_provider.py` binds `get_recorder`/`reset_for_testing` at module level (to the OLD module object) but its degrade test patches `_OTEL_AVAILABLE` via a fresh `import kiro_crew.metrics.provider as provider_mod` — so if the pip-deps test runs FIRST in the same process, the flag is patched on the new module while `get_recorder` reads the old one, and `test_degrades_to_noop_when_otel_missing` fails (`rec.enabled is True`). Both files are byte-faithful to upstream (`873d1130`'s post-images have the identical restore shape and module-level import), so the fragility exists identically upstream. It does NOT fire in CI: collection order puts `test/metrics/` thousands of items before `test_pip_deps_consistency.py`, same-worker xdist execution follows collection order, and cross-worker is cross-process. Repro: `pytest test/test_pip_deps_consistency.py test/metrics/test_provider.py` (that explicit order). Fix shape (upstream-first): have the pip-deps teardown restore the ORIGINAL saved module object instead of re-importing a fresh one (`sys.modules["..provider"] = saved[...]`), or have `test_provider.py` import the module lazily inside each test; flow back on a later sync.

> **Batch-44 fork-side adaptation (platform_compat shim, not an upstream-logic change):** the port of [`efd8988b`](https://code.amazon.com/packages/MeshClaw/commits/efd8988bfbc7acccb23e169260a804046dca8814) (ping-gated wedge detection + `notifications/cancelled` receiver) rewrites `mcp_shared.run_mcp_stdio_loop` around a `select.select([sys.stdin], ...)` busy-poll while a worker thread runs the tool. `select()` on Windows only accepts sockets — polling `sys.stdin` raises `OSError` (WinError 10038) on every iteration, which would crash `kirocrew-core`/`kirocrew-cron` on the natively-supported Windows platform. Per the fork's CLAUDE.md/AGENTS.md platform rule the loop gates on `platform_compat.IS_POSIX`: on Windows, `tools/call` dispatches **synchronously** exactly as the pre-port loop did. **Known gap (accepted):** on Windows there is no in-flight cancel or ping interleave — a long-running tool cannot be cancelled mid-flight and gateway pings go unanswered while a tool runs (the idle-branch `ping`/`notifications/cancelled` handlers still work between calls). POSIX behavior is byte-identical to upstream. Upstream (Linux/macOS-only) needs no such gate; a future upstream stdio-loop edit must be re-applied on top of the gate. AI-review findings on the upstream-verbatim ported logic (e.g. the module-global `_thread_cancel_event`, non-ping/non-cancel requests dropped while busy) are inherited-upstream — disposition them here, never patch fork-side.

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
| [`08e28b8a`](https://code.amazon.com/packages/MeshClawWebsite/commits/08e28b8a7648ddfb739bb33522c1d8043b826401) (+ riders [`23f5a314`](https://code.amazon.com/packages/MeshClawWebsite/commits/23f5a314a71197eefe8066bb1622077e967bf613), [`082f45d5`](https://code.amazon.com/packages/MeshClawWebsite/commits/082f45d5e919c997c1108399217106a1c56622ed)) | frontend | batch 40 (DEFER: `SidePanel.tsx` hard-imports `ArtifactPanel.tsx`, blocked on then-open fork PR #14) | **PORTED (PARTIAL) in batch-42** (user-authorized frontend-UX defer rescue; PR #14 merged 2026-07-18 unblocked it) — the full Electron shell redesign: de-tabbed BaseWindow shell, tabbed `SidePanel.tsx` (`usePanelTabs`/`usePersistedBool`), unified top-bar capsule, and the BaseWindow reload/zoom/DevTools repairs (`role:` menu items silently no-op on BaseWindow; explicit `focusedDashboardWC` handlers — the fork's Cmd+R/DevTools no-op is fixed). Folds rider `23f5a314` (SidePanel root `shrink-0`); rider `082f45d5` (`visibleInstanceTabs()` + `mac-instancebar-inset` traffic-light inset relocation) ported LAST on the new inset stack, superseding batch-41's "no anchor" N/A. DROPPED hunks: the capsule's mwinit segment (Midway stubbed), the usageMode/costData Claude-cost usage branch (SKIP_NONKIROACP), the OverflowMenu "Share to Artifactory" restyle (absent subsystem). FORK-UX guards intact (`SHOW_ARTIFACT_ITERATE`, Channels hide, Board removal, Piper). [CR-289605811](https://code.amazon.com/reviews/CR-289605811), riders [CR-290332522](https://code.amazon.com/reviews/CR-290332522) / [CR-290355334](https://code.amazon.com/reviews/CR-290355334). |
| [`cd6730f`](https://code.amazon.com/packages/MeshClawWebsite/commits/cd6730f) | frontend | batch 18 (DEFER: masonry rewrite on a hard-diverged page + new `@virtuoso.dev/masonry` dep, zero fix value) | **ALREADY_PRESENT — closed in batch-42 re-audit.** The fork adopted the masonry gallery via merged [fork PR #14](https://github.com/kirodotdev/KiroCrew/pull/14) (artifacts-mirror) and is AHEAD of this commit's post-image (also carries `ef98477` stale-data guard, `edb11ea` sandbox widen, `a5609d4` popout, `20f1881` folders, `4ab933e` safeSetItem). The only `cd6730f` hunks absent — the `publishedOnly` compact filter + `CopyLinkBox` renders — are the recorded PR #14 publish-behind-seam strips (public edition has no publish provider), not missed generic hunks. Nothing to port. [CR-281375038](https://code.amazon.com/reviews/CR-281375038). |

---

## Governance-seam re-triage (2026-07-18)

A dedicated **re-triage of the CPP governance seam** re-examined 16 upstream
commit groups against the new `platform/` extension-point strategy (the seam an
internal companion composes against). Many rows above were skipped **before** the
seam existed ("no fork consumer", "subsystem absent", "Midway absent") — the seam
turns a class of them into *generic-core EXTRACTs*: the reusable mechanism lands
in the core behind a `Default*` no-op adapter, and the Amazon-specific behavior is
supplied by the companion adapter. This section is the durable record of **every**
verdict from that pass. It did **NOT** advance any sync boundary (no new upstream
window was triaged — see `last-synced.txt`); it re-decided already-in-window
commits.

**Landing branches:** all implemented items landed on
`feat/governance-seam-retriage` (rebased onto `origin/main`, which now includes
merged **PR #14** artifacts-mirror + **PR #20** credit-total). Five items were
first built on branches stacked behind an open PR and were consolidated onto this
branch once that PR merged — each such row reads "(consolidated after PR #14/#18
merged)"; no stacked branch remains pending. Seam additions total **3 v1 method
additions** to existing Protocols (`IdentityProvider.preflight_checks`,
`IdentityProvider.credential_watch_paths`, `CredentialPolicy.exempt_exact_hosts`)
plus a minimal `TunnelProvider` callback/status surface — **zero new Protocols,
zero new `SCOPE_CATALOG` rows, `CONTRACT_VERSION` stays 1** (see `governance.md`).

### Implemented (ports + seam extracts)

Verdict `PORT` = de-Amazoned content port; `EXTRACT` = pull a reusable mechanism
into generic core behind a CPP `Default*` no-op, companion supplies the behavior.

| SHA(s) | Item | Group | Verdict | Branch | Disposition |
|---|---|---|---|---|---|
| `b5c0f9c5` | P1 | defers | PORT | `feat/governance-seam-retriage` | Honor `parent_id` in chat-folder PATCH with a descendant-cycle guard — never triaged before (genuine miss; the fork FE already ships `moveFolderTo` → a silent no-op with no server cycle check). |
| `4ab97b39` | P2 | defers | PORT | `feat/governance-seam-retriage` | Resolve cross-slot pending approval in `api_chat_slot_approve` — never triaged before (genuine miss). Slot-agnostic `resolve_approval` fallback so "Trust tools" no longer half-applies then 404s. |
| `896f445` | P3 | defers | PORT | `feat/governance-seam-retriage` | Harden the chat-history search MCP tools (per-segment redaction before highlight markers, sha1 not-found fingerprint, role filter, full ranked-window scan, `..`-component key check, TOCTOU recheck). |
| `4964a3e` | P4 | defers | **ALREADY_PRESENT (via PR #20)** | — (DROPPED) | Canonical credits shape in the usage popup + agents page. **Superseded during rebase by independently-merged fork PR #20** (`382c9bd5` "show true Kiro credit total via usage API"), which implements the same fix. The DEFER at `last-synced.txt` (batch-30) is now **obsolete**. Not re-implemented on this branch. |
| `4be5e549`, `ff8b9047`, `3da3f45`, `305bcb5` | P5 | mcp-gateway | PORT | `feat/governance-seam-retriage` | MCP gateway metrics card on the System page (ported from the FE tip, which folds all four commits incl. the `305bcb5` move to `src/pages/`). The prior rows 224/244/454 "backend absent" reason is **stale** — the fork ships `GET /api/mcp-gateway/metrics` + prewarm hit-rate. |
| `2b338089` | P6 | defers | PORT | `feat/governance-seam-retriage` (consolidated after PR #18 merged) | Knowledge rebuild single-flight (BEGIN IMMEDIATE sweep-then-claim), `items_failed` accounting, re-embed backoff, and folding `base_url` into `embedder_signature`. **No prior ledger record — fell through triage.** |
| `176120c9` | P9 | singles | PORT | `feat/governance-seam-retriage` (consolidated after PR #18 merged) | Warm session pool for workflow agents + `kirocrew.session.startup.duration` phase metric. Batch-35 window recorded "ZERO SKIP" but this commit was **untriaged**. |
| `9f68580` | P10 | promptfarm | **ALREADY_PRESENT (via PR #18 batch-40)** | — (SKIPPED) | Multi-provider skill browser modal (FE half of `31b8a10b`). Was **DEFER "port next run"** in batch-38. **Superseded during consolidation:** PR #18's batch-40 ported the same upstream `9f68580` — merged `main` already carries `SkillBrowserModal.tsx`, the `discoverSkills`/`previewDiscoveredSkill`/`installDiscoveredSkill` client methods, the `DiscoveredSkill`/`DiscoverSkillsResponse`/`DiscoverSkillPreview`/`DiscoverInstallResult` types, the `SkillMetaStrip` export, and the `SkillsTab` Add-Skill mount. The stacked-branch commit `8aed8def` diverged only in 2 cosmetic lines (a header-comment reword + a ` `→space) plus a `SkillBrowserModal.test.tsx` vitest file; the cosmetic deltas are not worth a divergence and the test is optional. **Cherry-pick skipped**; no code lands on the consolidated branch. |
| `92104fcc` | P7 | aea-tunnel | PORT (contingent on G2) | `feat/governance-seam-retriage` | Overview tunnel-status tile, hidden unless a tunnel is active. Landed **because G2 landed**; the stale `SKIP_INTERNAL` row above is flipped. If G2 were ever reverted this reverts to `NO_ACTION`. |
| `803af9de` | G1 | midway-auth | EXTRACT | `feat/governance-seam-retriage` | Seam-supplied preflight checks (`IdentityProvider.preflight_checks()`) run before `gateway`/`token`. Generic runner extracted; **the `module:function` string mechanism was deliberately NOT ported** (agent-writable code-exec escalation). Midway `ensure_midway` stays companion-side. Row above flipped `SKIP_INTERNAL → EXTRACT`. |
| `9cf7e0af`, `1408ca73`, `b944c74d` | G2 | aea-tunnel | EXTRACT (lifecycle wiring only) | `feat/governance-seam-retriage` | Route the tunnel lifecycle through the `TunnelProvider` seam — the stub `TunnelManager` delegates `start`/`stop`/`public_url` unconditionally to `current_context().tunnel` (no `isinstance` edition branch); token-auth deny gate stays evaluated before `start()`; minimal `register_callbacks`/`status_snapshot` v1 additions. **The entire AEA supervisor stays dropped** (CLI `--protocol=v2` spawn `079b1402`, zombie health probe, in-process-TLS probe — companion `TunnelProvider`). The `1408ca73`/`b944c74d` probe internals remain `NO_ACTION` (see aea-tunnel group). |
| `6181474a` | G3 | defers | EXTRACT | `feat/governance-seam-retriage` | Exact-host heuristic exemption via `CredentialPolicy.exempt_exact_hosts()` — narrow-only (skips ONLY the base64/length heuristics; the hard-credential path+query floor still runs first). Closes the flagged human-review item above. M365 host list is companion data. |
| `40965431` | G6 | mcp-gateway | EXTRACT | `feat/governance-seam-retriage` (consolidated after PR #18 merged) | Blue-green pooled-backend drain on credential rotation + `credwatch.py` (content-digest, first-observation-baseline) + `IdentityProvider.credential_watch_paths()` seam route (gateway threads `--credential-watch-path` to the daemon; absent flag = no watcher). **Reclassifies the batch-39 `SKIP_INTERNAL`** — the drain machinery is Amazon-free; the credential path/trigger stays companion-side. |
| `5d99a8d4`, `16fa88d2` | G7 | midway-auth / singles | EXTRACT | `feat/governance-seam-retriage` | Expose `sandbox.userns_available()` (public alias, now consumed by the internal caller) + `is_wsl()` host probes for the companion `JailProvider`. Overturns the `5d99a8d4` "no fork consumer" SKIP above; MCS-Jail orchestration stays companion-side. |
| `a6a7c2db` | G8 | telemetry-pkgmgr | EXTRACT | `feat/governance-seam-retriage` | Per-interaction `telemetry.record_event("interaction", …)` at the two chat-success sites (dashboard + slack). **Payload is strictly metadata** (`session_key`/`surface`/`model`). Corrects the misfiled "frontend / SKIP_INTERNAL" row above — it is a **backend** commit. `DefaultTelemetryProvider` is a no-op (standalone byte-identical); usage_upload queue / cookie jar stay companion-side. |
| `25feb233`, `186aeaa`, `e83c0bd7`, `9a696e65` | G4 | publish-providers | EXTRACT | `feat/governance-seam-retriage` (consolidated after PR #14 merged) | Provider-neutral remote artifact browse/fork/clone/pull surface at `/api/remote-artifacts/{provider}/…`, wiring PR #14's dormant `publish_sync` orchestration. Egress routes (overwrite-remote, clone-with-auto-sync) pass the `capabilities.publish` gate; browse/fork/pull-latest stay ungated ingress. The vendor `artifactory_client`/`api_artifactory_*` handlers are NOT ported. Inert in the public edition (empty registry → 404/503, remote tab hidden). |
| `a84eabc4`, `475146ca` | G5 | defers / publish-providers | PORT | `feat/governance-seam-retriage` (consolidated after PR #14 merged) | Artifact comment MCP tools (`artifact_get_comments`/`post_comment`/`mark_review`/`delete_comment`) — the MCP-first agent surface PR #14 shipped routes for but no tools. **Attribution corrected:** `a84eabc4` (not `475146ca`) introduced `post_comment`. |
| `1c48788e` | X1 | singles | EXTRACT | `feat/governance-seam-retriage` | De-Amazoned `llm-council` skill (data file). Never triaged before (genuine miss). Research allowlist reduced to `web_search/web_fetch/fs_read/grep/glob` (dropped the five `@builder-mcp/*`); ARCC/taskei/CR references genericized. Fan-out already governed by `capabilities.spawn` — no core change, no new scope. |

### Rejected recommendations (recorded, deliberately NOT implemented)

Adversarial verifiers proposed these; each was rejected on the merits. The
existing ledger rows stand unless noted.

| SHA(s) | Recommendation | Verdict | Reason |
|---|---|---|---|
| `4a41b8a3` | Extract `strip_slack_user_content` into `slack/format.py` | **REJECTED (YAGNI)** | Every upstream consumer is a companion-only app; **zero fork call sites**; config ships `mcpServers: {}` so there is no core ingest chokepoint; upstream itself keeps a parity-tested second copy, so the companion carries its own. The `SKIP_INTERNAL` row above stands. |
| `49dc9c29`, `a179b9e0`, `d2240c48` | `resolve_tunnel_enabled` + owner no-origin DM | **REJECTED (SEAM_EXISTS / NO_ACTION on tunnel hunks)** | The generic salvage (`_dm_owner`/`_dispatch_owner_dm`) is **already ported** (batch-28 `5d66a45`, PORTED PARTIAL). The Slack→tunnel auto-enable rule guards a coupling the fork **deliberately diverged from** (opt-in `slack.use_tunnel_url`); the "broken Slack links" premise is false (a working token fallback link exists). Companion policy is expressible inside the existing `TunnelProvider.enabled()`. |
| `9a696e65`, `25feb233`, `0e7322ce` | Publish/remote **10-tool MCP set** (beyond G5's 4 comment tools) | **REJECTED (reverses PR #14; SEAM_EXISTS)** | Reverses PR #14's code-documented decision (publish = dashboard HTTP action, "NOT LLM tools"); **7 of 10 tools call routes that exist in no worktree**; the upstream tools hardcode a vendor enum + "Amazon employees" wording. Companion path = `McpToolingProvider.extra_mcp_servers` against PR #14's governed endpoints. **May be re-proposed narrowly after G4 lands.** (G5's 4 comment tools are a different surface, approved separately.) |
| `f8383887` | `deploy_web` hero SVGs (both variants) | **REJECTED (DEFER stands)** | The `fd633154` builtin-ui materialization substrate (`_materialize_builtin_assets` + pkg_ui copytree) is **absent** from the fork; `routes.py` serves only from `apps_dir()/name/ui/`, so the SVGs would 404 (the already-landed workflows heroes 404 today, proving it). The DEFER rows above remain correct. The right future shape is an EXTRACT porting the materialization hunk + SVGs + `app.json` keys **together**. |

### Group-summary verdicts (documentation-only)

The 16 re-triage groups, with the composite verdict for each group's SHA cluster.
`SEAM_EXISTS` = a companion composes it against an existing extension point (no
core change needed); `RESOLVED_BY_PR` = the content already landed via a merged/
open fork PR; `NO_ACTION` = nothing to do (content present, or fork-intentional
divergence, or internal-data-only).

| Group | Verdict summary |
|---|---|
| **app-registry** | SEAM_EXISTS (18 SHAs via `config.registries` federation + `AppRegistryPolicy`); NO_ACTION `754ef420`/`23853a3d` (Brazil versionSets). |
| **mcp-gateway** | NO_ACTION stale-ledger cluster (19 SHAs now content-present, incl. `1063f78f` evict `keep_spawn_lock` — audit "unaccounted" row, verified present at fork `pool.py`); RESOLVED_BY_PR #18 `7de4f830` (spill); SEAM_EXISTS `568c6baf`/`d6e48920` (`McpToolingProvider` pooling); **P5 PORT** + **G6 EXTRACT** (above). |
| **writing-review** | SEAM_EXISTS (20 SHAs — `AppsLoader` + `DashboardContributor` + app UI bundle); RESOLVED_BY_PR #14 `54f01fc0`/`bc92b8b5`/`d5dc40e` (publish leg); NO_ACTION `73fb9dd0`/`e870fed4` (generic hunks already present) + `deterministic.py`/`annotate_docx.py`/internal-read allowlist. |
| **midway-auth** | SEAM_EXISTS `ea6348d5`/`4f753ed0`/`439ae3b6` (`IdentityProvider` + `DashboardContributor` mwinit) + `5d99a8d4` `JailProvider` (probes extracted via **G7**); **G1 EXTRACT** (above); NO_ACTION `437262df`/`ebee95e7`/`71ac5fcc`/`38864fd9`, `1ae3c85`/`744f5ad`; RESOLVED (already content-ported) `dff082fb`/`d6e48920`. |
| **internal-apps** | SEAM_EXISTS secretary (10 SHAs), legacy code_reviewer (`58c73651`/`3a182608`/`1fee24ce`), team-manager (`80c564f5`/`b9036ac4`/`ffee2086`, plus pre-snapshot frontend `765a3a7` Standup app UI — audit "unaccounted" row, SKIP_INTERNAL: team-manager absent), taskkeeper (`938a147d`/`e3641e2`); NO_ACTION code-review-sage stale drops `79868424`/`e8a1010b`/`6ecb54d2` (content present); `4a41b8a3` rejected (above). |
| **build-only** | NO_ACTION all 19 SHAs (4 clusters — Brazil/toolbox/version-bump/mailmap). |
| **promptfarm** | SEAM_EXISTS pull side (`cf5f017a`/`79785150`/`053a988c` — `053a988c` + `86e9f84` were **previously unledgered**, recorded now) and publish side (`cf5f017a`/`599d6f64`/`ed7c87f`); **P10 PORT** `9f68580` (above); NO_ACTION `527459a0`/`86e9f84`. |
| **aea-tunnel** | **G2 EXTRACT** + **P7 PORT** (above); REJECTED `49dc9c29`-cluster (above); NO_ACTION probe internals + docs (`1408ca73`/`b944c74d` internals, `437262df`, `5627a0a4`, `e8cfe381`, `079b1402`). |
| **publish-providers** | RESOLVED_BY_PR #14 (`9a696e65`/`e06b2dd7`/`5e13d8b7`/`1984ccc`/`a5979e42`/`6055397`/`d87672ea`); SEAM_EXISTS `0e7322ce` (MarkBin); **G4/G5** (above); REJECTED MCP publish 10-tool set (above); NO_ACTION `54f01fc0`/`d5dc40e`/`ca927a74`. |
| **gitfarm-cloudsync** | SEAM_EXISTS all (`9a9e5fe1`/`5c7b2c93`/`aaf7cfe3`/`7eb7d6c`/`9fa9b796`/`15b1f9c`; `33658c12`) — upstream itself disabled the feature (dormant). |
| **telemetry-pkgmgr** | **G8 EXTRACT** (above); SEAM_EXISTS `4bb769ff` (`frontend_rum_config`) and AIM auto-update (`313a13d6`/`2b95f6ac`/`fc06e9ab`/`7e1d2a1c`, plus `3e1fca67` persist-AIM-installed-MCP-spec — audit "unaccounted" row, SKIP_INTERNAL: aim package manager absent — via `DashboardContributor`/`PackageManager`). |
| **singles** | **G7 EXTRACT** + **X1 EXTRACT** (above); RESOLVED_BY_PR fork #57 (`3e5d7132`/`9dca095` workflows); SEAM_EXISTS `cec38579` (`McpToolingProvider`), `0fe90c30` (mimir via `AppsLoader`); NO_ACTION `a4a35008`/`16c5375` (fork-initiated divergence — keep out), `d0a78b07`, `7c75dec`, `7f86241d`; `f8383887` rejected (above). **Stale batch-21 note:** `afed9312`'s "fork has no `McpGatewayConfig`" reason is stale (`max_backends=64` present). |
| **defers** | RESOLVED_BY_PR #14 comment cluster (`affffcf`/`4685d34`/`475146ca`/`86315dc`/`0000f561`/`f746d60`) + masonry `cd6730f`; **G3/P6/P3/P4** (above); NO_ACTION `2bbf2e0b` (pod harness). |
| **four-misses** | The four genuinely-untriaged commits, all IMPLEMENTED above: `b5c0f9c5` (P1), `4ab97b39` (P2), `1c48788e` (X1), `176120c9` (P9 — batch-35 window recorded "ZERO SKIP" but the commit was untriaged). |
| **nonkiroacp** | SEAM_EXISTS CC re-registration (`641a6f0c`/`e42cb331`/`cdd68ecb`/`cdb2d135`/`c143b659`/`2e976a72`/`76cfc2e0` — via the dormant `ACP_BACKEND_CLAUDE` seam) and CC spend surface (`3997c2ae`/`7b74179b`/`c6bc2744`/`f6e38b3b`); RESOLVED_BY_PR #14 `d5dc40e`/`186aeaa`; NO_ACTION `3d466e6c`/`42ebd21` and the remaining 6 (`a8224f18`/`056aa42`/`d7271865`/`f6e38b3b`/`79868424`/`823ec07`). |
| **beta-collision** | Collision map: **PR #14** = `platform/*` reformat + publish files (`publish_provider.py`/`publish_sync.py`/`capabilities.publish`); **PR #18** = `mcp_gateway`/`knowledge`/`providers/acp.py`/`skill_providers`. RESOLVED_BY_PR rows as noted; NO_ACTION housekeeping (`8c8d085d`/`23853a3d`/`4dfc18ff`/`25cfdce3`); `9f68580` handled by P10; `08e28b8` stays with the next dailysync run. |

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
| [`5d99a8d4`](https://code.amazon.com/packages/MeshClaw/commits/5d99a8d4198a5904f91eacff87b02380bf781bcb) | backend | SKIP_INTERNAL (**"no fork consumer" overturned 2026-07-18, G7**) | feat(security) MCS-Jail Midway AgentContext (Mesh-1517) — Midway/MCS coupling; `jail.py` absent, MCS-Jail Brazil dep. Its one generic hunk (`sandbox.py` `userns_available()` public alias) was skipped for having **no fork consumer** — only the absent `jail.py` called it (anti-miss (b); same precedent as batch-16 `useVisibilityInterval`). **→ 2026-07-18 governance-seam re-triage:** the two host probes (`userns_available()` + `is_wsl()`) are now **EXTRACT (G7)** — the CPP seam strategy (`JailProvider` extension point) makes them core host-probes a companion jail backend consumes, and the internal caller now routes through the public alias so it has a core consumer. The jail orchestration (`jail.py`, `mcs_jail_provider.py`, `--no-jail`, `agent.jail` enum, cli_doctor jail status) stays companion-side. See the 2026-07-18 re-triage section. |
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
| ~~[`475146ca`](https://code.amazon.com/packages/MeshClaw/commits/475146caa019888fc5ee6784efe50c469d438452)~~ | backend | ~~DEFER~~ → **PORTED (PARTIAL) in batch-42** | feat(artifacts) comment lifecycle — audited agent delete + orphaned-anchor detection. Store + HTTP halves landed via PR #14 (artifacts-mirror, with fork-side hardening: `_run_off_loop` offload + server-side reason redaction). Batch-42 ported the missing agent-facing surface: the five comment MCP tools (`artifact_get_comments`/`post`/`reply`/`mark_review`/`delete_comment` — folding by content the `a84eabc4` base four and the [`de64d07b`](https://code.amazon.com/packages/MeshClaw/commits/de64d07bcaa29cf936e5949549612d1f0f734ebb) full-body/anchor formatting whose old "subsystem absent" reason was stale post-PR #14), the validation schemas, the comment-triage rubric in `skills/artifacts/SKILL.md`, and the spec's Comments & Lifecycle section. `artifact_update_comment` (`a5979e42` edit-comment tool) deliberately NOT ported — separate un-ported row. |
| ~~[`86315dc`](https://code.amazon.com/packages/MeshClawWebsite/commits/86315dcb17bc2641b9dea8a97d394c01c63017b8)~~ | frontend | ~~DEFER~~ → **resolved (PARTIAL) in batch-42** | feat(artifacts) comment lifecycle UI — orphaned-anchor warning + comment activity events. All production hunks landed verbatim via PR #14 (`CommentsSidebar.tsx` ORPHAN_WARN + syncWarn join + opacity-60; `ArtifactDetailPage.tsx` comment verb map/snippet/reason/no-arrow; `types/index.ts` `'comment'`/metadata/anchor_orphaned). Batch-42 ported the missing tests: the new `CommentsSidebar.test.tsx` orphaned-anchors describe (Mesh-2752, 4 specs) and the activity-timeline comment-events spec in `ArtifactDetailPage.test.tsx`. |

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
| ~~[`0000f561`](https://code.amazon.com/packages/MeshClawWebsite/commits/0000f561ecc8f750b51203290279dcdb9ee9d6a7) / [`f746d60`](https://code.amazon.com/packages/MeshClawWebsite/commits/f746d60d6beeb1b9219bcbcb0322986bb05a3793)~~ | frontend | ~~DEFER~~ → **resolved (PARTIAL) in batch-42** | Comment-anchor offset fix / collapse empty comment sidebar. Production content of BOTH commits landed verbatim via PR #14 (artifacts-mirror): `0000f561`'s startOffset field + bestScore/bestDist tiebreak in `useMarkdownCommentHighlights.ts` + the create-site offset persistence, and `f746d60`'s sidebarUserToggledRef/sidebarNavRef collapse-by-default in `ArtifactDetailPage.tsx`. Batch-42 ported the missing test coverage (rangeForAnchor occurrence specs; sidebar collapse/auto-open/nav-reset regression tests + artifactComments mock-leak reset). `f746d60`'s RemoteArtifactDetailPage hunks stay dropped (Harmony/Chorus remote surface stripped). |

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
| ~~[`de64d07b`](https://code.amazon.com/packages/MeshClaw/commits/de64d07bcaa29cf936e5949549612d1f0f734ebb)~~ | backend | ~~SKIP_INTERNAL~~ → **folded into batch-42 `475146ca` port** | artifact_get_comments full body — old "artifact-comments subsystem absent" reason went stale after PR #14 landed the comment store; the full-body + anchor-formatting dispatch was ported by content (from beta HEAD) with the comment MCP tools. The `a84eabc4` base four (get/post/reply/mark_review) were folded in the same way. |
| [`321f9fbc`](https://code.amazon.com/packages/MeshClawWebsite/commits/321f9fbcd6aa45b09d326c7a839546cac7ca8899) / [`eed2773d`](https://code.amazon.com/packages/MeshClawWebsite/commits/eed2773df99fd7a4d713bc1a5d517356372e67f6) | frontend | SKIP_INTERNAL | WritingReview context dialogs / resume scan — `WritingReviewPage` absent. |
| [`3a182608`](https://code.amazon.com/packages/MeshClawWebsite/commits/3a1826087667b36e8afc3b56984d77b1f3b46597) | frontend | SKIP_INTERNAL | code-reviewer workspace pin/drag — `apps/code-reviewer/CodeReviewerPage` absent. |
| [`5e13d8b7`](https://code.amazon.com/packages/MeshClawWebsite/commits/5e13d8b75f5b06d3d75e3f3ed4f4ed05ccdb82c6) | frontend | SKIP_INTERNAL | Provider-aware share panel + LIVE mode — Harmony Artifactory + Chorus publish absent. |
| [`92104fcc`](https://code.amazon.com/packages/MeshClawWebsite/commits/92104fccaa5364243a21fbabcf5ae752d84e1d1d) | frontend | ~~SKIP_INTERNAL~~ → **PORTED (P7, contingent on G2)** | Tunnel status tile — the old "AEA-tunnel subsystem absent" rationale was stale (the fork ships the `/api/tunnel/status` endpoint). **→ 2026-07-18 governance-seam re-triage:** ported as **P7** (Overview `TunnelStatus` tile, hidden unless a tunnel is active), unblocked by **G2** routing the tunnel lifecycle through the `TunnelProvider` seam. See the 2026-07-18 re-triage section. |
| [`3997c2ae`](https://code.amazon.com/packages/MeshClaw/commits/3997c2aefb4856fb1c7bff77c34702ccd074f142) / [`7b74179b`](https://code.amazon.com/packages/MeshClaw/commits/7b74179b3065e830f8bf927b66188b4c6fb0a683) | backend | SKIP_NONKIROACP | `/api/usage/cost` + per-turn token tracking — the deleted `claude_code` provider spend surface. |
| [`c6bc2744`](https://code.amazon.com/packages/MeshClawWebsite/commits/c6bc2744182f901c3e31c74c5e1747a392ff4184) / [`f6e38b3b`](https://code.amazon.com/packages/MeshClawWebsite/commits/f6e38b3b625d46f35be0dcfcc844f490ed79c9d2) | frontend | SKIP_NONKIROACP | Claude $-spend top-bar pill / model-label dedup — provider-spend + provider-selection surface. |
| [`3e5d7132`](https://code.amazon.com/packages/MeshClaw/commits/3e5d7132e46c3fd7dd4394dd182fba9f58656025) | backend | DEFER (still open — re-audit/close next batch) | Reject type-unsafe authored workflow scripts. Batch-28 re-audit: fork PR #57 carries all three fixes by content. Batch-42 sweep: PR #57 has MERGED and the fork ships `src/kiro_crew/workflows/validate.py` — blocker resolved; verify by content and close (likely ALREADY_PRESENT). |
| [`f8383887`](https://code.amazon.com/packages/MeshClaw/commits/f8383887d8dec0f3583a3cbcf674468172e3dd45) | backend | DEFER (still open — re-audit next batch) | Hero images for deploy_web/workflows — asset-only. Batch-28 re-audit: needed the `fd633154` builtin-ui materialization substrate. Batch-42 sweep: that blocker is SUPERSEDED — fork PR #26 (`afe20357`) shipped a different fork-native hero architecture (`website/public/app-assets/`); the `workflows` hero exists and `deploy-web` declares no heroImage (no broken-image bug). Re-audit → likely ALREADY_PRESENT-by-architecture / tiny adaptation. |
| ~~[`d2240c48`](https://code.amazon.com/packages/MeshClaw/commits/d2240c48bbd72de6e7141fbc313c1390f9087760)~~ | backend | ~~DEFER~~ → **PORTED (PARTIAL) in batch-28** | Was: tunnel-off Slack-link warning hardening. Batch-28 rescued the generic `_dm_owner`/`_dispatch_owner_dm` redacting DM exit point (fork `5d66a45`); the tunnel `resolve_tunnel_enabled` hunks remain absent-subsystem drops. |
| ~~[`0000f561`](https://code.amazon.com/packages/MeshClawWebsite/commits/0000f561ecc8f750b51203290279dcdb9ee9d6a7)~~ | frontend | ~~DEFER~~ → **resolved (PARTIAL) in batch-42** | Comment-anchor highlight offset — production hunks landed verbatim via PR #14 (artifacts-mirror); batch-42 ported the missing rangeForAnchor test coverage. |
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
| [`16fa88d2`](https://code.amazon.com/packages/MeshClaw/commits/16fa88d2) | backend | ~~SKIP_INTERNAL~~ → **EXTRACT (G7, probe only)** | fix(jail) skip MCS-Jail under WSL. **→ 2026-07-18 governance-seam re-triage:** the generic host probe `is_wsl()` is extracted to `sandbox.py` alongside `userns_available()` (**G7**, paired with `5d99a8d4`) for the companion `JailProvider` to consume; the MCS-Jail WSL-skip orchestration stays companion-side. See the 2026-07-18 re-triage section. |
| [`71ac5fcc`](https://code.amazon.com/packages/MeshClaw/commits/71ac5fcc) | backend | SKIP_INTERNAL | fix POST→GET downgrade on Midway redirect in Taskei GraphQL client — both internal. |
| [`803af9de`](https://code.amazon.com/packages/MeshClaw/commits/803af9de) | backend | ~~SKIP_INTERNAL~~ → **EXTRACT (G1)** | feat(cli) pluggable preflight checks with midway refresh. **→ 2026-07-18 governance-seam re-triage:** the generic runner is now **EXTRACT (G1)** — a new `preflight.py` runs seam-supplied `IdentityProvider.preflight_checks()` (already-resolved callables only; NO `module:function` string mechanism — that would be a code-exec escalation) before the `gateway`/`token` commands; `DefaultIdentityProvider` returns `[]` so standalone startup is byte-identical. The Midway `ensure_midway` check is returned by the companion's adapter, not the core. See the 2026-07-18 re-triage section. |
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
| [`a6a7c2db`](https://code.amazon.com/packages/MeshClaw/commits/a6a7c2db) | ~~frontend~~ **backend** | ~~SKIP_INTERNAL~~ → **EXTRACT (G8)** | AppSenseAIUsage telemetry. **→ 2026-07-18 governance-seam re-triage:** correcting a misfile — this is a **backend** commit (chat-success telemetry sites), not frontend. Re-triaged as **EXTRACT (G8)**: the two chat-success sites (`dashboard/chat_runner.py`, `slack/handler.py`) now emit a best-effort `current_context().telemetry.record_event("interaction", …)` — payload is **strictly metadata** (`session_key`/`surface`/`model`, never prompt/response text). `DefaultTelemetryProvider` is a no-op, so standalone is byte-identical. The usage_upload queue / cookie jar / appsense endpoint stay companion-side inside its `TelemetryProvider`. See the 2026-07-18 re-triage section. |
| `24f23968` | frontend | SKIP_INTERNAL | Knight Rider *world* — parody theme (like LCARS), absent substrate. |
| `7c9c140f`, `92a1d3bb`, `94196d1a`, `fedf6e4d` | — | SKIP_INTERNAL | changelog/version bumps to 3.x — fork is 0.1.0. |
| [`6181474a`](https://code.amazon.com/packages/MeshClaw/commits/6181474a) | backend | ~~SKIP_INTERNAL (**flagged**)~~ → **RESOLVED (EXTRACT, G3)** | SharePoint/Loop redaction carve-out — see [Human-decision](#still-open-pending-a-decision). **Closed 2026-07-18** as EXTRACT (G3): the exact-host heuristic exemption is generic via `CredentialPolicy.exempt_exact_hosts()`; host list is companion data. See the 2026-07-18 re-triage section. |
| [`3396e112`](https://code.amazon.com/packages/MeshClaw/commits/3396e112) | backend | SKIP | **(recovered on re-audit, not in the original 13)** Byte-identical twin of the ported `3ef2bdbc` (same upstream CR-281616797); intentionally not ported to avoid a duplicate. |
| [`cd6730f`](https://code.amazon.com/packages/MeshClawWebsite/commits/cd6730f) | frontend | ~~DEFER~~ → **ALREADY_PRESENT (batch-42 re-audit)** | artifacts masonry — fork ahead via merged PR #14; see the [Resolved section](#resolved-deferred-earlier-later-ported--recorded-for-the-audit-trail). |
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
