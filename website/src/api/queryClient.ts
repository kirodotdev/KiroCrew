import { QueryClient } from '@tanstack/react-query'

/**
 * True when the error is an HTTP 429 (edge/proxy rate limit). When the
 * dashboard is served through a fronting proxy such as Builder Tunnels
 * (API Gateway), request bursts — e.g. opening the Settings→Usage page,
 * which fires several queries on top of the regular polling — can trip the
 * edge throttle and return 429 {"message":"Rate exceeded"} before the
 * request ever reaches the gateway. These are transient by definition, so
 * they get a longer, jittered retry ladder instead of surfacing an error
 * card after a single retry.
 *
 * Duck-typed on `.status` (set by api/client.ts ApiError) rather than an
 * `instanceof ApiError` check to avoid a queryClient ⇄ client import cycle
 * (client.ts imports this module for warm-path refresh recovery).
 */
export const isThrottleError = (error: unknown): boolean =>
  typeof error === 'object' && error !== null
  && (error as { status?: unknown }).status === 429

/** True when the failure is a deadline WE set (lib/withDeadline's reason). */
export const isDeadlineError = (error: unknown): boolean =>
  typeof error === 'object' && error !== null
  && (error as { name?: unknown }).name === 'TimeoutError'

/**
 * Retry up to 4 times on 429 throttles; never retry a deadline we set ourselves;
 * keep the previous single retry otherwise.
 *
 * The deadline clause binds HERE rather than per query, for the same reason the
 * deadline itself binds inside `api.skills`: react-query dedupes on the key, so a
 * per-initiator rule is only as strong as the weakest initiator of a shared key.
 * Retrying a deadline also doubles the wait it exists to bound — a 15s bound
 * settles at ~31s once the single retry and its backoff are counted.
 */
export const retryPolicy = (failureCount: number, error: unknown): boolean =>
  isDeadlineError(error) ? false
    : isThrottleError(error) ? failureCount < 4 : failureCount < 1

/**
 * Jittered exponential backoff for throttles (1s → 2s → 4s → 8s, ±500ms so
 * parallel queries don't re-burst in lockstep and re-trip the edge limit);
 * react-query's default curve for everything else.
 */
export const retryDelayPolicy = (attempt: number, error: unknown): number =>
  isThrottleError(error)
    ? Math.min(1_000 * 2 ** attempt, 15_000) + Math.random() * 500
    : Math.min(1_000 * 2 ** attempt, 30_000)

/**
 * Single shared QueryClient instance. Exported so non-React modules (notably
 * api/client.ts's warm-path refresh recovery) can invalidate cached queries
 * such as ['auth-me'] without holding a React context handle. main.tsx passes
 * this same instance to QueryClientProvider, so useQueryClient() hits it too.
 */
export const queryClient = new QueryClient({
  defaultOptions: {
    queries: {
      retry: retryPolicy,
      retryDelay: retryDelayPolicy,
      // Infinity: queries never go stale on their own. Freshness is driven
      // exclusively by WebSocket push (invalidateQueries on server events).
      // This eliminates the focus-refetch storm (refetchOnWindowFocus only
      // fires on *stale* queries) without changing the safe default — the
      // option stays true, so any query that sets a finite staleTime will
      // still refetch on focus as React Query intends.
      staleTime: Infinity,
    },
  },
})

type DefaultMemoryMode = 'persistent' | 'incognito' | 'temporary'
type DashboardConfigMemoryMode = { default_memory_mode?: unknown }

const DEFAULT_MEMORY_MODES: ReadonlySet<DefaultMemoryMode> = new Set([
  'persistent',
  'incognito',
  'temporary',
])
let pendingDefaultMemoryMode: { token: symbol; value: DefaultMemoryMode } | undefined
let defaultMemoryModeGeneration = 0
let defaultMemoryModeWriteTail: Promise<void> = Promise.resolve()

function beginDefaultMemoryModeUpdate(value: DefaultMemoryMode): symbol {
  const token = Symbol('default-memory-mode-update')
  defaultMemoryModeGeneration += 1
  pendingDefaultMemoryMode = { token, value }
  return token
}

function finishDefaultMemoryModeUpdate(token: symbol): void {
  if (pendingDefaultMemoryMode?.token === token) pendingDefaultMemoryMode = undefined
}

/** Queue mode PUTs in selection order, even across Settings unmount/remount. */
export function serializeDefaultMemoryModeUpdate<T>(
  value: DefaultMemoryMode,
  write: () => Promise<T>,
): Promise<T> {
  const token = beginDefaultMemoryModeUpdate(value)
  const run = defaultMemoryModeWriteTail.then(write)
  defaultMemoryModeWriteTail = run.then(() => undefined, () => undefined)
  return run.finally(() => finishDefaultMemoryModeUpdate(token))
}

function currentPendingDefaultMemoryMode(): DefaultMemoryMode | undefined {
  return pendingDefaultMemoryMode?.value
}

function normalizeDefaultMemoryMode(value: unknown): DefaultMemoryMode {
  // Older backends omit the field and retain the historical Persistent default.
  if (value === undefined) return 'persistent'
  return DEFAULT_MEMORY_MODES.has(value as DefaultMemoryMode)
    ? value as DefaultMemoryMode
    : 'temporary'
}

/**
 * Resolve the mode for a new dashboard chat. A same-tab choice whose save is
 * still in flight wins. Otherwise verify the server: React Query caches are
 * process-local, so another tab or device can change this privacy boundary
 * without invalidating the cache in the process creating the next chat.
 * A response overlapping a mode write is stale by construction and is retried
 * once; repeated churn fails closed to Temporary.
 */
export async function resolveDefaultMemoryMode(
  load: () => Promise<DashboardConfigMemoryMode>,
): Promise<DefaultMemoryMode> {
  for (let attempt = 0; attempt < 2; attempt += 1) {
    const generation = defaultMemoryModeGeneration
    const pending = currentPendingDefaultMemoryMode()
    if (pending) return pending
    try {
      const loaded = await load()
      const latest = currentPendingDefaultMemoryMode()
      if (latest) return latest
      if (generation !== defaultMemoryModeGeneration) continue
      return normalizeDefaultMemoryMode(loaded.default_memory_mode)
    } catch {
      const latest = currentPendingDefaultMemoryMode()
      if (latest) return latest
      if (generation !== defaultMemoryModeGeneration) continue
      return 'temporary'
    }
  }
  return 'temporary'
}
