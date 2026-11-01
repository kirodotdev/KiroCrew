/** The sender of a user message in a shared session, from the server's
 *  `meta.author` stamp. The viewer's own messages carry no label; a message
 *  anyone else wrote, the session creator included, carries the sender's name,
 *  so a right-aligned message without a name is always the reader's own. Until
 *  the viewer read lands the session's creator stands in for the viewer
 *  (`bylineSelfId`). Bylines follow the same mode gate as the session list: a
 *  personal Crew, one that cannot attribute, or one whose mode is not known yet
 *  shows none. */
import { useContext, useSyncExternalStore } from 'react'
import { ReactReduxContext } from 'react-redux'
import { bylineFor, bylineSelfId, messageAuthor, principalName } from '../../../api/sharedCrewUi'
import { useSharedCrewViewer, useTeamAttribution } from '../../../hooks/sharedCrew/useSharedCrew'
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

function SenderLine({ name }: { name: string }) {
  return (
    <div className="text-[12px] leading-5 text-muted mb-0.5 pr-1 truncate max-w-full" data-testid="message-byline" title={i18nT('sharedCrew.sent_by', { name })}>
      {name}
    </div>
  )
}

function AuthoredBy({ author, slotKey }: { author: PublicPrincipal; slotKey?: string }) {
  const team = useTeamAttribution()
  const attribution = useSlotAttribution(slotKey)
  const viewer = useSharedCrewViewer()
  const sender = bylineFor(author, bylineSelfId(viewer.data, attribution))
  if (!team || !sender) return null
  return <SenderLine name={principalName(sender) || i18nT('sharedCrew.unattributed')} />
}

export function MessageByline({ meta, slotKey }: { meta?: Record<string, unknown>; slotKey?: string }) {
  const author = messageAuthor(meta)
  if (!author) return null
  return <AuthoredBy author={author} slotKey={slotKey} />
}
