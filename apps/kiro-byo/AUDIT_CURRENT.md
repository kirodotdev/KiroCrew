# Kiro BYO 0.1.0 Release Audit & KiroCrew 0.9.0-insider.6 Compatibility

## Scope

0.1.0 is the stable Kiro BYO milestone built on the hardened 0.9.x line. The release keeps the verified KiroCrew host target at `v0.9.0-insider.6` (`b4cf148`, re-audited 2026-10-13) and `release/0.9.0`; the Kiro BYO 0.1 version does **not** imply a KiroCrew 1.0 dependency.

The release audit covers the provider-neutral Agent Harness boundary, mid-turn steering through native tools, host-mediated directory access, the redesigned App UI, Xiaomi MiMo native adaptation, and the existing semantic Crew integration verifier.

## Product and internal identity boundary

The product/display identity is **Kiro BYO** with the subtitle **ACP Harness for KiroCrew**. Because
0.1.0 is still unreleased, the technical identity is unified rather than carrying a pre-release
compatibility alias: KiroCrew backend/App ID `kiro-byo`, Python distribution/CLI `kiro-byo`, import
package `kiro_byo`, `KIRO_BYO_*` environment variables, BYO data/config locations, the Crew overlay
module `kiro_byo.py`, credential namespace `KiroBYO/`, and persisted session/backend identity all use
the BYO name. ACP `agentInfo.name/title` reports `Kiro BYO` as the corresponding display identity.

## Agent Harness execution and steering

Mid-turn steering now reaches every expensive phase that can reasonably cooperate: provider streaming, ordinary Crew permission waits, directory-scope permission waits, one-shot shell execution and managed process launch/control. On Windows the current one-shot process tree is terminated when a steer arrives, the tool result records the interruption, later tool calls from the abandoned plan are not executed, and the queued user instruction is folded back into the same turn for replanning. `session/cancel` remains semantically distinct and ends the current turn.

Broad inspection prompts use a fast-first policy: shallow reconnaissance, structured filesystem search/list/read tools and batched known-file reads are preferred. Tests, builds, linters, installs and exhaustive scans are not launched merely because the user said “look at this project”; they remain available when explicitly requested or needed to verify a concrete issue.

## Directory permission boundary

`cwd` and ACP `additionalDirectories` remain the normal workspace roots. When an otherwise valid filesystem tool targets a directory outside those roots, Kiro BYO can ask KiroCrew for that exact tool permission and, after approval, grant only the narrow required directory scope. `allow_once` is revoked immediately after the call; `allow_always` lasts only for the current native runtime/session. Neither path rewrites global `native_tools.json`. Explicit sensitive paths and other hard-deny policy still fail before dynamic scope can be granted.

PowerShell continues to run with the current Windows user's OS authority; Kiro BYO does not claim a fake process-level filesystem sandbox. The Agent instruction therefore prefers structured filesystem tools and forbids using shell merely to bypass a directory scope the user refused.

## Provider boundary and Xiaomi MiMo

Provider adaptation remains one-way. `acp_server.py` and `turn_runner.py` consume the provider-neutral facade and do not import concrete vendor adapters or branch on vendor identity; `tests/test_provider_boundary.py` enforces this architecture.

Xiaomi MiMo V2.6 is a first-class provider adapter on Xiaomi's OpenAI-shaped Chat API rather than a generic preset only. The adapter owns MiMo's `thinking` switch, streaming/tool-call assembly and required `reasoning_content` replay across assistant tool turns while presenting the same neutral `ChatTurn` contract to the Harness. Both Xiaomi credential families are now explicit first-party paths: pay-as-you-go uses `sk-` keys at `api.xiaomimimo.com/v1`, while Token Plan uses `tp-`/`ttp-` keys at Xiaomi's documented CN/SGP/AMS subscription endpoints. Token Plan keeps the native adapter rather than being misclassified as a custom relay.

## App UI

The management App keeps the KiroCrew visual language but reorganizes information hierarchy across Overview, Providers, Models, Accounts, Diagnostics and Integration. High-value status and actions are visually primary; advanced endpoint/model-id fields are folded out of the normal provider connection flow; Diagnostics separates request health from token/cache accounting and does not represent missing latency samples as `0ms`. Responsive layouts collapse dense metric rows instead of leaving orphan cards and large unused grid areas.

An App-local React error boundary still contains Kiro BYO render failures inside the App surface rather than escalating them to KiroCrew's outer crash boundary.

## KiroCrew v0.9.0-insider.6 source compatibility

The adaptive `v0.9` integration is source-audited through KiroCrew `v0.9.0-insider.6`. The current contracts still expose the shared `HarnessAdapter`, dynamic backend registration, explicit harness registry and semantic seams required by `adaptive09`. Insider.6 also exposes a distinct member-DM backend axis (`agent.member_acp_backend`), which is why a root Kiro BYO session could previously coexist with KAS-backed members. The adapter now makes Kiro BYO authoritative for that member resolution only while `agent.acp_backend == "kiro-byo"`; switching away restores Crew's native member-backend rule. Kiro BYO also participates in Crew's existing member-dispatch/member-panel mounts rather than recreating member orchestration.

