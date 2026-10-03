import { useEffect, useRef, useState, type Dispatch, type SetStateAction } from 'react'
import { X } from 'lucide-react'

import ErrorNotice from '../../../components/ErrorNotice'
import VoicePlaybackNotice from '../../../components/VoicePlaybackNotice'
import { Btn } from '../../../components/ui'
import { i18nT } from '../../../i18n/t'
import type { useProvider } from '../../../providers'
import { useAppSelector, type AppDispatch, type RootState } from '../../../store'
import { clearSwitchSlotGone, clearUndeletableHistory, clearUnresumableResume, refreshSlot, switchSlotNoticeCopy } from '../../../store/chatSlice'
import { findSurfaceBySlotMode, surfaceLabel } from '../../../surfaces/registry'
import { slotChannelLabel } from '../../../utils/channelOrigin'
import { historyDeleteRefusalMessage } from '../../../utils/historyDeleteRefusal'

/**
 * Sentence for the unresumable-resume notice, built from the raw facts the chat
 * slice records (#5925).
 *
 * The slice stores `{ key, title, surface, reason }` rather than a finished
 * string because a reducer cannot localize: the label for a session's origin is
 * derived from its KEY, and that derivation lives at the render site. Keyed on
 * the stored key alone, because the resume being narrated often came from
 * another surface entirely (the command palette, a notification) whose row is
 * nowhere in this page's lists.
 *
 * `reason: 'failed'` gets its own sentence: nothing was resumed, so there is no
 * surface to name, and telling the user it "belongs to" somewhere would be a
 * guess.
 *
 * For `reason: 'surface'` the label is resolved, never interpolated raw. The
 * wire `surface` is a MACHINE value (`member`, `subagent`), so dropping it into
 * localized copy renders lowercase machine vocabulary mid-sentence -- and its
 * empty case reads "it's a Session session". So: the localized dashboard label
 * for a dashboard key, the channel label for a channel key, the surface
 * registry's own label when the mode is a registered surface, and otherwise a
 * sentence that names no surface at all -- and does not say "surface" either,
 * which is vocabulary a user meets only in settings prose.
 *
 * The registry lookup depends on `surfaces/builtins` having been imported (it
 * registers by module side effect, from `App.tsx`), which always holds wherever
 * this page renders. A miss degrades to the surface-free sentence rather than to
 * a wrong label, so the coupling cannot produce a lie.
 *
 * The message keys moved to this namespace with the notice; #3640's string said
 * "from the chat sidebar", which names a surface three of the four resume entry
 * points never touch. The two label keys stay under `pages.chatSidebar.*`
 * because the sidebar's own row still renders them.
 */
function unresumableNoticeMessage(r: { key: string; title: string; surface: string; reason: 'surface' | 'failed' }): string {
  const title = r.title || r.key
  if (r.reason === 'failed') {
    return i18nT('pages.chatPage.could_not_open_this_session', { title })
  }
  const registered = findSurfaceBySlotMode(r.surface)
  const surface = r.key.startsWith('dashboard')
    ? i18nT('pages.chatSidebar.dashboard_source')
    : slotChannelLabel(r.key) || (registered ? surfaceLabel(registered) : '')
  if (!surface) {
    return i18nT('pages.chatPage.this_session_is_not_a_chat_session', { title })
  }
  return i18nT('pages.chatPage.this_session_cannot_be_opened_in_chat', { title, surface })
}

interface ChatPaneNoticesProps {
  uploadHint: string
  setUploadHint: (hint: string) => void
  uploadError: string
  setUploadError: (error: string) => void
  sidError: string
  setSidError: (error: string) => void
  /** For the effort-options notice: the slot's ACP capability read failed. */
  activeSlot: string | null
  provider: ReturnType<typeof useProvider>
  selectionCapabilitiesQ: { isError: boolean }
  /** For the model-default notice: the Settings default model read failed. */
  chipDefault: { failed: boolean }
  actionError: { title?: string; message: string; preserveOnSwitch?: boolean } | null
  setActionError: Dispatch<SetStateAction<{ title?: string; message: string; preserveOnSwitch?: boolean } | null>>
  switchSlotGone: RootState['chat']['switchSlotGone']
  setVoiceRecoverySlot: (slot: string | null) => void
  pinError: string | null
  pinStatus: string | null
  dismissPinStatus: () => void
  unresumableResume: RootState['chat']['unresumableResume']
  undeletableHistory: RootState['chat']['undeletableHistory']
  /** Split view is showing: each grid pane carries its own failed-heal notice. */
  gridShown: boolean
  dispatch: AppDispatch
}

/**
 * The chat pane's notices above the transcript: upload validation and failure,
 * a dead `?sid=` link, unavailable effort options, the page's action failure, a
 * listed session that is gone, voice playback, pins, and the resume / delete
 * refusals every entry point converges on.
 */
