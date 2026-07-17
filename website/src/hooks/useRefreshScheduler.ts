/**
 * useRefreshScheduler — proactively refresh the dashboard's access cookie
 * before it expires.
 *
 * Spec: KiroCrew docs/token-refresh/REQUIREMENTS.md (backend feature).
 *
 * Architecture:
 *   - `useQuery({ queryKey: ['auth-me'] })` is the single source of truth
 *     for `session_exp`. Cache-deduplicated across consumers; invalidated
 *     after every successful refresh so any future component using session
 *     info stays in sync.
 *   - `useMutation` wraps POST /api/auth/refresh — both the on-mount 401
 *     fallback (banner-flash fix) and the timer-driven proactive refresh
 *     go through it.
 *   - A useEffect watches `meQuery.data?.session_exp` and schedules a
 *     setTimeout at `session_exp - LEAD_MS`. Timer-based scheduling is
 *     a deliberate carve-out: there's no React Query primitive that
 *     models "fire once on a wall-clock timer relative to a server-
 *     reported expiry"; setTimeout is the right tool here.
 *
 * Flow:
 * 1. On mount, useQuery fires GET /api/auth/me.
 * 2. If 401, queryFn proactively POSTs /api/auth/refresh ONCE and retries
 *    /api/auth/me — silently mints new cookies before any auth UI flashes.
 * 3. On success, useEffect schedules setTimeout at `session_exp - 1h`.
 * 4. Timer fires → mutation.mutate() → POST /api/auth/refresh.
 * 5. On 200, mutation.onSuccess invalidates ['auth-me'], which triggers
 *    refetch and step 3 reschedules off the new session_exp.
 * 6. On 401 with `refresh_chain_revoked`, mutation.onError calls
 *    onChainRevoked (or navigates to "/").
 * 7. On transient failure (5xx, network), exponential backoff:
 *    1m → 4m → 16m → cap at 1h.
 * 8. On tab visibility hidden when the timer fires, defer until the tab
 *    becomes visible again.
 *
 * Older-server case: if /api/auth/me returns 404, log once and stop.
 * The user falls back to the existing URL-mint flow.
 */

import { useEffect, useRef } from 'react'
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import { refreshOnce } from '../api/refreshOnce'

// Refresh `LEAD_MS` before the access cookie's session_exp.
const LEAD_MS = 60 * 60 * 1000  // 1 hour

// Minimum time between scheduling and firing — guards against negative
// or zero timeouts when session_exp is already past.
const MIN_DELAY_MS = 5 * 1000  // 5 seconds

// Backoff schedule for transient failures.
const BACKOFF_SCHEDULE_MS = [
  60 * 1000,         // 1 minute
  4 * 60 * 1000,     // 4 minutes
  16 * 60 * 1000,    // 16 minutes
  60 * 60 * 1000,    // 1 hour (cap)
]

interface AuthMeResponse {
  user_id: string
  session_exp: number  // epoch seconds
  refresh_exp: number  // epoch seconds (0 if no refresh cookie present)
}

interface RefreshError extends Error {
  status?: number
  body?: string
}

export interface UseRefreshSchedulerOptions {
  /**
   * Called when the refresh chain has been revoked (RFC 6819 reuse
   * detection fired). Default: navigate to "/" so the existing 403
   * page surfaces and the user re-mints. Tests inject a vi.fn().
   */
  onChainRevoked?: () => void
}

/**
 * Mount this hook ONCE at the dashboard root (e.g. inside the top-level
 * App component).
 */
