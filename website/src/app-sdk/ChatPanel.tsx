/**
 * ChatPanel — mounts the full KiroCrew ChatPage inside an app.
 *
 * ChatPage accepts `embedded` prop which skips URL sync effects.
 * ChatPanel dispatches switchSlot() to activate the workspace session.
 * The result is the complete native chat experience with no redirects.
 *
 * An app usually creates the session a moment before mounting the panel, and
 * the dashboard's slot list only learns of it from the next live push. ChatPage
 * clears an active slot that is missing from that list and falls back to the
 * last-used session, so the panel adds a placeholder row first, the same way
 * the artifact companion does, and refreshes the list to fill the row in.
 *
 * Usage:
 *   const { ChatPanel } = window.__kirocrew_modules['@kirocrew/app-sdk']
 *   <ChatPanel slotKey="coder-abc123" />
 */
import { useEffect, useRef, useState } from 'react'
import { useAppDispatch, useAppSelector, useAppStore } from '../store'
import { switchSlot } from '../store/chatSlice'
import { addSlotOptimistic, fetchSlots } from '../store/dashboardSlice'
import type { ChatSlot } from '../types'
import ChatPage from '../pages/ChatPage'
import ErrorNotice from '../components/ErrorNotice'
import { Btn } from '../components/ui'
import { errMessage, isMissingSlotError } from '../utils/thunkError'
import { i18nT } from '../i18n/t'

export interface ChatPanelProps {
  slotKey: string
  conversationOnly?: boolean
}

export default function ChatPanel({ slotKey, conversationOnly = false }: ChatPanelProps) {
  const dispatch = useAppDispatch()
  const store = useAppStore()
  // The embedded ChatPage renders the dashboard's ACTIVE slot.
  const activeSlot = useAppSelector(state => state.chat.activeSlot)
  // The handed session once switchSlot() has made it active.
  const [readyKey, setReadyKey] = useState<string | null>(null)
  // How the session became unusable: '' none, 'gone' a vanished session (no
  // retry can recover it), 'error' a transient failure worth a "Try again".
  const [failureKind, setFailureKind] = useState<'' | 'gone' | 'error'>('')
  const [failure, setFailure] = useState('')
  // Bumped by "Try again" to run the switch again.
  const [attempt, setAttempt] = useState(0)
  const prevSlotRef = useRef<string | null>(null)

  useEffect(() => {
    setFailure('')
    setFailureKind('')
    setReadyKey(null)
    if (!slotKey) return
    // The optimistic placeholder is added once per slot, not on a retry: the
    // row is already there, and re-adding a key the user is closing would drop
    // its close tombstone.
    if (slotKey !== prevSlotRef.current) {
      prevSlotRef.current = slotKey
      const { slots, closingSlots } = store.getState().dashboard
      if (!slots.some(s => s.key === slotKey) && !closingSlots?.[slotKey]) {
        dispatch(addSlotOptimistic({ key: slotKey, messages: 0, running: false } as ChatSlot))
        dispatch(fetchSlots())
      }
    }
    let current = true
    // Show the chat only once the switch has succeeded. A deleted session (404)
    // leaves the active slot on whatever chat the dashboard had open, so the
    // panel must never render — or send into — that other chat.
    dispatch(switchSlot(slotKey)).unwrap()
      .then(() => { if (current) setReadyKey(slotKey) })
      .catch((err: unknown) => {
        if (!current) return
        if (isMissingSlotError(err)) setFailureKind('gone')
        else { setFailureKind('error'); setFailure(errMessage(err) || i18nT('store.chatSlice.session_open_error')) }
      })
    return () => { current = false }
  }, [slotKey, attempt, dispatch, store])

  // In conversationOnly mode the panel shows a single session with NO sidebar,
  // so the user cannot legitimately change the active slot: once the switch has
  // settled, the active slot moving off our session means the session went away
  // (deleted elsewhere, the store falling back to a sibling) — treat it as gone
  // and never show that other chat. In the default (full) mode the embedded
  // ChatPage carries its own sidebar, where switching sessions is legitimate,
  // so there we do NOT re-gate (it would blank the whole panel on navigation).
  const vanished = conversationOnly && readyKey === slotKey && activeSlot !== slotKey
  const showChat = readyKey === slotKey && (!conversationOnly || activeSlot === slotKey)

  return (
    <div className="flex flex-col h-full min-h-0 overflow-hidden">
      {failureKind === 'gone' || vanished ? (
        // The session is gone for good: a retry would fail the same way.
        // Rendered through the shared ErrorNotice (errors-use-error-notice)
        // rather than a bare <span>. This panel has no new-chat affordance of
        // its own, so the footer states the way forward — reopen the session
        // from the host app.
        // No hand-off: a vanished session cannot be recovered by the agent (the
        // chat the hand-off would open is the one that is gone), and the panel
        // holds no unsaved draft — the composer lives inside the ChatPage that
        // never mounted.
        <ErrorNotice
          className="m-2 animate-rise"
          message={i18nT('appSdk.chatPanel.session_unavailable')}
          noHandoffReason="A vanished session cannot be reopened by the agent; the host app reopens it."
          actionPlacement="below"
          footer={i18nT('appSdk.chatPanel.session_unavailable_detail')}
        />
      ) : failureKind === 'error' ? (
        // A transient failure (gateway down, 5xx): "Try again" can recover. A
        // failed session load holds nothing unsaved in this panel (the composer
        // lives inside the ChatPage that never mounted), so the hand-off is safe.
        <ErrorNotice
          className="m-2 animate-rise"
          message={failure}
          askAgent
          actionPlacement="below"
          footer={
            <Btn type="button" onClick={() => setAttempt(n => n + 1)}>
              {i18nT('components.webPreviewPanel.try_again')}
            </Btn>
          }
        />
      ) : showChat ? (
        <ChatPage
          embedded
          embedMode={conversationOnly ? 'chat' : undefined}
          noUrlSync={conversationOnly || undefined}
          followupOpensFullPage={conversationOnly || undefined}
        />
      ) : (
        // Switch in flight: a status line, not a blank body.
        <div className="flex-1 flex items-center justify-center text-muted text-[13px]" role="status">
          <span className="animate-pulse">{i18nT('appSdk.chatPanel.starting_session')}</span>
        </div>
      )}
    </div>
  )
}