`integration/kiro_crew/v0.9-adaptive/compatibility.json` remains the machine-readable source-audit record. In addition to semantic inspection of every Crew file Kiro BYO transforms, the current adapter now validates the unmodified Crew APIs imported by the owned Harness module as **35 executable module bindings across 15 source files plus 3 verified dynamic facade exports**. That check is AST-based, so a stale symbol name surviving only in a comment or string cannot satisfy the contract. Unknown source shapes therefore fail before integration writes begin. The version string itself is no longer the authority: a newer semver-like KiroCrew build may pass semantic preflight when every required seam remains intact, while the UI separately marks versions outside the audited family as unverified. Exact installed-package acceptance must still run the App verifier/install/runtime/repair/rollback sequence after a KiroCrew update.

The semantic transformer is also provider-neutral at its patch anchors: it binds Kiro BYO next to Crew's native/default backend symbols rather than locating seams through DeepSeek or another third-party backend. Concrete provider/model names remain confined to Kiro BYO's Provider layer.

On `session/load`, insider.6 has no public HarnessAdapter flag for hosts that require the active agent projection to be resent; that resume branch is explicitly KAS-only upstream. The integration therefore widens only that existing branch to Kiro BYO. Kiro BYO continues not to persist authorization grants, so resumed sessions receive a fresh Crew projection instead of reviving stale permissions.

The Crew-authoritative capability result remains **9/10**. `markdown_agents` is supported through Crew's own Markdown resolver/projection seam; `member_saved_agent` remains deliberately unclaimed because insider.6 defines it as loading the complete saved member spec, which includes member-level model selection and is stronger than member dispatch/session projection.

## Mode / execution policy audit

Kiro BYO now has a **hot-pluggable Mode layer backed by Policy Packs**. The existing Sky Type / Multi Type / Power Type / Glitter Type definitions are data in the built-in **Tiga Policy Pack**, not hard-coded branches in `mode_router.py`. The generic Execution Planner consumes schema-validated routing weights, complexity adaptation, secondary-stage triggers/objectives and abstract reasoning intent. User JSON packs under `<KIRO_BYO_HOME>/policy_packs/` are re-read on each config/UI load and can be added, edited, removed or intentionally override a built-in pack id without restarting Kiro BYO; malformed packs are surfaced by the management catalog and cannot execute code. Removing the selected pack fails runtime config closed instead of silently changing behavior.

Every policy may use one or multiple configured models. The primary route remains capability-gated and provider-neutral; a deterministic Execution Planner then decides whether a bounded, tool-free secondary stage is worth the extra latency/cost. The Tiga pack currently expresses Sky as low-overhead speed/economy, Multi as expected-value balance, Power as capability/reliability, and Glitter as maximum-quality final review. Secondary-stage failure always preserves the accepted primary draft and can never execute tools or side effects. Policy Pack reasoning intent is explicitly orthogonal to model capability: the harness maps only onto effort controls the selected model actually exposes and never invents thinking/effort support. Runtime Trace records the active pack, policy, execution mode, rationale, secondary role/model/reason and stage outcome. Manual concrete-model selection still bypasses BYO Mode routing entirely.

The interaction boundary is now explicit: the App management page changes only the active **Mode** and shows that Mode's available Types read-only. Type selection is carried per session from KiroCrew's conversation picker using private `kiro-byo-type:<mode-id>:<type-id>` sentinels; those entries are presented before concrete provider models. The previous `kiro-byo-mode` virtual model has been removed. A Mode switch resets stale/unsupported Type selections to the new pack's declared default, while an explicit concrete-model selection remains independent and bypasses the planner.

## Personal Agent and observability audit

Kiro BYO is compatible with KiroCrew's personal/custom-agent architecture at the backend boundary: Crew owns agent selection and JSON/Markdown parsing; Kiro BYO receives the active projection on new/load and enforces its prompt, visible/allowed/excluded tools, MCP projection and Crew-injected resources. Crew-owned hooks and saved-member lifecycle remain outside BYO, and provider/model selection deliberately remains inside BYO. This supports Personal Agent use without falsely claiming `member_saved_agent`.

Observability now has two deliberately separate stores. `TurnTelemetryStore` remains metadata-only. `RuntimeTraceStore` is a bounded structured event ledger covering ACP inbound/outbound frames, provider rounds/stream deltas, tool search/calls/results and turn completion. Its default `metadata` mode stores no prompt/response/tool body; `full` mode is explicit local debugging and recursively redacts credential-shaped fields. Release packaging must exclude `data/runtime_trace` exactly like sessions/telemetry/audit runtime state.

## Release QA

