import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { useQuery, type QueryClient } from '@tanstack/react-query'

import { api } from '../../../api/client'
import { useAnchoredTriggerRect } from '../../../hooks/useAnchoredTriggerRect'
import { useFolderSortMode } from '../../../hooks/useFolderSortMode'
import { dashboardSessionKey, SESSION_CONTROL_STATUS_KEY, useSessionControls, useSessionControlStatuses } from '../../../hooks/useSessionControls'
import type { ChatFolder, ChatSlot } from '../../../types'

/** One shared empty list, so an absent folder cache keeps a stable identity. */
const NO_CHAT_FOLDERS: ChatFolder[] = []

interface ComposerSessionControlsOptions {
  activeSlot: string | null
  currentSlot: ChatSlot | undefined
  queryClient: QueryClient
}

/**
 * App-contributed session controls for the composer bar: which exist, which one
 * is open (bound to the chat it was opened in), their status chips, and the
 * folder context each is handed -- the folder list and sort order the
 * folder-suggestion card also reads.
 */
export function useComposerSessionControls({ activeSlot, currentSlot, queryClient }: ComposerSessionControlsOptions) {
  // App-contributed session controls (contributes.sessionControls). Discovered once;
  // openSessionControl holds the composite `${app}:${id}` key of the open one
  // plus the slot it was opened in, so at most one control popover is mounted
  // at a time, and only against the chat it was opened for.
  const { controls: sessionControls, error: sessionControlsError } = useSessionControls()
  const [openSessionControl, setOpenSessionControl] =
    useState<{ key: string; slot: string } | null>(null)
  const { rect: sessionControlRect, anchorTo: anchorSessionControl } = useAnchoredTriggerRect(
    !!openSessionControl && openSessionControl.slot === activeSlot,
  )
  // Re-poll a control's status when its popover closes: that is when the user
  // has most likely just changed the thing the chip reports. A finished turn
  // re-polls too, from the socket layer (refreshSessionControlStatusesAfterTurn).
  // React Query owns the cache, so this is an invalidation rather than a token
  // the hook watches.
  const refreshSessionControlStatuses = useCallback(() => {
    queryClient.invalidateQueries({ queryKey: [SESSION_CONTROL_STATUS_KEY] })
  }, [queryClient])
  // Drop the open-control state when the chat changes. Correctness does not
  // depend on this effect: the host render is gated on the captured opening
  // slot matching activeSlot, so the committed render after a chat switch
  // mounts nothing against the new session. This only resets the state so the
  // control does not reappear if the user switches back to the original chat.
  useEffect(() => {
    setOpenSessionControl(null)
  }, [activeSlot])
  // Folder names for the session-control context. Shares the sidebar's own
  // ['chat-folders'] cache, so this costs no extra request.
  const { data: chatFoldersRaw, error: chatFoldersError } = useQuery<ChatFolder[]>({
    queryKey: ['chat-folders'],
    // Guard the call, not just its rejection: a partially-mocked `api` (tests,
    // or any future trimmed surface) would throw synchronously here and take
    // the whole chat page down. A folder name is a label — never worth that.
    queryFn: () =>
      typeof api?.chatFolders === 'function' ? api.chatFolders() : Promise.resolve([]),
  })
  // Normalize the shape, not just the absence: a generic fetch mock (or a
  // future payload change) can resolve to a non-array, and `= []` only covers
  // undefined — which crashed the whole chat page on `.find`.
  const chatFolders: ChatFolder[] = Array.isArray(chatFoldersRaw) ? chatFoldersRaw : NO_CHAT_FOLDERS
  // Folder data is usable whenever the cache holds it: a failed background
  // refetch sets `error` but keeps the last good `data`, and the mobile
  // new-chat button must keep resolving against that data.
  const chatFoldersLoaded = chatFoldersRaw !== undefined
  // One create at a time from the mobile new-chat button: the ref blocks a
  // second tap synchronously, the state disables the button until it settles.
  const mobileNewSessionInFlightRef = useRef(false)
  const [mobileNewSessionBusy, setMobileNewSessionBusy] = useState(false)
  // The sidebar's folder sort mode, for the folder-suggestion card's option list:
  // the card draws the same tree the sidebar draws and must list it in the same
  // order. Read here (shared kirocrewConfig query) so the card stays pure. The
  // read's failure travels too, for the one case the sidebar's banner cannot
  // cover: this screen with no sidebar on it (see `sidebarOnScreen` below).
  const { mode: folderSortMode, error: folderSortError } = useFolderSortMode()
  const activeFolderName =
    chatFolders.find(f => f.id === currentSlot?.folder_id)?.name || ''
  // The session IDENTITY, not the display slot. `activeSlot` is the slot id
  // (`chat-2`); the key the rest of the system stores session-scoped state under
  // is `dashboard:<slot>` — the same derivation MobileConnectModal, ChatInput's
  // skill slot and workflows/runModel use. Handing an app the bare slot would
  // key its per-session state on a string nothing else uses, which is precisely
  // the mis-binding this feature exists to remove.
  const sessionControlKey = dashboardSessionKey(activeSlot)
  const { statuses: sessionControlStatuses, error: sessionControlStatusError } =
    useSessionControlStatuses(
      sessionControls,
      sessionControlKey,
      currentSlot?.folder_id || '',
      activeFolderName,
    )
  return {
    sessionControls, sessionControlsError,
    openSessionControl, setOpenSessionControl, sessionControlRect, anchorSessionControl,
    refreshSessionControlStatuses,
    chatFolders, chatFoldersError, chatFoldersLoaded, folderSortMode, folderSortError, activeFolderName,
    mobileNewSessionInFlightRef, mobileNewSessionBusy, setMobileNewSessionBusy,
    sessionControlKey, sessionControlStatuses, sessionControlStatusError,
  }
}

/** The composer bar's chip model for each control. */
export function useSessionControlChips({ sessionControls, openSessionControl, activeSlot, sessionControlStatuses }: {
  sessionControls: ReturnType<typeof useSessionControls>['controls']
  openSessionControl: { key: string; slot: string } | null
  activeSlot: string | null
  sessionControlStatuses: ReturnType<typeof useSessionControlStatuses>['statuses']
}) {
  return useMemo(() => sessionControls.map(sc => ({
    key: sc.key,
    label: sc.label,
    icon: sc.icon,
    active: openSessionControl?.key === sc.key && openSessionControl.slot === activeSlot,
    state: sessionControlStatuses[sc.key]?.state,
    statusTooltip: sessionControlStatuses[sc.key]?.tooltip,
    detail: sessionControlStatuses[sc.key]?.detail,
  })), [sessionControls, openSessionControl, activeSlot, sessionControlStatuses])
}
