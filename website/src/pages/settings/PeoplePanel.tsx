/** Settings > People: who this Crew has seen, kept apart from any authoritative
 *  assigned-user directory, each row labelled with where its evidence came from
 *  and, when the Crew can tell, how much work that person has active and queued.
 *
 *  Access to a Crew is decided by the network and the sign-in in front of it.
 *  The in-Crew controls are the owner's Tailscale invite card
 *  (`TailscaleInvites.tsx`), which shares this machine through Tailscale and
 *  admits the login that accepted, and the access card (`TailscaleAccess.tsx`),
 *  where the owner approves a same-tailnet person who is waiting. The set of
 *  people who get in is computed, never typed; there is no role or admin control, and no
 *  management URL is ever taken from a peer or from config. An enterprise Crew
 *  points to its console only when its capabilities say one is configured.
 *
 *  The surface follows the Crew's mode (`GET /api/shared-crew/mode`), and the
 *  modes never mix (`PeopleModes.tsx`): a personal Crew shows the setup empty
 *  state (`SharedAccessSetup`) and personal Remote Control; a network Crew shows
 *  who has been seen, each network adapter read-only and Remote Control; a
 *  Console Crew shows read-only Team Access and its Console link only; an
 *  invalid team setup shows an error, never the personal surface. */
import { ExternalLink, Users } from 'lucide-react'
import { SettingsCard, SettingsSection } from '../../components/settings'
import { Badge, ContentSkeleton, EmptyState } from '../../components/ui'
import ErrorNotice from '../../components/ErrorNotice'
import { capabilityUsable, channelName, membershipPointer, othersSeenIn, principalInitials, principalName, principalVia, sharedAccessConfigured, type PeopleRow, type PeopleSource } from '../../api/sharedCrewUi'
import { ChannelBrandIcon } from '../../components/ChannelBrandIcon'
import { SharedAccessSetup } from './SharedAccessSetup'
import { ConsoleAccess, NetworkAdapters, RemoteControlCard, TeamConfigInvalid } from './PeopleModes'
import { TailscaleInvites } from './TailscaleInvites'
import { TailscaleAccess } from './TailscaleAccess'
import { avatarUrl } from '../../api/sharedCrewInvites'
import { effectiveMode } from '../../api/sharedCrewMode'
import { useSharedCrewCapabilities, useSharedCrewMode, useSharedCrewPeople } from '../../hooks/sharedCrew/useSharedCrew'
import { fmtDateTime, fmtNumber } from '../../i18n/format'
import { i18nT } from '../../i18n/t'
import { TailnetDirectLink, tailnetDirectActive } from '../../components/TailnetDirectLink'

/** The one fixed, public page where a tailnet's users are managed. */
const TAILSCALE_USERS_URL = 'https://login.tailscale.com/admin/users'

function sourceLabel(source: PeopleSource): string {
  switch (source) {
    case 'directory': return i18nT('sharedCrew.source_directory')
    case 'session_creator': return i18nT('sharedCrew.source_session_creator')
    default: return i18nT('sharedCrew.source_observed')
  }
}

