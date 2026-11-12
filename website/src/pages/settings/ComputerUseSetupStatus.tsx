import { SettingsSection, SettingsCard } from '../../components/settings'
import { ExternalLink } from 'lucide-react'
import { Badge, Btn, FormSkeleton } from '../../components/ui'
import InfoTip from '../../components/InfoTip'
import ErrorNotice from '../../components/ErrorNotice'
import type { ComputerUseSetupDoc } from '../../api/client'
import { i18nT } from '../../i18n/t'
import { useComputerUseSetupAction, useComputerUseSetupQuery } from '../../hooks/useComputerUseSetup'
import { blockedKey, openSystemSettings, problemText, readinessFor, sessionKindKey, type Readiness, type SetupPane } from './computerUseSetup'

const READINESS_KEY: Record<Readiness, string> = {
  off: 'pages.settings.computerUseSetup.readiness_off',
  ready: 'pages.settings.computerUseSetup.readiness_ready',
  ready_builtin: 'pages.settings.computerUseSetup.readiness_ready_builtin',
  needs_permission: 'pages.settings.computerUseSetup.readiness_needs_permission',
  needs_setup: 'pages.settings.computerUseSetup.readiness_needs_setup',
  attention: 'pages.settings.computerUseSetup.readiness_attention',
  unavailable: 'pages.settings.computerUseSetup.readiness_unavailable',
}
const READINESS_VARIANT: Record<Readiness, 'ok' | 'warn' | 'err' | 'muted'> = {
  off: 'muted',
  ready: 'ok',
  ready_builtin: 'ok',
  needs_permission: 'warn',
  needs_setup: 'warn',
  attention: 'err',
  unavailable: 'muted',
}

const PERM_KEY: Record<ComputerUseSetupDoc['permissions']['accessibility'], string> = {
  granted: 'pages.settings.computerUseSetup.perm_granted',
  missing: 'pages.settings.computerUseSetup.perm_missing',
  unknown: 'pages.settings.computerUseSetup.perm_unknown',
  not_applicable: 'pages.settings.computerUseSetup.perm_not_applicable',
}

const CAPABILITY_KEY: Record<keyof ComputerUseSetupDoc['capabilities'], string> = {
  observe_tree: 'pages.settings.computerUseSetup.cap_observe_tree',
  screenshot: 'pages.settings.computerUseSetup.cap_screenshot',
  background_input: 'pages.settings.computerUseSetup.cap_background_input',
  foreground_input: 'pages.settings.computerUseSetup.cap_foreground_input',
  window_capture: 'pages.settings.computerUseSetup.cap_window_capture',
}

function permLabel(value: string): string {
  return i18nT(Object.prototype.hasOwnProperty.call(PERM_KEY, value) ? PERM_KEY[value as keyof typeof PERM_KEY] : PERM_KEY.unknown)
}

function driverLabel(doc: ComputerUseSetupDoc): string {
  if (doc.driver.kind === 'native') return i18nT('pages.settings.computerUseSetup.driver_builtin')
  if (!doc.driver.installed) return i18nT('pages.settings.computerUseSetup.driver_not_installed')
  if (!doc.driver.healthy) return i18nT('pages.settings.computerUseSetup.driver_not_responding')
  return i18nT('pages.settings.computerUseSetup.driver_installed', { version: doc.driver.version || '?' })
}

function Row({ label, children, testId }: { label: string; children: React.ReactNode; testId?: string }) {
  return (
    <div className="flex items-center justify-between gap-3 py-1.5" data-testid={testId}>
      <span className="text-[13px] text-text">{label}</span>
      <span className="flex min-w-0 items-center gap-2 text-right text-[13px] text-muted">{children}</span>
    </div>
  )
}

/**
 * Settings -> Computer Use, the setup status card: what is ready at a glance,
 * one primary action for whatever is missing, restart for a driver that is
 * installed, and diagnostics folded away.
 *
 * Reads the shared setup document once; the dialog it opens does the polling.
 */
