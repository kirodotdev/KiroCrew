import { ACP_BACKEND_KAS, ACP_BACKEND_KIRO } from '../api/acpBackend'
import type { McpSessionReport } from '../types'

/** Kiro Crew's own servers, which a session gets whatever its spec names:
 *  `CONTROL_PLANE_SERVERS` in `mcp_cleanup.py`, pinned equal by `test_session_facts.py`. */
const CONTROL_PLANE_SERVERS = ['kirocrew-core', 'kirocrew-cron']

/** Harnesses that mount a spec's servers themselves, so the `session/new` array is
 *  not the whole delivery: `ACP_BACKENDS_SPEC_SERVERS_OFF_WIRE`, pinned the same way. */
const SPEC_SERVERS_OFF_WIRE = [ACP_BACKEND_KIRO, ACP_BACKEND_KAS]

/**
 * Per-server state derived from ONE session's MCP report.
 *
 * `no_report` is the load-bearing member: the backend's init drain is time
 * bounded and a registration frame can still arrive mid-turn, so a server the
 * session has not reported on is *unreported*, NOT *absent*. Rendering it as
 * absent is the same class of false authority this whole view exists to remove
 * — it would just move the wrong answer from one surface to another.
 */
export type McpSessionServerState = 'started' | 'failed' | 'awaiting_auth' | 'no_report'

/**
 * What ``name`` reported in this session.
 *
 * Returns `no_report` when there is no report at all, which is what a slot with
 * no live session looks like.
 */
export function mcpSessionServerState(
  name: string,
  report?: McpSessionReport | null,
): McpSessionServerState {
  if (!report) return 'no_report'
  if (report.ready?.includes(name)) return 'started'
  if (report.failed?.includes(name)) return 'failed'
  if (report.awaiting_auth?.includes(name)) return 'awaiting_auth'
  return 'no_report'
}

/** Whether a session received Kiro Crew's own MCP servers. */
export type GatewayToolsState = 'connected' | 'failed' | 'sent' | 'not_sent' | 'pending'

/**
 * Whether ``backend``'s session received Kiro Crew's own servers, read off its report.
 *
 * `connected` / `failed` when the harness reported every one started, or one
 * failed. Otherwise it depends on the harness: one that mounts its spec's servers
 * itself reports them, so silence is `pending`; on any other the `session/new`
 * array is the whole delivery, so they were `sent`, or `not_sent` when Crew
 * withheld them. `null` until a session has reported both its backend and a report.
 */
export function gatewayToolsState(
  report: McpSessionReport | null | undefined,
  backend: string | null | undefined,
): GatewayToolsState | null {
  if (!report || backend == null) return null
  const states = CONTROL_PLANE_SERVERS.map(name => mcpSessionServerState(name, report))
  if (states.includes('failed')) return 'failed'
  if (states.every(state => state === 'started')) return 'connected'
  if (SPEC_SERVERS_OFF_WIRE.includes(backend)) return 'pending'
  return CONTROL_PLANE_SERVERS.every(name => report.configured?.includes(name)) ? 'sent' : 'not_sent'
}

/** The reported failure reason for ``name``, or '' when none was reported. */
export function mcpSessionFailureReason(
  name: string,
  report?: McpSessionReport | null,
): string {
  return report?.failures?.[name] ?? ''
}

/** How many of ``names`` this session reported as started. */
export function mcpSessionStartedCount(
  names: string[],
  report?: McpSessionReport | null,
): number {
  if (!report) return 0
  return names.filter(n => report.ready?.includes(n)).length
}

/**
 * Servers this session **started** that are NOT in ``names``.
 *
 * The backend starts the agent spec's own servers as well as the ones Kiro Crew
 * injects on the wire, so the report is a superset of any single configured
 * list. Surfacing the difference is the point: it is session truth that the
 * configured view cannot show.
 *
 * Deliberately `ready` only. The copy beside this list says these STARTED, and a
 * server that failed or is waiting for authorization did not — folding those in
 * would make the sentence false. The cost is named rather than hidden: a server
 * absent from the configured list that failed to start is not surfaced here.
 */
export function mcpSessionExtraServers(
  names: string[],
  report?: McpSessionReport | null,
): string[] {
  if (!report) return []
  const shown = new Set(names)
  const seen = new Set<string>()
  const out: string[] = []
  for (const n of report.ready ?? []) {
    if (shown.has(n) || seen.has(n)) continue
    seen.add(n)
    out.push(n)
  }
  return out
}

/**
 * True when a report exists at all.
 *
 * ANY non-null report means a session published one, so the panel must switch to
 * session marks. Requiring a populated bucket would send a session that sent a
 * roster but has not been reported on yet back to the configured-flag green dots
 * — reasserting the false "everything is fine" this view exists to remove. With
 * no bucket entries every server correctly reads "no report from this session
 * yet", which is the honest state.
 */
export function mcpSessionHasReport(report?: McpSessionReport | null): boolean {
  return Boolean(report)
}
