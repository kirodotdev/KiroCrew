/**
 * The pages an app's own agent may browse, for an app page (`@kirocrew/app-sdk`,
 * level 13): read the list, and ask the owner to change it.
 *
 * The list is the app's declared `permissions.browserSites` plus the sites the owner
 * added, each with where it came from (`source`) and where it stands (`status`:
 * `approved`, `pending`, `disabled`, or `removed` for a declared site the owner removed,
 * which stays listed so it can be added back).
 * `active` is what the agent may open right now. The gateway enforces it on every
 * browser op (`apps/browser_sites.py`), so nothing here widens that list by itself.
 *
 * It is host code, like `useHostSessions`: it calls the dashboard's own client on the
 * app's own `/api/apps/<app>/browser-sites`, so the app declares no `permissions.api`
 * path for it. The app comes from the identity the host published.
 *
 * - `requestRemove` and `requestDisable` only shrink the list, so they apply at once.
 * - `requestAdd` widens it (a new site, a turned-off or removed one back on, or a
 *   declared one still waiting for approval): the dashboard draws its own dialog naming the
 *   app (its display name, author and id, read from the dashboard's own app row), the site
 *   and what the agent may then do, and sends the change only when the owner presses
 *   Allow. Otherwise it resolves `{ ok: false, code: 'declined' }`.
 *
 * The dialog is a guardrail, not a boundary. An app page runs in the dashboard's own
 * document with the owner's session, so its code could reach the same route without
 * it, exactly as it could reach `/api/apps/<app>/enable` (see the trust model). The
 * gateway refuses the change to every caller that is not the owner's dashboard: the
 * app's token, its backend and its agent's tools.
 */
import { createElement, Fragment, useCallback, useEffect, useRef, useState } from 'react'
import { api } from '../api/client'
import type { AppBrowserSite, AppBrowserSiteAction, AppBrowserSites } from '../api/client/apps'
import { ApiError } from '../api/apiError'
import { queryClient as dashboardQueryClient } from '../api/queryClient'
import { i18nT } from '../i18n/t'
import { askAppConfirm } from '../lib/appConfirm'
import { useAppIdentity } from './identity'

export type BrowserSite = Readonly<AppBrowserSite>

/** What a request resolves to: the list after it, or why nothing changed. */
export type BrowserSiteResult =
  | { readonly ok: true; readonly sites: readonly BrowserSite[]; readonly active: readonly string[] }
  | { readonly ok: false; readonly code: string; readonly error: string }

export interface BrowserSites {
  /** Every listed site, or null before the first read (or when it failed). */
  readonly sites: readonly BrowserSite[] | null
  /** The sites the app's agent may open now. */
  readonly active: readonly string[]
  readonly loading: boolean
  /** The last failed read's code, or null. */
  readonly error: string | null
  refresh(): Promise<void>
  /** Ask the owner, in the dashboard's dialog, to put *site* in force. */
  requestAdd(site: string): Promise<BrowserSiteResult>
  /** Take *site* off the list now. */
  requestRemove(site: string): Promise<BrowserSiteResult>
  /** Keep *site* listed but out of force now. */
  requestDisable(site: string): Promise<BrowserSiteResult>
}

/** The refusal's `code` and message from a gateway answer, or a generic one. */
function refusal(err: unknown): { ok: false; code: string; error: string } {
  if (err instanceof ApiError) {
    try {
      const body = JSON.parse(err.body || '{}') as { code?: unknown; error?: unknown }
      return {
        ok: false,
        code: typeof body.code === 'string' && body.code ? body.code : 'request_failed',
        error: typeof body.error === 'string' ? body.error : err.message,
      }
    } catch {
      return { ok: false, code: 'request_failed', error: err.message }
    }
  }
  return { ok: false, code: 'request_failed', error: err instanceof Error ? err.message : String(err) }
}

/** The installed app's display name and author, as the dashboard's own client reads them. */
interface AskingApp {
  readonly displayName: string
  readonly author: string
}