export default function ChatPaneNotices({
  uploadHint,
  setUploadHint,
  uploadError,
  setUploadError,
  sidError,
  setSidError,
  activeSlot,
  provider,
  selectionCapabilitiesQ,
  chipDefault,
  actionError,
  setActionError,
  switchSlotGone,
  setVoiceRecoverySlot,
  pinError,
  pinStatus,
  dismissPinStatus,
  unresumableResume,
  undeletableHistory,
  gridShown,
  dispatch,
}: ChatPaneNoticesProps) {
  // The open chat's re-read after a serving change (an allow or revoke) failed
  // while its rows were still marked: some may show content the change revoked,
  // so say so with a Retry rather than leave a revoked link silently clickable.
  const activeHealFailed = useAppSelector(s => !!(s.chat.activeSlot && s.chat.slotHealFailed?.[s.chat.activeSlot]))
  // The notice stays up while its Retry is in flight, so the button says it is busy.
  const [healRetrying, setHealRetrying] = useState(false)
  // Bumped as each Retry settles: re-keying the notice re-mounts it, so a retry
  // that failed again is announced again rather than reading as an ignored click.
  // Back to 0 once the heal is over, so a later failure reads as a first one.
  const [healAttempts, setHealAttempts] = useState(0)
  useEffect(() => { if (!activeHealFailed) setHealAttempts(0) }, [activeHealFailed])
  // The re-mount above destroys the Retry button; a keyboard user who pressed it
  // must not be dropped to <body> on every attempt. Set on press, read by the new
  // button's ref as it mounts, and cleared once that commit has run.
  const healRetryFocusRef = useRef(false)
  useEffect(() => { healRetryFocusRef.current = false }, [healAttempts])
  const showHeal = !!activeSlot && activeHealFailed && !gridShown
  return (
    <>
      {/* Pane-level notices above the composer. Every ErrorNotice here has the
          hand-off ON: the composer beneath holds a live draft, but it is
          persisted per slot on every keystroke and on slot switch (the
          page's draft persistence), and an in-chat hand-off opens a FRESH slot
          without navigating away -- so the draft survives. */}
      {uploadHint && (
        <div role="status" className="mx-4 mt-2 mb-0 bg-bg-elevated border rounded-lg p-3 flex items-center gap-3 animate-rise" style={{ borderColor: 'color-mix(in srgb, var(--warn) 45%, transparent)' }}>
          <span className="text-sm text-text flex-1">{uploadHint}</span>
          <Btn onClick={() => setUploadHint('')} aria-label={i18nT('app.dismiss')} className="shrink-0 px-1.5 py-0.5 text-muted hover:text-text"><X className="w-3.5 h-3.5" /></Btn>
        </div>
      )}
      <ErrorNotice
        message={uploadError}
        onDismiss={() => setUploadError('')}
        askAgent
        className="mx-4 mt-2 mb-0 animate-rise"
        testId="upload-error"
      />
      <ErrorNotice
        message={sidError}
        onDismiss={() => setSidError('')}
        askAgent
        className="mx-4 mt-2 mb-0 animate-rise"
        testId="sid-error"
      />
      {/* No hand-off: navigating away would discard the unsent composer draft;
          the Retry is the recovery for the failed re-read. Not in split view:
          each grid pane shows its own slot's notice and Retry, so this banner
          would repeat the open chat's beside it. */}
      <ErrorNotice
        key={`heal-${healAttempts}`}
        message={showHeal ? i18nT(healAttempts > 0 ? 'components.chatPane.heal_failed_again' : 'components.chatPane.heal_failed') : ''}
        footer={showHeal && activeSlot
          // `aria-disabled`, not `disabled`: a disabled button drops keyboard focus.
          // Dimmed less than a disabled button, so "Retrying…" stays legible.
          ? <Btn
              // One-shot: this inline ref re-attaches on every commit, so a flag left
              // set would pull focus back here on each re-render (a keystroke in the
              // composer re-renders this) for as long as the retry is in flight.
              ref={el => { if (el && healRetryFocusRef.current) { healRetryFocusRef.current = false; el.focus() } }}
              aria-disabled={healRetrying}
              aria-busy={healRetrying}
              className={healRetrying ? 'opacity-60 cursor-not-allowed' : undefined}
              onClick={e => {
                if (healRetrying) return
                // Armed only as the retry settles, right before the re-mount it
                // targets, so the re-renders during the retry cannot consume it.
                const hadFocus = document.activeElement === e.currentTarget
                setHealRetrying(true)
                void Promise.resolve(dispatch(refreshSlot(activeSlot))).finally(() => { healRetryFocusRef.current = hadFocus; setHealRetrying(false); setHealAttempts(n => n + 1) })
              }}
              data-testid="heal-retry"
            >{i18nT(healRetrying ? 'components.chatPane.heal_retrying' : 'components.chatPane.retry')}</Btn>
          : undefined}
        className="mx-4 mt-2 mb-0 animate-rise"
        testId="heal-error"
      />
      {/* No hand-off: navigating away would discard the unsent composer draft. */}
      <ErrorNotice
        message={activeSlot && provider.capabilities.reasoningEffort && selectionCapabilitiesQ.isError
          ? i18nT('pages.chatPage.effort_options_unavailable') : ''}
        className="mx-4 mt-2 mb-0 animate-rise"
        testId="effort-capabilities-error"
      />
      {/* No hand-off: navigating away would discard the unsent composer draft. */}
      <ErrorNotice
        message={activeSlot && chipDefault.failed
          ? i18nT('pages.settings.chatPanel.failed_to_load_config') : ''}
        className="mx-4 mt-2 mb-0 animate-rise"
        testId="model-default-error"
      />
      <ErrorNotice
        title={actionError?.title}
        message={actionError?.message}
        onDismiss={() => setActionError(null)}
        askAgent
        className="mx-4 mt-2 mb-0 animate-rise"
        testId="action-error"
      />
      {/* A click on a listed-but-gone session (#6372): the fact at the click
          locus, through the required ErrorNotice surface. The store carries
          the NAME; the sentence resolves here so a locale switch re-renders it. */}
      <ErrorNotice
        message={switchSlotGone ? switchSlotNoticeCopy(switchSlotGone.kind, switchSlotGone.name) : ''}
        report={switchSlotGone?.report}
        onDismiss={() => dispatch(clearSwitchSlotGone())}
        askAgent
        className="mx-4 mt-2 mb-0 animate-rise"
        testId="switch-slot-gone"
      />
      <VoicePlaybackNotice slot={activeSlot} onBlockedSlotChange={setVoiceRecoverySlot} />
      <ErrorNotice
        message={pinError}
        onDismiss={dismissPinStatus}
        askAgent
        className="mx-4 mt-2 mb-0 animate-rise"
        testId="pin-error"
      />
      {pinStatus && (
        <div role="status" className="mx-4 mt-2 mb-0 bg-bg-elevated border rounded-lg p-3 flex items-center gap-3 animate-rise" style={{ borderColor: 'color-mix(in srgb, var(--warn) 45%, transparent)' }}>
          <span className="text-sm text-text flex-1">{pinStatus}</span>
          <button onClick={dismissPinStatus} aria-label={i18nT('app.dismiss')} className="text-muted hover:text-text leading-none p-0.5"><X className="w-4 h-4" /></button>
        </div>
      )}
      {/* Every resume entry point converges here (#5925): the sidebar row,
          this page's own "Continue a previous chat" list, the notification
          panel's Resume button and the two command-palette providers all end
          on /chat -- and the two providers are plain modules with no component
          of their own, so one shared site is what lets them narrate at all.

          It sits with the pane-level banners, OUTSIDE ChatPage's
          split / no-slot / transcript ternary, because a resume can land
          here with NO active slot at all (a palette or notification resume
          while no tab is open) -- and that ternary's `!activeSlot` branch
          renders only the empty state, so a notice placed inside the transcript
          branch was silent in exactly that case.

          Deliberately NOT in the sidebar, where #3640 first put it: that
          pane's Older Sessions section starts closed, so a notice inside it is
          invisible to anyone who had not already opened it, which is everyone
          arriving from the other three paths. */}
      {unresumableResume && (
        <div className="mx-4 mt-2 mb-0" data-testid="unresumable-resume-error">
          {/* Hand-off on. The composer beneath holds a live draft, but it is
              persisted per slot on every keystroke and on slot switch (the
              page's draft persistence), and an in-chat hand-off opens a FRESH
              slot without navigating away -- so the draft survives. */}
          <ErrorNotice
            message={unresumableNoticeMessage(unresumableResume)}
            onDismiss={() => dispatch(clearUnresumableResume())}
            variant="block"
            askAgent
          />
        </div>
      )}
      {undeletableHistory && (
        <div className="mx-4 mt-2 mb-0" data-testid="undeletable-history-error">
          {/* Same site and shape as the unresumable notice above: a sidebar
              click the gateway answered with a refusal, narrated here because
              the row it names is still in the sidebar and looks untouched.
              The sentence is chosen from the gateway's `code`, so the remedy
              matches the cause (release the cron jobs / retry / repair). */}
          <ErrorNotice
            message={historyDeleteRefusalMessage(undeletableHistory)}
            report={undeletableHistory.report}
            onDismiss={() => dispatch(clearUndeletableHistory())}
            variant="block"
            askAgent
          />
        </div>
      )}
    </>
  )
}
