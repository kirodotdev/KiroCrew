/**
 * CoAuthorPanel — the embedded co-author chat for a Papyrus project.
 *
 * Mounts the FULL native ChatPage (`switchSlot()` + `<ChatPage embedded />`), the
 * same approach `ArtifactChatPanel` takes, so the co-author experience is
 * identical to the normal chat page: follow-up chips, question cards, collapsible
 * tool groups, regenerate, voice, approvals — all of it. `embedMode="chat"`
 * selects the single-session chrome (no sessions sidebar) and `noUrlSync` keeps
 * ChatPage's deep-link handling off the host route, which the Papyrus page owns.
 *
 * The upstream app hand-rolled its own chat: a raw WebSocket, exponential-backoff
 * reconnect, and a regex that stripped the agent's tool-use markup out of the
 * stream. That is a reimplementation of the dashboard's chat with none of its
 * affordances, and the markup-stripping regex is exactly the sort of thing that
 * silently breaks when the transcript format shifts. Mounting the real ChatPage
 * removes all of it.
 *
 * Session lifecycle (find-or-create, remember which slot belongs to which paper)
 * lives in `PapyrusPage`; this component activates whatever slot it is handed.
 */
import { useEffect, useRef, useState } from 'react'
import { ExternalLink, Loader2, MessageSquarePlus, Sparkles, X } from 'lucide-react'
import { useAppDispatch, useAppSelector } from '../../store'
import { switchSlot } from '../../store/chatSlice'
import ChatPage from '../../pages/ChatPage'
import ErrorNotice from '../../components/ErrorNotice'
import { Btn } from '../../components/ui'
import { errMessage, isMissingSlotError } from '../../utils/thunkError'

import { i18nT } from '../../i18n/t'

export interface CoAuthorPanelProps {
  /** The project's chat slot key, or null when none exists yet. */
  slotKey: string | null
  /** True while a slot create is in flight. */
  creating: boolean
  onStartSession: () => void
  /** The gateway answered "not found" for this slot key: the session is gone. */
  onSessionGone: (slotKey: string) => void
  /** The embedded chat replaced the session with a new one (a memory-mode pick). */
  onSessionReplaced: (oldKey: string, newKey: string) => void
  onOpenFull: () => void
  onClose: () => void
}

