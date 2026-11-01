> **Historical record:** this document describes the 0.9.10 / supplied Insider.1
> implementation baseline. For the current 1.0.0 / KiroCrew 0.9 evidence and
> ownership boundaries, see `AUDIT_CURRENT.md` and `ARCHITECTURE_BOUNDARY.md`.

# Kiro BYO 0.9.10 — official-aligned implementation record

Target inspected and dry-run against the supplied installed source of
**KiroCrew 0.9.0-insider.1**. Kiro BYO source version: **0.9.10**.

## What “parity” means now

0.9.10 intentionally stops using one overloaded word for three different things:

1. **ACP compatibility** — standard session/control methods and negotiated Kiro
   extensions behave according to the running `initialize` capabilities.
2. **Crew integration capability** — KiroCrew owns its ten-row backend card and
   computes it from the registered harness/capability sets. Kiro BYO does not
   duplicate that score.
3. **Kiro harness feature parity** — not claimed. Kiro's unified harness contains
   more tools and product behavior than Kiro BYO's shipped core surface.

This distinction follows current Kiro architecture: the standalone harness owns
the agent loop, tools, context, permissions and model communication, while clients
communicate with it over ACP.

## Crew 0.9 architecture

KiroCrew 0.9 multiplexes compatible ACP hosts behind `HarnessAdapter` and shared
`AcpRuntime`. Kiro BYO registers a dedicated `KiroByoHarness` module and does not
patch the legacy `acp/client.py` per-session spawn path.

Crew remains authoritative for normal session queue/steer orchestration, Side
Chat policy, saved-agent governance, hooks, permission/trust and its backend card.
Kiro BYO owns its ACP server, upstream provider transport, local filesystem
confinement and execution of its native tools.

The semantic integration modifies only eight existing Crew seams and creates one
dedicated harness module. Every changed source is inspected and compiled before
transactional installation; unknown upstream shapes fail closed.

## Crew-authoritative capability result

On the supplied 0.9.0-insider.1 source:

| Capability | Native Kiro | Kiro BYO 0.9.10 |
|---|:---:|:---:|
| `crew_tools` | ✓ | ✓ |
| `member_thread_tools` | ✗ | ✗ |
| `member_saved_agent` | ✓ | ✗ |
| `side_chat_tools` | ✓ | ✓ |
| `subagent_continuation` | ✓ | ✓ |
| `mid_turn_steer` | ✓ | ✓ |
| `manual_compact` | ✓ | ✓ |
| `reasoning_effort` | ✓ | ✓ |
| `model_switch` | ✓ | ✓ |
| `markdown_agents` | ✗ | ✗ |

Result: **native Kiro 8/10; Kiro BYO 7/10**.

The missing `member_saved_agent` row is deliberate. Kiro BYO consumes the saved
agent's projected id/prompt/tool visibility, but Crew defines this row as full
saved-spec loading plus runtime evidence. Partial projection is not promoted into
a full-support claim.

## ACP control plane

- ACP protocol version remains `1`; Kiro CLI V3 also negotiates v1, so the
  generation is never inferred from protocol version.
- `initialize` is the runtime capability authority.
- model selection uses `session/set_config_option` with `configId: model`.
- reasoning effort uses `configId: effortLevel`.
- legacy `session/set_model` exists only as an unadvertised compatibility path.
- optional Kiro methods are advertised in
  `agentCapabilities._meta.kiro.extensionMethods`.
- `cwd` and `additionalDirectories` are validated as absolute existing roots.
  Additional directories are client-owned and must be re-sent on load/resume.
- unsupported inbound methods return explicit JSON-RPC errors rather than hanging.

## Tool and permission boundary

