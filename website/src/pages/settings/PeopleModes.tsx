/** The mode-specific pieces of Settings > People (`api/sharedCrewMode`).
 *
 *  Each mode has its own surface and they never mix:
 *  - invalid: one error state naming the refused team setup's reason code;
 *  - console: read-only Team Access (people seen signing in, labelled as seen)
 *    and a server-validated Console link; no share, Remote Control or
 *    Tailscale setup surface;
 *  - network: each network adapter read-only, pointing to where that network
 *    manages access, plus personal Remote Control;
 *  - personal: the setup guidance plus personal Remote Control.
 *
 *  Nothing here invites, revokes or assigns roles, and no link is taken from a
 *  peer or a config value: the Console URL is validated by the server and again
 *  here, and the other links are fixed public documentation pages. */
import { ExternalLink, Smartphone } from 'lucide-react'
import type { SetupPath, SetupPathCapability } from '../../api/sharedCrew'
import { consoleUrl, type CrewModeResponse } from '../../api/sharedCrewMode'
import { SETUP_PATH_ORDER } from '../../api/sharedCrewUi'
import { SettingsCard, SettingsSection } from '../../components/settings'
import { Badge, Btn } from '../../components/ui'
import { MobileConnectDialog, useMobileConnect } from '../../components/mobileConnect'
import ErrorNotice from '../../components/ErrorNotice'
import { i18nT } from '../../i18n/t'

/** Fixed public documentation pages, never configurable. */
export const TAILSCALE_SHARING_DOCS_URL = 'https://tailscale.com/kb/1084/sharing'
export const SESSION_MANAGER_DOCS_URL = 'https://docs.aws.amazon.com/systems-manager/latest/userguide/session-manager.html'

export function TeamConfigInvalid({ mode }: { mode: CrewModeResponse }) {
  const reason = mode.team_config.reason || 'invalid'
  return (
    <>
      {/* No hand-off: People also holds unsaved invite emails, name, picture and API key. */}
      <ErrorNotice
        title={i18nT('sharedCrew.mode_invalid_title')}
        message={i18nT('sharedCrew.mode_invalid_body')}
        footer={<p className="text-[12px] font-mono break-all" data-testid="team-config-reason">{i18nT('sharedCrew.mode_invalid_reason', { reason })}</p>}
        testId="people-team-config-invalid"
      />
    </>
  )
}

function statusBadge(row: SetupPathCapability | undefined) {
  const state = row?.state ?? 'unavailable'
  if (state === 'available') return <Badge variant="ok">{i18nT('sharedCrew.setup_status_available')}</Badge>
  if (state === 'degraded') return <Badge variant="warn">{i18nT('sharedCrew.setup_status_degraded')}</Badge>
  if (row?.reason === 'not_in_this_build') return <Badge variant="muted">{i18nT('sharedCrew.setup_status_not_in_build')}</Badge>
  return <Badge variant="muted">{i18nT('sharedCrew.setup_status_not_set_up')}</Badge>
}

function adapterName(path: SetupPath): string {
  switch (path) {
    case 'tailscale': return i18nT('sharedCrew.setup_tailscale')
    case 'kiro_login': return i18nT('sharedCrew.setup_kiro_login')
    default: return i18nT('sharedCrew.setup_systems_manager')
  }
}

function adapterBody(path: SetupPath): string {
  switch (path) {
    case 'tailscale': return i18nT('sharedCrew.adapter_tailscale_body')
    case 'kiro_login': return i18nT('sharedCrew.setup_kiro_login_body')
    default: return i18nT('sharedCrew.adapter_ssm_body')
  }
}

function DocLink({ href, label, testId }: { href: string; label: string; testId: string }) {
  return (
    <a className="inline-flex items-center gap-1 text-[13px] text-accent self-start" href={href} target="_blank" rel="noopener noreferrer" data-testid={testId}>
      {label}
      <ExternalLink size={12} className="lucide-inline" aria-hidden />
    </a>
  )
}

/** Network mode: every adapter, read-only, with its honest status. */
export function NetworkAdapters({ adapters }: { adapters: CrewModeResponse['adapters'] }) {
  return (
    <SettingsSection title={i18nT('sharedCrew.adapters_title')}>
      <SettingsCard>
        <ul className="flex flex-col divide-y divide-border" aria-label={i18nT('sharedCrew.adapters_title')}>
          {SETUP_PATH_ORDER.map(path => (
            <li key={path} className="flex flex-col gap-1 py-2.5 min-w-0" data-testid={`adapter-${path}`} data-state={adapters[path]?.state ?? 'unavailable'}>
              <div className="flex items-center gap-2 min-w-0 flex-wrap">
                <span className="text-sm font-medium text-text-strong">{adapterName(path)}</span>
                {statusBadge(adapters[path])}
              </div>
              <p className="text-[13px] text-muted">{adapterBody(path)}</p>
              {path === 'tailscale' && <DocLink href={TAILSCALE_SHARING_DOCS_URL} label={i18nT('sharedCrew.adapter_tailscale_docs')} testId="adapter-tailscale-docs" />}
              {path === 'systems_manager' && <DocLink href={SESSION_MANAGER_DOCS_URL} label={i18nT('sharedCrew.adapter_ssm_docs')} testId="adapter-ssm-docs" />}
            </li>
          ))}
        </ul>
      </SettingsCard>
    </SettingsSection>
  )
}

/** Console mode: where team access is managed. */
export function ConsoleAccess({ mode }: { mode: CrewModeResponse }) {
  const url = consoleUrl(mode)
  return url ? (
    <DocLink href={url} label={i18nT('sharedCrew.console_link')} testId="console-link" />
  ) : (
    <p className="text-[13px] text-muted" data-testid="console-link-missing">{i18nT('sharedCrew.console_link_missing')}</p>
  )
}

/** Personal and network modes: the operator's own phone connection. */
export function RemoteControlCard() {
  const mobile = useMobileConnect('settings-people')
  const query = mobile.mobileConnectQuery
  return (
    <SettingsSection title={i18nT('sharedCrew.remote_control_title')}>
      <SettingsCard>
        <div className="flex flex-col gap-2 min-w-0" data-testid="remote-control-setup">
          <p className="text-[13px] text-muted">{i18nT('sharedCrew.remote_control_body')}</p>
          {query.isError && (
            <div>
              {/* No hand-off: People also holds unsaved invite emails, name, picture and API key. */}
              <ErrorNotice
                message={query.error instanceof Error ? query.error.message : i18nT('components.mobileConnect.could_not_check_remote_access')}
                testId="remote-control-error"
              />
              <Btn onClick={() => { void query.refetch() }}>{i18nT('components.mobileConnect.try_again')}</Btn>
            </div>
          )}
          {query.isPending && <p className="text-[12px] text-muted" role="status">{i18nT('components.mobileConnect.checking_remote_access')}</p>}
          {mobile.hasRenderableMobileConnect ? (
            <Btn className="self-start" onClick={() => mobile.setMobileConnectOpen(true)}>
              <Smartphone size={14} className="lucide-inline" aria-hidden /> {i18nT('sharedCrew.remote_control_open')}
            </Btn>
          ) : query.isSuccess ? (
            <p className="text-[12px] text-muted" data-testid="remote-control-unavailable">{i18nT('sharedCrew.remote_control_unavailable')}</p>
          ) : null}
        </div>
        <MobileConnectDialog mobileConnect={mobile} />
      </SettingsCard>
    </SettingsSection>
  )
}
