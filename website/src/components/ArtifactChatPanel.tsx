import { useEffect, useRef } from 'react'
import { useIsMobile } from '../hooks/useIsMobile'
import { Plus, ExternalLink, X, Sparkles } from 'lucide-react'
import { useAppDispatch } from '../store'
import { switchSlot } from '../store/chatSlice'
import ChatPage from '../pages/ChatPage'

import { i18nT } from '../i18n/t'

/**
 * ArtifactChatPanel — the embedded companion chat for an artifact.
 *
 * Embeds the FULL native ChatPage (`switchSlot()` + `<ChatPage embedded />`) so
 * the chatting experience is identical to the normal chat page — OPTIONS
 * follow-up chips, question cards, stop-event cards, steer-send, collapsible
 * tool groups, regenerate, voice, the works. `embedMode="chat"` selects the
 * single-session chrome (no sessions sidebar) and `noUrlSync` keeps ChatPage's
 * deep-link sync off the host route — the panel lives on /artifacts/:slug,
 * which the page owns.
 *
 * Session lifecycle (binding resolution, create, archive) lives in
 * ArtifactDetailPage; this component activates whatever bound slot it is handed
 * via the global active-session pointer. That global switch is deliberate: the
 * companion session IS your active session while you work on the artifact, so
 * /chat lands on it afterwards.
 *
 * Composer staging ("Ask agent to address") rides the existing `writePrefill`
 * sessionStorage channel that ChatPage already consumes when the slot
 * activates — no ChatPage fork needed.
 *
 * Header actions:
 * - **New chat** — archives the current bound session and starts a fresh one
 *   (the page enforces archive-then-create so the ≤1-active-bound-session
 *   invariant never observably breaks).
 * - **Open in chat page** — full-page escape hatch; routes through the page's
 *   nav dispatcher so it forwards correctly from popout windows.
 * - **×** — closes the *panel* only; the session stays active and bound.
 */
export function ArtifactChatPanel({
  slotKey,
  creating,
  onNewChat,
  onOpenFull,
  onClose,
}: {
  /** Bound session's slot key, or null when no active bound session exists. */
  slotKey: string | null
  /** True while a bound session create is in flight. */
  creating: boolean
  onNewChat: () => void
  onOpenFull: () => void
  onClose: () => void
}) {
  const isMobile = useIsMobile()
  const dispatch = useAppDispatch()
  const prevSlotRef = useRef<string | null>(null)

  // Activate the bound session. Re-dispatches on rebind (New chat replaces the
  // slot) but not on unrelated re-renders.
  useEffect(() => {
    if (slotKey && slotKey !== prevSlotRef.current) {
      prevSlotRef.current = slotKey
      dispatch(switchSlot(slotKey))
    }
  }, [slotKey, dispatch])

  // 480px cannot fit a phone viewport, and this row has no horizontal scroll,
  // so the overhang was clipped rather than reachable. While narrow the panel
  // takes the width and the artifact body steps aside.
  return (
    <aside
      className={`${isMobile ? 'w-full' : 'w-[480px] shrink-0'} flex flex-col rounded-xl border border-border bg-card overflow-hidden`}
      style={{ height: 'calc(100vh - 240px)', minHeight: 480 }}
      aria-label={i18nT('components.artifactChatPanel.artifact_companion_chat')}
    >
      <div className="flex items-center gap-1 px-2 py-1.5 border-b border-border shrink-0">
        <Sparkles size={13} className="text-accent shrink-0" />
        <span className="text-[12px] font-medium text-text flex-1 truncate">
          {i18nT('components.artifactChatPanel.agent_chat')}
        </span>
        {slotKey && (
          <button
            type="button"
            onClick={onNewChat}
            className="p-1 rounded text-muted hover:text-text hover:bg-bg-hover cursor-pointer bg-transparent border-none transition-colors"
            title={i18nT('components.artifactChatPanel.new_chat_archive_this_conversation_and_start_fresh')}
            aria-label={i18nT('components.artifactChatPanel.new_chat')}
          >
            <Plus size={13} />
          </button>
        )}
        {slotKey && (
          <button
            type="button"
            onClick={onOpenFull}
            className="p-1 rounded text-muted hover:text-text hover:bg-bg-hover cursor-pointer bg-transparent border-none transition-colors"
            title={i18nT('components.artifactChatPanel.open_in_chat_page')}
            aria-label={i18nT('components.artifactChatPanel.open_in_chat_page')}
          >
            <ExternalLink size={13} />
          </button>
        )}
        <button
          type="button"
          onClick={onClose}
          className="p-1 rounded text-muted hover:text-danger hover:bg-danger/10 cursor-pointer bg-transparent border-none transition-colors"
          title={i18nT('components.artifactChatPanel.close_panel_the_session_stays_active')}
          aria-label={i18nT('components.artifactChatPanel.close_chat_panel')}
        >
          <X size={13} />
        </button>
      </div>
      <div className="flex-1 min-h-0 flex flex-col overflow-hidden">
        {slotKey ? (
          <ChatPage embedded embedMode="chat" noUrlSync />
        ) : creating ? (
          <div className="flex-1 flex items-center justify-center text-muted text-[13px]" role="status">
            <span className="animate-pulse">{i18nT('components.artifactChatPanel.starting_session')}</span>
          </div>
        ) : (
          <div className="flex-1 flex flex-col items-center justify-center gap-2 text-muted text-[13px] px-4 text-center">
            <span>{i18nT('components.artifactChatPanel.no_active_session_for_this_artifact')}</span>
            <button
              type="button"
              onClick={onNewChat}
              className="px-2.5 py-1 rounded-md text-[12px] font-medium border border-accent text-accent-fg bg-accent cursor-pointer hover:bg-accent-hover transition-all"
            >
              {i18nT('components.artifactChatPanel.start_chat')}
            </button>
          </div>
        )}
      </div>
    </aside>
  )
}