function PersonRow({ row, usageAvailable }: { row: PeopleRow; usageAvailable: boolean }) {
  const name = row.member?.alias || principalName(row.principal) || i18nT('sharedCrew.unattributed')
  const login = row.principal.display.login
  // A Slack person is attributed only: named on what they wrote, never admitted.
  const via = principalVia(row.principal)
  return (
    <li className="flex items-start gap-3 py-2 min-w-0" data-testid={`person-${row.principal.id}`}>
      {row.member?.avatar_id ? (
        <img src={avatarUrl(row.member.avatar_id)} alt="" className="w-8 h-8 rounded-full object-cover shrink-0" data-testid="person-avatar" />
      ) : (
        <span className="inline-flex items-center justify-center w-8 h-8 rounded-full bg-bg-hover text-text text-[12px] font-semibold shrink-0" aria-hidden>
          {principalInitials(row.principal)}
        </span>
      )}
      <div className="flex flex-col gap-0.5 min-w-0 flex-1">
        <div className="flex items-center gap-2 min-w-0 flex-wrap">
          <span className="text-sm font-medium text-text-strong truncate max-w-full" title={name}>{name}</span>
          {row.is_viewer && <Badge variant="muted">{i18nT('sharedCrew.you')}</Badge>}
          {row.principal.kind === 'machine' && <Badge variant="muted">{i18nT('sharedCrew.kind_machine')}</Badge>}
          {via && (
            <span
              className="inline-flex items-center gap-1"
              data-testid="person-via"
              data-via={via}
              title={i18nT('sharedCrew.people_via_channel_hint', { channel: channelName(via) })}
            >
              <ChannelBrandIcon channel={via} size={12} />
              <Badge variant="muted">{i18nT('sharedCrew.people_via_channel', { channel: channelName(via) })}</Badge>
              <span className="sr-only">{i18nT('sharedCrew.people_via_channel_hint', { channel: channelName(via) })}</span>
            </span>
          )}
          {row.sources.map(s => <Badge key={s} variant="aim">{sourceLabel(s)}</Badge>)}
        </div>
        {login && login !== name && <span className="text-[12px] text-muted truncate" title={login}>{login}</span>}
        <span className="text-[12px] text-muted">
          {row.last_seen_at ? i18nT('sharedCrew.last_seen', { when: fmtDateTime(row.last_seen_at * 1000) }) : i18nT('sharedCrew.not_seen_live')}
          {(row.sessions_created ?? 0) > 0 && (
            <><span aria-hidden>{'\u00A0·\u00A0'}</span>{i18nT('sharedCrew.sessions_created', { n: fmtNumber(row.sessions_created ?? 0) })}</>
          )}
        </span>
        {usageAvailable && row.usage && !via && (
          <span className="text-[12px] text-muted" data-testid="person-usage">
            {i18nT('sharedCrew.usage_line', { active: fmtNumber(row.usage.active), queued: fmtNumber(row.usage.queued) })}
          </span>
        )}
      </div>
    </li>
  )
}

