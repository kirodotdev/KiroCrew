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
import { useEffect, useRef } from 'react'
import { ExternalLink, Loader2, MessageSquarePlus, Sparkles, X } from 'lucide-react'
import { useAppDispatch } from '../../store'
import { switchSlot } from '../../store/chatSlice'
import ChatPage from '../../pages/ChatPage'

import { i18nT } from '../../i18n/t'

export interface CoAuthorPanelProps {
  /** The project's chat slot key, or null when none exists yet. */
  slotKey: string | null
  /** True while a slot create is in flight. */
  creating: boolean
  onStartSession: () => void
  onOpenFull: () => void
  onClose: () => void
}

export default function CoAuthorPanel({
  slotKey,
  creating,
  onStartSession,
  onOpenFull,
  onClose,
}: CoAuthorPanelProps) {
  const dispatch = useAppDispatch()
  const prevSlotRef = useRef<string | null>(null)

  // Activate the project's session. Re-dispatches when the project changes (a
  // different paper means a different slot) but not on unrelated re-renders.
  useEffect(() => {
    if (slotKey && slotKey !== prevSlotRef.current) {
      prevSlotRef.current = slotKey
      dispatch(switchSlot(slotKey))
    }
  }, [slotKey, dispatch])

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
          className="p-1 rounded text-muted hover:text-danger hover:bg-danger/10 cursor-pointer bg-transparent border-none transition-colors"
        >
          <X className="lucide-inline" />
        </button>
      </div>

      <div className="flex-1 min-h-0 flex flex-col overflow-hidden">
        {slotKey ? (
          <ChatPage embedded embedMode="chat" noUrlSync />
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