export default function CoAuthorPanel({
  slotKey,
  creating,
  onStartSession,
  onSessionGone,
  onSessionReplaced,
  onOpenFull,
  onClose,
}: CoAuthorPanelProps) {
  const dispatch = useAppDispatch()
  // The embedded ChatPage renders the dashboard's ACTIVE slot, whatever it is.
  const activeSlot = useAppSelector(state => state.chat.activeSlot)
  const slots = useAppSelector(state => state.dashboard.slots)
  // Read when a switch settles; a change to either must not re-run the switch.
  const activeSlotRef = useRef(activeSlot)
  activeSlotRef.current = activeSlot
  const slotsRef = useRef(slots)
  slotsRef.current = slots
  // The paper's session once switchSlot() has made it active. The chat is shown
  // only from then on: during the switch, and after a 404 makes the store put
  // back the chat that was active before, the panel shows no chat at all.
  const [readyKey, setReadyKey] = useState<string | null>(null)
  // Message of a switchSlot() failure other than a 404 (gateway down, 5xx).
  const [failure, setFailure] = useState('')
  // Bumped by the error message's "Try again" button to run the switch again.
  const [attempt, setAttempt] = useState(0)
  // Slot keys that existed when the paper's session was shown, and whether that
  // session has been seen in the slot list since: see the replacement effect.
  const knownAtReadyRef = useRef(new Set<string>())
  const seenInListRef = useRef(false)

  // Make the paper's session the active slot once per session key: when the
  // panel opens, the paper changes, or a session is started or adopted. A later
  // change of the active slot is not undone here, because the embedded chat
  // makes such changes itself (picking a memory mode replaces the session).
  useEffect(() => {
    setFailure('')
    if (!slotKey) return
    knownAtReadyRef.current = new Set()
    seenInListRef.current = false
    const ready = () => {
      knownAtReadyRef.current = new Set(slotsRef.current.map(s => s.key))
      setReadyKey(slotKey)
    }
    // Skip the switch only for a session that is active AND still listed: a
    // session deleted while the panel was closed can still be the active slot,
    // and showing it would show the chat the store falls back to a moment later.
    if (activeSlotRef.current === slotKey && slotsRef.current.some(s => s.key === slotKey)) {
      ready()
      return
    }
    let current = true
    dispatch(switchSlot(slotKey)).unwrap()
      .then(() => { if (current) ready() })
      .catch((err: unknown) => {
        // A deleted session: the page forgets the key and offers a new one.
        if (isMissingSlotError(err)) onSessionGone(slotKey)
        // Anything else is shown, with a "Try again" button; it is never retried
        // on its own, so a gateway outage does not loop.
        else if (current) setFailure(errMessage(err) || i18nT('store.chatSlice.session_open_error'))
      })
    return () => { current = false }
  }, [slotKey, attempt, dispatch, onSessionGone])

  // The paper's session left the slot list while it was shown. Picking a memory
  // mode in the embedded chat does this: it creates a new session, makes it
  // active, then deletes the old one, so the page adopts the new session. A
  // session that existed before the paper's was shown is never adopted -- that
  // is the neighbour the store falls back to when the session is deleted -- and
  // the page instead forgets the key and offers "Start a session".
  useEffect(() => {
    if (!slotKey || readyKey !== slotKey) return
    if (slots.some(s => s.key === slotKey)) { seenInListRef.current = true; return }
    // Not in a list that has not yet caught up with a just-created session.
    if (!seenInListRef.current) return
    const next = activeSlot
    if (next && next !== slotKey && !knownAtReadyRef.current.has(next)) {
      setReadyKey(next)
      onSessionReplaced(slotKey, next)
    } else {
      onSessionGone(slotKey)
    }
  }, [slots, activeSlot, slotKey, readyKey, onSessionGone, onSessionReplaced])

  return (
    <aside
      className="flex flex-col h-full min-h-0 border-l border-border bg-card overflow-hidden"
      aria-label={i18nT('apps.papyrus.coAuthor.co_author_chat')}
      data-testid="papyrus-co-author"
    >
      <div className="flex items-center gap-1 px-2 py-1.5 border-b border-border shrink-0">
        <Sparkles className="lucide-inline text-accent shrink-0" />
        <span className="flex-1 truncate text-[12px] font-medium text-text">
          {i18nT('apps.papyrus.coAuthor.co_author')}
        </span>
        {slotKey && (
          <button
            type="button"
            onClick={onOpenFull}
            title={i18nT('apps.papyrus.coAuthor.open_in_chat_page')}
            aria-label={i18nT('apps.papyrus.coAuthor.open_in_chat_page')}
            className="p-1 rounded text-muted hover:text-text hover:bg-bg-hover cursor-pointer bg-transparent border-none transition-colors"
          >
            <ExternalLink className="lucide-inline" />
          </button>
        )}
        <button
          type="button"
          onClick={onClose}
          title={i18nT('apps.papyrus.coAuthor.close_panel')}
          aria-label={i18nT('apps.papyrus.coAuthor.close_co_author_panel')}
          className="p-1 rounded text-muted hover:text-danger-text hover:bg-danger/10 cursor-pointer bg-transparent border-none transition-colors"
        >
          <X className="lucide-inline" />
        </button>
      </div>

      <div className="flex-1 min-h-0 flex flex-col overflow-hidden">
        {slotKey ? (
          failure ? (
            // No hand-off: `askAgent` navigates away from the Papyrus page, unmounting
            // its unsaved editor buffer, and the agent's session is what failed to open.
            // No dismiss button either: the panel would be left empty with no way
            // back, so the notice's only action is "Try again".
            <ErrorNotice
              className="m-2 animate-rise"
              message={failure}
              actionPlacement="below"
              footer={
                <Btn type="button" onClick={() => setAttempt(n => n + 1)}>
                  {i18nT('components.webPreviewPanel.try_again')}
                </Btn>
              }
            />
          ) : readyKey === slotKey ? <ChatPage embedded embedMode="chat" noUrlSync /> : null
        ) : creating ? (
          <div className="flex-1 flex items-center justify-center gap-2 text-muted text-[13px]" role="status">
            <Loader2 className="lucide-inline animate-spin motion-reduce:animate-none" />
            {i18nT('apps.papyrus.coAuthor.starting_session')}
          </div>
        ) : (
          <div className="flex-1 flex flex-col items-center justify-center gap-3 px-4 text-center text-muted text-[13px]">
            <span>{i18nT('apps.papyrus.coAuthor.no_co_author_session_for_this_paper_yet')}</span>
            <button
              type="button"
              onClick={onStartSession}
              className="inline-flex items-center gap-1.5 rounded-md border border-border bg-bg-elevated px-3 py-1.5 text-[13px] text-text hover:bg-bg-hover cursor-pointer transition-colors focus-ring"
            >
              <MessageSquarePlus className="lucide-inline" />
              {i18nT('apps.papyrus.coAuthor.start_a_session')}
            </button>
          </div>
        )}
      </div>
    </aside>
  )
}
