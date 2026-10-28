/** A folder's default crew: where its New chat starts, and the shared "start a
 *  chat on a crew" create that both that route and the New chat menu use. */
import { useCallback } from 'react'
import { useQueryClient, type QueryClient } from '@tanstack/react-query'
import { useStore } from 'react-redux'
import { api } from '../api/client'
import { fetchDashboardConfig } from '../api/dashboardConfigQuery'
import { resolveDefaultMemoryMode } from '../api/queryClient'
import { useRemoteCrewChat } from './usePreviewFlag'
import type { RootState } from '../store'
import type { ChatFolder } from '../types'
import { i18nT } from '../i18n/t'
import { captureForkOrigin, forkMayOpen } from '../store/chatSlice'
import { currentCrewWindow, openCrewWindow } from '../pages/chat/crew-window/crewWindowStore'

/** What a folder's New chat does about its `default_crew`.
 *
 *  - `local`: no crew pinned, or the Remote crew sessions preview is off. The
 *    preview gate lives here so every caller honours it the same way.
 *  - `crew`: pinned and connected; the chat starts on that crew.
 *  - `offline`: pinned but not connected. Never a silent fallback to Local: the
 *    caller says so and offers Local for that one chat. `registered` is false
 *    when the crew list is loaded and no longer has the pinned id (the crew was
 *    removed), so the caller does not call a crew that is gone "offline". */
export type FolderCrewRoute =
  | { kind: 'local' }
  | { kind: 'crew'; id: string; name: string }
  | { kind: 'offline'; id: string; name: string; registered: boolean }

interface RouteContext {
  previewOn: boolean
  /** Ids of the crews holding a live tunnel (`instances.warm`'s keys). */
  warm: Record<string, unknown> | undefined
  /** The registered crews, for the display name; `undefined` while the list
   *  has not loaded, when whether the pin is still registered is unknown. */
  instances: { id: string; name: string }[] | undefined
}

/** Decide a folder's route. Direct to the folder only, not inherited from an
 *  ancestor: Local has to stay choosable on a subfolder of a crew-pinned
 *  folder, and an empty value cannot mean both "inherit" and "Local". */
export function routeFolderCrew(folder: ChatFolder | undefined, ctx: RouteContext): FolderCrewRoute {
  const id = folder?.default_crew?.trim()
  if (!id || !ctx.previewOn) return { kind: 'local' }
  const known = ctx.instances?.find(i => i.id === id)
  const name = known?.name || id
  if (ctx.warm && Object.prototype.hasOwnProperty.call(ctx.warm, id)) return { kind: 'crew', id, name }
  return { kind: 'offline', id, name, registered: ctx.instances === undefined || !!known }
}

/** The router for a component: re-renders on the preview flag, and reads the
 *  connected set and the crew names at CALL time. Both would otherwise
 *  subscribe the host (the sidebar, the chat page) to every connection change
 *  for a value only a click needs. */
export function useFolderCrewRouter(): (folder: ChatFolder | undefined) => FolderCrewRoute {
  const previewOn = useRemoteCrewChat()
  const store = useStore<RootState>()
  const queryClient = useQueryClient()
  return useCallback((folder: ChatFolder | undefined) => routeFolderCrew(folder, {
    previewOn,
    warm: store.getState().instances?.warm,
    instances: queryClient.getQueryData<{ instances?: { id: string; name: string }[] }>(['instances'])?.instances,
  }), [previewOn, store, queryClient])
}

export interface CrewChatDeps {
  getState: () => RootState
  queryClient: QueryClient
  /** Where a crew window opens on a host with no chat pane (see ChatSidebar). */
  onOpenPeerSession?: (instanceId: string, key: string) => void
}

/** Mint a session ON a connected crew and open it as a window (CrewChatWindow).
 *
 *  The peer owns it: nothing is created on this machine, so there is no local
 *  row, transcript or history for it, and the crew's group lists it from the
 *  peer's own slot list.
 *
 *  Deliberately NO `agent`: the local default agent names a crew from THIS
 *  machine's roster, so the peer applies its own default. `memory_mode` always
 *  rides the create, because it is the user's privacy boundary: an explicit
 *  incognito or temporary request wins over the configured default. The
 *  version check runs first because the window talks to the peer's chat API
 *  directly, and a peer a release apart can lack a route or a frame it reads.
 *
 *  `inNewTab` is the create control's tab gesture. A crew window is not a tab
 *  in this machine's strip, so the gesture maps onto what it promises:
 *  `background` (Cmd/Ctrl-click, middle-click) keeps the user where they are,
 *  so the chat is created on the crew and listed in its group, and no window
 *  opens; `foreground` and a plain click open it. */
export async function startChatOnCrew(
  deps: CrewChatDeps,
  instanceId: string,
  memoryMode?: 'incognito' | 'temporary',
  inNewTab: 'foreground' | 'background' | false = false,
): Promise<void> {
  const { getState, queryClient, onOpenPeerSession } = deps
  const origin = captureForkOrigin(getState())
  const originWindow = currentCrewWindow()
  const caps = await api.instancesCapabilities(instanceId)
  if (!caps.version_match) {
    throw new Error(i18nT('pages.chat.crewWindow.version_mismatch', { peer: caps.version || '?', local: caps.local_version }))
  }
  const memory_mode = memoryMode ?? await resolveDefaultMemoryMode(fetchDashboardConfig)
  const created = await api.crewPeerPost(instanceId, 'api/chat/slots', { memory_mode }) as { key?: unknown }
  if (typeof created?.key !== 'string' || !created.key) throw new Error(i18nT('pages.chat.crewWindow.create_unnamed'))
  // The crew's group re-reads the peer list so the new row shows at once.
  void queryClient.invalidateQueries({ queryKey: ['instance-slots', instanceId] })
  if (inNewTab === 'background') return
  // Same rule as createSlot.fulfilled (the fork rule): a user who moved during
  // the round-trip, even back to the same session, is not yanked onto the new one.
  const moved = !forkMayOpen(getState(), origin) || currentCrewWindow() !== originWindow
  if (moved) return
  if (onOpenPeerSession) onOpenPeerSession(instanceId, created.key)
  else openCrewWindow({ instanceId, key: created.key })
}

/** `startChatOnCrew` bound to this component's store and query client, as one
 *  stable callback for every New chat route that can land on a crew. */
export function useStartCrewChat(
  onOpenPeerSession?: (instanceId: string, key: string) => void,
): (instanceId: string, memoryMode?: 'incognito' | 'temporary', inNewTab?: 'foreground' | 'background' | false) => Promise<void> {
  const store = useStore<RootState>()
  const queryClient = useQueryClient()
  // Read through a ref-free closure over the latest callback: the host passes a
  // fresh function when its tab layout changes, and the create must call it.
  return useCallback(
    (instanceId: string, memoryMode?: 'incognito' | 'temporary', inNewTab?: 'foreground' | 'background' | false) => startChatOnCrew(
      { getState: store.getState, queryClient, onOpenPeerSession },
      instanceId,
      memoryMode,
      inNewTab,
    ),
    [store, queryClient, onOpenPeerSession],
  )
}
