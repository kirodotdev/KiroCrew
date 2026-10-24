import { useCallback, useEffect, useRef, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'

import { ApiError } from '../api/apiError'
import {
  api,
  type BrowserCookiesStatus,
  type BrowserCookiesImportResult,
} from '../api/client'

/** Query key for the imported-cookies status, scoped by the slot the read is
 * made on behalf of: a restricted slot's 403 must not be served from (or
 * poison) the cache of a persistent one. */
export const BROWSER_COOKIES_KEY = ['browserCookies'] as const
export const browserCookiesKey = (sessionKey?: string) =>
  [...BROWSER_COOKIES_KEY, sessionKey ?? ''] as const

/**
 * What the panel shows when the ENDPOINT itself is absent (404/501) — an older
 * gateway, or this frontend running ahead of the backend that serves cookie
 * import. Presented as "no cookies imported" rather than an error: the control
 * is additive, so a gateway without it should read as an empty state, not a
 * fault.
 */
const ENDPOINT_ABSENT: BrowserCookiesStatus = {
  present: false,
  summary: null,
}

/** True for a status the gateway answers by not having the route at all. */
function isMissingRoute(e: unknown): boolean {
  // 501 as well as 404: a gateway that knows the route but cannot serve it on
  // this platform is the same thing to the panel — the capability is absent.
  return e instanceof ApiError && (e.status === 404 || e.status === 501)
}

/** True for the owner-only guard's refusal. The cookie controls are owner-only,
 * exactly like the browser view's start/open, so a non-owner must not even see
 * them — the same treatment the view gives a non-owner. */
function isForbidden(e: unknown): boolean {
  return e instanceof ApiError && e.status === 403
}

/** How many times a failed status read is retried before it settles as `error`. */
const STATUS_RETRIES = 2

/**
 * Observe, import and clear the browser cookies the gateway applies to the
 * agent's Playwright CLI browser sessions.
 *
 * `enabled` should be the Live view's own visibility: the status is read on
 * mount, re-read after an import or clear, and refreshed on window focus to
 * pick up changes made in another dashboard window, but never polled.
 *
 * `sessionKey` is the ACTIVE chat slot's key and rides every request as
 * X-Session-Key, the way the browser view's open does. The gateway's
 * restricted-session guard reads that header; without it the shared
 * `dashboard:ui` placeholder answers "not restricted", and an incognito or
 * temporary slot would read the sites a stored credential unlocks and persist
 * a logged-in browser state it promised to keep nothing of.
 *
 * A missing route degrades to the empty "no cookies" state, because that IS the
 * honest reading for a gateway that predates the feature. A 403 — a non-owner,
 * or a restricted slot — sets `forbidden` so the caller can HIDE the control
 * silently, mirroring how the browser view treats a non-owner. Any OTHER
 * failure is left as an error so the panel can surface it.
 */
export function useBrowserCookies(enabled: boolean, sessionKey?: string) {
  const queryClient = useQueryClient()
  const queryKey = browserCookiesKey(sessionKey)
  // The outcome outlives the menu, but belongs to the slot that sent DELETE.
  // Keep it separate from mutation lifecycle so an import cannot reset a
  // pending DELETE and enable a second concurrent clear.
  const [clearOutcome, setClearOutcome] = useState<{
    sessionKey?: string
    error?: Error
    result?: Awaited<ReturnType<typeof api.clearBrowserCookies>>
  } | null>(null)
  const resetClear = useCallback(() => setClearOutcome(null), [])
  const previousPresent = useRef<{ sessionKey?: string; present?: boolean }>({ sessionKey })

  const query = useQuery({
    queryKey,
    queryFn: async (): Promise<BrowserCookiesStatus> => {
      try {
        return await api.getBrowserCookies(sessionKey)
      } catch (e) {
        if (isMissingRoute(e)) return ENDPOINT_ABSENT
        throw e
      }
    },
    enabled,
    // Mutations refresh this window directly; focus picks up import/clear
    // operations performed in another dashboard window without polling.
    refetchInterval: false,
    refetchOnWindowFocus: 'always',
    // A 403 (non-owner) and a missing route are settled answers, not transient
    // faults — retrying would only delay hiding the control. Anything else is
    // retried a bounded number of times, so a persistent failure settles into
    // `error` and the panel can say so.
    retry: (count, e) => count < STATUS_RETRIES && !isForbidden(e) && !isMissingRoute(e),
  })

  const forbidden = isForbidden(query.error)

  // The stored set is one per gateway, but each slot caches its own read. After
  // an import or clear, this slot's entry is written directly; every OTHER
  // slot's entry is marked stale so it refetches (under its own slot's key, so
  // its own authorization) instead of showing the old set.
  const ownKey = queryKey[1]
  const invalidateOtherSlots = useCallback(() => {
    void queryClient.invalidateQueries({
      queryKey: BROWSER_COOKIES_KEY,
      predicate: (q) => q.queryKey[1] !== ownKey,
    })
  }, [queryClient, ownKey])

  // A status read still in flight was answered about the set as it stood
  // BEFORE this mutation, and the query is never polled or invalidated for
  // this slot afterwards. Cancel it before writing the mutation's answer, or
  // its late response would overwrite that answer and stick: an import would
  // hide the chip and the Clear control while the cookies stay applied.
  const cancelOwnRead = useCallback(
    () => queryClient.cancelQueries({ queryKey: browserCookiesKey(sessionKey), exact: true }),
    [queryClient, sessionKey],
  )

  const importMutation = useMutation({
    mutationFn: (content: string) => api.importBrowserCookies(content, sessionKey),
    onSuccess: async (data: BrowserCookiesImportResult) => {
      await cancelOwnRead()
      resetClear()
      // The import answer carries the fresh summary, so write status straight
      // into the cache: the chip must reflect the new set NOW, not after a
      // follow-up read.
      queryClient.setQueryData<BrowserCookiesStatus>(queryKey, {
        present: true,
        summary: data.summary,
      })
      invalidateOtherSlots()
    },
  })

  const importCookies = useCallback(
    (content: string) => importMutation.mutateAsync(content),
    [importMutation],
  )

  const clearMutation = useMutation({
    mutationFn: (clearedSessionKey: string | undefined) => api.clearBrowserCookies(clearedSessionKey),
    onMutate: resetClear,
    onSuccess: async (result, clearedSessionKey) => {
      // Mutation variables retain the initiating slot even if the panel changes
      // slots before the request settles and updates this observer's callbacks.
      const clearedKey = browserCookiesKey(clearedSessionKey)
      await queryClient.cancelQueries({ queryKey: clearedKey, exact: true })
      setClearOutcome({ sessionKey: clearedSessionKey, result })
      queryClient.setQueryData<BrowserCookiesStatus>(clearedKey, {
        present: false,
        summary: null,
      })
      void queryClient.invalidateQueries({
        queryKey: BROWSER_COOKIES_KEY,
        predicate: (q) => q.queryKey[1] !== clearedKey[1],
      })
    },
    onError: (error, clearedSessionKey) => {
      setClearOutcome({ sessionKey: clearedSessionKey, error })
    },
  })

  const clear = useCallback(() => clearMutation.mutateAsync(sessionKey), [clearMutation, sessionKey])
  const present = query.data?.present
  useEffect(() => {
    // Only a new set (absent -> present) supersedes a settled clear. A failed
    // DELETE leaves present true, so an ordinary refetch must keep its notice.
    const previous = previousPresent.current
    if (previous.sessionKey === sessionKey && previous.present === false && present === true
      && !clearMutation.isPending) resetClear()
    previousPresent.current = { sessionKey, present }
  }, [sessionKey, present, clearMutation.isPending, resetClear])
  const ownClearOutcome = clearOutcome?.sessionKey === sessionKey ? clearOutcome : null

  return {
    /** Latest status, or undefined before the first answer arrives. */
    data: query.data,
    /** No answer yet (first load, or `enabled` is false). */
    pending: query.isPending,
    /** The status read failed for a reason that is NOT "route absent" or 403. */
    error: forbidden ? null : query.error,
    /** The caller is not the owner, or the slot is restricted: hide the control entirely. */
    forbidden,
    /** POST a pasted/loaded export. Resolves with the import result (summary +
     *  hot-load report); rejects with the ApiError on a 400/other failure. */
    importCookies,
    importing: importMutation.isPending,
    importError: importMutation.error,
    /** The last import's hot-load report, for the "applies to new sessions" hint. */
    lastImport: importMutation.data ?? null,
    /** DELETE the stored set. Resolves with the clear result, whose `live.failed`
     *  names the live sessions that did NOT take the clear (the on-disk set is
     *  gone regardless); rejects with the ApiError when the DELETE itself failed. */
    clear,
    clearing: clearMutation.isPending,
    clearError: ownClearOutcome?.error ?? null,
    lastClear: ownClearOutcome?.result ?? null,
    resetClear,
  }
}
