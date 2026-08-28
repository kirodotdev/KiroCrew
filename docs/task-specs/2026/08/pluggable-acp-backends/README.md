# Pluggable experimental ACP backends

This work adds a registry of ACP backends and ships Claude Code and OpenAI Codex
as experimental entries. `AGENTS.md` was amended on this branch: ACP adapters
selected at `agent.acp_backend` are a shipped goal, not a divergence. The
conditions that remain are the ones the amendment already lists —
`agent.provider` stays `acp`, there is no API-key path, and an adapter whose
tool calls Kiro Crew cannot govern is refused unless the operator names the
opt-out.

Read this before concluding that `src/kiro_crew/acp/backends.py` should not exist.

## The rule as amended

`AGENTS.md`, "Never re-introduce":

> **Other providers.** `agent.provider` stays fixed to `acp` (`enum=["acp"]`) and
> kiro-cli remains the first-class harness. What is NO LONGER forbidden is **ACP
> adapter** support: Kiro Crew is an ACP *client*, and driving a registry adapter
> (`claude-acp`, `codex-acp`, …) selected at `agent.acp_backend` is a shipped
> goal rather than a divergence. Adapters are OPERATOR-INSTALLED, never bundled,
> and discovered through the upstream ACP Registry rather than a hand-maintained
> table. Still gone, still do not re-add: a second `agent.provider` value, an
> API-key path, a provider selector. Adapter identity and capability rules remain
> governed by Harness parity, and the trust rule is that an adapter whose tool
> calls Kiro Crew cannot govern is REFUSED by default with one named opt-out.

What this work does NOT do, so the rest of the rule stands unchanged:

- `agent.provider` remains fixed to `acp` with a single-valued enum. There is no
  second `LLMProvider`.
- No API-key or `base_url` adapter, no OpenAI-compatible passthrough, no Bedrock.
  Every backend here is an ACP agent authenticated by its own vendor CLI.
- kiro-cli remains the only default and the only non-experimental backend.
- The dashboard provider selector stays absent. What is added is a *backend*
  selector, which is a different control.
- An adapter whose tool decisions do not reach Kiro Crew's PreToolUse gate is
  refused (refuse-unless-routed), with one named opt-out.

## Why

Two reasons, in order of weight.

**Subscription reuse is a different ask from the one upstream keeps answering.**
Every provider request on the tracker gets normalised to an API-key `base_url`
adapter and then stalls on an unanswered policy question. The use case here is
narrower: an operator who already pays for a ChatGPT or Claude subscription wants
their own entitlement to serve their own turns, with no key anywhere and no
Kiro-Crew-held credential. `codex login` and the Claude CLI own sign-in
completely; Kiro Crew reads no token. That framing has never been ruled on — see
the upstream state below, where the one issue that raised it was closed as a
duplicate of the API-key discussion.

**The seam already exists and is already maintained.** `ACP_BACKEND_CLAUDE`, the
`_is_claude` branches, `_resolve_claude_acp_bin`, `AcpProvider.start()`'s
legacy-client arm and `ProviderRegistry.register_acp_backends` are all live code.
Upstream has since added a THIRD backend id, `ACP_BACKEND_KAS`, plus
`ACP_BACKENDS_KNOWN` and `ACP_BACKENDS_SELECTABLE` — a membership gate and a
selectability gate. So the shape this work extends is upstream's own: it opens a
gate upstream built, rather than inventing a mechanism.

## Conditions that still hold

The amendment keeps everything the rule already protected and constrains only
*how* a backend is added:

- `agent.provider` stays `acp`; kiro-cli stays the only default.
- A backend is added only through the registry, never by a parallel mechanism.
- An `experimental: true` marker is a condition of registration, not a courtesy.
- A backend must establish that its tool decisions reach Kiro Crew's PreToolUse
  gate, or refuse to start.
- Still forbidden: a second `LLMProvider`, API-key or `base_url` adapters, and a
  dashboard *provider* selector.

The registry is additive, and the default path is byte-identical.

