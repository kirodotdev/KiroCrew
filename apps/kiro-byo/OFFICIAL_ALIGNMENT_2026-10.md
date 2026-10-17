# Official alignment notes — October 2026

This file records the public upstream contracts used by the historical Kiro BYO 0.9.10
alignment pass. It is retained as an audit snapshot, not as the current 1.0.0 provider
capability matrix. See `README.md` and `CHANGELOG.md` for the current provider
transport/runtime claims.

## Kiro

Current Kiro documentation describes one standalone unified agent harness behind
IDE, CLI, Web and Mobile. The harness owns conversation orchestration, tool
execution, context management, permission evaluation and model/provider
communication. Clients communicate with it over ACP.

Current ACP guidance says the running server's `initialize` response is
authoritative. Clients should enable optional behavior only when advertised and
should not infer support from product name, version or a fixed method catalog.

Kiro CLI V3 still negotiates ACP protocol version 1, so ACP protocol version does
not identify v2 versus V3. V3 moves model/effort selection to standard
`session/set_config_option` (`model`, `effortLevel`), uses standard session
discovery/load, standard permission options plus Kiro consent metadata, and
advertises optional Kiro extension methods.

## KiroCrew

Public KiroCrew documentation keeps `agent.provider = acp`; `agent.acp_backend`
selects the harness. The baseline/default backend remains Kiro CLI, while other
selectable harnesses implement the same ACP provider seam with capability-gated
differences. The default Kiro backend is launched as `kiro-cli acp --agent
<name>` in current public Crew documentation; this does not make Kiro BYO a Kiro
CLI plugin. Kiro BYO is a sibling ACP harness.

The user's installed KiroCrew 0.9.0-insider.1 additionally exposes shared
`HarnessAdapter` / `AcpRuntime` integration. Kiro BYO uses that shared-runtime
path and leaves Crew's product-level capability card authoritative.

## Consequences for Kiro BYO 0.9.10

- no local hard-coded Crew 8/10 table;
- honest 7/10 in the supplied Crew build until full saved-member-spec fidelity;
- runtime `initialize` is the ACP capability authority;
- V3-style model/effort config options are primary;
- `cwd` / `additionalDirectories` are client-owned session inputs;
- permission prompts use standard options with Kiro metadata; Kiro BYO keeps no
  second per-tool approval/trust policy;
- the harness owns its provider/tool loop, while Crew owns orchestration and UI;
- OpenAI-compatible is the only shipped upstream provider transport today.
- Kiro V3 tool visibility tags are mirrored for the native `read`, `write`,
  `shell` and `web` families; Crew remains the authorization authority.
- 0.9.10 adds native `web_fetch` / `web_search` plus richer file read/list/delete
  primitives, but does not claim Kiro's code-intelligence/knowledge/subagent
  implementations.

## MCP note

Kiro BYO 0.9.10 implements dual-era MCP negotiation for stdio and Streamable
HTTP. It probes the current 2026-07-28 `server/discover` protocol first and falls
back to the legacy initialize/initialized handshake only when the peer clearly
rejects the modern protocol shape (method-not-found / unsupported-protocol, or
transport no-response/timeout evidence appropriate to legacy stdio/HTTP). A
malformed or capability-invalid modern DiscoverResult fails closed instead of
being misclassified as a legacy server. Modern HTTP is stateless and uses
`MCP-Protocol-Version`, `Mcp-Method`, `Mcp-Name` and validated `Mcp-Param-*`
routing headers. Stdio performs the modern probe and any legacy fallback on
the **same child process and the same stdin/stdout connection**, which matches
the current official SDK's automatic protocol negotiation.

The 2026 multi-round `resultType: input_required` path is deliberately not
bridged yet: until Crew exposes an elicitation/sampling/roots projection Kiro BYO
fails it explicitly instead of fabricating a successful result.


Modern `server/discover` validation requires the 2026-07-28 wire fields used by
the current schema (`resultType`, non-empty `supportedVersions`, object
`capabilities`, non-negative integer `ttlMs`, and `cacheScope` of `public` or
`private`). A `-32020 HeaderMismatch` from a modern tool call triggers exactly one
`tools/list` refresh followed by one retry, matching the current SDK recovery
shape and preventing retry loops.
