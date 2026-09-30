import { useEffect, useId, useState } from 'react'
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'

import {
  api,
  ApiError,
  browserInstallConflictJob,
  type BrowserEngine,
  type BrowserInstallData,
  type BrowserInstallJob,
} from '../../api/client'
import { SettingsSection, SettingsCard, SettingsToggle } from '../../components/settings'
import { FormSkeleton } from '../../components/ui'
import ErrorNotice from '../../components/ErrorNotice'
import { isElectron } from '../../lib/electron'
import type { DashboardConfig } from '../chat/ChatSettings'
import { i18nT } from '../../i18n/t'
import { BrowserInstallStatus, engineLabel } from './BrowserInstallStatus'
import { ConnectBrowserSection } from './ConnectBrowserSection'
import { ManagedBrowsersSection } from './ManagedBrowsersSection'
import {
  currentActivity,
  finishedJob,
  installBlock,
  type InstallBlock,
  type PendingRequest,
} from './browserInstallState'

const INSTALL_KEY = ['browserInstall'] as const

/**
 * Poll cadence while an install runs. The install downloads a browser, so it
 * outlives any single request and progress is only observable by re-reading.
 */
const INSTALLING_POLL_MS = 2_000

/** Poll cadence at rest, which only has to notice an install done elsewhere. */
const IDLE_POLL_MS = 15_000

/**
 * Human text for a rejected call. The `ApiError` message is preferred because
 * it is the string the error journal is keyed on, so `ErrorNotice` recovers the
 * endpoint / status from it; `fallback` covers a non-Error rejection.
 */
const failText = (e: unknown, fallback: string): string =>
  e instanceof Error && e.message ? e.message : fallback

/**
 * A rejection whose outcome is unknown: the request may have reached the gateway
 * and started a download even though no answer came back (a dropped connection,
 * or a proxy's own 502/503/504). Retrying one of those blind could start a
 * duplicate, so the panel re-reads the status first.
 */
const outcomeIsUnknown = (e: unknown): boolean => !(e instanceof ApiError) || e.status >= 502

/** Visible (and `aria-describedby`) text for a blocked install control. */
function blockText(block: InstallBlock | null): string | null {
  if (!block) return null
  switch (block.reason) {
    case 'status_unavailable':
      return i18nT('pages.settings.browserPanel.reason_status_unavailable')
    case 'checking':
      return i18nT('pages.settings.browserPanel.reason_checking')
    case 'engine_busy':
      return i18nT('pages.settings.browserPanel.reason_engine_busy', { engine: engineLabel(block.engine) })
    case 'cli_busy':
      return i18nT('pages.settings.browserPanel.reason_cli_busy')
    case 'busy_generic':
      return i18nT('pages.settings.browserPanel.reason_busy_generic')
  }
}

/** Fold a 409's active job into the cached status, so this tab shows what the
 *  gateway is actually doing instead of the operation it refused. */
function withConflictJob(job: BrowserInstallJob) {
  return (old: BrowserInstallData | undefined): BrowserInstallData | undefined =>
    old ? { ...old, install_job: job, installing: true } : old
}

/**
 * Browser settings, as three setup paths that are independent of each other:
 * managed browsers the gateway downloads, the operator's own browser reached
 * through the Playwright extension, and the desktop app's built-in panel.
 *
 * Install progress is one region above all three, driven by the job the gateway
 * reports, so a refreshed page or a second tab shows the same operation as the
 * tab that started it.
 */
