import { useState, type ReactNode } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Download, Loader2, ShieldCheck } from 'lucide-react'

import { api } from '../../api/client'
import type { AgentDesktopDriver, AgentDesktopPermission, AgentDesktopStatus } from '../../api/client'
import ErrorNotice from '../../components/ErrorNotice'
import { SettingsCard, SettingsSection } from '../../components/settings'
import { Badge, Btn, FormSkeleton } from '../../components/ui'
import { i18nT } from '../../i18n/t'
import {
  failedJob,
  installAction,
  jobRunning,
  permissionsIncomplete,
  permissionVariant,
  statusPollInterval,
  versionState,
} from './computerUseInstallState'

export const AGENT_DESKTOP_QK = ['computer-use-agent-desktop']

/** Catalog KEYS, as full literals so the key checks resolve them. Keys rather
 *  than strings: a module-level `i18nT()` would freeze the boot language. */
const DRIVER_KEY: Record<AgentDesktopDriver, string> = {
  'agent-desktop': 'pages.settings.computerUseInstall.driver_agent_desktop',
  native: 'pages.settings.computerUseInstall.driver_native',
  unsupported: 'pages.settings.computerUseInstall.driver_unsupported',
}

const PERM_KEY: Record<AgentDesktopPermission, string> = {
  granted: 'pages.settings.computerUseInstall.perm_granted',
  denied: 'pages.settings.computerUseInstall.perm_denied',
  unknown: 'pages.settings.computerUseInstall.perm_unknown',
}

const ACTION_KEY = {
  install: 'pages.settings.computerUseInstall.install',
  update: 'pages.settings.computerUseInstall.update',
  reinstall: 'pages.settings.computerUseInstall.reinstall',
} as const

/** A wire token this build does not know renders as itself rather than blank.
 *  `hasOwnProperty`, not `in`, so `toString` off the wire is not a key. */
function label(table: Record<string, string>, token: string): string {
  return Object.prototype.hasOwnProperty.call(table, token) ? i18nT(table[token]) : token
}

function Row({ name, children }: { name: string; children: ReactNode }) {
  return (
    <div className="flex items-center justify-between gap-3 py-1.5">
      <span className="text-[13px] text-text">{name}</span>
      <span className="flex min-w-0 items-center gap-2 text-[13px]">{children}</span>
    </div>
  )
}

function versionBadge(data: AgentDesktopStatus) {
  const v = versionState(data)
  if (v === 'current') return <Badge variant="ok">{i18nT('pages.settings.computerUseInstall.version_current')}</Badge>
  if (v === 'outdated') return <Badge variant="warn">{i18nT('pages.settings.computerUseInstall.version_outdated')}</Badge>
  if (v === 'missing') return <Badge variant="muted">{i18nT('pages.settings.computerUseInstall.not_installed')}</Badge>
  return <Badge variant="muted">{i18nT('pages.settings.computerUseInstall.version_unknown')}</Badge>
}

/**
 * Settings → Computer Use → Desktop driver.
 *
 * Shows whether the agent-desktop driver is installed, its version against the
 * pinned one, which driver is active, and (on macOS) the two TCC grants. Offers
 * Install / Update and the OS permission guide. While an install job runs, the
 * status is polled and the installer's log tail is shown, like the Browser card.
 *
 * No error here hands off to the agent: the surrounding panel holds numeric
 * drafts that only commit on blur, and the hand-off navigation would lose them.
 */
