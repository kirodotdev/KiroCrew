import { useCallback } from 'react'
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
 * fault. `config_path` is empty because the gateway never named one.
 */
const ENDPOINT_ABSENT: BrowserCookiesStatus = {
  present: false,
  summary: null,
  config_path: '',
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

/**
 * Observe, import and clear the browser cookies the gateway applies to the
 * agent's Playwright CLI browser sessions.
 *
 * `enabled` should be the Live view's own visibility: the status is read on
 * mount and re-read after an import or clear, but never polled — imported
 * cookies do not change on their own the way a supervised view process does.
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
    // A stored cookie set does not change without an import/clear on this same
    // hook, so there is nothing to poll for; the mutations refresh it directly.
    refetchInterval: false,
    // A 403 (non-owner) and a missing route are settled answers, not transient
    // faults — retrying would only delay hiding the control.
    retry: (_count, e) => !isForbidden(e) && !isMissingRoute(e),
  })

  const forbidden = isForbidden(query.error)

  const importMutation = useMutation({
    mutationFn: ({ content, filename }: { content: string; filename?: string }) =>
      api.importBrowserCookies(content, filename, sessionKey),
    onSuccess: (data: BrowserCookiesImportResult) => {
      // The import answer carries the fresh summary, so write status straight
      // into the cache: the chip must reflect the new set NOW, not after a
      // follow-up read.
      queryClient.setQueryData<BrowserCookiesStatus>(queryKey, (prev) => ({
        present: true,
        summary: data.summary,
        config_path: prev?.config_path ?? '',
      }))
    },
  })

  const importCookies = useCallback(
    (content: string, filename?: string) =>
      importMutation.mutateAsync({ content, filename }),
    [importMutation],
  )

  const clearMutation = useMutation({
    mutationFn: () => api.clearBrowserCookies(sessionKey),
    onSuccess: () => {
      queryClient.setQueryData<BrowserCookiesStatus>(queryKey, (prev) => ({
        present: false,
        summary: null,
        config_path: prev?.config_path ?? '',
      }))
    },
  })

  const clear = useCallback(() => clearMutation.mutateAsync(), [clearMutation])

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
    /** DELETE the stored set. */
    clear,
    clearing: clearMutation.isPending,
    clearError: clearMutation.error,
  }
}
