# Kiro BYO Changelog
## 0.1.0

- Replace the fixed 24-round turn stopper and Glitter's fixed six-worker-round handback with an adaptive execution guard. Productive long runs may continue; repeated identical tool+argument+result work and short action cycles first receive an internal re-plan request and stop only if stagnation continues. A very high emergency tool-call backstop remains for catastrophic runaway protection. Runtime Trace records threshold, re-plan, handback and halt events.
- Add per-model deep-thinking policy controls: `Auto / On / Off` for models that can actually switch thinking. Explicit On/Off are hard user locks; Auto delegates each invocation to the selected Type. Fixed-on, fixed-off and unknown-capability models expose disabled controls rather than fabricated choices. Reasoning effort remains an independent axis and is only selectable when the concrete model exposes real effort levels.
- Stream provider-authentic reasoning/thinking into Crew's thinking lane without mirroring normal answer content. Provider continuation state remains private/model-scoped, and fallback is disabled after visible reasoning has begun so one model's thinking cannot precede another model's final answer.
- Make Auto thinking and Auto effort policy-driven per invocation. Tiga uses speed-biased Sky, task-adaptive Multi, reliability-biased Power, and per-call collaborative Glitter; explicit user settings and concrete model capability always win.

- Namespace virtual Type wire ids as `kiro-byo-type:<mode-id>:<type-id>` and reject the old unnamespaced form. Type identity is now Mode-local, so future Dyna/Gaia packs can expose different Type sets or reuse local Type ids without collision. Dashboard Type labels/order are generated from installed pack metadata instead of hardcoding Tiga's four forms.

