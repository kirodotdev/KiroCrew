import { useCallback, useEffect, useRef, useState } from 'react'
import OnboardingFlow from '../../components/OnboardingFlow'
import AgentImportFlow from '../../components/AgentImportFlow'
import { OnboardingShellHost } from '../../components/OnboardingChapterShell'
import ErrorNotice from '../../components/ErrorNotice'
import { Btn } from '../../components/ui'
import { OverlayPortal } from '../../components/overlayRoot'
import { i18nT } from '../../i18n/t'

/** The completion flags and writers `useTheme` owns, read afresh every render. */
interface FirstRunFlags {
  onboarded: boolean
  importOnboarded: boolean
  themeBootReady: boolean
  /** The host's first-run state could not be read, or this tab may not write it
   *  (not the owner); nothing may open from this browser's flags. */
  hostStateUnread?: boolean
  /** The owner check failed while first run is still owed: say so, with a retry. */
  ownerCheckFailed?: boolean
  retryOwnerCheck?: () => void
  markOnboarded: () => void
}

/**
 * Which first-run chapter is on screen: Import setup, then the Customize tour.
 * Derived from the completion flags on every change, so the hand-offs below and
 * the derive effect cannot disagree.
 */
export function useFirstRunChapters({ onboarded, importOnboarded, themeBootReady, hostStateUnread = false, ownerCheckFailed = false, retryOwnerCheck, markOnboarded }: FirstRunFlags) {
  // The E2E Playwright suite depends on this onboarding gate: playwright/auth.setup.ts
  // seeds localStorage['mc-onboarded']='1' so the first-run "Choose your look" modal
  // never overlays the shell and intercepts every spec's interactions. If this flag is
  // renamed or the modal moves off localStorage, update auth.setup.ts to match.
  const locallyImportOnboarded =
    !!localStorage.getItem('mc-import-onboarded') || !!localStorage.getItem('mc-onboarded')
  const [showAgentImport, setShowAgentImport] = useState(false)
  // Set by a replay of the importer alone, never by first run. First run finishes
  // the chapter by itself when nothing is found to import; a replay shows that
  // result, or the click that asked for it gets no answer.
  const [keepEmptyImport, setKeepEmptyImport] = useState(false)
  // Never seeded while the host's state is unread or this tab is not yet
  // confirmed as the owner: the derive below opens it once both answer.
  const [showOnboarding, setShowOnboarding] = useState(
    () => !hostStateUnread && locallyImportOnboarded && !localStorage.getItem('mc-onboarded'),
  )
  // Read by the derive effect without being one of its inputs: closing the
  // importer must not re-run the derive (its onComplete/onSkipAll already set
  // the next chapter, and a re-derive would close a tour `/onboarding` asked
  // for). Mirrored in an effect declared before the derive, so the derive in the
  // same commit reads the current value.
  const agentImportOpen = useRef(false)
  useEffect(() => { agentImportOpen.current = showAgentImport }, [showAgentImport])
  const continueTourAfterImport = useRef(false)
  // Set when a chapter's exit hands off to the tour (the end of a replay
  // `/onboarding` asked for, or of first run itself), cleared once it closes.
  // The derive below opens the tour first run owes and closes one the seed
  // opened from this browser's flags, but never closes a handed-off one: it
  // re-runs when a late input answers (the owner check, say), and its false
  // for an owner who finished first run would shut the tour they asked for.
  const tourHandedOff = useRef(false)
  useEffect(() => { if (!showOnboarding) tourHandedOff.current = false }, [showOnboarding])
  const openTour = useCallback(() => {
    tourHandedOff.current = true
    setShowOnboarding(true)
  }, [])
  // The ONLY way the tour chapter ends first run, shared by BOTH its exits
  // ("Done" and every skip: "Skip all", a popover Skip, Escape).
  const endFirstRun = useCallback(() => {
    setShowOnboarding(false)
    markOnboarded()
  }, [markOnboarded])
  // Dismiss onboarding when server reports user is already onboarded
  // (handles the race: boot fetch completes after useState initializer ran).
  useEffect(() => { if (onboarded) setShowOnboarding(false) }, [onboarded])
  // The tour's seed above reads this browser's flags before the host answers;
  // when the host cannot be read those flags are all there is, so the seeded
  // tour closes rather than finish first run from them.
  useEffect(() => { if (hostStateUnread) setShowOnboarding(false) }, [hostStateUnread])
  // Seeds — and re-derives — which first-run chapter is open from the two
  // completion flags. Chapter order is Import setup → Customize/tour,
  // so each chapter opens only once its predecessor is marked done. Runs on
  // every flag change (not just boot) so the hand-offs below and this effect
  // can never disagree about what should be on screen.
  useEffect(() => {
    if (!themeBootReady) return
    // OPEN-ONLY for the import chapter. Deriving `false` here is what made the
    // page close itself: Import is the one chapter with a manual entry point
    // (the `mc-start-import` event below), and for a user who already finished
    // it this effect's own answer is `false`. So any later run — theme boot
    // resolving, or any flag write — drove `initialOpen` true→false, and
    // AgentImportFlow closes on that edge. Nothing is lost by not closing here:
    // the real completion paths (`onComplete`, `onSkipAll`) already call
    // `setShowAgentImport(false)` themselves, so the false branch was redundant
    // for every case except the one it broke. Same split as the `onboarded`
    // effect above, which only ever closes the tour.
    if (!importOnboarded) setShowAgentImport(true)
    // One chapter at a time. A replayed importer is open while the flags still
    // say a later chapter is due; deriving that chapter now would stack it on
    // the importer. The importer's own exits open whatever comes next.
    if (agentImportOpen.current) return
    const tourOwed = importOnboarded && !onboarded
    if (tourOwed || !tourHandedOff.current) setShowOnboarding(tourOwed)
  }, [importOnboarded, onboarded, themeBootReady])
  useEffect(() => {
    const replay = (event: Event) => {
      continueTourAfterImport.current =
        !!(event as CustomEvent<{ continueOnboarding?: boolean }>).detail?.continueOnboarding
      // `/onboarding` replays all of first run, where an empty import step passes
      // by itself; Settings replays the importer alone.
      setKeepEmptyImport(!continueTourAfterImport.current)
      setShowOnboarding(false)
      setShowAgentImport(true)
    }
    window.addEventListener('mc-start-import', replay)
    return () => window.removeEventListener('mc-start-import', replay)
  }, [])
  return {
    showAgentImport, setShowAgentImport, keepEmptyImport, showOnboarding, setShowOnboarding,
    continueTourAfterImport, openTour, endFirstRun,
    ownerCheckFailed, retryOwnerCheck,
  }
}

