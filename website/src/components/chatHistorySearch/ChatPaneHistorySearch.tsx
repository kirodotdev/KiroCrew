/** A bot chat pane's history search, drawn where the host keeps it.
 *
 *  It draws into `host`, the host's side panel tab (the full-width overlay on
 *  a phone), so it never covers the transcript; the host learns when to show
 *  that tab through `onOpenChange`. */
import { useEffect, useRef } from 'react'
import { createPortal } from 'react-dom'
import { i18nT } from '../../i18n/t'
import { lazyRetry } from '../lazyRetry'
import type { ChatHistorySearchProps } from './ChatHistorySearch'

// A chunk that fails to load shows its notice where the search draws, with Try Again.
// No hand-off: the pane's composer may hold an unsent draft.
const ChatHistorySearch = lazyRetry(() => import('./ChatHistorySearch'), {
  layout: 'page',
  title: () => i18nT('components.chatHistorySearch.load_failed'),
  scope: 'chat-history-search',
  askAgent: false,
})

interface ChatPaneHistorySearchProps extends Pick<ChatHistorySearchProps, 'slotKey' | 'bot' | 'onQuoteMessage'> {
  isOpen: boolean
  focusNonce: number
  onClose: () => void
  host?: HTMLElement | null
  onOpenChange?: (open: boolean) => void
}

export default function ChatPaneHistorySearch({ isOpen, focusNonce, onClose, host, onOpenChange, ...search }: ChatPaneHistorySearchProps) {
  const onOpenChangeRef = useRef(onOpenChange)
  onOpenChangeRef.current = onOpenChange
  const reportedRef = useRef(false)
  // Each ⌘F while open is reported again, so the host shows the tab even after
  // the person moved to another tab or hid the panel.
  useEffect(() => {
    if (isOpen) {
      reportedRef.current = true
      onOpenChangeRef.current?.(true)
    } else if (reportedRef.current) {
      reportedRef.current = false
      onOpenChangeRef.current?.(false)
    }
  }, [isOpen, focusNonce])
  // A pane that unmounts with the search open (a switch to the team view)
  // takes the host's tab with it.
  useEffect(() => () => { if (reportedRef.current) onOpenChangeRef.current?.(false) }, [])

  if (!isOpen) return null
  // In the host's tab the search can stay mounted out of sight, so the host's
  // element owns Escape while it is on screen (`historySearchHostMarks`).
  const body = <ChatHistorySearch {...search} onClose={onClose} focusNonce={focusNonce} escapeOwner={false} />
  return host ? createPortal(<div className="h-full min-h-0 flex flex-col" data-testid="chat-pane-history-search">{body}</div>, host) : null
}
