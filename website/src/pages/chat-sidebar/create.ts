/** Creating sessions from the sidebar: the New chat variants (local, autopilot, crew,
 *  plain, ephemeral) and a new chat inside a folder. */
import { useState, useRef, useCallback, type Dispatch, type SetStateAction } from 'react'
import { useMutation } from '@tanstack/react-query'
import { useNavigate } from 'react-router-dom'
import { type ErrorReport, findReport } from '../../utils/errorReport'
import { resolveFolderAgent, resolveFolderProjectDir } from '../../utils/folderAgent'
import { loadChatConfig } from '../chat/ChatSettings'
import { createSlot } from '../../store/chatSlice'
import { focusComposer } from '../chat/composerFocus'
import { ApiError } from '../../api/apiError'
import { i18nT } from '../../i18n/t'
import type { Slot } from './types'
import type { AppDispatch } from '../../store'
import type { ChatFolder } from '../../types'
import type { FolderMutations } from './folders'
import type { BoardColumnMutations } from './board'
import { errMessage } from '../../utils/thunkError'
import { usePreviewFlag } from '../../hooks/usePreviewFlag'
import { PREVIEW_CREW, PREVIEW_REMOTE_CREW_CHAT } from '../../utils/previewFlags'
import { settingsPath } from '../../components/settingsPath'
import { SETTINGS_CREW_MEMBERS_PREVIEW_ID } from '../../hooks/useSettingHighlight'

