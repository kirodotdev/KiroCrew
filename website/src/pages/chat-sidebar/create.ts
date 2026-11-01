/** Creating sessions from the sidebar: the New chat variants (local, crew,
 *  ephemeral) and a new chat inside a folder. */
import { useState, useRef, useCallback, type Dispatch, type SetStateAction } from 'react'
import { useMutation, useQueryClient } from '@tanstack/react-query'
import { type ErrorReport, findReport } from '../../utils/errorReport'
import { isStaleProjectDirError, resolveFolderAgent, resolveFolderProjectDir } from '../../utils/folderAgent'
import { unwrapResult } from '@reduxjs/toolkit'
import { captureForkOrigin, createSlot, forkMayOpen, type ForkOrigin, requestSlotFolderOpen } from '../../store/chatSlice'
import { focusComposer } from '../chat/composerFocus'
import { i18nT } from '../../i18n/t'
import type { Slot } from './types'
import type { AppDispatch } from '../../store'
import type { ChatFolder } from '../../types'
import type { FolderMutations } from './folders'
import type { BoardColumnMutations } from './board'
import { errMessage } from '../../utils/thunkError'
import { useRemoteCrewChat } from '../../hooks/usePreviewFlag'
import { api } from '../../api/client'
import { resolveDefaultMemoryMode } from '../../api/queryClient'
import { fetchDashboardConfig } from '../../api/dashboardConfigQuery'
import { currentCrewWindow, openCrewWindow } from '../chat/crew-window/crewWindowStore'
import { useStore } from 'react-redux'
import type { RootState } from '../../store'

/**
 * How a create opens into the tab strip. `foreground` (Cmd/Ctrl+Shift-click)
 * adds a tab beside the current one and switches to it; `background`
 * (Cmd/Ctrl-click or middle-click)
 * adds the tab and leaves the user where they are. `false` is a plain create,
 * which activates the slot in the current tab.
 */
export type NewTabMode = 'foreground' | 'background' | false

/**
 * The tab mode a gesture's create may still use once it has resolved. A
 * foreground open switches the view, so it is only safe while the user has
 * not moved since they clicked: if they moved while the create was in flight,
 * even back to the same session, switching would undo what they just picked (the
 * hijack createSlot's own origin check prevents for a plain create, which
 * `activate: false` skips). Then the tab still opens, in the background.
 */
function settledTabMode(store: { getState: () => RootState }, inNewTab: NewTabMode, origin: ForkOrigin): NewTabMode {
  if (inNewTab !== 'foreground') return inNewTab
  return forkMayOpen(store.getState(), origin) ? 'foreground' : 'background'
}

/** The action a plain folder create's repeat guard is scoped to: the folder,
 *  the board lane when the "+" sits on one, and the memory mode. Only a click
 *  that would send the same request again is a repeat; New incognito or New
 *  temporary chat on a folder whose plain "+" is pending is a different one. */
function plainCreateKey(folderId: string, columnId?: string, memoryMode?: string): string {
  return JSON.stringify([folderId, columnId ?? null, memoryMode ?? null])
}

/**
 * Dispatch createSlot and return the new slot with where the user stood
 * when the create started. createSlot already records that origin in its
 * fulfilled meta, which `.unwrap()` would drop. A rejection throws exactly
 * as `.unwrap()` does, so the callers' onError paths are unchanged.
 */
async function createSlotWithOrigin(dispatch: AppDispatch, args: Parameters<typeof createSlot>[0]): Promise<{ slot: Slot; origin: ForkOrigin }> {
  const action = await dispatch(createSlot(args))
  if (createSlot.fulfilled.match(action)) {
    const m = action.meta
    return { slot: action.payload, origin: { activeSlot: m.originActiveSlot ?? null, moves: m.originActiveSlotMoves, autoMoves: m.originActiveSlotAutoMoves, requestMoves: m.originActiveSlotRequestMoves, seq: m.originRequestSeq, burst: m.originRequestBurst } }
  }
  unwrapResult(action)
  throw new Error('createSlot settled without fulfilling or rejecting')
}

