import { Suspense, lazy, type Dispatch, type SetStateAction } from 'react'
import { X } from 'lucide-react'

import ErrorBoundary from '../../../components/ErrorBoundary'
import ErrorNotice from '../../../components/ErrorNotice'
import VoicePlaybackNotice from '../../../components/VoicePlaybackNotice'
import { Btn } from '../../../components/ui'
import { fmtDate } from '../../../i18n/format'
import { i18nT } from '../../../i18n/t'
import { useLanguageGeneration } from '../../../i18n/useLanguageGeneration'
import { selectionCapabilitiesFailed } from '../../../lib/effort'
import type { useProvider } from '../../../providers'
import type { AppDispatch, RootState } from '../../../store'
import { clearSwitchSlotGone, clearUndeletableHistory, clearUnresumableResume, switchSlotNoticeCopy } from '../../../store/chatSlice'
import { findSurfaceBySlotMode, surfaceLabel } from '../../../surfaces/registry'
import { slotChannelLabel } from '../../../utils/channelOrigin'
import { sessionDisplayTitle } from '../../../utils/sessionDisplayTitle'
import { historyDeleteRefusalMessage } from '../../../utils/historyDeleteRefusal'
import type { ShortNameMatch } from '../../../utils/sessionKeys'
import type { ErrorReport } from '../../../utils/errorReport'

// Lazy so the notice's code AND its fetch/poll (useUnrestoredTabs) stay out of the
// always-loaded chat-route chunk. The notice renders nothing in the common case
// (no tabs were dropped), so a null Suspense fallback is invisible, and an
// ErrorBoundary keeps a chunk-load hiccup from taking the pane's other notices
// down with it.
const UnrestoredTabsNotice = lazy(() => import('./UnrestoredTabsNotice'))

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
  const title = sessionDisplayTitle(r)
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
 * Every translated or locale-formatted string the pane renders, built in one
 * call that takes the language generation as an argument.
 *
 * This component is compiled by the React Compiler, which memoizes each value
 * on the reactive inputs it reads. `i18nT()` and `fmtDate()` read the active
 * catalog and locale without being inputs, so a string the compiler cached
 * would keep the previous language after a switch even though the component
 * re-renders. Passing the generation makes the language an input of this call,
 * so a switch rebuilds every string here.
 */
function paneCopy(_languageGeneration: number, facts: {
  sidMatches: ChatPaneNoticesProps['sidMatches']
  effortUnavailable: boolean
  configFailed: boolean
  switchSlotGone: ChatPaneNoticesProps['switchSlotGone']
  unresumableResume: ChatPaneNoticesProps['unresumableResume']
  undeletableHistory: ChatPaneNoticesProps['undeletableHistory']
}) {
  const { sidMatches, switchSlotGone, unresumableResume, undeletableHistory } = facts
  return {
    dismiss: i18nT('app.dismiss'),
    sidAlsoNames: sidMatches
      ? i18nT('pages.chatPage.short_name_also_names', {
        name: sidMatches.name,
        title: sidMatches.opened.title,
        date: fmtDate(sidMatches.opened.mintedAt * 1000),
      })
      : '',
    sidStarted: sidMatches
      ? sidMatches.others.map(match => i18nT('pages.chatPage.short_name_started', { date: fmtDate(match.mintedAt * 1000) }))
      : [],
    effortUnavailable: facts.effortUnavailable ? i18nT('pages.chatPage.effort_options_unavailable') : '',
    configFailed: facts.configFailed ? i18nT('pages.settings.chatPanel.failed_to_load_config') : '',
    switchSlotGone: switchSlotGone ? switchSlotNoticeCopy(switchSlotGone.kind, switchSlotGone.name) : '',
    unresumable: unresumableResume ? unresumableNoticeMessage(unresumableResume) : '',
    undeletable: undeletableHistory ? historyDeleteRefusalMessage(undeletableHistory) : '',
  }
}

