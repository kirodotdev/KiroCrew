import ErrorNotice from '../../../components/ErrorNotice'
import { i18nT } from '../../../i18n/t'
import { useLanguageGeneration } from '../../../i18n/useLanguageGeneration'

import { useUnrestoredTabs } from './useUnrestoredTabs'

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
 * run on every chat page render whether or not a notice was ever shown. It is
 * loaded through a lazy boundary from `ChatPaneNotices`, so the notice's code and
 * its fetch stay out of the always-loaded chat-route chunk.
 */
export function UnrestoredTabsNotice() {
  // Subscribes to language switches: a parent compiled by the React Compiler
  // caches this element while its props are unchanged, so without its own
  // subscription the notice would keep the previous language's text.
  useLanguageGeneration()
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

export default UnrestoredTabsNotice
