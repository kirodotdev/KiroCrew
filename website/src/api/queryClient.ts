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

/** Retry up to 4 times on 429 throttles; keep the previous single retry otherwise. */
export const retryPolicy = (failureCount: number, error: unknown): boolean =>
  isThrottleError(error) ? failureCount < 4 : failureCount < 1

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