export function ComputerUseInstallStatus({ platform, index }: { platform: string; index?: number }) {
  const qc = useQueryClient()
  const [actionError, setActionError] = useState('')
  const [guideOpened, setGuideOpened] = useState(false)

  const installMut = useMutation({
    mutationFn: () => api.installAgentDesktop(),
    onMutate: () => setActionError(''),
    onSuccess: data => {
      if (data && typeof data === 'object' && 'job' in data) qc.setQueryData(AGENT_DESKTOP_QK, data)
    },
    onError: () => setActionError(i18nT('pages.settings.computerUseInstall.install_request_failed')),
    // A 409 (a job already running) lands here too; the re-read shows that job.
    onSettled: () => qc.invalidateQueries({ queryKey: AGENT_DESKTOP_QK }),
  })

  const guideMut = useMutation({
    mutationFn: () => api.openAgentDesktopPermissionsGuide(),
    onMutate: () => {
      setActionError('')
      setGuideOpened(false)
    },
    onSuccess: () => setGuideOpened(true),
    onError: () => setActionError(i18nT('pages.settings.computerUseInstall.guide_failed')),
    // The user may have granted something while the guide was open.
    onSettled: () => qc.invalidateQueries({ queryKey: AGENT_DESKTOP_QK }),
  })

  const pending = installMut.isPending
  const q = useQuery<AgentDesktopStatus>({
    queryKey: AGENT_DESKTOP_QK,
    queryFn: api.getAgentDesktopStatus,
    staleTime: 0,
    refetchOnWindowFocus: true,
    refetchInterval: query => statusPollInterval(query.state.data, pending),
  })

  const title = i18nT('pages.settings.computerUseInstall.title')
  const data = q.data

  if (!data) {
    return (
      <SettingsSection title={title}>
        <SettingsCard index={index}>
          {q.isError ? (
            <div className="flex flex-wrap items-center gap-3" data-testid="agent-desktop-status-error">
              {/* No hand-off: the panel's numeric drafts would be lost to the navigation. */}
              <ErrorNotice
                className="flex-1 min-w-[16rem]"
                message={i18nT('pages.settings.computerUseInstall.status_failed')}
              />
              <Btn onClick={() => q.refetch()}>{i18nT('pages.settings.computerUseInstall.retry')}</Btn>
            </div>
          ) : (
            <FormSkeleton rows={['field', 'field']} />
          )}
        </SettingsCard>
      </SettingsSection>
    )
  }

  const running = jobRunning(data, pending)
  const action = installAction(data)
  const failure = failedJob(data, pending)
  const showPerms = platform === 'macos'
  const logTail = running ? data.job?.log_tail ?? '' : ''

  return (
    <SettingsSection title={title}>
      <SettingsCard index={index}>
        <section aria-label={i18nT('pages.settings.computerUseInstall.status_region_label')} data-testid="agent-desktop-status">
          <Row name={i18nT('pages.settings.computerUseInstall.driver_label')}>
            <Badge variant={data.driver_active === 'agent-desktop' ? 'ok' : 'muted'}>
              {label(DRIVER_KEY, data.driver_active)}
            </Badge>
          </Row>
          <Row name={i18nT('pages.settings.computerUseInstall.installed_label')}>
            {data.installed && data.version && <span className="font-mono text-muted">{data.version}</span>}
            {versionBadge(data)}
          </Row>
          <Row name={i18nT('pages.settings.computerUseInstall.pinned_label')}>
            <span className="font-mono text-muted">{data.pinned}</span>
          </Row>
          {data.path && (
            <Row name={i18nT('pages.settings.computerUseInstall.path_label')}>
              <span className="truncate font-mono text-muted" title={data.path}>{data.path}</span>
            </Row>
          )}
          {showPerms && (
            <>
              <Row name={i18nT('pages.settings.computerUseInstall.accessibility')}>
                <Badge variant={permissionVariant(data.permissions?.accessibility)}>
                  {label(PERM_KEY, data.permissions?.accessibility ?? 'unknown')}
                </Badge>
              </Row>
              <Row name={i18nT('pages.settings.computerUseInstall.screen_recording')}>
                <Badge variant={permissionVariant(data.permissions?.screen_recording)}>
                  {label(PERM_KEY, data.permissions?.screen_recording ?? 'unknown')}
                </Badge>
              </Row>
            </>
          )}

          {running && (
            <div className="mt-2 flex flex-col gap-1.5" data-testid="agent-desktop-running">
              <div role="status" className="flex items-center gap-2 text-[13px] font-medium">
                <Loader2 size={16} className="text-accent shrink-0 animate-spin" aria-hidden="true" />
                {i18nT('pages.settings.computerUseInstall.installing')}
              </div>
              {/* Outside the live region: each poll would otherwise be read aloud. */}
              {logTail && (
                <pre
                  aria-label={i18nT('pages.settings.computerUseInstall.log_tail_label')}
                  className="max-h-40 overflow-auto rounded-md border border-border bg-bg px-2 py-1.5 font-mono text-[12px] text-muted whitespace-pre-wrap break-words"
                  data-testid="agent-desktop-log"
                >
                  {logTail}
                </pre>
              )}
            </div>
          )}

          {failure && (
            /* No hand-off: the panel's numeric drafts would be lost to the navigation.
               The detail is the installer's own redacted output, shown so it is actionable. */
            <ErrorNotice
              className="mt-2"
              title={i18nT('pages.settings.computerUseInstall.install_failed')}
              message={
                [failure.error, failure.logTail].filter(Boolean).join('\n\n') ||
                i18nT('pages.settings.computerUseInstall.failure_no_detail')
              }
              messagePlacement="below"
              messageClassName="font-mono whitespace-pre-wrap break-words"
              testId="agent-desktop-failed"
            />
          )}

          {/* No hand-off: the panel's numeric drafts would be lost to the navigation. */}
          <ErrorNotice message={actionError || null} className="mt-2" />

          {guideOpened && (
            <div className="mt-2 text-[13px] text-muted">{i18nT('pages.settings.computerUseInstall.guide_opened')}</div>
          )}

          {(action || (showPerms && permissionsIncomplete(data))) && (
            <div className="mt-2 flex flex-wrap items-center gap-2">
              {action && (
                <Btn primary onClick={() => installMut.mutate()} disabled={running}>
                  <Download className="lucide-inline" aria-hidden="true" />
                  {i18nT(ACTION_KEY[action])}
                </Btn>
              )}
              {showPerms && permissionsIncomplete(data) && (
                <Btn onClick={() => guideMut.mutate()} disabled={guideMut.isPending}>
                  <ShieldCheck className="lucide-inline" aria-hidden="true" />
                  {i18nT('pages.settings.computerUseInstall.open_permission_guide')}
                </Btn>
              )}
            </div>
          )}
        </section>
      </SettingsCard>
    </SettingsSection>
  )
}