## Upstream state of the policy question

Recorded so a future reader can tell a stalled question from a settled one.

| Ref | Ask | Outcome |
|---|---|---|
| [#1693](https://github.com/kirodotdev/KiroCrew/issues/1693) | `agent.provider` ∈ {acp, ollama, bedrock, openai_compatible} with `base_url`/`api_key` | **Open, no maintainer ruling.** The canonical tracking issue; later asks are folded into it. |
| [#1872](https://github.com/kirodotdev/KiroCrew/pull/1872) | A full LiteLLM multi-provider implementation | **Closed unmerged.** Shelved on the author's fork. API-key shaped, so the wrong auth model for subscription reuse regardless. |
| [#2107](https://github.com/kirodotdev/KiroCrew/pull/2107) | A docs-only RFC asking maintainers to *rule* — accept, accept narrowly, or reaffirm and close | **Open and unanswered.** Clean bot reviews; a collaborator requested changes on 2026-08-08 with "shared with our PM and will get back next week". The policy question itself is stuck. |
| [#2463](https://github.com/kirodotdev/KiroCrew/issues/2463) | BYOM naming existing **ChatGPT / GLM / Kimi subscriptions** | **Closed NOT_PLANNED within hours** as a duplicate of #1693. The subscription-OAuth framing — the exact case this work serves — was flattened into the API-key adapter scope and lost. |
| [#2573](https://github.com/kirodotdev/KiroCrew/issues/2573) | OpenAI-compatible / Gemini / Groq / Mistral endpoints | Open, `needs-human`. Triage: decide whether a passthrough is in scope at all. |
| [#2033](https://github.com/kirodotdev/KiroCrew/issues/2033) | Pi coding agent as a selectable harness | Open, `needs-human`, "strategic decision". The only other-*harness* ask rather than other-*model*. |

**This work is not an answer to #1693**, or to any of the rows above. The
amendment permits ACP adapters at `agent.acp_backend`; it does not settle the
API-key / multi-provider question, and presenting this branch as that resolution
would misrepresent an unanswered maintainer decision.

## Prior art

The `so0k/KiroCrew` branch `feat/codex-acp-oauth` implements the Codex half with a
hardcoded two-value enum. It is the reference for the wire-level details and for
several corrections adopted here — most usefully its live probe finding that the
`codex` CLI does **not** serve ACP (it treats `acp` as a prompt), which invalidates
an obvious-looking resolver fallback.

Deviations from it, each because the behaviour was measured rather than described:

- No `["codex", "acp"]` resolver rung, per the probe above.
- The adapter's `approval_policy` file is not a session gate; Kiro Crew applies
  and verifies the session's advertised `mode=read-only` option instead.
- A non-routed verdict **refuses** the session instead of warning; the fork warns
  and proceeds, which leaves Kiro Crew's security controls silently unenforced.
- `reserved_managed_names()` fails closed. The fork returns an empty set on error,
  which lets a configured `kirocrew core` sanitise onto the trusted
  `kirocrew-core`.
- KAS capability levels mirror kiro wherever they are not independently measured,
  so wiring gates to the registry cannot change KAS behaviour.

## Where the pieces live

| Concern | Module |
|---|---|
| Descriptors, capabilities, dialects | `src/kiro_crew/acp/backends.py` |
| Codex paths, resolution, policy probe, MCP shaping | `src/kiro_crew/acp/codex.py` |
| Claude permission-mode probe and seeding | `src/kiro_crew/acp/claude.py` |
| Tool-gate verdicts and enforcement | `src/kiro_crew/acp/tool_gate.py` |
| Agent-profile fail-closed guard | `src/kiro_crew/acp/spec_agent_guard.py` |
| Doctor rows | `src/kiro_crew/acp/doctor.py` |

The backend vocabulary itself — ids, `ACP_BACKENDS_KNOWN`, `ACP_BACKENDS_SELECTABLE`,
the provider labels — stays in `src/kiro_crew/acp/types.py`, which is upstream's
own home for it.