export function ComputerUseSetupStatus({
  onOpenSetup,
  part = 'all',
}: {
  onOpenSetup: () => void
  /** `summary`: the status row, the user action and the one primary button, for
   *  the panel's main card. `details`: the rows, Restart driver / Run check and
   *  the diagnostics, for More. `all` (default): both, in their own section. */
  part?: 'all' | 'summary' | 'details'
}) {
  const setupQ = useComputerUseSetupQuery()
  const actionMut = useComputerUseSetupAction()
  const doc = setupQ.data

  const wrap = (body: React.ReactNode) =>
    part === 'all' ? (
      <SettingsSection title={i18nT('pages.settings.computerUseSetup.status_title')}>
        <SettingsCard index={1}>{body}</SettingsCard>
      </SettingsSection>
    ) : (
      <>{body}</>
    )
  if (setupQ.isError) {
    if (part === 'details') return null
    return wrap(
      <div className="flex flex-wrap items-center gap-3">
        <ErrorNotice className="flex-1 min-w-[16rem]" askAgent message={i18nT('pages.settings.computerUseSetup.status_load_error')} />
        <Btn onClick={() => setupQ.refetch()}>{i18nT('pages.settings.computerUseSetup.check_again')}</Btn>
      </div>,
    )
  }
  if (!doc) {
    return part === 'details' ? null : wrap(<FormSkeleton rows={['field']} />)
  }
  const summary = part !== 'details'
  const details = part !== 'summary'

  const readiness = readinessFor(doc)
  const blockedTextKey = blockedKey(doc)
  const blocked = blockedTextKey ? i18nT(blockedTextKey, { how: doc.blocked ?? '' }) : null
  const problem = problemText(doc, blocked)
  const macos = doc.platform === 'macos'
  const desktop = doc.platform === 'windows' || doc.platform === 'linux'
  const primaryKey =
    readiness === 'off'
      ? 'pages.settings.computerUseSetup.set_up_action'
      : readiness === 'needs_setup' || readiness === 'attention'
        ? 'pages.settings.computerUseSetup.continue_setup'
        : null
  const canRestart = doc.driver.installed && doc.driver.kind === 'upstream' && doc.state !== 'disabled'

  return wrap(
        <div data-testid="cu-setup-status" data-state={doc.state} data-part={part}>
          {summary && (
            <>
          <Row label={i18nT('pages.settings.computerUseSetup.row_status')}>
            <Badge variant={READINESS_VARIANT[readiness]}>{i18nT(READINESS_KEY[readiness])}</Badge>
          </Row>
          {problem ? (
            <div className="pt-1" data-testid="cu-status-problem">
              <ErrorNotice variant="inline" askAgent message={problem} />
            </div>
          ) : blocked ? (
            <p className="m-0 pt-1 text-[13px] text-text" data-testid="cu-status-blocked">{blocked}</p>
          ) : (
            doc.user_action && readiness !== 'ready' && readiness !== 'off' && (
              <p className="m-0 pt-1 text-[13px] text-text" data-testid="cu-status-user-action">{doc.user_action}</p>
            )
          )}
              <div className="flex flex-wrap items-center gap-2 pt-2">
            {primaryKey && (
              <Btn primary onClick={onOpenSetup} data-testid="cu-open-setup">
                {i18nT(primaryKey)}
              </Btn>
            )}
              </div>
            </>
          )}
          {details && (
            <>
          <Row label={i18nT('pages.settings.computerUseSetup.row_os')}>
            {i18nT(readiness === 'unavailable' ? 'pages.settings.computerUseSetup.os_unsupported' : 'pages.settings.computerUseSetup.os_supported')}
          </Row>
          <Row label={i18nT('pages.settings.computerUseSetup.row_driver')} testId="cu-status-driver">
            {driverLabel(doc)}
          </Row>
          {macos && (
            <>
              <Row label={i18nT('pages.settings.computerUseSetup.row_accessibility')} testId="cu-status-accessibility">
                <Badge variant={doc.permissions.accessibility === 'granted' ? 'ok' : doc.permissions.accessibility === 'missing' ? 'warn' : 'muted'}>
                  {permLabel(doc.permissions.accessibility)}
                </Badge>
              </Row>
              <Row label={i18nT('pages.settings.computerUseSetup.row_screen_recording')} testId="cu-status-screen-recording">
                <Badge variant={doc.permissions.screen_recording === 'granted' ? 'ok' : doc.permissions.screen_recording === 'missing' ? 'warn' : 'muted'}>
                  {permLabel(doc.permissions.screen_recording)}
                </Badge>
              </Row>
              {doc.driver.kind === 'native' && (
                <>
                  <div className="flex items-center gap-1.5 pb-1 text-[12px] text-muted">
                    <span>{i18nT('pages.settings.computerUsePanel.advisory_only')}</span>
                    <InfoTip text={i18nT('pages.settings.computerUsePanel.not_detected_does_not_always_mean_unavailable_ma')} />
                  </div>
                  {/* The native driver's grants have no setup step: offer the shortcut here, on a click only. */}
                  {(['accessibility', 'screen_recording'] as SetupPane[]).some(p => doc.permissions[p] === 'missing') && (
                    <div className="flex flex-wrap gap-2 pb-1">
                      {(['accessibility', 'screen_recording'] as SetupPane[])
                        .filter(p => doc.permissions[p] === 'missing')
                        .map(p => (
                          <Btn key={p} onClick={() => openSystemSettings(p)} data-testid={`cu-status-open-${p}`}>
                            {i18nT(p === 'accessibility' ? 'pages.settings.computerUseSetup.open_accessibility' : 'pages.settings.computerUseSetup.open_screen_recording')}
                            <ExternalLink size={13} aria-hidden="true" />
                          </Btn>
                        ))}
                    </div>
                  )}
                </>
              )}
            </>
          )}
          {desktop && (
            <Row label={i18nT('pages.settings.computerUseSetup.row_session')} testId="cu-status-session">
              {i18nT(sessionKindKey(doc.session.kind))}
            </Row>
          )}
          {doc.limitations.length > 0 && (
            <div className="pt-1 text-[13px]">
              <p className="m-0 text-text">{i18nT('pages.settings.computerUseSetup.limitations_title')}</p>
              <ul className="m-0 list-disc pl-5 text-muted">
                {doc.limitations.map(item => <li key={item}>{item}</li>)}
              </ul>
            </div>
          )}
              <div className="flex flex-wrap items-center gap-2 pt-2">
            {canRestart && (
              <Btn onClick={() => actionMut.mutate('restart_driver')} disabled={actionMut.isPending} busy={actionMut.isPending && actionMut.variables === 'restart_driver'}>
                {i18nT('pages.settings.computerUseSetup.restart_action')}
              </Btn>
            )}
            {/* Run check exercises the upstream driver; the native driver has no such check. */}
            {(readiness === 'ready' || readiness === 'ready_builtin' || doc.problem?.code === 'verify_failed') && doc.driver.kind === 'upstream' && (
              <Btn onClick={() => actionMut.mutate('verify')} disabled={actionMut.isPending} busy={actionMut.isPending && actionMut.variables === 'verify'}>
                {i18nT('pages.settings.computerUseSetup.verify_action')}
              </Btn>
            )}
              </div>
              {canRestart && (
                <p className="m-0 pt-1 text-[12px] text-muted" data-testid="cu-restart-hint">{i18nT('pages.settings.computerUseSetup.restart_hint')}</p>
              )}
              {/* Restart driver and Run check are this instance's mutation, so its failure shows here. */}
              <ErrorNotice variant="inline" askAgent message={actionMut.isError ? i18nT('pages.settings.computerUseSetup.action_failed') : null} />
              {part === 'all' && readiness !== 'off' && readiness !== 'unavailable' && (
                <p className="m-0 pt-1 text-[12px] text-muted">{i18nT('pages.settings.computerUseSetup.turn_off_hint')}</p>
              )}
          <details className="pt-2 text-[12px] text-muted" data-testid="cu-status-diagnostics">
            <summary className="cursor-pointer select-none text-[13px] text-text">{i18nT('pages.settings.computerUseSetup.diagnostics_title')}</summary>
            <p className="m-0 py-1">{i18nT('pages.settings.computerUseSetup.diag_hint')}</p>
            <dl className="m-0 grid grid-cols-[max-content_1fr] gap-x-3 gap-y-0.5">
              <dt>{i18nT('pages.settings.computerUseSetup.diag_state')}</dt>
              <dd className="m-0 font-mono" translate="no">{doc.state}</dd>
              <dt>{i18nT('pages.settings.computerUseSetup.diag_platform')}</dt>
              <dd className="m-0 font-mono" translate="no">{doc.platform}</dd>
              <dt>{i18nT('pages.settings.computerUseSetup.row_driver')}</dt>
              <dd className="m-0 font-mono" translate="no">{[doc.driver.kind, doc.driver.version].filter(Boolean).join(' ')}</dd>
              <dt>{i18nT('pages.settings.computerUseSetup.row_session')}</dt>
              <dd className="m-0 font-mono" translate="no">{[doc.session.kind, doc.session.detail].filter(Boolean).join(': ')}</dd>
              {(Object.keys(CAPABILITY_KEY) as (keyof ComputerUseSetupDoc['capabilities'])[]).map(key => (
                <div key={key} className="contents">
                  <dt>{i18nT(CAPABILITY_KEY[key])}</dt>
                  <dd className="m-0">{i18nT(doc.capabilities[key] ? 'pages.settings.computerUseSetup.yes' : 'pages.settings.computerUseSetup.no')}</dd>
                </div>
              ))}
            </dl>
          </details>
            </>
          )}
        </div>,
  )
}
