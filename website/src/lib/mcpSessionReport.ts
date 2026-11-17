import type { McpSessionReport } from '../types'

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

/** The reported failure reason for ``name``, or '' when none was reported. */
export function mcpSessionFailureReason(
  name: string,
  report?: McpSessionReport | null,
): string {
  return report?.failures?.[name] ?? ''
}

/**
 * True when ``name`` failed for want of a credential, so restarting the session
 * once the credential exists loads it.
 */
export function mcpSessionNeedsRestart(name: string, report?: McpSessionReport | null): boolean {
  return Boolean(report?.restart_to_load?.includes(name))
}

/**
 * True when ``name`` started in this session but gave it no tools.
 *
 * A separate fact from the state above: such a server DID start, so its ring
 * stays `started`; this flags the warning shown beside it.
 */
export function mcpSessionGaveNoTools(name: string, report?: McpSessionReport | null): boolean {
  return Boolean(report?.no_tools?.includes(name))
}

/** Servers that gave this session no tools and are NOT in ``names``. */
export function mcpSessionExtraNoTools(names: string[], report?: McpSessionReport | null): string[] {
  const shown = new Set(names)
  return [...new Set(report?.no_tools ?? [])].filter(n => !shown.has(n))
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

/**
 * One server's state: `connected`, or one of the keys of the report's `states`.
 * The words are read off the report and never worked out here, so the chat
 * cannot call a server "connecting" that the gateway reported as failed.
 */
export type McpSessionLoadState =
  | 'connecting'
  | 'needs_sign_in'
  | 'failed'
  | 'disabled'
  | 'registry_dropped'
  | 'connected'

/** The not-ready states, in the order a name is matched against them. */
const NOT_READY_ORDER: readonly Exclude<McpSessionLoadState, 'connected'>[] = [
  'connecting',
  'needs_sign_in',
  'failed',
  'disabled',
  'registry_dropped',
]

export interface McpSessionLoadRow {
  name: string
  state: McpSessionLoadState
}

export interface McpSessionLoading {
  /** Servers still connecting. Empty means nothing is loading. */
  connecting: string[]
  /** Servers that connected. */
  ready: string[]
  /**
   * The servers the count is about, still-connecting ones first: every
   * `connecting` and every `ready` server, so its length is the count's total.
   */
  counted: McpSessionLoadRow[]
  /**
   * The servers the count leaves out, because none of them is loading: failed,
   * waiting for a sign-in, switched off, or left out by policy.
   */
  uncounted: McpSessionLoadRow[]
  /**
   * The report left servers out because one of its lists was full. A server
   * with no state may be one of those, so it is not known to be loading.
   */
  partial: boolean
}

/** Wire data: keep the strings of a list, each once, in order. */
function names(value: unknown): string[] {
  if (!Array.isArray(value)) return []
  return [...new Set(value.filter((n): n is string => typeof n === 'string' && n !== ''))]
}

/**
 * Which of this session's servers are still connecting, which are ready, and
 * what is true of the rest.
 *
 * Read from `states` only. A report without it (a gateway or an agent backend
 * that does not say which servers are still connecting) yields no `connecting`
 * entry, so nothing is ever drawn as loading on a guess: a server missing from
 * every bucket is *unreported*, which is not the same claim.
 *
 * A server is in exactly one row. A not-ready state wins over `ready`, so a
 * server that reconnects is not also counted as ready.
 */
export function mcpSessionLoading(report?: McpSessionReport | null): McpSessionLoading {
  const states = report?.states
  const seen = new Set<string>()
  const take = (state: McpSessionLoadState, list: unknown): McpSessionLoadRow[] => {
    const fresh = names(list).filter(n => !seen.has(n))
    for (const name of fresh) seen.add(name)
    return fresh.map(name => ({ name, state }))
  }
  const notReady = NOT_READY_ORDER.flatMap(state =>
    states && typeof states === 'object' ? take(state, states[state]) : [],
  )
  const connecting = notReady.filter(row => row.state === 'connecting')
  const ready = take('connected', report?.ready)
  const omitted = report?.states_omitted
  return {
    connecting: connecting.map(row => row.name),
    ready: ready.map(row => row.name),
    counted: [...connecting, ...ready],
    uncounted: notReady.filter(row => row.state !== 'connecting'),
    partial: typeof omitted === 'number' && omitted > 0,
  }
}