/** The asking app's row, or empty details when the read fails or carries none. */
async function readAskingApp(app: string): Promise<AskingApp> {
  try {
    const row = await api.getApp(app)
    return { displayName: row?.displayName?.trim() || app, author: row?.manifest?.author?.trim() ?? '' }
  } catch {
    return { displayName: app, author: '' }
  }
}

/** The dialog body: who asks, the site as typed, and what allowing it lets the agent do. */
function addBody(app: string, asking: AskingApp, site: string) {
  return createElement(
    Fragment,
    null,
    createElement('p', null, i18nT('appSdk.browserSites.add_asked_by', { app: asking.displayName })),
    createElement(
      'p',
      { className: 'mt-1 text-[12px] text-muted', 'data-testid': 'browser-site-add-app' },
      asking.author
        ? i18nT('appSdk.browserSites.add_app_details', { author: asking.author, id: app })
        : i18nT('appSdk.browserSites.add_app_id', { id: app }),
    ),
    createElement('code', { className: 'mt-2 block break-all rounded border border-border bg-bg-elevated px-2 py-1 font-mono text-[13px] text-text', 'data-testid': 'browser-site-add-site' }, site),
    site.includes('*') ? createElement('p', { className: 'mt-2' }, i18nT('appSdk.browserSites.add_star')) : null,
    createElement('p', { className: 'mt-2' }, i18nT('appSdk.browserSites.add_risk')),
  )
}

/**
 * Ask the owner, in the dashboard's own dialog, to let *app*'s agent browse *site*.
 * Resolves true only when the owner presses Allow; it sends nothing itself.
 */
async function askToAddBrowserSite(app: string, site: string): Promise<boolean> {
  const asking = await readAskingApp(app)
  return askAppConfirm({
    title: i18nT('appSdk.browserSites.add_title'),
    body: addBody(app, asking, site),
    confirmLabel: i18nT('appSdk.browserSites.add_allow'),
    danger: false,
    primary: true,
  })
}

export function useBrowserSites(): BrowserSites {
  const app = useAppIdentity()?.appId ?? ''
  const [list, setList] = useState<AppBrowserSites | null>(null)
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState<string | null>(null)
  // An answer for an app the page has since moved off is dropped.
  const current = useRef(app)
  current.current = app

  const refresh = useCallback(async () => {
    const asked = app
    if (!asked) {
      setError('no_app')
      return
    }
    setLoading(true)
    try {
      const answer = await api.getAppBrowserSites(asked)
      if (current.current !== asked) return
      setList(answer)
      setError(null)
    } catch (err) {
      if (current.current !== asked) return
      setList(null)
      setError(refusal(err).code)
    } finally {
      if (current.current === asked) setLoading(false)
    }
  }, [app])

  useEffect(() => {
    setList(null)
    void refresh()
  }, [refresh])

  const change = useCallback(async (action: AppBrowserSiteAction, site: string): Promise<BrowserSiteResult> => {
    const asked = app
    if (!asked) return { ok: false, code: 'no_app', error: 'no app identity' }
    const entry = typeof site === 'string' ? site.trim() : ''
    if (!entry) return { ok: false, code: 'bad_site', error: 'site is required' }
    if (action === 'add') {
      const allowed = await askToAddBrowserSite(asked, entry)
      if (!allowed) return { ok: false, code: 'declined', error: 'the owner did not allow it' }
    }
    try {
      const answer = await api.changeAppBrowserSite(asked, action, entry)
      // The detail page's card caches the same list.
      void dashboardQueryClient.invalidateQueries({ queryKey: ['apps', asked, 'browser-sites'] })
      if (current.current === asked) {
        setList(answer)
        setError(null)
      }
      return { ok: true, sites: answer.sites, active: answer.active }
    } catch (err) {
      return refusal(err)
    }
  }, [app])

  return {
    sites: list?.sites ?? null,
    active: list?.active ?? [],
    loading,
    error,
    refresh,
    requestAdd: useCallback((site: string) => change('add', site), [change]),
    requestRemove: useCallback((site: string) => change('remove', site), [change]),
    requestDisable: useCallback((site: string) => change('disable', site), [change]),
  }
}
