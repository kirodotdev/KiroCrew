import { useEffect, useState } from 'react'
import { useIsMobile } from '../hooks/useIsMobile'
import { Plus, ExternalLink, X, Sparkles } from 'lucide-react'
import { useAppDispatch, useAppSelector } from '../store'
import { switchSlot } from '../store/chatSlice'
import ChatPage from '../pages/ChatPage'
import ErrorNotice from './ErrorNotice'
import { Btn } from './ui'
import { errMessage, isMissingSlotError } from '../utils/thunkError'

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
 * useArtifactDetailPageCompanion.ts (the open path in ArtifactDetailPage.tsx); this component activates whatever bound slot it is handed
 * via the global active-session pointer. That global switch is deliberate: the
 * companion session IS your active session while you work on the artifact, so
 * /chat lands on it afterwards.
 *
 * Composer staging ("Draft a prompt in chat") rides the existing `writePrefill`
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
  // The embedded ChatPage renders the dashboard's ACTIVE slot. This panel has
  // no sidebar (embedMode="chat"), so the user cannot legitimately change the
  // active slot from inside it: once our switch has settled, the active slot
  // moving off our session means the session went away (deleted elsewhere, the
  // store falling back to a sibling). We re-gate on it and never show that
  // other chat.
  const activeSlot = useAppSelector(state => state.chat.activeSlot)
  // The bound session once switchSlot() has made it active. The chat is shown
  // only from then on.
  const [readyKey, setReadyKey] = useState<string | null>(null)
  // How the session became unusable: '' none, 'gone' a 404 / vanished session
  // (a retry cannot recover it — offer a new chat instead), 'error' a transient
  // failure (gateway down, 5xx) worth a "Try again".
  const [failureKind, setFailureKind] = useState<'' | 'gone' | 'error'>('')
  // Message shown for a transient failure.
  const [failure, setFailure] = useState('')
  // Bumped by "Try again" to run the switch again.
  const [attempt, setAttempt] = useState(0)

  // Activate the bound session and show the chat only once the switch has
  // succeeded. Re-dispatches on rebind (New chat replaces the slot), on
  // "Try again", but not on unrelated re-renders. A deleted session (404)
  // leaves the active slot on whatever chat the dashboard had open, so the
  // panel must never render — or send into — that other chat.
  useEffect(() => {
    setFailure('')
    setFailureKind('')
    setReadyKey(null)
    if (!slotKey) return
    let current = true
    dispatch(switchSlot(slotKey)).unwrap()
      .then(() => { if (current) setReadyKey(slotKey) })
      .catch((err: unknown) => {
        if (!current) return
        // A deleted session: an explicit "unavailable" state with a way to
        // start a new chat, never the chat the store fell back to.
        if (isMissingSlotError(err)) setFailureKind('gone')
        // Anything else (gateway down, 5xx) is a transient failure, shown with
        // "Try again"; it is never retried on its own, so an outage does not loop.
        else { setFailureKind('error'); setFailure(errMessage(err) || i18nT('store.chatSlice.session_open_error')) }
      })
    return () => { current = false }
  }, [slotKey, attempt, dispatch])

  // The switch settled on our session but the active slot has since moved off
  // it: with no sidebar here, that only happens when the session went away and
  // the store fell back to another one. Treat it as a gone session.
  const vanished = readyKey === slotKey && activeSlot !== slotKey
  // The chat is shown only while the bound session is the active slot.
  const showChat = readyKey === slotKey && activeSlot === slotKey

  // 480px cannot fit a phone viewport, and this row has no horizontal scroll,
  // so the overhang was clipped rather than reachable. While narrow the panel
  // takes the width and the artifact body steps aside.
  // FLEX-DRIVEN HEIGHT: the column bounds the viewport, so the panel takes what the notice beside
  // it leaves -- a fixed calc() here lands the composer past the fold whenever both mount.
  return (
    <aside
      className={`${isMobile ? 'w-full' : 'w-[480px] shrink-0'} flex flex-col flex-1 min-h-0 rounded-xl border border-border bg-card overflow-hidden`}
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
          className="p-1 rounded text-muted hover:text-danger-text hover:bg-danger/10 cursor-pointer bg-transparent border-none transition-colors"
          title={i18nT('components.artifactChatPanel.close_panel_the_session_stays_active')}
          aria-label={i18nT('components.artifactChatPanel.close_chat_panel')}
        >
          <X size={13} />
        </button>
      </div>
      <div className="flex-1 min-h-0 flex flex-col overflow-hidden">
        {slotKey ? (
          failureKind === 'gone' || vanished ? (
            // The session is gone for good: a 404, or it vanished while open.
            // Rendered through the shared ErrorNotice (errors-use-error-notice)
            // so a rejected-request failure uses the one error surface, not a
            // bare <span>. "Try again" would just 404 again, so the forward
            // action is a new chat — the panel's own New-chat handler, in the
            // footer.
            // No hand-off: a vanished session cannot be recovered by the agent
            // (the chat the hand-off would open is the one that is gone), and
            // this panel holds no unsaved draft — the composer lives inside the
            // ChatPage that never mounted. The way forward is Start chat.
            <ErrorNotice
              className="m-2 animate-rise"
              message={i18nT('components.artifactChatPanel.session_unavailable')}
              noHandoffReason="A vanished session cannot be reopened by the agent; the forward action is a new chat."
              actionPlacement="below"
              footer={
                <button
                  type="button"
                  onClick={onNewChat}
                  className="px-2.5 py-1 rounded-md text-[12px] font-medium border border-accent text-accent-fg bg-accent cursor-pointer hover:bg-accent-hover transition-all"
                >
                  {i18nT('components.artifactChatPanel.start_chat')}
                </button>
              }
            />
          ) : failureKind === 'error' ? (
            // A transient failure (gateway down, 5xx): "Try again" can recover.
            // A failed session load holds nothing unsaved in this panel (the
            // composer lives inside the ChatPage that never mounted), so the
            // agent hand-off is safe to offer.
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
            <ChatPage embedded embedMode="chat" noUrlSync followupOpensFullPage />
          ) : (
            // Switch in flight: a status line, not a blank body.
            <div className="flex-1 flex items-center justify-center text-muted text-[13px]" role="status">
              <span className="animate-pulse">{i18nT('components.artifactChatPanel.starting_session')}</span>
            </div>
          )
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
