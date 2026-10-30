/** The mode-specific pieces of Settings > People (`api/sharedCrewMode`).
 *
 *  Each mode has its own surface and they never mix:
 *  - invalid: one error state naming the refused team setup's reason code;
 *  - console: read-only Team Access (people seen signing in, labelled as seen)
 *    and a server-validated Console link; no share, Remote Control or
 *    Tailscale setup surface;
 *  - network: the share card, the People list and personal Remote Control
 *    (`PeopleCrew.tsx`);
 *  - personal: the setup guidance plus personal Remote Control.
 *
 *  No link is taken from a peer or a config value: the Console URL is validated
 *  by the server and again here, and the other links are fixed public pages. */
import { ExternalLink, Smartphone } from 'lucide-react'
import type { SetupPathCapability } from '../../api/sharedCrew'
import { consoleUrl, type CrewModeResponse } from '../../api/sharedCrewMode'
import { SettingsCard, SettingsSection } from '../../components/settings'
import { Badge, Btn } from '../../components/ui'
import { MobileConnectDialog, useMobileConnect } from '../../components/mobileConnect'
import ErrorNotice from '../../components/ErrorNotice'
import InfoTip from '../../components/InfoTip'
import { i18nT } from '../../i18n/t'

/** The one fixed, public page where a tailnet's users are managed. Removing a
 *  person from this Crew is removing them there. */
export const TAILSCALE_USERS_URL = 'https://login.tailscale.com/admin/users'

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

/** A setup path's honest status, as a badge. */
export function statusBadge(row: SetupPathCapability | undefined) {
  const state = row?.state ?? 'unavailable'
  if (state === 'available') return <Badge variant="ok">{i18nT('sharedCrew.setup_status_available')}</Badge>
  if (state === 'degraded') return <Badge variant="warn">{i18nT('sharedCrew.setup_status_degraded')}</Badge>
  if (row?.reason === 'not_in_this_build') return <Badge variant="muted">{i18nT('sharedCrew.setup_status_not_in_build')}</Badge>
  return <Badge variant="muted">{i18nT('sharedCrew.setup_status_not_set_up')}</Badge>
}

/** An ⓘ for a heading: the section's longer explanation lives here, so the
 *  heading keeps at most one short visible line. Passed as `SettingsSection`'s
 *  `badge`, or set beside a row's name. */
export function headingTip(text: string, testId?: string) {
  return <span className="inline-flex align-middle" data-testid={testId}><InfoTip text={text} /></span>
}

function DocLink({ href, label, testId }: { href: string; label: string; testId: string }) {
  return (
    <a className="inline-flex items-center gap-1 text-[13px] text-accent self-start" href={href} target="_blank" rel="noopener noreferrer" data-testid={testId}>
      {label}
      <ExternalLink size={12} className="lucide-inline" aria-hidden />
    </a>
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
    <SettingsSection title={i18nT('sharedCrew.remote_control_title')} badge={headingTip(i18nT('sharedCrew.remote_control_body'), 'remote-control-tip')}>
      <SettingsCard>
        <div className="flex flex-col gap-2 min-w-0" data-testid="remote-control-setup">
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
            <Btn className="self-start" onClick={() => mobile.setMobileConnectOpen(true)} aria-label={i18nT('sharedCrew.remote_control_open')}>
              <Smartphone size={14} className="lucide-inline" aria-hidden /> {i18nT('sharedCrew.remote_control_setup')}
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