- Python suite: **308 passed + 21 subtests**.
- Branch coverage: **66%**, above the 60% hard gate.
- UI JavaScript syntax: PASS (`node --check ui/index.mjs`).
- Python compileall: PASS.
- Release metadata consistency and release-tree runtime/secret-state gate: PASS for VERSION / pyproject / App manifest / package / ACP agent version.
- Wheel build: PASS (`kiro_byo-0.1.0-py3-none-any.whl`, built with `pip wheel --no-build-isolation`).
- Fresh virtualenv wheel install, package import, provider facade import and CLI smoke test: PASS.
- Xiaomi MiMo wire-format/streaming/tool-continuation behavior is covered by local regression tests; no production MiMo API call is claimed without a user credential.
- Exact installed KiroCrew integration remains fail-closed and should be re-run from the App after installing/updating KiroCrew.

---

# Kiro BYO 0.9.18 Audit

## Purpose

0.9.18 fixes the next defect exposed by real KiroCrew Cron execution after 0.9.17 proved the copied ACP runtime was current. The new traceback contained the 0.9.16+ Web error string, proving the latest Web code was actually running, but a single blocked `web_fetch` destination still aborted the entire ACP prompt.

## Real failure evidence

The observed error was:

```text
AcpError: Prompt error: {'code': -32603,
  'message': 'internal error: WebToolError: web host resolved to a blocked local/private/special-use destination'}
```

Because that exact message only exists in the newer Web resolver, this failure is no longer a stale-runtime problem. It exposed a native-tool exception-boundary bug.

## Root cause A — Web preflight leaked the wrong exception type

`NativeToolRuntime.preflight()` called `SafeWebClient._assert_public_url()` for `web_fetch`. That method raises `WebToolError`. `TurnRunner._execute_native_tool()` intentionally converts `NativeToolError`, `SandboxError`, and `OSError` preflight failures into ordinary failed tool results, but it did not catch raw `WebToolError`.

The result was:

```text
web_fetch
  -> preflight
  -> WebToolError
  -> escapes NativeToolRuntime boundary
  -> escapes TurnRunner tool failure path
  -> ACP -32603 prompt failure
  -> Cron job fails
```

This is semantically wrong for an Agent Harness. A single inaccessible or policy-blocked webpage should normally be a tool failure the model can inspect and recover from, not a fatal failure of the whole conversation turn.

## Fix A — normalize Web policy failures to native tool failures

`NativeToolRuntime.preflight()` now catches `WebToolError` and raises `NativeToolError` with the same human-readable detail. TurnRunner already knows how to report `NativeToolError` as:

```text
role=tool
status=failed
content=Error: ...
```

The model can then choose another source or explain that a particular destination is unavailable. ACP remains alive.

An ACP-level regression test deliberately forces the Web preflight to reject a hostname and proves the prompt still returns a normal result instead of an error response.

## Root cause B — resolver answer policy was stricter than IP pinning required

The resolver previously rejected the entire hostname as soon as *any* `getaddrinfo()` answer was non-global. On Windows, split DNS, VPNs, filtering resolvers, IPv4/IPv6 combinations, and transparent-proxy stacks can produce multiple answers where one is public and another is special-use.

After 0.9.11, Kiro BYO does not connect by hostname after validation. It pins the actual socket to one validated `sockaddr`. Therefore an unused private/special answer cannot become the TCP peer if a public candidate is explicitly selected.

0.9.18 now applies this rule:

```text
public candidate exists
  -> choose only validated public candidate
  -> ignore unused blocked siblings

no public candidate, pure RFC2544 Fake-IP
  -> allow the validated Fake-IP for transparent TUN/proxy use

Fake-IP + blocked answer
  -> fail closed

only blocked answers
  -> fail closed
```

Literal non-global IP URLs remain blocked. Redirects are still resolved and validated independently hop-by-hop. TCP connections remain pinned to the selected validated address, and HTTPS still preserves the original hostname for Host/SNI/certificate verification.

## Better diagnostics

Blocked DNS results are now classified as one of:

- loopback
- private
- link-local
- multicast
- unspecified
- special-use

The error includes the hostname and category set but not the full URL path/request body. This makes the next real Windows failure actionable without weakening privacy or SSRF policy.

## Security assessment

0.9.18 does **not** disable SSRF protection. It changes selection, not trust:

- direct literal private/special IPs remain blocked;
- only validated public `sockaddr`s are used when public answers exist;
- unused blocked answers are never connected;
- DNS is still revalidated on every redirect;
- actual TCP uses the selected numeric `sockaddr`;
- Fake-IP remains a narrow DNS-only exception;
- Fake-IP mixed with blocked answers remains fail-closed.

## QA

- 184 tests passed
- 5 subtests passed
- branch coverage: 63.08% (60% hard gate)
- release metadata checker: PASS
- compileall: PASS
- wheel build: PASS
- fresh virtualenv install: PASS
- package version: 0.9.18
- ACP agent version: 0.9.18