Shipped native core now includes `read`, `glob`, `grep`, `read_files`,
`list_directory`, `write`, `edit`, `delete_file`, one-shot `shell`, session-owned
`execute_shell_process` / `control_shell_process`, `code_symbols`, `web_fetch`, and
`web_search`. `code_query` is exposed only when the optional Tree-sitter adapter
is genuinely available. This remains **syntax-level** code intelligence, not LSP
semantic parity; knowledge, todo-list and subagent-native semantics remain separate
work and Crew-owned orchestration is not duplicated.

Native requests pass through four distinct stages:

```text
schema validation
  -> non-mutating sandbox preflight
  -> Kiro BYO hard execution policy (read-only / roots / sensitive paths / web limits)
  -> Crew session/request_permission
  -> execution
```

Preflight rejects impossible out-of-root/sensitive-path requests before bothering
the user. Kiro BYO has no local per-tool approval/trust level; every surviving
call reaches Crew's permission request. Filesystem canonicalization includes
symlink/junction escape checks.

PowerShell has cwd/timeout/cancel/process-tree controls but still runs with the
current Windows user's authority; OS-level shell confinement is not claimed.

## MCP

0.9.13 supports stdio and Streamable HTTP across both current MCP wire eras.
Connections probe 2026-07-28 `server/discover` first and fall back to legacy
initialize only on protocol-shaped rejection. Modern HTTP is stateless, strips
user-supplied transport-owned headers case-insensitively, emits the standard
protocol/method/name routing headers and mirrors valid `x-mcp-header` arguments as
`Mcp-Param-*`. Stdio probes and, when necessary, falls back to the legacy
handshake on the **same child process/stdin/stdout connection**; it does not
launch a disposable probe process. Deprecated standalone SSE fails closed. Modern
MRTR `input_required` also fails explicitly until Crew exposes a corresponding
interactive projection.

Permission requests include standard options plus Kiro consent context and
versioned MCP tool identity metadata. Tool annotations remain untrusted hints.

## Provider layer

`providers/base.py` defines provider-neutral turn/tool/usage contracts and
`providers/registry.py` selects an adapter. This document section originally
described the 0.9.13 single-transport state; current 1.0.0 now ships multiple
verified official adapters (Responses-family transports, Anthropic/MiniMax Messages,
Gemini Interactions, Cohere V2, Mistral/Kimi/Xiaomi MiMo/GLM/Qwen provider-aware Chat paths) plus
the OpenAI-compatible fallback. `README.md` is authoritative for the current matrix.

## Runtime decomposition

The previous monolithic server was split so responsibilities are auditable:

- `acp_server.py` — ACP/session/control plane;
- `turn_runner.py` — inference/tool loop;
- `native_specs.py` — declarative native schemas and local policy;
- `native_tools.py` — native execution;
- `sandbox.py` — filesystem root/sensitive-path boundary;
- `agent_projection.py` — Crew saved-agent projection;
- `providers/*` — provider interface and transport adapters;
- `mcp_client.py` — session-scoped MCP catalogue, deferred search and bindings;
- `mcp_transports.py` — stdio/HTTP MCP wire negotiation and transport security.

Crew 0.9 runtime integration is a dedicated HarnessAdapter. The old static/legacy
overlay installers are not part of 0.9.10; integration is semantic-generated plus
a create-only Kiro BYO harness module.

## Verification performed

```text
PYTHONPATH=src pytest -q
131 passed

PYTHONPATH=src python scripts/verify_crew09_integration.py --crew-package <supplied kiro_crew>
Crew-derived score: native Kiro 8/10; Kiro BYO 7/10
Shared AcpRuntime: True; legacy AcpClient spawn: False
PASS
```

The suite includes tests for client-owned `additionalDirectories` on reload,
preflight-before-permission, model/effort config options, real usage/context
reporting, MCP permission identity, dual-era stdio/HTTP MCP negotiation, strict
2026-07-28 DiscoverResult validation, one-shot `-32020 HeaderMismatch` schema
refresh/retry, durable session recovery, V3 steering clear/inject lifecycle and
adapter fail-closed behavior. A malformed modern discover result is never
reclassified as a legacy server.
