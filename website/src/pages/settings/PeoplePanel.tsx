/** Settings > People: how people reach this Crew, and who has.
 *
 *  Top to bottom: the connection sources (`PeopleSources.tsx`), one card per
 *  source and all of them at once, read from the mode route's `sources`; then
 *  the one People list (`PeopleCrew.tsx`), each row with the chip of the source
 *  it came in through, and invites nobody has used yet; then, for the owner, a
 *  collapsed Advanced section (`PeopleAdvanced.tsx`).
 *
 *  Access to a Crew is decided by each source and the sign-in in front of it.
 *  The in-Crew controls are the owner's invite buttons on the Tailscale and AWS
 *  SSM cards; a teammate sees them locked. There is no role or admin control,
 *  and no management URL is ever taken from a peer or from config: an
 *  organization Console is linked only when the server validated its URL.
 *
 *  An invalid team setup shows an error, never the sources (`PeopleModes.tsx`).
 *  A Crew that cannot say who people are shows why instead of the list. */
import { Users } from 'lucide-react'
import { Btn, ContentSkeleton, EmptyState } from '../../components/ui'
import ErrorNotice from '../../components/ErrorNotice'
import { capabilityUsable, membershipPointer, sharedAccessConfigured } from '../../api/sharedCrewUi'
import { TeamConfigInvalid } from './PeopleModes'
import { PeopleAdvanced } from './PeopleAdvanced'
import { PeopleList, PersonalWaiting, useOwnerRead } from './PeopleCrew'
import { PeopleSources } from './PeopleSources'
import { connectionSources, effectiveMode, rowSourceFallback } from '../../api/sharedCrewMode'
import { useSharedCrewCapabilities, useSharedCrewMode, useSharedCrewPeople, useViewerIsNotOwner } from '../../hooks/sharedCrew/useSharedCrew'
import { i18nT } from '../../i18n/t'

export function PeoplePanel() {
  const caps = useSharedCrewCapabilities()
  const people = useSharedCrewPeople(true)
  const modeQuery = useSharedCrewMode(true)
  if (caps.isLoading || people.isLoading || modeQuery.isLoading) return <ContentSkeleton rows={4} />
  const failures = [
    { query: caps, testId: 'people-capabilities-error', fallback: i18nT('sharedCrew.people_failed') },
    { query: people, testId: 'people-error', fallback: i18nT('sharedCrew.people_failed') },
    { query: modeQuery, testId: 'people-mode-error', fallback: i18nT('sharedCrew.mode_failed') },
  ].filter(({ query }) => query.isError)
  const missingData = failures.some(({ query }) => query.data === undefined)
  const mode = effectiveMode(modeQuery.data, {
    teamPath: sharedAccessConfigured(caps.capabilities),
    othersSeen: (people.data?.people ?? []).some(row => !row.is_viewer),
  })
  const hasDraftSurface = !missingData && (mode === 'personal' || mode === 'network')
  return (
    <>
      {failures.map(({ query, testId, fallback }) => (
        <div key={testId}>
          {/* No hand-off: cached People content holds unsaved invite emails, name, picture and API key. */}
          <ErrorNotice message={query.error instanceof Error ? query.error.message : fallback} askAgent={!hasDraftSurface} testId={testId} />
          <Btn onClick={() => { void query.refetch() }}>{i18nT('components.mobileConnect.try_again')}</Btn>
        </div>
      ))}
      {!missingData && <PeopleContent capabilities={caps.capabilities} data={people.data} modeData={modeQuery.data} />}
    </>
  )
}

function PeopleContent({ capabilities, data, modeData }: {
  capabilities: ReturnType<typeof useSharedCrewCapabilities>['capabilities']
  data: ReturnType<typeof useSharedCrewPeople>['data']
  modeData: ReturnType<typeof useSharedCrewMode>['data']
}) {
  const pointer = membershipPointer(capabilities.membership_link)
  const ownerRead = useOwnerRead()
  const { notOwner } = useViewerIsNotOwner()
  const rows = data?.people ?? []
  // Someone other than the viewer already connected: sharing works, whatever the
  // setup paths say (Tailscale Serve set up by hand, for one).
  const othersSeen = rows.some(row => !row.is_viewer)
  const mode = effectiveMode(modeData, { teamPath: sharedAccessConfigured(capabilities), othersSeen })
  if (mode === 'invalid' && modeData) return <TeamConfigInvalid mode={modeData} />
  const viewerOwns = ownerRead || rows.some(row => row.is_viewer && row.is_owner)
  const sourceList = connectionSources(modeData, capabilities, data ? rows : null)
  const sources = (
    <PeopleSources
      sources={sourceList}
      capabilities={capabilities}
      mode={modeData}
      teammate={notOwner}
    />
  )
  const attributionReason = capabilities.reasons?.attribution
  if (!data || !capabilityUsable(capabilities.attribution)) {
    // Alone on this machine there is nobody to name yet: the sources and any
    // unused invite are the whole page.
    if (mode === 'personal') {
      return (
        <div className="flex flex-col gap-4" data-testid="people-personal">
          {sources}
          <PersonalWaiting />
          {ownerRead && <PeopleAdvanced />}
        </div>
      )
    }
    const unnamed = attributionReason === 'platform_unprovable' || attributionReason === 'daemon_same_user'
    return (
      <div className="flex flex-col gap-4" data-testid="people-network-unattributed">
        {sources}
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
        {viewerOwns && <PeopleAdvanced />}
      </div>
    )
  }
  const usageAvailable = !!data.usage_available
  return (
    <div className="flex flex-col gap-4" data-testid={mode === 'personal' ? 'people-personal' : 'people-panel'}>
      {sources}
      <PeopleList
        people={data.people}
        usageAvailable={usageAvailable}
        fallbackSource={rowSourceFallback(sourceList)}
        notes={[
          !usageAvailable && !data.usage_off && i18nT('sharedCrew.usage_unavailable'),
          attributionReason === 'daemon_not_found' && i18nT('sharedCrew.people_daemon_not_found'),
        ].filter((n): n is string => !!n)}
        footer={pointer === 'console' && (
          <p className="text-[13px] text-muted pt-1" data-testid="console-pointer">{i18nT('sharedCrew.access_console')}</p>
        )}
      />
      {viewerOwns && <PeopleAdvanced />}
    </div>
  )
}