/** The two chapters, inside the one first-run chrome host. */
export function FirstRunChapters({ firstRun, onboarded, markOnboarded, markImportOnboarded }: {
  firstRun: ReturnType<typeof useFirstRunChapters>
  onboarded: boolean
  markOnboarded: () => void
  markImportOnboarded: () => void
}) {
  const {
    showAgentImport, setShowAgentImport, keepEmptyImport, showOnboarding, setShowOnboarding,
    continueTourAfterImport, openTour, endFirstRun,
    ownerCheckFailed, retryOwnerCheck,
  } = firstRun
  // Dismissed for this page load: the notice sits over the composer's corner,
  // and an outage that outlasts Try again must not keep send out of reach.
  const [ownerNoticeDismissed, setOwnerNoticeDismissed] = useState(false)
  return (
    <>
    {/* First run is owed but held: the owner check failed, so no chapter may
        open. Said, with a retry, instead of a dashboard that looks finished. */}
    {ownerCheckFailed && !ownerNoticeDismissed && (
      <OverlayPortal>
        <div
          className="fixed bottom-safe-offset-4 right-safe-offset-4 z-[9999] w-[min(360px,calc(100vw-2rem-var(--safe-area-x)-var(--safe-area-x)))] rounded-lg shadow-lg"
          style={{ background: 'var(--bg)' }}
          data-testid="first-run-owner-check-error"
        >
          <ErrorNotice
            message={i18nT('components.onboardingChapterShell.owner_check_failed')}
            impact="partial"
            askAgent
            actionPlacement="below"
            onDismiss={() => setOwnerNoticeDismissed(true)}
            actions={retryOwnerCheck && (
              <Btn type="button" onClick={() => { void retryOwnerCheck() }}>
                {i18nT('components.agentImportFlow.try_again')}
              </Btn>
            )}
          />
        </div>
      </OverlayPortal>
    )}
    {/* First-run modal chrome mounted ONCE (scrim + accent panel + floating
        mascots) so the import→customize hand-off swaps only the right-column
        content — the mascots never remount/replay, killing the transition
        glitch. Both flows portal their content into this single shell; each
        still renders standalone (its own chrome) when used outside a host. */}
    <OnboardingShellHost>
      {/* First-run chapter 1 — import gate. Existing users inherit the old
          onboarding marker, while new users reach the feature tour only after
          this flow. */}
      <AgentImportFlow
        initialOpen={showAgentImport}
        keepOpenWhenEmpty={keepEmptyImport}
        onComplete={() => {
          markImportOnboarded()
          setShowAgentImport(false)
          const wantsTour = !onboarded || continueTourAfterImport.current
          continueTourAfterImport.current = false
          if (wantsTour) openTour()
        }}
        onDismiss={() => setShowAgentImport(false)}
        onSkipAll={() => {
          // Skip the rest of first run: the user lands in the product (new
          // chat) straight away.
          markImportOnboarded()
          setShowAgentImport(false)
          continueTourAfterImport.current = false
          markOnboarded()
          setShowOnboarding(false)
        }}
      />

      {/* First-run chapter 2 — Customize + feature tour (theme → about you →
          Schedule → Apps → Sessions). Rendered unconditionally so the
          `/onboarding` slash command can reopen it anytime; internal
          visibility is seeded by `initialOpen`. */}
      <OnboardingFlow
        initialOpen={showOnboarding}
        onComplete={endFirstRun}
        onSkipAll={endFirstRun}
      />
    </OnboardingShellHost>
    </>
  )
}
