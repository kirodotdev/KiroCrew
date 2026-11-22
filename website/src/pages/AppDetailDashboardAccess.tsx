/**
 * The app detail page's "Dashboard access" card: the owner's one switch for an app that
 * drives this dashboard on their behalf (a Crew Tunnel, say). Shows the words the gateway
 * offers (publisher, source and version come from the install record, not the manifest),
 * asks the owner to confirm, grants, then offers a restart because the access is bound
 * into the app backend when it starts.
 *
 * Shown only when something is to be said: an offer exists, the access is on, or the app
 * asked for it and was refused (the "needs dashboard access" state). Where the sandbox
 * cannot bind the access, the card gives that reason in place of the control.
 *
 * Owner only: `PUT /api/apps/{name}/dashboard-proxy` refuses any other caller.
 */
import { useEffect, useState } from 'react'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import { api } from '../api/client'
import type { AppDashboardAccess } from '../api/client/apps'
import { Btn, Card, CardTitle } from '../components/ui'
import { useConfirm } from '../components/ConfirmDialog'
import ErrorNotice from '../components/ErrorNotice'
import { i18nT } from '../i18n/t'

export default function AppDetailDashboardAccess({ appName }: { appName: string }) {
  const queryClient = useQueryClient()
  const queryKey = ['apps', appName, 'dashboard-proxy']
  const access = useQuery<AppDashboardAccess>({ queryKey, queryFn: () => api.getAppDashboardAccess(appName) })
  const { confirm, confirmDialog } = useConfirm()
  const [busy, setBusy] = useState(false)
  const [restartAsked, setRestartAsked] = useState(false)
  const [notice, setNotice] = useState('')
  const [error, setError] = useState('')
  useEffect(() => { setRestartAsked(false); setNotice(''); setError('') }, [appName])

  const data = access.data
  if (!data && access.isError) {
    return (
      <Card data-testid="app-dashboard-access">
        <CardTitle>{i18nT('pages.appDetailPage.dashboard_access_title')}</CardTitle>
        <ErrorNotice
          message={i18nT('pages.appDetailPage.dashboard_access_load_failed')}
          variant="inline"
          askAgent
          testId="dashboard-access-load-failed"
        />
      </Card>
    )
  }
  if (!data || (!data.granted && !data.offer && !data.needs_access)) return null

  const scope = i18nT('pages.appDetailPage.dashboard_access_scope')
  const store = (next: AppDashboardAccess) => queryClient.setQueryData(queryKey, next)
  const allow = async () => {
    if (!data.offer) return
    const ok = await confirm({
      title: i18nT('pages.appDetailPage.dashboard_access_confirm_title'),
      body: `${data.offer.text} ${scope}`,
      confirmLabel: i18nT('pages.appDetailPage.dashboard_access_allow'),
      danger: false,
      primary: true,
    })
    if (!ok) return
    setBusy(true)
    setError('')
    try {
      store(await api.grantAppDashboardAccess(appName))
      setRestartAsked(true)
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
    } finally {
      setBusy(false)
    }
  }
  const restart = async () => {
    setBusy(true)
    setError('')
    try {
      const next = await api.restartAppForDashboardAccess(appName)
      store(next)
      setRestartAsked(false)
      if (next.restarted) setNotice(i18nT('pages.appDetailPage.dashboard_access_restarted'))
      else setError(i18nT('pages.appDetailPage.dashboard_access_restart_failed'))
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
    } finally {
      setBusy(false)
    }
  }

  return (
    <Card data-testid="app-dashboard-access">
      <CardTitle>{i18nT('pages.appDetailPage.dashboard_access_title')}</CardTitle>
      {!data.platform_supported ? (
        <div className="mt-2 text-[13px] text-muted" data-testid="dashboard-access-unsupported">
          {i18nT('pages.appDetailPage.dashboard_access_unsupported')}
        </div>
      ) : data.granted ? (
        <>
          <div className="mt-2 text-[13px] text-text">{i18nT('pages.appDetailPage.dashboard_access_on')}</div>
          <div className="mt-1 text-[12px] text-muted" data-testid="dashboard-access-scope">{scope}</div>
        </>
      ) : (
        <>
          <div className="mt-1 text-[12px] text-muted" data-testid="dashboard-access-scope">{scope}</div>
          {data.needs_access && (
            <div className="mt-2 text-[13px] text-warn" role="status" data-testid="dashboard-access-needed">
              {i18nT('pages.appDetailPage.dashboard_access_needed')}
            </div>
          )}
          <div className="mt-3">
            <Btn onClick={allow} disabled={busy || !data.offer}>{i18nT('pages.appDetailPage.dashboard_access_allow')}</Btn>
          </div>
        </>
      )}
      {restartAsked && data.granted && (
        <div className="mt-3 flex items-center gap-2" data-testid="dashboard-access-restart">
          <span className="text-[13px] text-text">{i18nT('pages.appDetailPage.dashboard_access_restart_hint')}</span>
          <Btn onClick={restart} disabled={busy}>{i18nT('pages.appDetailPage.dashboard_access_restart')}</Btn>
        </div>
      )}
      {notice && <div className="mt-2 text-[12px] text-ok-text" role="status">{notice}</div>}
      {error && <ErrorNotice message={error} variant="inline" askAgent />}
      {confirmDialog}
    </Card>
  )
}
