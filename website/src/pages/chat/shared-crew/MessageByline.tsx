/** The sender of a user message in a shared session, from the server's
 *  `meta.author` stamp. The viewer's own messages carry no label; a message
 *  anyone else wrote, the session creator included, carries the sender's name,
 *  so a right-aligned message without a name is always the reader's own. Until
 *  the viewer read lands the session's creator stands in for the viewer
 *  (`bylineSelfId`). Bylines follow the same mode gate as the session list: a
 *  personal Crew, one that cannot attribute, or one whose mode is not known yet
 *  shows none. A message an app sent is the one exception: it always names the
 *  app, by its display name (`AppSenderByline`). */
import { useContext, useSyncExternalStore } from 'react'
import { ReactReduxContext } from 'react-redux'
import { useQuery } from '@tanstack/react-query'
import { api } from '../../../api/client'
import { appDisplayName } from '../../../components/appstore/appManifest'
import { appSender, bylineFor, bylineSelfId, messageAuthor, principalName } from '../../../api/sharedCrewUi'
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
    <div className="text-[12px] leading-5 text-muted mb-0.5 pr-1 truncate max-w-full" data-testid="message-byline">
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

/** The installed app's display name for *id*, or *id* until the app list arrives.
 *  Reads the shared `['apps']` query, and only for a row an app sent. A failed
 *  read leaves the id; the composer dock reports that failure once per chat
 *  (`useSessionControls` reads the same query), so a row repeats nothing. */
function useAppName(id: string): string {
  const { data: apps } = useQuery({
    queryKey: ['apps'],
    queryFn: () => api.listApps(),
    enabled: !!id,
    staleTime: 60_000,
  })
  const app = id ? (apps ?? []).find(a => a.name === id) : undefined
  return (app && appDisplayName(app)) || id
}

function AppSenderLine({ id }: { id: string }) {
  const name = useAppName(id)
  return (
    <div className="text-[12px] leading-5 text-muted mb-0.5 pr-1 truncate max-w-full" data-testid="message-app-sender">
      {name ? i18nT('components.mcpApp.from_app', { app: name }) : i18nT('sharedCrew.sent_by_an_app')}
    </div>
  )
}

/** An app's send is labelled in every Crew mode and on every branch of the user
 *  row, a steered answer included: an app holding a session grant writes into the
 *  owner's own chats, so without the label its message reads as the owner's own
 *  typing. Draws nothing for a row no app sent. */
export function AppSenderByline({ meta }: { meta?: Record<string, unknown> }) {
  const app = appSender(meta)
  return app ? <AppSenderLine id={app.name} /> : null
}

export function MessageByline({ meta, slotKey }: { meta?: Record<string, unknown>; slotKey?: string }) {
  if (appSender(meta)) return <AppSenderByline meta={meta} />
  const author = messageAuthor(meta)
  if (!author) return null
  return <AuthoredBy author={author} slotKey={slotKey} />
}