/** New chat inside a folder, with its inline failure line. */
export function useFolderChatCreate({ folders, defaultAgent, mode, dispatch, dropSlotMutation, onOpenSlotInNewTab, updateFolderMutation, clearBoardCollapse }: {
  folders: ChatFolder[]
  defaultAgent: string
  mode: string | undefined
  dispatch: AppDispatch
  dropSlotMutation: BoardColumnMutations['dropSlotMutation']
  onOpenSlotInNewTab: ((key: string, opts?: { background?: boolean | undefined; } | undefined) => void) | undefined
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
  // `inNewTab` is the folder-create twin of createChatMutation's flag (see the
  // comment there): the Cmd/Ctrl-click and middle-click gesture creates the
  // session WITHOUT activating it, then hands the key to `onOpenSlotInNewTab`
  // in background mode so the user stays on the transcript they were reading.
  type CreateChatInFolderVars = { folderId: string; columnId?: string; focus?: boolean; attempt: number; memoryMode?: 'incognito' | 'temporary'; inNewTab?: boolean }
  const createChatInFolderMutation = useMutation({
    mutationFn: ({ folderId, memoryMode, inNewTab }: CreateChatInFolderVars) => {
      const agent = resolveFolderAgent(folders, folderId, defaultAgent)
      // A mode-specific create pins plain mode, not the defaultAutopilot preference.
      const ephemeral = !!memoryMode
      const effectiveMode = (!ephemeral && loadChatConfig().defaultAutopilot) ? 'orchestrator' : (mode || '')
      // Carry folder membership in the create payload so createSlot publishes
      // the new slot to Redux in its final location. Assigning it after create
      // lets the sidebar render one frame at root before moving it.
      //
      // Folder linked to a project directory (directly or via an ancestor):
      // carry it in the create payload so the slot starts on the linked
      // project — createSlot applies it before the slot activates, so the
      // first message can't race a late project switch.
      const project = resolveFolderProjectDir(folders, folderId)
      // The tab gesture registers the slot without stealing focus -- same
      // `activate: false` contract as the header New button's gesture.
      return dispatch(createSlot({ agent, mode: effectiveMode, folder_id: folderId, project, activate: !inNewTab, ...(memoryMode ? { memory_mode: memoryMode } : {}) })).unwrap()
    },
    onSuccess: (slot: Slot, { folderId, columnId, focus, attempt, inNewTab }: CreateChatInFolderVars) => {
      // A create that went through supersedes an earlier failure notice for
      // the same folder (e.g. the user fixed the folder's project directory
      // and retried); notices for OTHER folders stay put, and a stale success
      // (an older attempt settling late) must not clear a newer failure.
      if (attempt === folderCreateAttemptRef.current) {
        setFolderCreateError(prev => (prev && prev.folderId === folderId ? null : prev))
      }
      // Focus only after the create fulfils: the composer is bound to the
      // active slot, so focusing while createSlot is still in flight puts the
      // caret on the OLD session and anything typed lands in its draft. The
      // background-tab case never focuses: the user stays where they are.
      if (focus && !inNewTab) focusComposer()
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
        // Background: adds a tab beside the active one without switching, same
        // as the header New button's gesture (see createChatMutation).
        onOpenSlotInNewTab(slot.key, { background: true })
      }
    },
    onError: (err: unknown, { folderId, columnId, attempt }: CreateChatInFolderVars) => {
      // eslint-disable-next-line no-console -- surface chat-creation failures for diagnostics
      console.error('Failed to create chat in folder:', err)
      if (attempt !== folderCreateAttemptRef.current) return
      // The backend refusing the folder's project directory (HTTP 400
      // "Not a directory" from the slot-project endpoint) is the one failure
      // the user can fix themselves, so it gets a specific message naming the
      // stale path and where to change it. createSlot rethrows the ApiError,
      // but createAsyncThunk serializes thrown errors down to
      // {name, message, stack} — the instance and its `status` are gone by the
      // time `.unwrap()` delivers it here — so match the live instance when
      // present and fall back to the serialized shape.
      const isStaleProjectDir = err instanceof ApiError
        ? err.status === 400 && err.message === 'Not a directory'
        : (err as { name?: unknown } | null)?.name === 'ApiError'
          && (err as { message?: unknown }).message === 'Not a directory'
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
  })
  const createChatInFolder = useCallback((folderId: string, opts?: { columnId?: string; focus?: boolean; memoryMode?: 'incognito' | 'temporary'; inNewTab?: boolean }) => {
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
  }, [createChatInFolderMutation, folders, updateFolderMutation, clearBoardCollapse])
  return { folderCreateError, setFolderCreateError, createChatInFolder }
}

/** The New chat variants and the Crew Members door. */
export function useSessionCreate({ setNewChatError, dispatch, defaultAgent, mode, onOpenSlotInNewTab, setRemoteCrewError, setNewChatMenuOpen }: {
  setNewChatError: Dispatch<SetStateAction<string>>
  dispatch: AppDispatch
  defaultAgent: string
  mode: string | undefined
  onOpenSlotInNewTab: ((key: string, opts?: { background?: boolean | undefined; } | undefined) => void) | undefined
  setRemoteCrewError: Dispatch<SetStateAction<string>>
  setNewChatMenuOpen: Dispatch<SetStateAction<boolean>>
}) {
  // Create autopilot session mutation (consistent with useMutation pattern)
  //
  // Every local create below reports through `newChatError`. `createSlot(...)
  // .unwrap()` rejects with RTK's SerializedError — a PLAIN object carrying
  // `message`, not an Error instance — so the reader accepts both shapes (same
  // reasoning as createRemoteChatMutation's onError further down). Falls back to
  // a fixed sentence rather than rendering nothing: an empty message would make
  // the failed click a silent no-op again, which is the defect being fixed.
  // `errMessage` already reads the RTK SerializedError a rejected thunk carries.
  const onNewChatError = (err: unknown) => setNewChatError(errMessage(err) || i18nT('pages.chatSidebar.folder_create_failed'))
  const createAutopilotMutation = useMutation({
    mutationFn: () => {
      setNewChatError('')
      return dispatch(createSlot({ agent: defaultAgent || undefined, mode: 'orchestrator' })).unwrap()
    },
    onSuccess: focusComposer,
    onError: onNewChatError,
  })

  // Crew Members: the create menu's crew entry no longer creates anything. Crew
  // Mode (a `mode: 'crew'` session fanning topics out to sub-sessions) is
  // retired in favour of the Crew Members page, where each member is a
  // standing agent with its own DM thread — so the entry is a DOOR to that
  // page, kept in this menu because this is where people learned to look
  // for "crew".
  //
  // Always rendered, even while the page is still preview-gated: the flag
  // only decides WHERE the click lands. On, it opens `/members`. Off, it
  // opens Settings > Developer > Feature Previews with the crew card scrolled
  // into view and ringed (`useSettingHighlight`), so the user turns the page
  // on from the very switch that holds it instead of reading a toast about
  // one. `usePreviewFlag` rather than a bare read because the sidebar does
  // not remount when that toggle flips.
  const crewPreview = usePreviewFlag(PREVIEW_CREW)
  const navigate = useNavigate()
  const openCrewMembers = () => {
    navigate(crewPreview ? '/members' : settingsPath({ tab: 'developer', highlight: SETTINGS_CREW_MEMBERS_PREVIEW_ID }))
  }
  // Separate flag, separate feature: this one holds "New chat on crew", which
  // dispatches a session to another MACHINE. Its toggle is in Settings > Remote
  // crews rather than Settings > Developer > Feature Previews, because it only means
  // anything to someone who already has a crew connected.
  const remoteCrewChatPreview = usePreviewFlag(PREVIEW_REMOTE_CREW_CHAT)

  // Create default chat session mutation.
  //
  // `inNewTab` is the New button's modifier/middle-click gesture — the same
  // "open as a BACKGROUND tab" the session rows honour, applied to a session
  // that does not exist yet. A plain create activates the new slot, and the
  // tab strip's invariant then REPLACES the tab the user was on with it (see
  // useSessionTabs), which is exactly what the gesture asks not to happen. So
  // the create runs with `activate: false` — the slot is registered but focus
  // stays put — and on success the key is handed to `onOpenSlotInNewTab` in
  // background mode, which adds a tab beside the active one without switching.
  // The click site only sets `inNewTab` when that callback exists (embedded
  // hosts have no tab strip), so a modifier click there stays a plain create.
  const createChatMutation = useMutation({
    mutationFn: ({ inNewTab }: { inNewTab: boolean }) => {
      setNewChatError('')
      const effectiveMode = loadChatConfig().defaultAutopilot ? 'orchestrator' : (mode || '')
      return dispatch(createSlot({ agent: defaultAgent || undefined, mode: effectiveMode, activate: !inNewTab })).unwrap()
    },
    onSuccess: (slot, { inNewTab }) => {
      if (inNewTab && onOpenSlotInNewTab) {
        // Background: the user stays on their transcript, so its composer keeps
        // whatever focus it had — no `focusComposer`, same as the row gesture.
        onOpenSlotInNewTab(slot.key, { background: true })
        return
      }
      focusComposer()
    },
    onError: onNewChatError,
  })

  // Create a PLAIN chat, ignoring the `defaultAutopilot` preference.
  // The caret menu lists "New chat" and "New autopilot chat" side by side, so
  // each must name exactly what it makes. Routing the plain entry through
  // createChatMutation would hand an autopilot session to anyone who turned the
  // default on — the one case where they picked the non-default on purpose.
  // The button's main segment keeps honouring the preference; only this explicit
  // entry pins the mode.
  // Create a LOCAL session whose turns run on a peer crew. The session belongs to
  // this machine — local sidebar row, local transcript, local history and search —
  // and only its execution moves, so this goes through the ordinary `createSlot`
  // thunk with `instanceId` attached rather than reaching for the peer directly.
  //
  // This replaced an earlier shape that POSTed straight to the peer and then
  // switched to that crew's iframe pane. The session then existed only over
  // there, so the pane switch was not a choice: the local list had nowhere to
  // show it. Now it does, and staying put is the whole point — the user asked for
  // a session on that crew, not for a trip to that crew's dashboard.
  //
  // Deliberately NO `agent`, unlike every sibling entry below. `defaultAgent`
  // names a crew from THIS machine's roster, and the backend forwards any agent
  // it is given straight to the peer: sending it would either be refused over
  // there or bind a different crew than the name implies. Omitting it lets the
  // peer apply its own default — which is the point of the session running on it,
  // and what the header then reads back from the peer's `default_agent`.
  // A crew create fails more often than a local one — the backend opens the
  // peer's session BEFORE creating the local one, and refuses on a version-series
  // mismatch or an unreachable tunnel — and on failure leaves NOTHING behind (no
  // local row, no peer session). Without an onError the react-query rejection is
  // swallowed and the click reads as a silent no-op, so surface the backend's
  // reason inline in the submenu instead. `err.message` carries it: apiFailure
  // builds the ApiError message from the 502 body's `error` field, and the thunk's
  // `.unwrap()` rethrows that message.
  const createRemoteChatMutation = useMutation({
    mutationFn: (instanceId: string) => {
      setRemoteCrewError('')
      return dispatch(createSlot({ instanceId })).unwrap()
    },
    onSuccess: () => {
      // Close the menu explicitly: the crew rows use `onSelect preventDefault`
      // (so a FAILED create keeps the menu open long enough to read the error),
      // which also removed the auto-close on SUCCESS — a modal Radix menu is not
      // dismissed by `focusComposer` alone, so without this the session is created
      // behind the still-open menu and a second pick makes a duplicate (opus #8543).
      // `mutationFn` already cleared remoteCrewError, and onOpenChange clears it on
      // close, so no reset is needed here.
      setNewChatMenuOpen(false)
      focusComposer()
    },
    onError: (err: unknown) => {
      // `createSlot(...).unwrap()` rejects with RTK's SerializedError — a PLAIN
      // object carrying `message`, NOT an Error instance — so read `.message`
      // off the object rather than gating on `instanceof Error` (which would be
      // false here and drop the backend's reason). apiFailure already localizes
      // and puts the 502 body's `error` text into that message, so it is shown
      // verbatim; the crew submenu's errRow is gated on truthiness, so the unreachable
      // empty-message case simply renders nothing rather than a bare fallback.
      const msg =
        err instanceof Error
          ? err.message
          : err && typeof err === 'object' && typeof (err as { message?: unknown }).message === 'string'
            ? (err as { message: string }).message
            : ''
      setRemoteCrewError(msg)
    },
  })

  const createPlainChatMutation = useMutation({
    mutationFn: () => {
      setNewChatError('')
      return dispatch(createSlot({ agent: defaultAgent || undefined, mode: mode || '' })).unwrap()
    },
    onSuccess: focusComposer,
    onError: onNewChatError,
  })

  // Create an ephemeral chat — incognito (memory reads, no writes) or temporary
  // (neither). The mode is pinned plain for the same reason the plain entry
  // above pins it: these entries name the MEMORY mode, so routing them through
  // the `defaultAutopilot` preference would hand an autopilot session to
  // someone who came to this submenu to choose something else.
  const createEphemeralChatMutation = useMutation({
    mutationFn: (memoryMode: 'incognito' | 'temporary') => {
      setNewChatError('')
      return dispatch(createSlot({ agent: defaultAgent || undefined, mode: mode || '', memory_mode: memoryMode })).unwrap()
    },
    onSuccess: focusComposer,
    onError: onNewChatError,
  })
  return {
    createAutopilotMutation, crewPreview, openCrewMembers, remoteCrewChatPreview,
    createChatMutation, createRemoteChatMutation, createPlainChatMutation, createEphemeralChatMutation,
  }
}
