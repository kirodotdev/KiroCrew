/**
 * MCP servers under /api/mcp and /api/mcp-gateway: the probe cache, server
 * list and scopes, discovery and custom specs, probing and quarantine release,
 * sync/apply/toggle/remove, the OAuth callback relay, and the shared gateway
 * pool with its stub allowlist and shareability measurement.
 */

import { relocateRequestUrl } from '../../lib/dashboardRuntime'
import type { McpApplyChange } from '../../types'
import type { ClientTransport } from './transport'

/** One machine-readable ground for a sharing verdict.
 *
 *  `code` is stable and is what the UI translates. `detail` is verbatim data
 *  from the server or the config (an env name, a capability path, a protocol
 *  version) and is deliberately NOT translated.
 */
export type McpShareReason = {
  code: string
  detail: string
}

/** The gateway's advisory reading of whether a server's backend can be shared.
 *
 *  `strength` is the evidence tier, weakest first: `unknown`, `no_objection`,
 *  `declared`, `disqualified`, `refuted`. Only `declared` sets `recommendShare`,
 *  because finding nothing disqualifying is an absence of evidence rather than
 *  evidence of absence.
 *
 *  The wire object also carries a separate stub recommendation, which is not
 *  declared here: a TS type is structural, so the field costs a reader something
 *  and buys nothing until a component actually renders it.
 */
export type McpShareRecommendation = {
  strength: string
  // Two axes, not one verdict. `recommendStub` is the safe half — Kiro Crew's
  // stub in the path, backend still 1:1 with the session — while
  // `recommendShare` is the one that introduces co-tenancy. A bulk action has to
  // consult whichever one the global sharing switch makes true of a click.
  recommendStub: boolean
  recommendShare: boolean
  reasons: McpShareReason[]
}

/**
 * Where an operator-requested measurement pass got to.
 *
 * ``running`` is the only field a caller may branch on to decide whether to keep
 * polling: ``done`` and ``total`` are a readout, and both are 0 both before a
 * pass starts and when a pass found nothing to measure. ``error`` names the
 * exception class of a pass that stopped early, because a pass that dies
 * silently is indistinguishable from one that finished with nothing to do.
 */
export type McpMeasureProgress = {
  running: boolean
  // Servers attempted, which is what the progress line advances on.
  done: number
  // How many of those produced a verdict. Lower than `done` whenever a pre-flight
  // could not run, so any claim about the outcome is built from this one.
  measured: number
  total: number
  error?: string
}

export type McpManagedServer = {
  name: string
  stub: boolean            // effective: can_stub AND in_allowlist
  can_stub: boolean       // stdio AND not denylisted — a property of the server, not a choice
  in_allowlist: boolean    // present in config mcp_gateway.stub_servers
  entry_poolable: boolean  // some agent entry sets poolable:true — RETIRED, informational only
  agents: string[]         // agent configs that declare this server
  transport: string        // "stdio" (stubbable) or "http" (no stdio pipe to interpose on)
  denylisted: boolean      // in UNPOOLABLE_SERVERS — can never be pooled
  // True when stubbing this server cannot produce a SHARED backend anyway: the
  // rewriter leaves an env-declaring entry unwrapped rather than spawn a pooled
  // backend without a declared key. Optional for the same reason as
  // `recommendation` — an older gateway does not send it, and its absence must
  // read as "no obstacle known", not as an obstacle.
  pooling_blocked_by_env?: boolean
  // Optional because the field is only as old as the shareability detector: a
  // dashboard served from this build can be pointed at an older gateway (Make
  // Live to an earlier worktree), and a row with no verdict must read as "not
  // measured" rather than crash the table.
  recommendation?: McpShareRecommendation
}

