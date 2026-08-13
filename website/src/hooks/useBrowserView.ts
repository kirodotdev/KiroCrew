import { useCallback } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'

import { api, ApiError, type BrowserViewData } from '../api/client'

/** Query key for the browser view's status. */
export const BROWSER_VIEW_KEY = ['browserView'] as const

/**
 * Status poll interval. The view server is a supervised child process that can
 * come up or die without telling the dashboard, and there is no push channel for
 * it, so the panel re-reads. Slow enough to be invisible on the gateway, fast
 * enough that a session an agent just opened surfaces on its own.
 */
const POLL_MS = 5_000

/**
 * What the panel shows when the ENDPOINT itself is absent (404) — an older
 * gateway, or this frontend running ahead of the backend that serves the view.
 *
 * `reason` is deliberately null rather than a synthesized sentence: the field
 * carries server-authored prose, and putting our own guess there would be
 * indistinguishable from something the server actually reported. Null is the
 * caller's cue to use its own translated generic copy.
 */
const ENDPOINT_ABSENT: BrowserViewData = {
  status: 'unavailable',
  url: null,
  port: null,
  reason: null,
}

/** True for a status the gateway answers by not having the route at all. */
function isMissingRoute(e: unknown): boolean {
  // 501 as well as 404: a gateway that knows the route but cannot serve it on
  // this platform is the same thing to the panel — the capability is absent.
  return e instanceof ApiError && (e.status === 404 || e.status === 501)
}

/**
 * Observe (and start) the Playwright CLI browser view that the Browser panel
 * frames.
 *
 * Read-only except for `start`, which POSTs the idempotent start endpoint and
 * writes its response straight into the cache — the endpoint returns the same
 * shape as the GET, so there is no read-after-write round trip and no window
 * where the panel shows `stopped` for a view that is already up.
 *
 * A missing route degrades to `unavailable` instead of an error, because that IS
 * the honest reading: this gateway has no browser view. Any OTHER failure is left
 * as an error so the panel can show what went wrong rather than claiming the
 * capability does not exist.
 *
 * `enabled` should be the panel's own visibility: polling a view nobody is
 * looking at buys nothing.
 */
export function useBrowserView(enabled: boolean) {
  const queryClient = useQueryClient()

  const query = useQuery({
    queryKey: BROWSER_VIEW_KEY,
    queryFn: async (): Promise<BrowserViewData> => {
      try {
        return await api.getBrowserView()
      } catch (e) {
        if (isMissingRoute(e)) return ENDPOINT_ABSENT
        throw e
      }
    },
    enabled,
    refetchInterval: enabled ? POLL_MS : false,
    // A view that is down is a normal answer, not a transient fault to retry —
    // and retrying would delay the honest `stopped` card the user acts on.
    retry: false,
  })

  const startMutation = useMutation({
    mutationFn: () => api.startBrowserView(),
    onSuccess: (data) => {
      queryClient.setQueryData(BROWSER_VIEW_KEY, data)
    },
  })

  /**
   * A start that SUCCEEDED as a request and still did not produce a running view.
   *
   * The endpoint answers 200 with the post-attempt status rather than an HTTP
   * error, so a launch that failed (port taken, browser binary missing) comes back
   * as a perfectly good `stopped`. Without this the panel would re-render the same
   * card and the click would look like it did nothing at all.
   */
  const startDidNotTake = startMutation.isSuccess
    && startMutation.data?.status !== 'running'

  const start = useCallback(() => {
    startMutation.mutate()
  }, [startMutation])

  const refresh = useCallback(() => {
    void queryClient.invalidateQueries({ queryKey: BROWSER_VIEW_KEY })
  }, [queryClient])

  return {
    /** Latest status, or undefined before the first answer arrives. */
    data: query.data,
    /** No answer yet (first load, or `enabled` is false). */
    pending: query.isPending,
    /** The status read failed for a reason that is NOT "route absent". */
    error: query.error,
    /** POST the idempotent start endpoint. */
    start,
    starting: startMutation.isPending,
    startError: startMutation.error,
    /** The request went through but the view still is not running. */
    startDidNotTake,
    /** Re-read the status now (the retry affordance on a failed/absent view). */
    refresh,
  }
}