export function BrowserPanel() {
  const qc = useQueryClient()

  // Set when a request's answer was lost; cleared by the first status read that
  // arrives after it, which is what tells the panel whether a download started.
  const [unknownSince, setUnknownSince] = useState<number | null>(null)

  const installMut = useMutation({
    mutationFn: () => api.installBrowserCli(),
    onSuccess: (fresh) => qc.setQueryData(INSTALL_KEY, fresh),
    onError: (e) => {
      const job = browserInstallConflictJob(e)
      if (job) qc.setQueryData(INSTALL_KEY, withConflictJob(job))
      else if (outcomeIsUnknown(e)) setUnknownSince(Date.now())
    },
    onSettled: () => { void qc.invalidateQueries({ queryKey: INSTALL_KEY }) },
  })

  const engineMut = useMutation({
    mutationFn: (engine: BrowserEngine) => api.installBrowserEngine(engine),
    onSuccess: (fresh) => qc.setQueryData(INSTALL_KEY, fresh),
    onError: (e) => {
      const job = browserInstallConflictJob(e)
      if (job) qc.setQueryData(INSTALL_KEY, withConflictJob(job))
      else if (outcomeIsUnknown(e)) setUnknownSince(Date.now())
    },
    onSettled: () => { void qc.invalidateQueries({ queryKey: INSTALL_KEY }) },
  })

  // This tab's own request, attributed only until the gateway answers it.
  const pending: PendingRequest | null = installMut.isPending
    ? { kind: 'cli_setup' }
    : engineMut.isPending && engineMut.variables
      ? { kind: 'engine_download', engine: engineMut.variables }
      : null
  const pendingSince = installMut.isPending ? installMut.submittedAt : engineMut.submittedAt

  const q = useQuery<BrowserInstallData>({
    queryKey: INSTALL_KEY,
    queryFn: api.getBrowserInstall,
    // Stale at once, so returning to this page or this window re-reads the
    // status: an install started from another tab or a shell becomes visible
    // without waiting for the idle poll.
    staleTime: 0,
    refetchOnMount: 'always',
    refetchOnWindowFocus: true,
    refetchInterval: (query) => {
      const d = query.state.data
      const busy =
        d?.installing || d?.install_job?.status === 'running' || pending !== null || unknownSince !== null
      return busy ? INSTALLING_POLL_MS : IDLE_POLL_MS
    },
  })
  const { data } = q
  const outcomeUnknown = unknownSince !== null && q.dataUpdatedAt <= unknownSince
  // Once a read has answered, the marker has done its job; dropping it also
  // returns the poll to its idle cadence.
  useEffect(() => {
    if (unknownSince !== null && q.dataUpdatedAt > unknownSince) setUnknownSince(null)
  }, [unknownSince, q.dataUpdatedAt])

  // The host's name, when the dashboard can read it. Shared with the System
  // page's cache. Absent leaves the generic wording; a FAILED read is reported
  // in the section, since the panel otherwise has no way to say which machine
  // the downloads land on.
  const hostQ = useQuery({
    queryKey: ['system'],
    queryFn: () => api.system(),
    select: (d: { hostname?: unknown } | null | undefined) =>
      typeof d?.hostname === 'string' && d.hostname ? d.hostname : null,
  })
  const hostError = hostQ.isError
    ? failText(hostQ.error, i18nT('pages.settings.browserPanel.host_unavailable'))
    : null

  // The built-in-browser toggle lives in dashboard config (not the install
  // status), so it round-trips through /api/dashboard/config like the other
  // dashboard settings.
  const dashQ = useQuery<DashboardConfig>({
    queryKey: ['dashboardConfig'],
    queryFn: () => api.dashboardConfig(),
  })
  const dashMut = useMutation({
    // Send ONLY the changed key: the config handler applies keys present in the
    // body, so a full-object PUT built from this query's cache could clobber a
    // setting another client changed after we cached (lost update).
    mutationFn: (patch: Partial<DashboardConfig>) => api.updateDashboardConfig(patch),
    onSettled: () => { void qc.invalidateQueries({ queryKey: ['dashboardConfig'] }) },
  })
  const setUseBuiltin = (v: boolean) => { dashMut.mutate({ use_builtin_browser: v }) }

  // Never seeded from the server: the status carries only whether a token exists,
  // so there is nothing to prefill and no way for the value to leak back out.
  // Held here, above every section, so polling, a failed read and a language
  // switch all re-render around it without unmounting the field.
  const [token, setToken] = useState('')
  const tokenMut = useMutation({
    mutationFn: (value: string) => api.setBrowserToken(value),
    onSuccess: () => { setToken(''); void qc.invalidateQueries({ queryKey: INSTALL_KEY }) },
  })
  // Every agent hand-off on this panel navigates to a chat, which unmounts the
  // panel and discards an unsaved token draft, so it is offered only when there
  // is no draft to lose.
  const allowHandoff = token === ''
  const webReasonId = useId()

  if (q.isLoading) {
    return (
      <SettingsSection title={i18nT('pages.settings.browserPanel.browsing')}>
        <SettingsCard>
          <FormSkeleton rows={['info', 'field']} />
        </SettingsCard>
      </SettingsSection>
    )
  }
  if (!data) {
    return (
      <SettingsSection title={i18nT('pages.settings.browserPanel.browsing')}>
        {/* Nothing to lose: no status has ever loaded, so no section and no token
            field has mounted. */}
        <ErrorNotice message={i18nT('pages.settings.browserPanel.cannot_load')} askAgent />
      </SettingsSection>
    )
  }

  // A refetch that failed keeps the last answer on screen, but that answer may be
  // stale, so it is marked as such and the install controls wait for a fresh one.
  const statusUnavailable = q.isRefetchError
  const activity = currentActivity(data, pending, pendingSince)
  const block = installBlock(activity, statusUnavailable, outcomeUnknown)
  const installBlockText = blockText(block)
  const engineBlockText =
    installBlockText ?? (data.installed ? null : i18nT('pages.settings.browserPanel.reason_cli_first'))

  // The POST itself was refused (403, 500, a lost answer). A 409 that named the
  // active job is not shown here: the status region already shows that job.
  const cliRequestError =
    installMut.isError && !browserInstallConflictJob(installMut.error)
      ? failText(installMut.error, i18nT('pages.settings.browserPanel.install_request_failed'))
      : null
  const engineRequestError =
    engineMut.isError && engineMut.variables && !browserInstallConflictJob(engineMut.error)
      ? {
          engine: engineMut.variables,
          message: failText(engineMut.error, i18nT('pages.settings.browserPanel.engine_download_failed')),
        }
      : null

  return (
    <>
      <BrowserInstallStatus
        activity={activity}
        finished={finishedJob(data)}
        legacyError={data.install_job === undefined ? data.last_error : null}
        receivedAt={q.dataUpdatedAt}
        statusError={statusUnavailable ? failText(q.error, i18nT('pages.settings.browserPanel.cannot_load')) : null}
        allowHandoff={allowHandoff}
      />

      <ManagedBrowsersSection
        data={data}
        hostName={hostQ.data ?? null}
        hostError={hostError}
        activity={activity}
        installBlockText={installBlockText}
        engineBlockText={engineBlockText}
        onInstallCli={() => installMut.mutate()}
        onDownload={(engine) => engineMut.mutate(engine)}
        cliRequestError={cliRequestError}
        engineRequestError={engineRequestError}
        allowHandoff={allowHandoff}
      />

      <ConnectBrowserSection
        tokenStored={data.token}
        draft={token}
        onDraftChange={setToken}
        onSave={() => tokenMut.mutate(token)}
        onClear={() => { setToken(''); tokenMut.mutate('') }}
        saving={tokenMut.isPending}
        saveError={
          tokenMut.isError ? failText(tokenMut.error, i18nT('pages.settings.browserPanel.token_save_failed')) : null
        }
      />

      {/*
        The native panel is a desktop-app-only Electron view, so off the desktop
        the switch is force-disabled and reads OFF, and the reason sits OUTSIDE
        the dimmed row where it stays readable. When ON (desktop), the browser
        tool drives the built-in panel; OFF falls back to playwright-cli.
      */}
      <SettingsSection title={i18nT('pages.settings.browserPanel.desktop_title')}>
        <SettingsCard>
          <SettingsToggle
            label={i18nT('pages.settings.browserPanel.use_builtin_label')}
            configKey="dashboard.use_builtin_browser"
            description={isElectron ? i18nT('pages.settings.browserPanel.use_builtin_desc') : undefined}
            describedBy={isElectron ? undefined : webReasonId}
            checked={isElectron ? (dashQ.data?.use_builtin_browser ?? true) : false}
            onChange={setUseBuiltin}
            disabled={!isElectron || !dashQ.isSuccess || dashMut.isPending}
          />
          {!isElectron && (
            <p id={webReasonId} className="text-[13px] text-muted m-0">
              {i18nT('pages.settings.browserPanel.desktop_web_explains')}
            </p>
          )}
          {/* Desktop only, like the switch itself. No hand-off: the extension
              token draft shares this panel, and the navigation unmounts it. */}
          {isElectron && dashQ.isError && (
            <ErrorNotice
              variant="inline"
              className="mt-2"
              message={failText(dashQ.error, i18nT('pages.settings.browserPanel.builtin_status_unavailable'))}
            />
          )}
          {/* No hand-off: same token draft. */}
          {dashMut.isError && (
            <ErrorNotice
              variant="inline"
              className="mt-2"
              message={failText(dashMut.error, i18nT('pages.settings.browserPanel.builtin_save_failed'))}
            />
          )}
        </SettingsCard>
      </SettingsSection>
    </>
  )
}