export function createMcpEndpoints({ get, post, put, j }: ClientTransport) {
  const probeCache = {
    mcpProbeCache: () => fetch(relocateRequestUrl('/api/mcp/probe')).then(j),
  }

  const servers = {
    // MCP
    mcpServers: () => fetch(relocateRequestUrl('/api/mcp')).then(j),
    mcpGlobalScopes: () => fetch(relocateRequestUrl('/api/mcp/scopes')).then(j),
    /** Multi-provider MCP server discovery (official registry, plus the
     *  edition capability provider when one is installed). A query
     *  shorter than 2 chars returns {results: [], providers: [...]} without
     *  hitting any provider — a cheap availability probe. */
    mcpDiscover: (query: string, opts?: { provider?: string; limit?: number }) =>
      get(`/api/mcp/discover?q=${encodeURIComponent(query)}${opts?.provider ? `&provider=${opts.provider}` : ''}${opts?.limit ? `&limit=${opts.limit}` : ''}`).then(j) as Promise<import('../../types').McpDiscoverResponse>,
    /** Full description + install-plan preview for one discovered server. */
    mcpDiscoverDetail: (provider: string, id: string) =>
      get(`/api/mcp/discover/detail?provider=${encodeURIComponent(provider)}&id=${encodeURIComponent(id)}`).then(j) as Promise<import('../../types').McpDiscoverDetail>,
    /** Install a discovered MCP server. Throws ApiError(409) on name collision. */
    mcpDiscoverInstall: (provider: string, id: string) =>
      post('/api/mcp/discover/install', { provider, id }).then(j) as Promise<import('../../types').McpDiscoverInstallResult>,

    mcpCustomAdd: (servers: Record<string, import('../../types').McpCustomSpec>, enable: boolean) =>
      post('/api/mcp/custom', { servers, enable }).then(j) as Promise<{ ok: boolean; added: string[]; enabled: boolean }>,

    mcpCustomGet: (name: string) =>
      get(`/api/mcp/custom/${encodeURIComponent(name)}`).then(j) as Promise<import('../../types').McpCustomSpecResponse>,

    mcpCustomUpdate: (name: string, spec: import('../../types').McpCustomSpec) =>
      put(`/api/mcp/custom/${encodeURIComponent(name)}`, { spec }).then(j) as Promise<{ ok: boolean; name: string }>,
    mcpActive: (agent?: string) => fetch(relocateRequestUrl('/api/mcp/active' + (agent ? `?agent=${encodeURIComponent(agent)}` : ''))).then(j),
    mcpProbe: () => post('/api/mcp/probe').then(j),
    mcpResetProbeFailures: (name: string) =>
      post('/api/mcp/quarantine/clear', { name }).then(j) as Promise<{ ok: boolean; name: string; released: boolean }>,
    mcpSync: () => post('/api/mcp/sync').then(j),
    mcpApply: (changes: McpApplyChange[]) =>
      post('/api/mcp/apply', { changes }).then(j),
    mcpToggle: (name: string, enabled: boolean) => post('/api/mcp/toggle', { name, enabled }).then(j),
    mcpToggleTool: (server: string, tool: string, enabled: boolean) => post('/api/mcp/toggle-tool', { server, tool, enabled }).then(j),
    mcpToggleAll: (enabled: boolean) => post('/api/mcp/toggle-all', { enabled }).then(j),
    mcpRemove: (name: string) => post('/api/mcp/remove', { name }).then(j),
    mcpOAuthRelay: (server: string, redirectUrl: string) =>
      post('/api/mcp/oauth/relay', { server, redirect_url: redirectUrl }).then(j) as Promise<{ ok: boolean }>,
  }

  const gateway = {
    // MCP Gateway (shared pool)
    mcpGatewayStatus: () => fetch(relocateRequestUrl('/api/mcp-gateway/status')).then(j) as Promise<{ enabled: boolean; stub: string[]; stub_count: number; running: boolean; ping_ok: boolean; supported: boolean; launch_refused?: Record<string, { reason: 'added_outside_dashboard' | 'changed_needs_reapproval'; commands?: string[][]; envs?: string[][]; approved_commands?: string[][]; approved_envs?: string[][]; complete?: boolean; expected_launch?: string }> }>,
    mcpGatewayEnable: (enabled: boolean) => post('/api/mcp-gateway/enable', { enabled }).then(j) as Promise<{ ok: boolean; enabled: boolean; running: boolean; ping_ok: boolean }>,
    mcpGatewayMetrics: () => fetch(relocateRequestUrl('/api/mcp-gateway/metrics')).then(j) as Promise<{ running: boolean; size?: number; max_backends?: number; backends: { server: string; agent: string; pid: number | null; stubs?: number; idle_s: number; rss_kb: number }[]; warm_pool_hits?: number; warm_pool_misses?: number; warm_pool_hit_rate_pct?: number }>,
    mcpGatewayServers: () => fetch(relocateRequestUrl('/api/mcp-gateway/servers')).then(j) as Promise<{ servers: McpManagedServer[] }>,
    // What the gateway would run for this server, so the operator approves a
    // command rather than a name. `expected_launch` is the identity the approval
    // is written against and is present only when every command could be shown.
    mcpGatewayLaunchPreview: (name: string) => fetch(relocateRequestUrl(`/api/mcp-gateway/servers/launch?name=${encodeURIComponent(name)}`)).then(j) as Promise<{ name: string; commands: string[][]; envs: string[][]; complete: boolean; expected_launch?: string }>,
    mcpGatewaySetStub: (name: string, stub: boolean, expectedLaunch?: string, resolveEligibility = false) => post('/api/mcp-gateway/servers/stub', { name, stub, ...(expectedLaunch ? { expected_launch: expectedLaunch } : {}), ...(resolveEligibility ? { resolve_eligibility: true } : {}) }).then(j) as Promise<{ ok: boolean; name: string; stub: boolean; stubbed?: string[]; skipped?: Array<{ name: string; reason: string }>; sharing_on?: boolean; enabled?: boolean; applied?: boolean; restart_required?: boolean; stub_servers?: string[] }>,
    mcpResolveRefresh: () => post('/api/mcp-gateway/resolve-refresh', {}).then(j) as Promise<{ ok: boolean; reason?: string; resolved: Record<string, 'ready' | 'unresolved' | 'error'>; ready?: string[] }>,
    // Starting a measurement pass returns immediately: it spawns two processes per
    // unmeasured server, so the answer arrives through the progress read, not here.
    mcpMeasureStart: () => post('/api/mcp/measure', {}).then(j) as Promise<McpMeasureProgress>,
    mcpMeasureProgress: () => fetch(relocateRequestUrl('/api/mcp/measure')).then(j) as Promise<McpMeasureProgress>,
    // Batch form of the above, for turning stubs OFF -- one config write for the
    // whole set, so "unstub all" can't land the allowlist half-flipped. Like the
    // single form it records rather than applies, and answers `restart_required`.
    //
    // `stub: false` only, and the endpoint refuses a batch stub=true: turning a stub
    // ON approves the exact command that server would run, and one body cannot carry
    // one launch identity per name. Enabling is therefore a request per server.
    // The response reports `stubbed` and `skipped` rather than echoing the request,
    // because the server decides which names it acts on.
    mcpGatewaySetStubMany: (names: string[], stub: false) => post('/api/mcp-gateway/servers/stub', { names, stub }).then(j) as Promise<{ ok: boolean; names: string[]; stub: false; stubbed?: string[]; skipped?: Array<{ name: string; reason: string }>; sharing_on?: boolean; applied?: boolean; restart_required?: boolean; stub_servers?: string[] }>,
  }

  return { probeCache, servers, gateway }
}
