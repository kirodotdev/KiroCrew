/** The sender of a user message in a shared session, from the server's
 *  `meta.author` stamp. The session's creator is its default owner and needs no
 *  label; a message any other participant wrote carries the sender's name. A
 *  session with no recorded creator treats the viewer as its owner, so a
 *  single-person Crew's transcript is unchanged. Bylines follow the same mode
 *  gate as the session list: a personal Crew, one that cannot attribute, or one
 *  whose mode is not known yet shows none. */
import { useContext, useSyncExternalStore } from 'react'
import { ReactReduxContext } from 'react-redux'
import { bylineFor, capabilityUsable, channelName, messageAuthor, othersSeenIn, principalName, principalVia, sessionOwnerId, sharedAccessConfigured } from '../../../api/sharedCrewUi'
import { ChannelBrandIcon } from '../../../components/ChannelBrandIcon'
import { effectiveMode, hidesTeamAttribution } from '../../../api/sharedCrewMode'
import { useSharedCrewCapabilities, useSharedCrewMode, useSharedCrewPeople, useSharedCrewViewer } from '../../../hooks/sharedCrew/useSharedCrew'
import type { PublicPrincipal, SessionAttribution } from '../../../api/sharedCrew'
import type { RootState } from '../../../store'
import { i18nT } from '../../../i18n/t'

const noSubscribe = () => () => {}

/** The slot's creator attribution from the store; `null` outside a store, so a
 *  transcript rendered on its own still renders. */
function useSlotAttribution(slotKey: string | undefined): SessionAttribution | null {
  const store = useContext(ReactReduxContext)?.store
  return useSyncExternalStore(store ? store.subscribe : noSubscribe, () => {
    if (!store || !slotKey) return null
    const state = store.getState() as RootState
    return state.dashboard.slots.find(x => x.key === slotKey)?.attribution ?? null
  })
}

/** Whether this Crew shows team attribution, decided the way the session list
 *  decides it (`useSharedCrewListView`). */
function useTeamAttribution(): boolean {
  const { capabilities } = useSharedCrewCapabilities()
  const attributes = capabilityUsable(capabilities.attribution)
  const modeQuery = useSharedCrewMode(attributes)
  const people = useSharedCrewPeople(attributes)
  // An unreadable mode is an error, not a guessed mode: no bylines until a read
  // succeeds, the same as the session list.
  const mode = effectiveMode(modeQuery.data, {
    teamPath: sharedAccessConfigured(capabilities),
    othersSeen: othersSeenIn(people.data?.people),
  })
  return attributes && modeQuery.isSuccess && !hidesTeamAttribution(mode)
}

function SenderLine({ name, via }: { name: string; via: string }) {
  const channel = channelName(via)
  const title = channel
    ? i18nT('sharedCrew.sent_by_via', { name, channel })
    : i18nT('sharedCrew.sent_by', { name })
  return (
    <div className="flex items-center gap-1 text-[12px] leading-5 text-muted mb-0.5 pr-1 max-w-full min-w-0" data-testid="message-byline" title={title}>
      <span className="truncate min-w-0">{name}</span>
      {channel && (
        <span className="inline-flex shrink-0" data-testid="message-byline-via" data-via={via}>
          <ChannelBrandIcon channel={via} size={12} />
          <span className="sr-only">{i18nT('sharedCrew.via_channel', { channel })}</span>
        </span>
      )}
    </div>
  )
}

function AuthoredBy({ author, slotKey }: { author: PublicPrincipal; slotKey?: string }) {
  const team = useTeamAttribution()
  const attribution = useSlotAttribution(slotKey)
  const viewer = useSharedCrewViewer()
  const ownerId = sessionOwnerId(attribution, viewer.isSuccess ? viewer.data?.id : '')
  const sender = bylineFor(author, ownerId)
  if (!team || !sender) return null
  return <SenderLine name={principalName(sender) || i18nT('sharedCrew.unattributed')} via={principalVia(sender)} />
}

export function MessageByline({ meta, slotKey }: { meta?: Record<string, unknown>; slotKey?: string }) {
  const author = messageAuthor(meta)
  if (!author) return null
  return <AuthoredBy author={author} slotKey={slotKey} />
}
