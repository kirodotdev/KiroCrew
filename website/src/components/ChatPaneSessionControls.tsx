import { useQueryClient } from '@tanstack/react-query'

import { useComposerSessionControls, useSessionControlChips } from '../pages/chat/page/sessionControls'
import type { ChatInputProps } from './chat-input/props'
import ErrorNotice from './ErrorNotice'
import SessionControlHost from './SessionControlHost'
import { i18nT } from '../i18n/t'
import type { ChatSlot } from '../types'

/**
 * App session controls (`contributes.sessionControls`) for a pane's composer,
 * bound to the pane's own slot. The same hook, chips and host the chat page
 * uses; only the slot differs, so a control opened here is handed this pane's
 * `dashboard:<slot>` session and never the main chat's.
 *
 * Off unless the host opts in (`appSessionControls`): with it off the pane gets
 * no chips, no notice and no host, and no status is polled.
 */
export function useChatPaneSessionControls(slotKey: string, paneSlot: ChatSlot | undefined, enabled: boolean) {
  const queryClient = useQueryClient()
  const activeSlot = enabled ? slotKey : null
  const {
    sessionControls, sessionControlsError, sessionControlStatusError, chatFoldersError,
    openSessionControl, setOpenSessionControl, sessionControlRect, anchorSessionControl,
    refreshSessionControlStatuses, sessionControlKey, sessionControlStatuses, activeFolderName,
  } = useComposerSessionControls({ activeSlot, currentSlot: paneSlot, queryClient })
  const chips = useSessionControlChips({ sessionControls, openSessionControl, activeSlot, sessionControlStatuses })
  if (!activeSlot) return { sessionControls: undefined, onSessionControlClick: undefined, notice: null, host: null }

  const onSessionControlClick: ChatInputProps['onSessionControlClick'] = (key, rect, trigger) => {
    anchorSessionControl(rect, trigger)
    if (openSessionControl?.key === key) {
      setOpenSessionControl(null)
      refreshSessionControlStatuses()
    } else {
      setOpenSessionControl({ key, slot: activeSlot })
    }
  }
  // Same gate as the chat page's dock: mount only against the slot the control
  // was opened in, so a Bot switch never hands it the next thread's session.
  const open = openSessionControl?.slot === activeSlot
    ? sessionControls.find(c => c.key === openSessionControl.key)
    : undefined
  const failure = sessionControlsError || sessionControlStatusError
    || (sessionControls.length > 0 ? chatFoldersError : null)
  return {
    sessionControls: chips,
    onSessionControlClick,
    notice: failure ? (
      <ErrorNotice
        variant="inline"
        className="mx-4 mt-2"
        testId="chat-pane-session-controls-error"
        title={i18nT('components.sessionControlHost.controls_unavailable')}
        message={failure.message}
      />
    ) : null,
    host: open ? (
      // Keyed on the control: the host's error boundary only resets on remount.
      <SessionControlHost
        key={open.key}
        control={open}
        anchorRect={sessionControlRect}
        onClose={() => {
          setOpenSessionControl(null)
          refreshSessionControlStatuses()
        }}
        session={{
          sessionKey: sessionControlKey,
          folderId: paneSlot?.folder_id || '',
          folderName: activeFolderName,
          cwd: paneSlot?.project || '',
        }}
      />
    ) : null,
  }
}