- Under Kiro BYO, show the Type outside the picker list as "<Mode> — <Type>" (e.g. "Tiga — Multi") instead of the raw wire id: composer model chip, status bar, and the "将 … 置顶到 default 代理" pin row. Done by a small label script inside the existing BYO `index.html` guard; it only rewrites visible text matching `kiro-byo-type:*`, so the official backend is unaffected. Labels come from all installed Mode packs.
- Under Kiro BYO, a session with no pinned model no longer shows Crew's "auto": `GET /api/agents/resolved-model` now reports the active Mode's default Type (Tiga: Multi), the same Type a new BYO session actually runs. Other backends and pinned models pass through Crew's answer unchanged, so switching back to Kiro restores "auto". Implemented as a BYO-owned wrapper rebound in `dashboard/handlers/agents.py`; `tests/test_type_labels.py` covers it.
- Fix false "Integration 已安装 / 运行正常" after Install/Update while the Gateway still runs pre-install code. The `.pth` hook runs in every Python started from Crew's interpreter (the Kiro BYO ACP child, MCP servers, scripts), and each of them overwrote `activation.json` with its own PID, so the App concluded a restart had happened. Only `python -m kiro_crew gateway` now records activation (`process_role.is_crew_gateway_process`), and inside the Gateway an integration counts as active only when the activation record belongs to the serving process and that process did not perform the install. Symptom fixed: BYO selected, but the picker kept old in-memory `/api/models` code (no Sky/Multi/Power/Glitter Types) with no restart banner.
- Add `tests/test_activation_truth.py`: process-role and activation-truth regressions for the incident, plus an opt-in end-to-end test (`KIRO_BYO_E2E_CREW=1`, Crew's bundled python) that calls Crew's real `api_models` through `KiroCrewConfig` backend resolution and asserts the four `kiro-byo-type:*` rows come first with no `auto` / `auto:jev`.

- Fix KiroCrew 0.9.0-insider.6 integration against the shipped dashboard: stop matching minified `withAutoFirst` / Jev helper bodies, validate the stable `data-model-id` picker DOM contract instead, and inject a backend-conditional HTML/CSS guard. This removes the false `expected one dashboard Auto helper asset, found 0` failure while keeping official Auto/Jev untouched outside BYO.
- Fix KiroCrew BYO model-picker semantics: expose the active Mode's Type rows before concrete BYO models, and suppress Crew-native `Auto` / `Auto (Jev)` only when a `kiro-byo-type:*` list is present.
- Allow a provably Kiro BYO-owned patched Crew seam with a missing pristine backup to stop blocking upgrades; destructive uninstall remains fail-closed when a pristine baseline cannot be proven.
- Keep Mode as its own management navigation surface instead of nesting it inside Models.

- Rename the product/display brand from **Kiro BYO** to **Kiro BYO**, subtitle **ACP Harness for KiroCrew**, and unify the unreleased project's internal identity at the same time: backend/application ID `kiro-byo`, Python package `kiro_byo`, CLI `kiro-byo`, `KIRO_BYO_*` environment variables, BYO config/data locations, Crew overlay module names, and session/backend identity.
- Clarify the architecture boundary: Kiro BYO is a provider-neutral Agent Backend that sits alongside the official Kiro CLI under KiroCrew; KiroCrew chooses the Agent Backend, while Kiro BYO chooses the Provider/Model. It is not a Kiro CLI child backend and not a KAS wrapper.
- Preserve the existing semantic compatibility probe and the Crew-authoritative 9/10 capability contract; no provider/model ownership or member-backend inheritance behavior changes in this branding release.
- Add a bounded provider-neutral Runtime Inspector event stream without changing 0.1.0. `TurnTelemetryStore` remains metadata-only; the new `RuntimeTraceStore` records ACP/provider/tool lifecycle events in `metadata` mode by default, supports explicit local `full` content tracing with recursive credential redaction, and can be disabled with the `KIRO_BYO_RUNTIME_TRACE` environment variable.
- Re-audit Personal/custom-agent fit: Kiro BYO continues to consume Crew-owned JSON/Markdown agent projections (prompt, tool visibility, allowed/excluded tools, MCP and resources) on new/load while leaving hooks, saved-member lifecycle and Provider/Model ownership in their existing layers. `member_saved_agent` remains deliberately unclaimed.
- Separate **Mode / Type / Model** in the user interaction model. The Kiro BYO management page selects the active Mode (Tiga today; Dyna/Gaia reserved) and only displays its Types. KiroCrew's conversation picker receives the active Mode's Type entries (`Sky Type`, `Multi Type`, `Power Type`, `Glitter Type`) ahead of real provider models; Type choice is per-session, while concrete-model choice bypasses Mode routing. Remove the obsolete `kiro-byo-mode` virtual model sentinel.

> Historical entries below retain the **Kiro BYO** name because that was the project name when those releases and audits were written.

## 1.0.0

- Fix a false KiroCrew incompatibility on the member-backend seam without changing 1.0.0. The adapter no longer depends on the exact text/formatting of `members.select_provider_backend`; it inserts a narrow AST-validated Kiro BYO inheritance guard and leaves every non-Kiro-BYO path under Crew ownership. Compatibility is now version-advisory rather than exact-release-pinned: newer semver-like Crew builds enter full semantic preflight and, if all source contracts still validate, are accepted as `semantic compatible / unaudited version` instead of being rejected solely for a version bump. Runtime self-heal now validates the same saved overlay dependency contract before writing after a Crew update.
- Re-audit the complete 1.0.0 release tree against the current KiroCrew `v0.9.0-insider.6` commit `b4cf148` (2026-10-13). The 9/10 capability claim remains correct; `member_saved_agent` is still deliberately unclaimed. Refresh the Crew source-contract CI target from insider.5 to insider.6, isolate process-lifecycle tests from the app home, and add fail-closed release checks so mutable session/telemetry/audit state or live provider/native-tool configuration cannot be shipped accidentally.
- Correct stale historical/current wording around projected agents: ACP active-mode acknowledgement verifies the session-specific active projection but does not imply the full saved-member capability contract.
- Harden the adaptive Crew integration's fail-closed boundary: the owned Kiro BYO Harness now has an AST-validated contract for 35 executable Crew bindings across 15 unmodified source files plus 3 verified dynamic facade exports, so moved symbols cannot be masked by stale comments/strings. Remove vendor-specific DeepSeek/provider-label patch anchors in favor of Crew's native/default backend symbols.
- Fix Xiaomi MiMo Token Plan as a first-class official connection path without changing the 1.0.0 version. Token Plan `tp-`/`ttp-` credentials now use Xiaomi's subscription Base URLs (`token-plan-cn/sgp/ams.xiaomimimo.com/v1`) while retaining the native MiMo Chat adapter, reasoning/tool continuation, and official V2.6 Pro/Flash model profiles. The UI auto-selects Token Plan when a Token Plan key is pasted, and mismatched pay-as-you-go vs subscription credentials fail with an actionable message instead of an opaque upstream auth error.
- Fix the Overview 2×2 card alignment at the actual CSS specificity root cause: the generic `.ka-panel+.ka-panel{margin-top:12px}` stacking rule outranked the old per-card `margin:0`, shifting grid items 2–4. Overview cards now use a later, equally/more specific grid-child reset and fill their fixed row track. Add App-bundle/UI fingerprint diagnostics so stale installed copies can be distinguished from a development checkout and the separate site-packages ACP runtime.
- Promote the hardened Kiro BYO line to 1.0.0 while keeping KiroCrew 0.9 as the currently verified host contract; Kiro BYO and KiroCrew version numbers are independent.
- Make mid-turn steering cooperative across provider generation, permission waits and native process execution. A user steer interrupts an in-flight shell/Python/test process tree, marks remaining same-plan tool calls as skipped, injects the new user instruction and replans without cancelling the whole session.
- Add host-mediated directory-scope escalation for explicit out-of-root filesystem work. The user can allow a single call or the current session; sensitive paths remain hard-denied and grants are never persisted as global roots.
- Tune the provider-neutral Agent Harness for fast-first project reconnaissance: prefer structured filesystem tools and batched reads, avoid unsolicited exhaustive scans/tests/builds, and keep shell for genuine process execution.
- Add Xiaomi MiMo V2.6 as a first-class native provider adapter, including thinking on/off semantics, streaming/tool-call handling and provider-native `reasoning_content` continuation across Agent tool turns.
- Redesign the App information hierarchy across Overview, Providers, Models, Accounts, Diagnostics and Integration. Diagnostics now separates request health from token/cache usage, empty statistical samples render as unknown rather than fake 0 ms values, advanced provider fields are folded out of the primary connection flow, and responsive layouts waste less space.
- Refine the final 1.0.0 management UI without changing the version: Overview now uses one true 2×2 equal-track grid instead of independently stacked columns, Native Tools are summarized by count, model capabilities are summarized from live metadata, sidebar counters are compact/localized, and wrapped model fact grids retain proper row separators on narrower windows.
- Fix whole-Crew backend inheritance for KiroCrew `v0.9.0-insider.6` without changing the 1.0.0 version. When `agent.acp_backend` is `kiro-byo`, Crew member/teammate sessions now resolve to the same Kiro BYO harness instead of falling through the separate `agent.member_acp_backend` axis; switching away from Kiro BYO restores Crew's native member-backend behavior. Kiro BYO now declares Crew's existing member-dispatch/member-panel mounts and consumes their per-session flags, with no provider or model names crossing the integration boundary.
- Re-project the active Crew agent on Kiro BYO `session/load` through the same narrow shared-runtime resume seam used by KAS. Authorization grants remain non-persistent and are rebuilt from the fresh Crew projection, preventing stale member/tool grants from surviving restart.
- Add genuine KiroCrew Markdown-agent support without changing 1.0.0. Kiro BYO now joins insider.6's `ACP_BACKENDS_MARKDOWN_AGENT_SPECS` and matching user-level-only spec scope because its harness delegates discovery/parsing/prompt resolution to Crew's KAS agent resolver and consumes the resulting projection on `session/new` / `session/load`. `member_saved_agent` remains deliberately unclaimed: insider.6 defines it as loading the full saved member spec, including the member template's model pin, while Kiro BYO intentionally keeps Provider/Model selection inside the backend.
- Keep the provider boundary regression gate and KiroCrew `v0.9.0-insider.6` semantic integration audit. Unknown upstream Crew source shapes still fail closed before any integration write.
- Centralize runtime HTTP/MCP identity strings on the package version so future releases cannot silently advertise stale User-Agent/clientInfo versions.

## 0.9.19

- Fix the Provider page render regression where `customEndpoint` was referenced before definition; custom official-endpoint detection is now explicit and the preset form also tolerates asynchronous preset discovery safely.
- Add an App-local React error boundary so a future Kiro BYO view render failure is contained inside the Kiro BYO surface instead of escalating to KiroCrew's whole-App crash boundary.
- Re-audit the adaptive integration against KiroCrew `v0.9.0-insider.5` (`dbdb3e7`, 2026-10-12), surface that source-audit target in Integration status, and extend the semantic verifier to validate the current process-reclaim name derived from `ACP_BACKEND_LAUNCH`.
- Add an automated provider-boundary regression gate: the ACP Agent Harness may consume the provider-neutral facade but cannot import concrete vendor adapters or branch on vendor identity.
- Add provider wire-format roundtrip regression coverage for both normal JSON responses and streaming/SSE paths across the major native adapter families, protecting text streaming, tool-call assembly, usage accounting and provider-native continuation/reasoning state.

- Add Xiaomi MiMo as a first-class provider using the official OpenAI-shaped Chat API, preserving `reasoning_content` across tool turns, supporting the V2.6 thinking toggle and multimodal capability metadata, and filtering the mixed Xiaomi model catalogue to the current V2.6 Agent-capable Pro / Flash / Pro-Ultraspeed models.

- Upgrade verified official providers to higher-fidelity transports without changing the 0.9.19 package version: Responses-family paths for DeepSeek, OpenAI, xAI, Ark, Qianfan and Groq; Anthropic Messages; Gemini Interactions; Cohere Chat V2; Mistral Chat with ThinkChunk replay; Kimi Chat with preserved reasoning/tool state; GLM Chat with native reasoning extensions; MiniMax's recommended Messages-compatible path; provider-aware Qwen Chat plus model-scoped Responses for supported Qwen 3.5–3.8 and current Tencent TokenHub Hunyuan models. StepFun stays on its documented OpenAI Chat API rather than receiving a fabricated transport.
- Preserve provider reasoning/tool continuation data between agent steps, enable documented provider caching/state features (including Qwen Session cache, xAI/Mistral prompt-cache keys, Anthropic/MiniMax prompt caching and provider Responses state), and safely fall back to OpenAI Compatible on custom relay endpoints. Diagnostics now separate cache-read hits from cache-write/creation tokens instead of collapsing them into one counter.
- Keep deep-thinking state and reasoning effort independent per model: exact provider-specific effort vocabularies are published, unavailable Off controls are omitted, and effort controls disappear while deep thinking is explicitly Off. Add current OpenAI GPT-5.6/GPT-6, Grok 4.3-4.7, Claude 5.x and Mistral reasoning profiles without fabricating unsupported levels.
- Expand provider presets and model capability discovery while keeping provider/model features capability-driven rather than fabricated.
- Redesign the KiroCrew App UI into separate Overview, Providers, Models, Accounts, Diagnostics, and Integration surfaces, including multimodal/reasoning capability display.
- Support multiple independent instances of the same provider family (for example multiple OpenAI accounts/endpoints), each with its own credential reference, model catalog, account status, and routing id.
- Add a fail-closed Integration uninstall path that restores exact pristine KiroCrew seams, removes the Kiro BYO runtime/bootstrap, and can optionally purge provider credentials and local Kiro BYO data.
- Preserve the 0.9.19 package/version surface; this is a packaging/documentation synchronization of the current release rather than a version bump.

## 0.9.18

- Fix a Web native-tool boundary bug discovered by real KiroCrew Cron execution: a DNS/SSRF rejection during `web_fetch` preflight previously leaked `WebToolError` past `NativeToolRuntime`, escalated into ACP `-32603`, and aborted the entire Agent turn. Web policy rejections are now normalized to `NativeToolError`, producing an ordinary failed tool result so the model can choose another source instead of killing the Cron job.
- Make DNS answer selection compatible with split/multi-address resolver results without weakening IP pinning: if a lookup contains at least one genuine public address, Kiro BYO pins and connects only to a validated public candidate and ignores unused blocked siblings. A pure blocked result remains rejected.
- Preserve the narrow RFC2544 Fake-IP exception from 0.9.16. Pure Fake-IP DNS remains supported; Fake-IP mixed with blocked answers remains fail-closed because final routing is delegated to the transparent proxy/TUN rather than to a directly validated public address.
- Improve Web rejection diagnostics by reporting the hostname plus blocked destination categories (`loopback`, `private`, `link-local`, `multicast`, `unspecified`, `special-use`) instead of the ambiguous generic `local/private` message.
- Add ACP-level regression coverage proving that a blocked `web_fetch` is returned to the model as a tool failure and does not become a prompt-fatal error.

## 0.9.17

- Fix integration readiness so a stale copied ACP runtime can no longer be reported as Ready after an App-only upgrade.
- Compare the live `site-packages/kiro_byo/_bootstrap_state.json` runtime version with the current App `VERSION`; mismatches are now `No / update required`.
- Surface the live runtime/App version mismatch in the Integration UI.
- This closes the failure mode where the App showed 0.9.16 while Cron still executed 0.9.15-or-earlier WebTool code.

# Changelog

## 0.9.16

- Fix Web Fetch/Search compatibility with transparent TUN/Fake-IP DNS environments by accepting RFC 2544 `198.18.0.0/15` only when it is returned by DNS for a hostname; literal benchmark addresses remain blocked.
- Preserve SSRF fail-closed behavior for loopback, private, link-local, CGNAT, ULA, and other special-use destinations; mixed Fake-IP plus genuinely blocked answers are still rejected.
- Prefer a genuine public DNS answer when both public and Fake-IP answers are present, while preserving single-resolution IP pinning and TLS SNI hostname validation.
- Update the native Web User-Agent to the actual release version.

## 0.9.15

- Fix backend-scoped Crew agent model pins for Kiro BYO models. When the active chat backend is `kiro-byo` but `agent.member_acp_backend` uses a different model namespace (for example KAS), Kiro BYO now detects a Kiro-BYO-only model before Crew probes the unrelated member backend entitlement catalog. The request is refused with an accurate model-scope explanation instead of the misleading “not available on your account; choose Claude/GPT…” message.
- Preserve Crew's model-pin safety contract: the patch does **not** bypass `_pin_entitlement_backend`, does not write an incompatible `agents.<name>.model`, does not silently change `member_acp_backend`, and does not claim member-dispatch capabilities. Current-session Kiro BYO model selection remains unchanged.
- Extend the Crew 0.9 semantic verifier with the model-scope guard so an older 0.9.14 `agents.py` integration is recognized as incomplete and regenerated from its proven pristine backup during Install / Update integration.
- Document an upstream UI limitation: Crew 0.9's current model picker always renders the “set as agent default” action based on current-session withholding only; Kiro BYO can safely reject an incompatible cross-namespace pin server-side, but does not patch compiled Crew frontend assets to hide that row.

## 0.9.14

- Fix KiroCrew theme integration on Windows/WebView: resolve native form-control polarity from Crew `data-mode`, so system/custom dark themes render native provider dropdowns in dark mode.
- Stop styling native `<option>` popups with app colors; the host/WebView now owns native menu rendering once `color-scheme` is correct.
- Fix overly bright sidebar metric wells by using Crew `--bg` + `--border` roles instead of the `--muted` ramp.
- Use Crew `--panel` / `--bg-hover` roles for secondary/hover surfaces.

## 0.9.13

- Fixed KiroCrew integration upgrades so a Kiro BYO-created harness file from the previous successful install is recognized by its exact runtime snapshot and can be safely replaced by the new version; genuinely foreign bytes at the same path still fail closed.
- Diagnostics are now user-manageable: the persistent telemetry ledger is bounded to the most recent 500 metadata-only turns, and the Kiro BYO App exposes a **Clear diagnostics** action.
- Reworked the App UI to consume KiroCrew theme CSS variables (`--bg`, `--card`, `--text`, `--muted`, `--border`, `--input`, `--accent`, status roles) and `useTheme()` rather than inventing independent light/dark colors. Native form controls now receive the host color scheme.
- Retains the 0.9.12 Process Model, Code Intelligence, Crew `allowedTools` projection fix, DNS-rebinding-hardened Web Fetch, and adaptive Crew 0.9 integration.

## 0.9.12-dev.2 (development)

- Fixed KiroCrew unattended/cron tool execution parity at both sides of the adapter: the Crew `KiroByoHarness` now carries the active saved agent's *safe* `allowedTools` projection, and Kiro BYO consumes that projection. Exact/glob-matched native and MCP tools emit normal tool lifecycle updates but skip `session/request_permission`, matching official Kiro semantics; unmatched tools still enter Crew's permission gate.
- The Crew-side projection is fail-closed rather than a raw copy: each no-prompt candidate must survive the live governance ceiling, Crew's own KAS `allowedTools` permission classifier, and PreToolUse-hook withholding. Broad shell/filesystem grants that would bypass Crew's deny/sensitive-path floor stay prompt-only.
- The local native sandbox, schema validation, and hard-deny policy run before any `allowedTools` grant, so auto-approval cannot widen filesystem/process safety boundaries. Authorization grants are deliberately not persisted by Kiro BYO; `session/load` requires a fresh Crew projection or falls back to prompting.
- Added regression coverage for the real failure shape (`@kirocrew-core` / `skill_search` in an unattended surface), MCP wildcard matching, native allowed-tools matching, and non-granted tools remaining gated.

- Corrected the development version spelling to strict SemVer (`0.9.12-dev.2`) so KiroCrew App installation accepts `app.json`. The release metadata checker now validates SemVer explicitly, preventing Python-only PEP 440 spellings such as `0.9.12dev1` from reaching a Drop-in ZIP.
- Added a session-owned long-running Process Model with stable `process_id`,
  bounded stdout/stderr ring buffers, stdin control, status/read/terminate/kill
  actions, process-group/tree cleanup, concurrent-process limits and bounded
  completed-process history. ACP session replacement/close/delete and server
  shutdown now close native runtimes so background processes cannot outlive the
  session that owns them.
- Added `execute_shell_process` and `control_shell_process` under the existing
  Crew-owned `shell` permission capability. `session/cancel` deliberately cancels
  the current turn without implicitly terminating persistent dev servers.
- Added a Code Intelligence adapter with `code_symbols` and optional `code_query`.
  Python symbol/overview extraction has an explicit stdlib-AST fallback;
  JavaScript/TypeScript and raw structural queries require Tree-sitter.
- Tree-sitter is an optional `code-intel` dependency set rather than a silent App
  install. `code_query` is hidden from advertised runtime tools unless the full
  Tree-sitter core + Python/JavaScript/TypeScript grammar set imports successfully.
- Updated saved-agent `read`/`shell` visibility tags so the new Harness-owned tools
  obey Crew projection and explicit `tools=[]` continues to disable all Native/MCP
  tools. Read-only native policy preserves structural code reads but removes both
  one-shot and long-running shell execution.
- Added Process Model, session-resource cleanup, Code Intelligence/fallback and
  capability-honesty tests. CI now has dedicated Windows/Linux Process Model jobs
  and a networked Tree-sitter-extra job in addition to the existing core, static
  analysis and Crew 0.9 source-contract gates.

## 0.9.11rc1 (release candidate)

- Re-audited Kiro BYO against the latest public KiroCrew 0.9 pre-release,
  `v0.9.0-insider.3` (`7cb49e9`, published 2026-10-10), and the public
  `release/0.9.0` branch. Both expose project metadata `0.9.0-rc.1` and the same
  registry/capability architecture inspected here. Public stable remains v0.8.0,
  while `main` has already advanced to 0.10.0; `main` is therefore only
  forward-looking architecture evidence, not the 0.9 runtime target.
- Updated the Crew 0.9 adaptive transformer for the release-candidate backend
  registry contract. Current 0.9 source uses `agent_sdk/backends.py` as the
  capability truth source and `register_selectable_backend()` as the selectability
  extension point. Kiro BYO now uses that dynamic contract when present and falls
  back to the legacy `BASELINE_SELECTABLE_BACKENDS` seam only for older compatible
  0.9 Insider source shapes.
- Added evidence-backed support for Crew's `HONOR_ZERO_TOOL_BAN` capability when the
  upstream capability exists. Kiro BYO satisfies it through its own saved-agent
  projection: explicit `tools=[]` hides both native and MCP tools. This is distinct
  from Crew's mirrored `AcpClient` / `SessionProjection.zero_tools` enforcement path.
- Preserved the deliberate orchestration boundary: Kiro BYO still does not claim
  Crew's full member saved-agent, member dispatch, or markdown-agent contracts merely
  to increase the legacy capability-card score. Crew owns those higher-level product
  and orchestration surfaces.
- Hardened native Web Fetch/Search against DNS-rebinding TOCTOU by resolving and
  validating a destination once per request hop, then connecting the socket directly
  to that validated `sockaddr`. HTTPS preserves the original hostname for HTTP Host,
  TLS SNI, and certificate hostname verification.
- Redirects are resolved, validated, and pinned again hop-by-hop. Mixed public/private
  DNS answers fail closed. Ambient proxy environment variables are intentionally not
  consumed by the pinned transport; a future proxy mode must use an explicitly trusted
  egress-proxy contract.
- Added `ARCHITECTURE_BOUNDARY.md` to make Crew-owned, Kiro-BYO-owned, shared-contract,
  and explicitly-out-of-scope responsibilities normative before future parity work.
- Added release engineering gates: synchronized release metadata checks, branch-aware
  coverage configuration, CI test/build/wheel-smoke jobs, and configured Ruff/mypy
  static-analysis jobs. The current local environment cannot install Ruff/mypy because
  outbound package installation is unavailable, so this release candidate does not
  claim those static-analysis jobs passed locally.
- Current local RC evidence: `140 passed, 5 subtests passed`; branch coverage 61%
  against a 60% fail-under gate; `compileall` succeeds; release metadata check passes;
  wheel build and clean-venv CLI/import smoke tests pass.
- Final `0.9.11` remains blocked on execution-level validation against the user's exact
  installed Crew 0.9 package: semantic verifier, App integration install, runtime
  status, startup self-repair, and transactional rollback. A source audit is not
  recorded as a runtime PASS.

## 0.9.10

- Expanded the native Agent Harness tool surface from 6 to 11 tools with
  `read_files`, `list_directory`, `delete_file`, `web_fetch`, and `web_search`
  while preserving the existing `read`, `glob`, `grep`, `write`, `edit`, and
  `shell` contracts.
- Added Kiro V3 visibility tags for the new read/write/web tools and legacy
  aliases (`readFiles`, `listDirectory`, `deleteFile`, `webFetch`, `webSearch`)
  without moving authorization out of KiroCrew.
- Added dependency-free native web execution with SSRF-sensitive destination
  checks, redirect re-validation, response-size/time limits, text/HTML extraction
  and a lightweight public search backend. Local/private/link-local destinations
  and embedded URL credentials fail closed.
- Added batched multi-file reads, explicit directory listing, and regular-file-only
  deletion with the same allowed-root/sensitive-path sandbox used by existing
  filesystem tools.
- Extended `native_tools.json` hard policy with `web_enabled`, web timeout, and
  web response-size limits. Per-tool trust remains Crew-owned.
- Kept the single-screen Overview compact when the native tool count grows by
  summarizing tool families instead of rendering all tool names in the stat row.
- Verification baseline: 131 source tests pass; the supplied KiroCrew
  0.9.0-insider.1 source still reports clean semantic seams, shared
  `AcpRuntime=True`, legacy `AcpClient` spawn=False, native Kiro 8/10 and
  Kiro BYO 7/10. Full transactional install and two-file startup self-repair
  (one semantic seam plus the create-only harness) are also verified.

## 0.9.9

- Refactored the KiroCrew App management surface into a persistent status/navigation sidebar plus a compact single-screen Overview. The sidebar now keeps health, Provider/model counts, recent turns, Crew/Integration state, credential backend and native-tool count visible while managing Providers, Diagnostics or Integration.
- Added a backend-owned `overview` status snapshot (`schema_version: 1`) so health/alert policy is computed once in Python instead of duplicated in the React UI. The snapshot derives `ready`, `needs_integration`, `repairable`, `needs_provider`, `degraded` and `incompatible` states from real runtime, provider, integration and telemetry facts without exposing secrets.
- Removed the remaining KiroCrew 0.8 integration overlays, semantic patchers,
  manual PowerShell overlay installers and their dedicated tests/docs.
- Restricted App integration to the Crew 0.9 `adaptive09` semantic transformer
  plus the create-only `KiroByoHarness` module; static fingerprint overlays are
  no longer a supported install path.
- Kept Crew's shared `HarnessAdapter` / `AcpRuntime` architecture authoritative
  and corrected the advertised capability result to the verifiable **7/10**.
  `member_saved_agent` remains intentionally unclaimed until Kiro BYO can satisfy
  Crew's complete saved-member activation contract.
- Split ACP dispatch and the prompt/tool runtime into smaller handlers. The main
  `_handle_prompt_request()` coordinator is reduced from 739 lines to 113 lines;
  provider/tool looping and native/MCP/deferred tool execution are isolated.
- Hardened MCP modern/legacy negotiation: `-32022 UnsupportedProtocolVersion`
  no longer causes blind legacy fallback, and modern `tools/list` cache metadata
  (`ttlMs`, `cacheScope`) is validated.
- Retained transactional integration preflight, exact rollback and startup
  semantic self-repair for known-clean Crew 0.9 seams.
- Updated Windows/App documentation so all supported installation instructions
  go through `Kiro BYO -> Integration`.
- Verification baseline: 122 source tests pass and the supplied
  KiroCrew 0.9.0-insider.1 source reports clean semantic seams, shared
  `AcpRuntime=True`, legacy `AcpClient` spawn=False, native Kiro 8/10 and
  Kiro BYO 7/10.