export function useRefreshScheduler(opts: UseRefreshSchedulerOptions = {}): void {
  const { onChainRevoked } = opts
  const queryClient = useQueryClient()

  // Refs survive re-renders without retriggering effects.
  const onChainRevokedRef = useRef(onChainRevoked)
  onChainRevokedRef.current = onChainRevoked
  const stoppedRef = useRef<boolean>(false)
  const backoffIdxRef = useRef<number>(0)
  const timerRef = useRef<number | null>(null)

  // ── Mutation: POST /api/auth/refresh ──────────────────────────────────
  // Used by both the 401-fallback inside the auth-me queryFn and the
  // timer-driven proactive refresh.
  const refreshMutation = useMutation({
    mutationFn: async (): Promise<unknown> => {
      // Delegate to the process-wide single-flight so the proactive timer and
      // the reactive warm-path recovery (api/client) never fire two concurrent
      // /api/auth/refresh POSTs for the same access-cookie lapse.
      const res = await refreshOnce()
      if (!res.ok) {
        const err: RefreshError = new Error(`refresh ${res.status}`)
        err.status = res.status
        err.body = (res.body as { error?: string } | null)?.error
        throw err
      }
      return res.body
    },
    onSuccess: () => {
      // Reset backoff and invalidate auth-me so the scheduler reschedules
      // off the freshly-rotated session_exp. This is the cache-dedup
      // benefit AutoSDE called out: any component that later starts using
      // session info will see the rotated value automatically.
      backoffIdxRef.current = 0
      void queryClient.invalidateQueries({ queryKey: ['auth-me'] })
    },
    onError: (err: RefreshError) => {
      if (stoppedRef.current) return
      if (err.status === 401 && err.body === 'refresh_chain_revoked') {
        // Chain is dead. Stop scheduling, force re-mint via existing flow.
        stoppedRef.current = true
        clearTimer()
        // eslint-disable-next-line no-console -- intentional auth breadcrumb
        console.warn('[refresh] chain revoked; re-auth required')
        const cb = onChainRevokedRef.current
        if (cb) cb()
        else window.location.assign('/')
        return
      }
      // Transient (5xx / no_refresh_cookie / network) — exponential backoff.
      applyBackoff()
    },
    retry: false,
  })

  // ── Query: GET /api/auth/me ───────────────────────────────────────────
  // Single source of truth for session_exp. Cached + invalidated after
  // every successful refresh.
  const meQuery = useQuery({
    queryKey: ['auth-me'],
    queryFn: async (): Promise<AuthMeResponse | null> => {
      const resp = await fetch('/api/auth/me', { credentials: 'include' })
      if (resp.status === 404) {
        // Older server; no refresh-token support. Mark stopped, surface
        // null so the scheduling effect bails.
        stoppedRef.current = true
        // eslint-disable-next-line no-console -- intentional auth breadcrumb
        console.info('[refresh] /api/auth/me unavailable; refresh-token unsupported on this server')
        return null
      }
      // The auth middleware denies an expired access cookie with 403 +
      // X-Auth-Required (token_auth._deny), not 401 — handle both so a cold
      // reopen silently refreshes before the session-expired banner flashes.
      if (
        resp.status === 401 ||
        (resp.status === 403 && resp.headers.get('X-Auth-Required') === 'true')
      ) {
        // Breadcrumb so a recurrence is visible in the console, not just server rotation counts.
        // eslint-disable-next-line no-console -- intentional auth breadcrumb
        console.info(`[refresh] access cookie gone (${resp.status}); attempting silent refresh`)
        try {
          await refreshMutation.mutateAsync()
        } catch {
          // Refresh failed — chain revoked / no_refresh_cookie / 5xx.
          // mutation.onError already handled chain-revoked + backoff.
          // eslint-disable-next-line no-console -- intentional auth breadcrumb
          console.warn('[refresh] on-boot refresh failed; auth UI will surface')
          return null
        }
        // Cookies rotated; retry /api/auth/me ONCE.
        const retry = await fetch('/api/auth/me', { credentials: 'include' })
        if (retry.ok) return retry.json() as Promise<AuthMeResponse>
        // eslint-disable-next-line no-console -- intentional auth breadcrumb
        console.warn(`[refresh] re-auth retry still unauthenticated (${retry.status})`)
        return null
      }
      if (!resp.ok) return null
      return resp.json() as Promise<AuthMeResponse>
    },
    retry: false,
    // staleTime longer than typical access TTL (20h) so routine renders
    // don't refetch; only invalidateQueries (after successful refresh)
    // triggers a re-fetch.
    staleTime: Infinity,
    gcTime: Infinity,
    refetchOnWindowFocus: false,
    refetchOnReconnect: false,
  })

  // ── Helpers ───────────────────────────────────────────────────────────
  const clearTimer = (): void => {
    if (timerRef.current !== null) {
      window.clearTimeout(timerRef.current)
      timerRef.current = null
    }
  }

  const scheduleNext = (delayMs: number): void => {
    clearTimer()
    const delay = Math.max(MIN_DELAY_MS, Math.floor(delayMs))
    timerRef.current = window.setTimeout(() => {
      timerRef.current = null
      void fireRefresh()
    }, delay) as unknown as number
  }

  const applyBackoff = (): void => {
    const idx = Math.min(backoffIdxRef.current, BACKOFF_SCHEDULE_MS.length - 1)
    const delay = BACKOFF_SCHEDULE_MS[idx]
    backoffIdxRef.current = Math.min(
      backoffIdxRef.current + 1,
      BACKOFF_SCHEDULE_MS.length - 1,
    )
    // eslint-disable-next-line no-console -- intentional auth breadcrumb
    console.info(`[refresh] backoff: retry in ${Math.round(delay / 1000)}s`)
    scheduleNext(delay)
  }

  const fireRefresh = async (): Promise<void> => {
    if (stoppedRef.current) return
    // Visibility-deferred: if the tab is hidden, wait until it returns.
    if (document.visibilityState === 'hidden') {
      const onVisible = (): void => {
        if (document.visibilityState !== 'hidden') {
          document.removeEventListener('visibilitychange', onVisible)
          void fireRefresh()
        }
      }
      document.addEventListener('visibilitychange', onVisible)
      return
    }
    // Mutation handles success (invalidate auth-me → reschedule) and
    // failure (chain_revoked / backoff) via its own onSuccess/onError.
    refreshMutation.mutate()
  }

  // ── Schedule the next refresh whenever session_exp changes ────────────
  useEffect(() => {
    if (stoppedRef.current) {
      clearTimer()
      return
    }
    const data = meQuery.data
    if (!data || !data.session_exp) {
      // No session yet (loading / null sentinel from older server).
      // If the query finished with null AND we're not stopped, that
      // means /api/auth/me returned a non-404 error — fall to backoff.
      if (meQuery.isFetched && data === null && !stoppedRef.current && !meQuery.isFetching) {
        applyBackoff()
      }
      return
    }
    const nowMs = Date.now()
    const expMs = data.session_exp * 1000
    const fireAtMs = expMs - LEAD_MS
    const delayMs = Math.max(MIN_DELAY_MS, fireAtMs - nowMs)
    scheduleNext(delayMs)
  // eslint-disable-next-line react-hooks/exhaustive-deps -- intentional deps
  }, [meQuery.data?.session_exp, meQuery.isFetched, meQuery.isFetching])

  // ── Cleanup on unmount ────────────────────────────────────────────────
  useEffect(() => {
    return () => {
      clearTimer()
    }
  }, [])
}