interface ChatPaneNoticesProps {
  uploadHint: string
  setUploadHint: (hint: string) => void
  uploadError: string
  setUploadError: (error: string) => void
  sidError: string
  setSidError: (error: string) => void
  /** The other sessions a short-name link (`?sid=chat-7`) also names. */
  sidMatches: { name: string; opened: ShortNameMatch; others: ShortNameMatch[] } | null
  setSidMatches: (matches: null) => void
  openShortNameMatch: (match: ShortNameMatch) => void
  /** For the effort-options notice: the slot's ACP capability read failed. */
  activeSlot: string | null
  provider: ReturnType<typeof useProvider>
  selectionCapabilitiesQ: { isError: boolean; error?: unknown }
  /** For the model-default notice: the Settings default model read failed. */
  chipDefault: { failed: boolean }
  actionError: { title?: string; message: string; preserveOnSwitch?: boolean; report?: ErrorReport } | null
  setActionError: Dispatch<SetStateAction<{ title?: string; message: string; preserveOnSwitch?: boolean; report?: ErrorReport } | null>>
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
  sidMatches,
  setSidMatches,
  openShortNameMatch,
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
  const languageGeneration = useLanguageGeneration()
  const copy = paneCopy(languageGeneration, {
    sidMatches,
    effortUnavailable: !!(activeSlot && provider.capabilities.reasoningEffort && selectionCapabilitiesFailed(selectionCapabilitiesQ)),
    configFailed: !!(activeSlot && chipDefault.failed),
    switchSlotGone,
    unresumableResume,
    undeletableHistory,
  })
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
          <Btn onClick={() => setUploadHint('')} aria-label={copy.dismiss} className="shrink-0 px-1.5 py-0.5 text-muted hover:text-text"><X className="w-3.5 h-3.5" /></Btn>
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
      {sidMatches && (
        <div role="status" data-testid="sid-matches" className="mx-4 mt-2 mb-0 bg-bg-elevated border rounded-lg p-3 flex items-start gap-3 animate-rise" style={{ borderColor: 'color-mix(in srgb, var(--warn) 45%, transparent)' }}>
          <div className="text-sm text-text flex-1 min-w-0">
            <p>{copy.sidAlsoNames}</p>
            {/* Bounded: one name can match many sessions, and the pane does not scroll. */}
            <ul className="mt-1 space-y-0.5 max-h-40 overflow-y-auto">
              {sidMatches.others.map((match, i) => (
                <li key={match.key} className="min-w-0">
                  <Btn type="button" onClick={() => openShortNameMatch(match)} className="border-none p-0 text-left text-accent hover:underline hover:bg-transparent">
                    {match.title}
                  </Btn>
                  <span className="text-muted"> · <code>{match.key}</code> · {copy.sidStarted[i]}</span>
                </li>
              ))}
            </ul>
          </div>
          <Btn onClick={() => setSidMatches(null)} aria-label={copy.dismiss} className="shrink-0 px-1.5 py-0.5 text-muted hover:text-text"><X className="w-3.5 h-3.5" /></Btn>
        </div>
      )}
      {/* No hand-off: navigating away would discard the unsent composer draft. */}
      <ErrorNotice
        message={copy.effortUnavailable}
        className="mx-4 mt-2 mb-0 animate-rise"
        testId="effort-capabilities-error"
      />
      {/* No hand-off: navigating away would discard the unsent composer draft. */}
      <ErrorNotice
        message={copy.configFailed}
        className="mx-4 mt-2 mb-0 animate-rise"
        testId="model-default-error"
      />
      <ErrorNotice
        title={actionError?.title}
        message={actionError?.message}
        report={actionError?.report}
        onDismiss={() => setActionError(null)}
        askAgent
        className="mx-4 mt-2 mb-0 animate-rise"
        testId="action-error"
      />
      {/* A click on a listed-but-gone session (#6372): the fact at the click
          locus, through the required ErrorNotice surface. The store carries
          the NAME; the sentence resolves here so a locale switch re-renders it. */}
      <ErrorNotice
        message={copy.switchSlotGone}
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
          <button onClick={dismissPinStatus} aria-label={copy.dismiss} className="text-muted hover:text-text leading-none p-0.5"><X className="w-4 h-4" /></button>
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
            message={copy.unresumable}
            onDismiss={() => dispatch(clearUnresumableResume())}
            variant="block"
            askAgent
          />
        </div>
      )}
      {/* Lazy boundary: the notice and its fetch live in their own chunk (see the
          import above). The Suspense fallback is null because the notice itself
          renders null in the common case, so there is nothing to stand in for
          while the chunk loads. The ErrorBoundary carries NO fallback, so a
          chunk-load failure renders its default error card rather than silently
          vanishing -- hiding a notice whose whole job is to report silent data
          loss would repeat the very defect this change exists to end. */}
      <ErrorBoundary languageGeneration={languageGeneration}>
        <Suspense fallback={null}>
          <UnrestoredTabsNotice />
        </Suspense>
      </ErrorBoundary>
      {undeletableHistory && (
        <div className="mx-4 mt-2 mb-0" data-testid="undeletable-history-error">
          {/* Same site and shape as the unresumable notice above: a sidebar
              click the gateway answered with a refusal, narrated here because
              the row it names is still in the sidebar and looks untouched.
              The sentence is chosen from the gateway's `code`, so the remedy
              matches the cause (release the cron jobs / retry / repair). */}
          <ErrorNotice
            message={copy.undeletable}
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
