import type { Dispatch, SetStateAction } from 'react'
import { X } from 'lucide-react'

import ErrorNotice from '../../../components/ErrorNotice'
import VoicePlaybackNotice from '../../../components/VoicePlaybackNotice'
import { Btn } from '../../../components/ui'
import { i18nT } from '../../../i18n/t'
import { selectionCapabilitiesFailed } from '../../../lib/effort'
import type { useProvider } from '../../../providers'
import type { AppDispatch, RootState } from '../../../store'
import { clearSwitchSlotGone, clearUndeletableHistory, clearUnresumableResume, switchSlotNoticeCopy } from '../../../store/chatSlice'
import { findSurfaceBySlotMode, surfaceLabel } from '../../../surfaces/registry'
import { slotChannelLabel } from '../../../utils/channelOrigin'
import { historyDeleteRefusalMessage } from '../../../utils/historyDeleteRefusal'
import { useUnrestoredTabs } from './useUnrestoredTabs'

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

/**
 * "N tabs were not restored", with the way to get them back.
 *
 * The user-visible half of #18252. A tab that the startup restore listed but
 * could not show leaves no other trace a person can see: the session is intact,
 * nothing was closed and nothing was deleted, so the sidebar simply has fewer
 * rows than it did before the restart, and the only way to notice was to
 * remember what used to be there.
 *
 * `?history=1` is the remedy rather than a reopen button, and deliberately so.
 * The keys are still in the reopen seed, so the next restart may well bring them
 * back on its own -- what the user needs NOW is the pane that lists every session
 * by name, which is the one place a tab can be identified and reopened. That pane
 * already answers the param on arrival, so pointing at it costs no new mechanism.
 *
 * Its own component because it owns a fetch: a hook called in the parent would
 * run on every chat page render whether or not a notice was ever shown.
 */
export function UnrestoredTabsNotice() {
  const { count, dismiss, failed, unknowable } = useUnrestoredTabs()
  if (failed || unknowable) {
    return (
      <div className="mx-4 mt-2 mb-0" data-testid="unrestored-tabs-read-failed">
        {/* Two ways to reach the same sentence, and both have to reach it. The read
            itself failed, or it succeeded and the answer is that the gateway could
            not read which tabs were open -- a restore that FINISHED with an
            unknowable set, where tabs may be missing and no count exists to show.
            Silence in either case repeats the very defect this notice exists for:
            the user would read an absent banner as "nothing was dropped" when the
            truth is that nobody could ask, or nobody could know. No dismiss -- a
            failed query clears this by succeeding, and a dismissed unknown is worse
            than a visible one. askAgent for the same reason the count's notice has
            it: the agent can read the gateway log, which is where the refusal that
            made the set unknowable is recorded. */}
        <ErrorNotice
          message={i18nT('pages.chatPage.could_not_check_restored_tabs')}
          variant="block"
          askAgent
          testId="unrestored-tabs-read-error"
        />
      </div>
    )
  }
  if (count <= 0) return null
  return (
    <div className="mx-4 mt-2 mb-0" data-testid="unrestored-tabs-notice">
      {/* Through ErrorNotice, not a hand-written status box. A tab the restore could
          not rebuild is a FAILED read, and `errors-use-error-notice` exists because a
          failure toned down to a polite status is still a failure: the reader loses
          the error affordances, the journal lookup and the hand-off.

          askAgent ON, the same decision every notice in this file makes and for the
          same reason: the composer beneath holds a live draft, but it is persisted
          per slot on every keystroke and on slot switch, and an in-chat hand-off
          opens a FRESH slot without navigating away. Here it also has somewhere to
          go -- the agent can read the gateway log and say which tabs were dropped,
          which is the question this notice raises and cannot answer.

          dismissLabel rather than a bare ✕: this dismissal is REMEMBERED for the
          browser session, so the label says so before the click. */}
      <ErrorNotice
        message={i18nT('pages.chatPage.tabs_were_not_restored', { count })}
        onDismiss={dismiss}
        dismissLabel={i18nT('pages.chatPage.dismiss_until_next_visit')}
        variant="block"
        askAgent
        footer={
          // An anchor rather than a Btn: it navigates, so middle-click, copy-link
          // and the browser's own affordances all work. Styled in the notice's own
          // register -- underlined danger text, the same treatment its Ask-the-agent
          // action wears -- so the remedy reads as part of the notice rather than as
          // a stray accent-coloured control inside a red band.
          <a href="/chat?history=1" className="underline text-danger hover:text-danger/80">
            {i18nT('pages.chatPage.open_older_sessions')}
          </a>
        }
        testId="unrestored-tabs-error"
      />
    </div>
  )
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
  selectionCapabilitiesQ: { isError: boolean; error?: unknown }
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
  dispatch,
}: ChatPaneNoticesProps) {
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
      {/* No hand-off: navigating away would discard the unsent composer draft. */}
      <ErrorNotice
        message={activeSlot && provider.capabilities.reasoningEffort && selectionCapabilitiesFailed(selectionCapabilitiesQ)
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
      <UnrestoredTabsNotice />
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