export function PeoplePanel() {
  const { capabilities, isLoading: capsLoading } = useSharedCrewCapabilities()
  const people = useSharedCrewPeople(true)
  const modeQuery = useSharedCrewMode(true)
  const pointer = membershipPointer(capabilities.membership_link)
  if (capsLoading || people.isLoading || modeQuery.isLoading) return <ContentSkeleton rows={4} />
  if (people.isError) {
    return <ErrorNotice message={people.error instanceof Error ? people.error.message : i18nT('sharedCrew.people_failed')} askAgent testId="people-error" />
  }
  // An unreadable mode is an error, never a silent personal Crew.
  if (modeQuery.isError) {
    return <ErrorNotice message={modeQuery.error instanceof Error ? modeQuery.error.message : i18nT('sharedCrew.mode_failed')} askAgent testId="people-mode-error" />
  }
  // Someone other than the viewer already connected: sharing works, whatever the
  // setup paths say (Tailscale Serve set up by hand, for one). A Slack person is
  // attributed only and says nothing about a team path.
  const othersSeen = othersSeenIn(people.data?.people)
  const modeData = modeQuery.data ?? null
  const mode = effectiveMode(modeData, { teamPath: sharedAccessConfigured(capabilities), othersSeen })
  if (mode === 'invalid' && modeData) return <TeamConfigInvalid mode={modeData} />
  if (mode === 'personal') {
    return (
      <div className="flex flex-col gap-4" data-testid="people-personal">
        <SharedAccessSetup capabilities={capabilities} />
        <TailscaleAccess />
        <TailscaleInvites />
        <RemoteControlCard />
      </div>
    )
  }
  const data = people.data
  if (mode === 'console' && modeData) {
    const rows = data?.people ?? []
    return (
      <div className="flex flex-col gap-4" data-testid="people-console">
        <SettingsSection title={i18nT('sharedCrew.team_access_title')}>
          <SettingsCard>
            <p className="text-[13px] text-muted">{i18nT('sharedCrew.team_access_note')}</p>
            {rows.length === 0 ? (
              <p className="text-[13px] text-muted pt-2">{i18nT('sharedCrew.team_access_empty')}</p>
            ) : (
              <ul className="flex flex-col divide-y divide-border" aria-label={i18nT('sharedCrew.team_access_title')}>
                {rows.map(row => <PersonRow key={row.principal.id} row={row} usageAvailable={!!data?.usage_available} />)}
              </ul>
            )}
            <div className="pt-2"><ConsoleAccess mode={modeData} /></div>
          </SettingsCard>
        </SettingsSection>
      </div>
    )
  }
  const attributionReason = capabilities.reasons?.attribution
  const adapters = modeData?.adapters ?? capabilities.setup_paths ?? {}
  const direct = capabilities.tailnet_direct
  const networkExtras = (
    <>
      {tailnetDirectActive(direct) && (
        <SettingsSection title={i18nT('sharedCrew.direct_share_title')}>
          <SettingsCard>
            <TailnetDirectLink direct={direct} />
          </SettingsCard>
        </SettingsSection>
      )}
      <TailscaleAccess />
      <TailscaleInvites />
      <NetworkAdapters adapters={adapters} />
      <RemoteControlCard />
    </>
  )
  if (!data || !capabilityUsable(capabilities.attribution)) {
    const unnamed = attributionReason === 'platform_unprovable' || attributionReason === 'daemon_same_user'
    return (
      <div className="flex flex-col gap-4" data-testid="people-network-unattributed">
      <EmptyState
        icon={<Users size={20} />}
        title={i18nT(unnamed ? 'sharedCrew.people_unnamed_title' : 'sharedCrew.people_unavailable_title')}
        subtitle={i18nT(
          attributionReason === 'daemon_same_user' ? 'sharedCrew.people_unnamed_same_user_body'
            : unnamed ? 'sharedCrew.people_unnamed_platform_body'
              : 'sharedCrew.people_unavailable_body',
        )}
        testId={unnamed ? 'people-unnamed' : 'people-unavailable'}
      />
      {networkExtras}
      </div>
    )
  }
  const usageAvailable = !!data.usage_available
  const directoryOn = capabilityUsable(data.directory.state) && capabilityUsable(capabilities.authoritative_directory)
  return (
    <div className="flex flex-col gap-4" data-testid="people-panel">
      <SettingsSection title={i18nT('sharedCrew.people_seen_title')}>
        <SettingsCard>
          <p className="text-[13px] text-muted">{i18nT('sharedCrew.people_seen_note')}</p>
          {data.people.length === 0 ? (
            <p className="text-[13px] text-muted pt-2">{i18nT('sharedCrew.people_seen_empty')}</p>
          ) : (
            <ul className="flex flex-col divide-y divide-border" aria-label={i18nT('sharedCrew.people_seen_title')}>
              {data.people.map(row => <PersonRow key={row.principal.id} row={row} usageAvailable={usageAvailable} />)}
            </ul>
          )}
          {!usageAvailable && <p className="text-[12px] text-muted pt-2">{i18nT('sharedCrew.usage_unavailable')}</p>}
          {capabilities.attribution_via === 'tailscale_direct' && (
            <p className="text-[12px] text-muted pt-2" data-testid="people-direct-caveat">{i18nT('sharedCrew.people_direct_caveat')}</p>
          )}
          {attributionReason === 'daemon_not_found' && (
            <p className="text-[12px] text-muted pt-2" data-testid="people-daemon-not-found">{i18nT('sharedCrew.people_daemon_not_found')}</p>
          )}
        </SettingsCard>
      </SettingsSection>
      <SettingsSection title={i18nT('sharedCrew.directory_title')}>
        <SettingsCard>
          {directoryOn && (data.directory.people?.length ?? 0) > 0 ? (
            <ul className="flex flex-col divide-y divide-border" aria-label={i18nT('sharedCrew.directory_title')}>
              {(data.directory.people ?? []).map(row => <PersonRow key={row.principal.id} row={row} usageAvailable={usageAvailable} />)}
            </ul>
          ) : (
            <p className="text-[13px] text-muted">{i18nT('sharedCrew.directory_unavailable')}</p>
          )}
        </SettingsCard>
      </SettingsSection>
      <SettingsSection title={i18nT('sharedCrew.access_title')}>
        <SettingsCard>
          <p className="text-[13px] text-muted">{i18nT('sharedCrew.access_tailscale')}</p>
          {pointer === 'tailscale_admin' && (
            <a className="inline-flex items-center gap-1 text-[13px] text-accent pt-1" href={TAILSCALE_USERS_URL} target="_blank" rel="noopener noreferrer" data-testid="tailscale-admin-link">
              {i18nT('sharedCrew.access_tailscale_link')}
              <ExternalLink size={12} className="lucide-inline" aria-hidden />
            </a>
          )}
          {pointer === 'console' && (
            <p className="text-[13px] text-muted pt-1" data-testid="console-pointer">{i18nT('sharedCrew.access_console')}</p>
          )}
        </SettingsCard>
      </SettingsSection>
      {networkExtras}
    </div>
  )
}
