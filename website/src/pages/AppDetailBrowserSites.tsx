/**
 * The app detail page's "Browsing sites" card: every page the app's own agent may
 * browse (the manifest's `permissions.browserSites` and the sites the owner added
 * from the app's own page), where each came from and where it stands, and its
 * controls: Remove on a listed site, Turn on on a turned-off one, Add back on a
 * declared one the owner removed. Hidden while the list is empty.
 *
 * Adding a new site is the app page's job (`useBrowserSites` in the app SDK); approving
 * a declared one is the reconsent notice above. Turn on and Add back widen what the
 * agent may open, so each asks the owner to confirm before it sends `add`, in the
 * owner's own words: the owner chose it here, so it is not the app's request dialog.
 * Remove ends a site at the agent's next step. The change is owner-only on the gateway (`POST /api/apps/{name}/browser-sites`).
 */
import { createElement, Fragment, useEffect, useState } from 'react'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import { api } from '../api/client'
import type { AppBrowserSite, AppBrowserSiteAction, AppBrowserSites } from '../api/client/apps'
import { askAppConfirm } from '../lib/appConfirm'
import { Badge, Btn, Card, CardTitle } from '../components/ui'
import ErrorNotice from '../components/ErrorNotice'
import { i18nT } from '../i18n/t'

const SOURCE_TEXT: Record<AppBrowserSite['source'], () => string> = {
  app: () => i18nT('pages.appDetailPage.browser_sites_source_app'),
  owner: () => i18nT('pages.appDetailPage.browser_sites_source_owner'),
}

const STATUS_TEXT: Record<AppBrowserSite['status'], () => string> = {
  approved: () => i18nT('pages.appDetailPage.browser_sites_status_approved'),
  pending: () => i18nT('pages.appDetailPage.browser_sites_status_pending'),
  disabled: () => i18nT('pages.appDetailPage.browser_sites_status_disabled'),
  removed: () => i18nT('pages.appDetailPage.browser_sites_status_removed'),
}

/** The owner's own confirm before Turn on or Add back: the site and what the agent may then do. */
function confirmRestore(site: string, status: AppBrowserSite['status']): Promise<boolean> {
  const addBack = status === 'removed'
  return askAppConfirm({
    title: i18nT(addBack ? 'pages.appDetailPage.browser_sites_add_back_title' : 'pages.appDetailPage.browser_sites_turn_on_title'),
    body: createElement(
      Fragment,
      null,
      createElement('code', { className: 'block break-all rounded border border-border bg-bg-elevated px-2 py-1 font-mono text-[13px] text-text', 'data-testid': 'browser-site-restore-site' }, site),
      createElement('p', { className: 'mt-2' }, i18nT('pages.appDetailPage.browser_sites_restore_risk')),
    ),
    confirmLabel: i18nT(addBack ? 'pages.appDetailPage.browser_sites_add_back' : 'pages.appDetailPage.browser_sites_turn_on'),
    danger: false,
    primary: true,
  })
}

const STATUS_VARIANT: Record<AppBrowserSite['status'], 'ok' | 'warn' | 'muted'> = {
  approved: 'ok',
  pending: 'warn',
  disabled: 'muted',
  removed: 'muted',
}

export default function AppDetailBrowserSites({ appName, onChanged }: { appName: string; onChanged?: () => void }) {
  const queryClient = useQueryClient()
  const key = ['apps', appName, 'browser-sites']
  const list = useQuery<AppBrowserSites>({ queryKey: key, queryFn: () => api.getAppBrowserSites(appName) })
  const { refetch } = list
  useEffect(() => {
    const refresh = () => { void refetch() }
    window.addEventListener('mc:apps-changed', refresh)
    return () => window.removeEventListener('mc:apps-changed', refresh)
  }, [refetch])
  const [busy, setBusy] = useState<string | null>(null)
  const [error, setError] = useState('')
  const sites = list.data?.sites ?? []
  if (list.isError) {
    const message = list.error instanceof Error ? list.error.message : String(list.error)
    return (
      <Card data-testid="app-browser-sites">
        <CardTitle>{i18nT('pages.appDetailPage.browser_sites_list_title')}</CardTitle>
        <ErrorNotice message={message} variant="inline" askAgent />
      </Card>
    )
  }
  if (sites.length === 0) return null

  const change = async (action: AppBrowserSiteAction, row: AppBrowserSite) => {
    const { site } = row
    setBusy(site)
    setError('')
    try {
      if (action === 'add' && !(await confirmRestore(site, row.status))) return
      queryClient.setQueryData(key, await api.changeAppBrowserSite(appName, action, site))
      // The reconsent notice reads the staged sites off the app row.
      onChanged?.()
    } catch (e) {
      const detail = { error: e instanceof Error ? e.message : String(e) }
      setError(action === 'add'
        ? i18nT('pages.appDetailPage.browser_sites_add_failed', detail)
        : i18nT('pages.appDetailPage.browser_sites_remove_failed', detail))
    } finally {
      setBusy(null)
    }
  }

  return (
    <Card data-testid="app-browser-sites">
      <CardTitle>{i18nT('pages.appDetailPage.browser_sites_list_title')}</CardTitle>
      <div className="mt-1 text-[12px] text-muted">{i18nT('pages.appDetailPage.browser_sites_list_hint')}</div>
      <ul className="mt-2 grid gap-2">
        {sites.map(row => (
          <li key={row.site} className="grid gap-1 text-[13px]" data-testid="app-browser-site-row">
            <code className="w-fit max-w-full break-all bg-bg-elevated border border-border px-1.5 py-0.5 rounded text-[11px] text-text">{row.site}</code>
            <div className="flex flex-wrap items-center gap-x-2 gap-y-1">
              <span className="text-muted">{SOURCE_TEXT[row.source]?.() ?? row.source}</span>
              <Badge variant={STATUS_VARIANT[row.status] ?? 'muted'}>{STATUS_TEXT[row.status]?.() ?? row.status}</Badge>
              <div className="ml-auto flex gap-2">
                {row.status === 'disabled' && (
                  <Btn onClick={() => void change('add', row)} disabled={busy !== null}>
                    {i18nT('pages.appDetailPage.browser_sites_turn_on')}
                  </Btn>
                )}
                {row.status === 'removed' ? (
                  <Btn onClick={() => void change('add', row)} disabled={busy !== null}>
                    {i18nT('pages.appDetailPage.browser_sites_add_back')}
                  </Btn>
                ) : (
                  <Btn onClick={() => void change('remove', row)} disabled={busy !== null}>
                    {i18nT('pages.appDetailPage.browser_sites_remove')}
                  </Btn>
                )}
              </div>
            </div>
          </li>
        ))}
      </ul>
      {error && <ErrorNotice message={error} variant="inline" askAgent />}
    </Card>
  )
}