/** New chat inside a folder, with its inline failure line. */
export function useFolderChatCreate({ folders, mode, dispatch, dropSlotMutation, onOpenSlotInNewTab, onSelectSlot, updateFolderMutation, clearBoardCollapse, connected, setFolderOfflineNotice }: {
  folders: ChatFolder[]
  connected: boolean
  setFolderOfflineNotice: Dispatch<SetStateAction<string>>
  mode: string | undefined
  dispatch: AppDispatch
  dropSlotMutation: BoardColumnMutations['dropSlotMutation']
  onOpenSlotInNewTab: ((key: string, opts?: { background?: boolean | undefined; requestSeq?: number } | undefined) => void) | undefined
  // A plain create ACTIVATES the new session, so the host must leave Split View
  // and show it — the same callback a row click fires (ChatSidebar's
  // `onSelectSlot`). Without it the grid keeps drawing the old split over a
  // session that is already active (#21935). The background-tab gesture
  // (`inNewTab`) deliberately does NOT fire it: that create keeps the user
  // where they are, so the split must stay.
  onSelectSlot: ((key: string) => void) | undefined
  updateFolderMutation: FolderMutations['updateFolderMutation']
  clearBoardCollapse: (folderId: string, columnId?: string) => void
}) {
  // The most recent failed folder-scoped create, surfaced inline under that
  // folder's header. A single {folderId, columnId, message} rather than a
  // per-folder record: the actionable failure is the one the user just clicked
  // into. The background-tab gesture makes rapid-fire creates possible, so an
  // older attempt settling after a newer one is real; the attempt counter below
  // keeps a stale settle from resurrecting or clearing the latest notice, at
  // the accepted cost that only the newest attempt's failure is surfaced.
  // `columnId`
  // scopes the notice to the board column the create was issued from (a root
  // folder renders once per column, and an unscoped notice would mount N
  // identical alerts). Cleared by dismissal or by the next successful create.
  const [folderCreateError, setFolderCreateError] = useState<{ folderId: string; columnId?: string; message: string; title?: string; report?: ErrorReport; offerSettings?: boolean } | null>(null)
  // Monotonic attempt counter: settle callbacks only act when they belong to
  // the LATEST attempt, so an older create failing after a newer one succeeded
  // cannot resurrect a stale notice (and a stale success cannot clear a newer
  // failure's notice).
  const folderCreateAttemptRef = useRef(0)
  // A plain folder create is in flight (see `createChatInFolder`). Set before
  // `mutate` so the second click of a double click sees it: `creatingSlot`
  // only flips once the mutation has dispatched `createSlot`.
  // Keyed by action (folder, board lane and memory mode, see
  // `plainCreateKey`), so a different create is never dropped as a repeat.
  const plainCreatePendingRef = useRef(new Set<string>())
  const store = useStore<RootState>()
  // `inNewTab` is the folder-create twin of createChatMutation's flag (see the
  // comment there): either tab gesture creates the session WITHOUT activating
  // it, then hands the key to `onOpenSlotInNewTab`, which adds the tab and,
  // for `foreground`, switches to it.
  type CreateChatInFolderVars = { folderId: string; columnId?: string; focus?: boolean; attempt: number; memoryMode?: 'incognito' | 'temporary'; inNewTab?: NewTabMode }
  const createChatInFolderMutation = useMutation({
    mutationFn: ({ folderId, memoryMode, inNewTab }: CreateChatInFolderVars) => {
      // Only a folder's own pin names an agent. Agent-less otherwise: the
      // server stamps the default custom agent (a template), never the
      // default crewmate alias.
      const agent = resolveFolderAgent(folders, folderId, '')
      // Carry folder membership in the create payload so createSlot publishes
      // the new slot to Redux in its final location. Assigning it after create
      // lets the sidebar render one frame at root before moving it.
      //
      // Folder linked to a project directory (directly or via an ancestor):
      // carry it in the create payload so the slot starts on the linked
      // project — createSlot applies it before the slot activates, so the
      // first message can't race a late project switch.
      const project = resolveFolderProjectDir(folders, folderId)
      // The tab gestures register the slot without activating it, so the
      // tab strip does not replace the current tab -- same `activate: false`
      // contract as the header New button's gestures.
      return createSlotWithOrigin(dispatch, { agent, mode: mode || '', folder_id: folderId, project, activate: !inNewTab, ...(memoryMode ? { memory_mode: memoryMode } : {}) })
    },
    onSuccess: ({ slot, origin }: { slot: Slot; origin: ForkOrigin }, { folderId, columnId, focus, attempt, inNewTab: requested }: CreateChatInFolderVars) => {
      const inNewTab = settledTabMode(store, requested ?? false, origin)
      // A create that went through supersedes an earlier failure notice for
      // the same folder (e.g. the user fixed the folder's project directory
      // and retried); notices for OTHER folders stay put, and a stale success
      // (an older attempt settling late) must not clear a newer failure.
      if (attempt === folderCreateAttemptRef.current) {
        setFolderCreateError(prev => (prev && prev.folderId === folderId ? null : prev))
      }
      // Focus only after the create fulfils: the composer is bound to the
      // active slot, so focusing while createSlot is still in flight puts the
      // caret on the OLD session and anything typed lands in its draft. A tab
      // gesture focuses below, after the switch, and only in the foreground.
      if (focus && !inNewTab) focusComposer()
      // A plain create activated the new slot, so leave Split View and show it,
      // the same answer a row click gets (#21935). The background-tab gesture
      // skips this: it keeps the user on their current session, so the split
      // must stay. Guard on the live activeSlot: `createSlot.fulfilled`
      // declines to activate when the user moved to another session while the
      // create was pending (switched-away guard, lifecycle.ts), leaving
      // activeSlot on the OLD session. Firing the callback then would leave
      // Split View and navigate the embedded Sessions frame onto a session the
      // user never opened (#21959 review). Only fire it when the new slot is
      // the one now active.
      if (!inNewTab && slot?.key && store.getState().chat.activeSlot === slot.key) onSelectSlot?.(slot.key)
      if (slot?.key && columnId) {
        // Board view: also drop the new session into the column it was created
        // from, so a status-lane column shows it immediately instead of the
        // untagged session vanishing from a tag-filtered column. Mirrors a
        // drag-drop and is a harmless no-op for filter-only / non-status columns.
        // Runs for the tab gesture too -- column membership is independent of
        // which slot has focus.
        dropSlotMutation.mutate({ slot: slot.key, columnId })
      }
      if (inNewTab && onOpenSlotInNewTab && slot?.key) {
        // Adds a tab beside the active one; `foreground` also switches to it,
        // same as the header New button's gestures (see createChatMutation).
        onOpenSlotInNewTab(slot.key, { background: inNewTab === 'background', ...(inNewTab === 'foreground' ? { requestSeq: origin.seq } : {}) })
        // A foreground tab switches the view onto the new session, so leave
        // Split View too — the same guarded callback the header New fires
        // after its own foreground switch (createChatMutation). The plain
        // branch above handles `false`; `background` keeps the user where they
        // are, so it keeps the split. Guard on the live activeSlot for the
        // switched-away case (see the plain branch): a slow foreground switch
        // the user raced leaves activeSlot off the new slot, and firing then
        // would strand them on a session they did not open.
        if (inNewTab === 'foreground' && store.getState().chat.activeSlot === slot.key) onSelectSlot?.(slot.key)
        if (focus && inNewTab === 'foreground') focusComposer()
      }
    },
    onError: (err: unknown, { folderId, columnId, attempt }: CreateChatInFolderVars) => {
      // eslint-disable-next-line no-console -- surface chat-creation failures for diagnostics
      console.error('Failed to create chat in folder:', err)
      if (attempt !== folderCreateAttemptRef.current) return
      // The backend refusing the folder's project directory is the one failure
      // the user can fix themselves, so it gets a specific message naming the
      // stale path and where to change it (see isStaleProjectDirError).
      const isStaleProjectDir = isStaleProjectDirError(err)
      const raw = (err as { message?: unknown } | null)?.message
      const message = isStaleProjectDir
        ? i18nT('pages.chatSidebar.folder_project_dir_missing', { path: resolveFolderProjectDir(folders, folderId) ?? '' })
        : (typeof raw === 'string' && raw ? raw : i18nT('pages.chatSidebar.folder_create_failed'))
      // The generic branch renders raw transport text ("no capacity", "fetch
      // failed") — give it a task-level lead so the user always sees WHAT
      // failed. The stale-dir message is already a full sentence; a title
      // there would double up. `message` stays the journal lookup key.
      const title = isStaleProjectDir || !(typeof raw === 'string' && raw)
        ? undefined
        : i18nT('pages.chatSidebar.folder_create_failed')
      // Resolve the journal report from the RAW error text, not the rendered
      // message: the journal keys entries on the transport-level string
      // ("Not a directory"), so the translated stale-dir message would never
      // match and the agent hand-off would silently lose the structured
      // endpoint/status context ErrorNotice exists to recover.
      const report = typeof raw === 'string' ? findReport(raw) : undefined
      setFolderCreateError({ folderId, columnId, message, title, report, offerSettings: isStaleProjectDir })
    },
    onSettled: (_data, _err, { folderId, columnId, memoryMode, inNewTab }: CreateChatInFolderVars) => {
      if (!inNewTab) plainCreatePendingRef.current.delete(plainCreateKey(folderId, columnId, memoryMode))
    },
  })
  /** The offline create refusal, in one place: the folder-row `+` buttons reach
   *  it through `createChatInFolder`, and the header split button's primary
   *  segment calls it directly (its create does not route through that sink). */
  const refuseCreateOffline = useCallback(() => {
    setFolderOfflineNotice(i18nT('utils.offline.gateway_offline_reconnect', { action: i18nT('utils.offline.create_sessions') }))
  }, [setFolderOfflineNotice])
  const createChatInFolder = useCallback((folderId: string, opts?: { columnId?: string; focus?: boolean; memoryMode?: 'incognito' | 'temporary'; inNewTab?: NewTabMode }) => {
    // Refused at the SINK: the menu rows that reach here are gated one by one,
    // but the folder-row quick-create `+` buttons call it too. Gating here covers
    // every caller at once, including the ancestor-expanding PATCH loop below,
    // which would otherwise fire a doomed `collapsed: false` write per ancestor.
    if (!connected) {
      refuseCreateOffline()
      return
    }
    // A plain create that repeats one still in flight (same folder, board lane
    // and memory mode) is dropped, so a double or triple click on a folder's
    // "+" makes one session. The check sits at the sink so the tree "+", the
    // board-lane "+" and the create menu's folder rows all follow it. Any
    // other create, such as a "+" on another folder or lane, or New incognito
    // chat on the same folder, is a separate request and goes through. It
    // reads only this hook's pending marks, not the global
    // `creatingSlot`: that flag is also set by header New and by background
    // creates, and a folder "+" during one of those is a separate request the
    // user expects to see. The tab gestures (Cmd/Ctrl-click, middle-click) are
    // exempt: each one opens its own new tab, so a quick run of them is a
    // batch of tabs on purpose.
    if (!opts?.inNewTab) {
      const key = plainCreateKey(folderId, opts?.columnId, opts?.memoryMode)
      if (plainCreatePendingRef.current.has(key)) return
      plainCreatePendingRef.current.add(key)
    }
    // A nested folder selected from the create menu may be hidden behind one
    // or more collapsed ancestors. Expand the complete path optimistically so
    // the destination and its new session are visible as creation begins.
    const visited = new Set<string>()
    let currentId: string | undefined = folderId
    while (currentId && !visited.has(currentId)) {
      visited.add(currentId)
      const folder = folders.find(f => f.id === currentId)
      if (!folder) break
      if (folder.collapsed) updateFolderMutation.mutate({ id: folder.id, body: { collapsed: false } })
      // Board columns keep their own collapse overrides; drop them for the
      // whole ancestor path so the destination is visible in the clicked
      // column (and every other) as creation begins.
      clearBoardCollapse(folder.id)
      currentId = folder.parent_id || undefined
    }
    createChatInFolderMutation.mutate({ folderId, columnId: opts?.columnId, focus: opts?.focus, attempt: ++folderCreateAttemptRef.current, memoryMode: opts?.memoryMode, inNewTab: opts?.inNewTab })
  }, [createChatInFolderMutation, folders, updateFolderMutation, clearBoardCollapse, connected, refuseCreateOffline])
  return { folderCreateError, setFolderCreateError, createChatInFolder, refuseCreateOffline }
}

