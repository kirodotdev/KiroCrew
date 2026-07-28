# RFC: Channel Plugin Architecture — shared runtime, channels as app extension points

- Status: DRAFT
- Author: zezhexu
- Created: 2026-07-28
- Related: rfc-federated-app-platform.md (frontend loading + registry this RFC rides), PR #572 + PR #627 (the parallel channel landings that produced the 9-file conflict set cited below), `kiro_crew/apps/module_loader.py` (SEC-012 trust model this RFC adopts)

## 1. Problem statement

KiroCrew has 7 chat channels (slack, discord, telegram, webex, wecom, teams, weixin). Each new channel is built by copying the previous one. Measured on main `6298146` (2026-07-28):

- **The turn-dispatch skeleton is duplicated 7 times: 3,796 lines total.** After normalizing channel names, `weixin/transport_dispatch.py` and `wecom/transport_dispatch.py` differ on 248 of ~760 combined lines — roughly two-thirds of every non-Slack dispatcher is the same sequence: governance gate → command intercept → busy-steer → session acquire → publish turn identity → build context → `TurnDriver.run` → guarded post-turn (record, persist, threshold notice, SEL audit) → `finally:` close/release. Telegram (1,045 lines) and Discord (803) are larger only because real per-channel features are interleaved with that same skeleton.
- **Adding a channel means hand-editing 9 core files:** `config/loader.py`, `sandbox.py`, `slack/gateway.py`, `dashboard/handlers_system.py`, `dashboard/state.py`, `dashboard/server.py`, the governance contract test, `website/src/api/client.ts`, and `ChannelsPanel.tsx` (+ its test). This is not theoretical coupling: when Teams (#572) and WeChat (#627) landed in parallel, these exact files were the merge-conflict set, twice.
- **The boot orchestrator lives inside the Slack package.** `slack/gateway.py` (5,512 lines) owns `_start_channel_transports()` and the governed members tuple. Every channel PR edits Slack's package to exist.
- Bug classes get re-fixed per channel. The weixin review alone re-discovered, in channel-local code, problems the next channel will hit again: credential file permissions, two-file credential commit atomicity, event-loop stalls from sync I/O, fail-open policy parsing, hot poll loops.

An 8th channel (Feishu) is planned. On the current architecture it is copy #8 of the skeleton and a tenth walk through the seams.

## 2. Design principles

1. **Extract what is measured to be identical; keep per-channel what actually varies.** The shared/pluggable line is drawn from the weixin↔wecom↔teams diffs, not from speculation. Varies: wire protocol, auth shape, event normalization, authorization semantics, rendering strategy. Identical: turn pipeline, lifecycle, registration, credential handling.
2. **One plugin system, not two.** KiroCrew already has an app platform with dynamic backend loading (`module_loader.py`, namespaced `importlib` loading, SEC-012 trust warning), manifest-declared extension points (`agents`, `skills`, `hooks`, `routes`, `crons`), signing + fleet admission (`signer`/`signature`/`trust_keys`), and a federated frontend plan (rfc-federated-app-platform.md). Channels become a `channels` extension point on that platform.
3. **Contract-first even while everything is builtin.** The interfaces are shaped for dynamic loading from day one, so opening the door later is a discovery change, not a rework.
4. **Slack is out of scope for the runtime migration.** It predates the transport contract (~11k lines across its own handler/events/interactions path) and works. The only Slack change here is evicting the boot orchestrator.
5. **Behavior-preserving migration.** Every step is pinned by the existing channel test suites (weixin alone carries 87; the governance contract test pins membership).

## 3. Architecture

Four layers. L0 exists and is untouched; L1/L2 are new; L3 is what channels become.

```
L3  channel plugins      discord · telegram · webex · wecom · teams · weixin · feishu(pilot)
                         [slack: legacy path, migrates last]        [installable: later tier]
                         each = client + transport + renderer + hooks + ChannelDescriptor
--------------------------- contract vN (frozen module) ---------------------------
L2  plugin host          discovery (builtin list → manifest `channels:` entry point)
                         config: schema-validated `channels.<type>` section
                         creds: injected store; denylist derived; atomic commit owned here
                         generic routes: GET/PUT /api/channels/{name}/config
                         settings UI: schema-driven form (custom panels stay in-tree)
L1  channel runtime      messaging/dispatch.py — ONE turn pipeline (hooks by composition)
                         messaging/registry.py — lifecycle: governance → start → maintain
                         → generic channel_state map (evicted from slack/gateway.py)
L0  turn engine          TurnDriver (redaction · approval ladder · SEL) · sessions ·
                         ctx_builder · identity/governance · conversation state
```

### 3.1 The contract (frozen, versioned)

One importable module exports the full plugin surface; `ChannelDescriptor.contract_version` states what the plugin targets.

- `MessagingTransport` ABC (exists): `connect/maintain/disconnect`, `receive → InboundMessage`, `authorize()` — deny-by-default remains a contract requirement enforced by contract tests.
- `Renderer` ABC (exists): turn lifecycle callbacks + `close()`.
- `ChannelHooks` protocol (new, narrow): pre-turn intercept, context decoration, tool-gate override, post-turn notice. Composition, not inheritance — a missing capability widens the protocol once rather than forking the pipeline.
- `ChannelDescriptor` (new): `channel_type`, `contract_version`, config schema, credential field names, capabilities, transport/renderer factories.

### 3.2 The pipeline (L1)

`messaging/dispatch.py` is the skeleton lifted verbatim from the 4-clone family (weixin/wecom/webex/teams). The governance gate is called by the pipeline — a plugin cannot skip `channel_inbound_permitted` because it never owns that code. Expected effect: 3,796 dispatcher lines → one ~450-line pipeline + per-channel hook sets (~120 lines each) ≈ 1,300.

### 3.3 Config and credentials (L2)

- Channel config moves from hand-written dataclasses in `loader.py` to descriptor-declared schemas validated by the host into a `channels.<type>` section. One config path for builtin and installed channels alike; the mypy protection that typed dataclasses provided moves into schema validation plus contract tests (the same trade the app manifest already makes). This retires the "AgentConfig constructor silently drops the key" bug class.
- Plugins never import `loader.py` or `env_path()`. The host injects a credential-store interface; the descriptor's credential field names automatically feed the sandbox denylist (`_AGENT_DENIED_ENV_KEYS`) and drive the store's atomic two-file commit (credential + config, with rollback) — currently implemented channel-locally in the weixin QR handler, moved host-side so it is solved once.

### 3.4 Registration (L2)

The nine hand-edited seams reduce to **one required edit** (register the descriptor; later: install the app) plus one optional custom settings panel:

| Seam today | Target |
|---|---|
| `slack/gateway.py` members tuple + start call | registry iterates descriptors |
| `dashboard/handlers_system.py` members | derived from registry |
| `dashboard/state.py` per-channel fields | generic `channel_state[type] = {connected, error}` map |
| `sandbox.py` denylist entries | derived from descriptor cred fields |
| `dashboard/server.py` per-channel config routes | generic `/api/channels/{name}/config` |
| governance contract test | stays, as the tripwire pinning membership |
| `config/loader.py` dataclass + ctor | descriptor schema |
| `api/client.ts` per-channel methods | generic channel-config client |
| `ChannelsPanel.tsx` registry row | served from backend registry; schema-form default |

### 3.5 Settings UI (L2)

A schema-driven form renderer (on the Radix `SettingsSelect` stack) is the default panel — sufficient for token-paste channels, which is most of them. Custom flows (weixin QR login) remain in-tree custom panels. Third-party custom panels arrive only via the federated app platform's ESM loading, and are out of scope here.

## 4. Migration plan

Each PR lands green on the existing suites; no behavior change until ⑤.

- **PR ① — extract the pipeline, adopt in weixin only.** Smallest dispatcher (377 lines), freshest tests (87). Proves the skeleton extraction against the channel it was measured from.
- **PR ② — adopt in wecom, webex, teams.** The other three ~380-line clones. Each dispatcher shrinks to hooks.
- **PR ③ — registry + host seams.** Boot evicted from `slack/gateway.py`; generic state map; descriptor-driven governance membership; schema config + injected cred store; generic routes. The 9→1 seam collapse lands here.
- **PR ④ — telegram and discord adopt.** The feature-heavy pair stress-tests the hook protocol against media/reactions weight. Divergences found here widen the protocol deliberately or stay channel-side in transport normalization.
- **PR ⑤ — Feishu ships as the first contract-native channel.** WebSocket long-connection ingress (wecom-shaped: outbound-only, no public URL), `app_id`+`app_secret` → tenant token refresh, Feishu/Lark dual-host config field. Estimated ~900 new lines (adapter + panel) versus ~2,400 on the copy model. Feishu doubles as the proof the contract holds for a channel it was not extracted from.
- **Later tier (explicitly not now):** manifest `channels:` entry point wired to `module_loader`, admission/signing treatment for channel plugins, installable distribution via the Apps registry.

## 5. Security model

- **Trust:** in-process plugins execute with full gateway privileges — identical to apps today, stated by SEC-012 in `module_loader.py`. Install-time trust via the existing admission/signing rails (`signer`, `signature`, fleet `trust_keys`). This RFC does not claim sandbox isolation for channel plugins; a process-isolation tier is future work.
- **What the host enforces regardless of plugin behavior:** the inbound governance gate (pipeline-side), deny-by-default `authorize()` (contract test), credential access only through the injected store, sandbox denylist derivation, SEL audit of turns.
- **What moves host-side because channels kept getting it wrong:** credential file permissions (`restrict_to_owner`), atomic credential+config commit with rollback, off-event-loop file I/O, backoff on protocol errors (a pipeline-owned poll-loop helper).

## 6. Non-goals

- Migrating Slack's event path onto the runtime (only the boot orchestrator moves).
- Process isolation for channel plugins.
- A generic auth-flow plugin primitive (QR-style flows stay in-tree).
- Media pipelines (tracked separately; the weixin AES-CDN envelope remains unshipped).

## 7. Open questions

1. **Hook protocol width.** Frozen only after PR ④ — telegram/discord adoption is the empirical test of whether four hooks suffice.
2. **Admission scope for channel plugins.** Same fleet admission as apps from day one, or builtin-only until the federated platform ships? Leaning: builtin-only until ESM panels exist, since a channel without a panel is half-usable.
3. **Frontend registry serving.** Whether `ChannelsPanel` consumes the backend registry (API-driven tabs) in PR ③ or stays a hand-listed registry until the federated platform lands.

## 8. Alternatives considered

- **Keep copying (status quo).** Rejected on measured cost: 3,796 duplicated lines growing ~380/channel, 9-seam conflicts on every parallel landing, and per-channel re-fixing of solved bug classes.
- **Channels as separate processes (MCP-style).** Real isolation, but adds IPC latency to typing-indicator lifecycles, complicates session/renderer state, and duplicates the app platform's trust story. Deferred as the isolation tier, not the foundation.
- **Keep typed dataclass config.** Defensible in a monolith; incompatible with out-of-tree plugins (they cannot edit `loader.py`). Schema validation + contract tests replace the type safety, matching the manifest's existing trade.
- **A channel-only plugin mechanism separate from apps.** Rejected: two discovery/trust/distribution systems to maintain, and the apps platform already solved loading, admission, and signing.