/** The New chat variants and the Crew Members door. */
export function useSessionCreate({ setNewChatError, dispatch, mode, onOpenSlotInNewTab, onSelectSlot, setRemoteCrewError, setNewChatMenuOpen, onOpenPeerSession }: {
  /** Where a crew window opens on a host with no chat pane (see ChatSidebar). */
  onOpenPeerSession?: (instanceId: string, key: string) => void
  setNewChatError: Dispatch<SetStateAction<string>>
  dispatch: AppDispatch
  mode: string | undefined
  onOpenSlotInNewTab: ((key: string, opts?: { background?: boolean | undefined; requestSeq?: number } | undefined) => void) | undefined
  // Leave Split View on a plain create so the new, now-active session shows
  // instead of the old split (#21935). Same callback a row click fires; the
  // background-tab gesture does not fire it (the split is kept on purpose).
  // A remote-crew create never fires it: nothing is created on this machine,
  // so there is no local slot to show in place of the split.
  onSelectSlot: ((key: string) => void) | undefined
  setRemoteCrewError: Dispatch<SetStateAction<string>>
  setNewChatMenuOpen: Dispatch<SetStateAction<boolean>>
}) {
  // Every local create below reports through `newChatError`. `createSlot(...)
  // .unwrap()` rejects with RTK's SerializedError — a PLAIN object carrying
  // `message`, not an Error instance — so the reader accepts both shapes (same
  // reasoning as createRemoteChatMutation's onError further down). Falls back to
  // a fixed sentence rather than rendering nothing: an empty message would make
  // the failed click a silent no-op again, which is the defect being fixed.
  // `errMessage` already reads the RTK SerializedError a rejected thunk carries.
  const onNewChatError = (err: unknown) => setNewChatError(errMessage(err) || i18nT('pages.chatSidebar.folder_create_failed'))
  // Separate flag, separate feature: this one holds "New chat on crew", which
  // dispatches a session to another MACHINE. The same switch shows the crew
  // groups in the sidebar (`sessionSources.ts`), so a chat started here has a
  // group to return to.
  const remoteCrewChatPreview = useRemoteCrewChat()
  const queryClient = useQueryClient()
  const store = useStore<RootState>()

  // Create default chat session mutation.
  //
  // `inNewTab` is the New button's tab gesture. A plain create activates the
  // new slot, and the tab strip's invariant then REPLACES the tab the user was
  // on with it (see useSessionTabs). Both tab gestures keep that tab: the
  // create runs with `activate: false`, so nothing is replaced, and on success
  // the key goes to `onOpenSlotInNewTab`, which adds a tab beside the active
  // one. `foreground` (Cmd/Ctrl+Shift-click) then switches to the new tab;
  // `background` (Cmd/Ctrl-click or middle-click) leaves the user where they
  // are, as on a session row. Switching only after the
  // tab exists is what keeps the old tab: activating first would replace it.
  // The click site only sets `inNewTab` when that callback exists (embedded
  // hosts have no tab strip), so a modifier click there stays a plain create.
  const createChatMutation = useMutation({
    mutationFn: ({ inNewTab }: { inNewTab: NewTabMode }) => {
      setNewChatError('')
      return createSlotWithOrigin(dispatch, { mode: mode || '', activate: !inNewTab, defaultFolder: true })
    },
    onSuccess: ({ slot, origin }, { inNewTab: requested }) => {
      const inNewTab = settledTabMode(store, requested, origin)
      if (inNewTab && onOpenSlotInNewTab) {
        onOpenSlotInNewTab(slot.key, { background: inNewTab === 'background', ...(inNewTab === 'foreground' ? { requestSeq: origin.seq } : {}) })
        // Background: the user stays on their transcript, so its composer keeps
        // whatever focus it had — no `focusComposer`, same as the row gesture.
        if (inNewTab === 'background') return
        // Foreground: the new chat is now in view, but its create ran with
        // `activate: false`, so createSlot asked the sidebar for nothing. Open
        // the default folder's path here, or its row stays hidden in a
        // collapsed folder, as for a plain New.
        dispatch(requestSlotFolderOpen({ key: slot.key, folder_id: slot.folder_id }))
      }
      // Plain create activated the new slot: leave Split View so it shows,
      // the same answer a row click gets (#21935). Guard on the live
      // activeSlot — see useFolderChatCreate's onSuccess: a user who switched
      // away during a slow create leaves activeSlot on the old session, and
      // firing the callback then would strand them on a session they did not
      // open (#21959 review).
      if (slot?.key && store.getState().chat.activeSlot === slot.key) onSelectSlot?.(slot.key)
      focusComposer()
    },
    onError: onNewChatError,
  })

  // Mint a session ON a connected crew and open it as a window (CrewChatWindow).
  // The peer owns it: nothing is created on this machine, so there is no local
  // row, transcript or history for it, and the crew's group lists it from the
  // peer's own slot list.
  //
  // Deliberately NO `agent`: `defaultAgent` names a crew from THIS machine's
  // roster, so the peer applies its own default. `memory_mode` always rides the
  // create, because it is the user's privacy boundary. The version check runs
  // first because the window talks to the peer's chat API directly, and a peer a
  // release apart can lack a route or a frame the window reads.
  //
  // Errors surface inline in the submenu (rows use `onSelect preventDefault`, so
  // a failed create keeps the menu open long enough to read the reason).
  const createRemoteChatMutation = useMutation({
    mutationFn: async (instanceId: string) => {
      setRemoteCrewError('')
      const origin = captureForkOrigin(store.getState())
      const originWindow = currentCrewWindow()
      const caps = await api.instancesCapabilities(instanceId)
      if (!caps.version_match) {
        throw new Error(i18nT('pages.chat.crewWindow.version_mismatch', { peer: caps.version || '?', local: caps.local_version }))
      }
      const memory_mode = await resolveDefaultMemoryMode(fetchDashboardConfig)
      const created = await api.crewPeerPost(instanceId, 'api/chat/slots', { memory_mode }) as { key?: unknown }
      if (typeof created?.key !== 'string' || !created.key) throw new Error(i18nT('pages.chat.crewWindow.create_unnamed'))
      // The crew's group re-reads the peer list so the new row shows at once.
      void queryClient.invalidateQueries({ queryKey: ['instance-slots', instanceId] })
      // Same rule as createSlot.fulfilled: a user who moved during the
      // round-trip is not yanked onto the new session.
      const moved = !forkMayOpen(store.getState(), origin) || currentCrewWindow() !== originWindow
      if (moved) return
      if (onOpenPeerSession) onOpenPeerSession(instanceId, created.key)
      else openCrewWindow({ instanceId, key: created.key })
    },
    onSuccess: () => {
      // Close the menu explicitly: the crew rows use `onSelect preventDefault`,
      // which also removed the auto-close on success.
      setNewChatMenuOpen(false)
    },
    onError: (err: unknown) => {
      setRemoteCrewError(errMessage(err))
    },
  })

  // Create an ephemeral chat — incognito (memory reads, no writes) or temporary
  // (neither).
  const createEphemeralChatMutation = useMutation({
    mutationFn: (memoryMode: 'incognito' | 'temporary') => {
      setNewChatError('')
      return dispatch(createSlot({ mode: mode || '', memory_mode: memoryMode, defaultFolder: true })).unwrap()
    },
    // Incognito/temporary always activate the new slot, so always leave Split
    // View and show it, then focus the composer (#21935). Guard on the live
    // activeSlot (see createChatMutation): a switched-away create leaves
    // activeSlot on the old session, and firing the callback then would strand
    // the user on a session they did not open (#21959 review).
    onSuccess: (slot) => {
      if (slot?.key && store.getState().chat.activeSlot === slot.key) onSelectSlot?.(slot.key)
      focusComposer()
    },
    onError: onNewChatError,
  })
  return {
    remoteCrewChatPreview,
    createChatMutation, createRemoteChatMutation, createEphemeralChatMutation,
  }
}
