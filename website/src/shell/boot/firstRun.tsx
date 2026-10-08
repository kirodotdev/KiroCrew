import { useCallback, useEffect, useRef, useState } from 'react'
import OnboardingFlow from '../../components/OnboardingFlow'
import AgentImportFlow from '../../components/AgentImportFlow'
import PrivacyChapter from '../../components/PrivacyChapter'
import { OnboardingShellHost } from '../../components/OnboardingChapterShell'

/** The completion flags and writers `useTheme` owns, read afresh every render. */
interface FirstRunFlags {
  onboarded: boolean
  importOnboarded: boolean
  privacyAcked: boolean
  themeBootReady: boolean
  markOnboarded: () => void
}

/**
 * Which first-run chapter is on screen: Import setup, then Privacy, then the
 * Customize tour, then Meet CrewMates. Derived from the completion flags on every
 * change, so the hand-offs below and the derive effect cannot disagree.
 */
export function useFirstRunChapters({ onboarded, importOnboarded, privacyAcked, themeBootReady, markOnboarded }: FirstRunFlags) {
  // The E2E Playwright suite depends on this onboarding gate: playwright/auth.setup.ts
  // seeds localStorage['mc-onboarded']='1' so the first-run "Choose your look" modal
  // never overlays the shell and intercepts every spec's interactions. If this flag is
  // renamed or the modal moves off localStorage, update auth.setup.ts to match.
  const locallyImportOnboarded =
    !!localStorage.getItem('mc-import-onboarded') || !!localStorage.getItem('mc-onboarded')
  // Mirrors `privacyAcked`'s own seed in useTheme. The tour's seed below MUST
  // consult it: a tree whose import chapter was completed by a build that
  // predates the Privacy chapter has `mc-import-onboarded` set and no
  // `mc-privacy-acked`, and seeding the tour open on that alone would put
  // Customize on screen ahead of Privacy until theme boot resolves — and its
  // "Done" would end first run from there. Same formula as the derive effect.
  const locallyPrivacyAcked =
    !!localStorage.getItem('mc-privacy-acked') || !!localStorage.getItem('mc-onboarded')
  const [showAgentImport, setShowAgentImport] = useState(false)
  const [showPrivacy, setShowPrivacy] = useState(false)
  const [showOnboarding, setShowOnboarding] = useState(
    () => locallyImportOnboarded && locallyPrivacyAcked && !localStorage.getItem('mc-onboarded'),
  )
  const continueTourAfterImport = useRef(false)
  // Where the mandatory Privacy chapter leads. 'customize' hands off to the
  // onboarding tour (the normal chapter order); 'finish' ends first run right
  // there, which is what "Skip all" from Import setup means — the user still has
  // to pass through Privacy, but nothing follows it.
  const privacyExit = useRef<'customize' | 'finish'>('customize')
  // The ONLY way the tour chapter ends first run — deliberately shared by BOTH
  // its exits ("Done" and every skip: "Skip all", a popover Skip, Escape).
  // Privacy is mandatory, so no exit may mark onboarding complete while it is
  // unacknowledged; handing the two props one function is what makes that
  // symmetric by construction instead of by two closures agreeing. In the normal
  // chapter order Privacy is already behind the user here and this just ends
  // first run; the branch is what holds the mandate for a tree whose import
  // chapter predates the Privacy chapter.
  const endFirstRun = useCallback(() => {
    setShowOnboarding(false)
    if (!privacyAcked) {
      privacyExit.current = 'finish'
      setShowPrivacy(true)
      return
    }
    markOnboarded()
  }, [privacyAcked, markOnboarded])
  // Dismiss onboarding when server reports user is already onboarded
  // (handles the race: boot fetch completes after useState initializer ran).
  useEffect(() => { if (onboarded) setShowOnboarding(false) }, [onboarded])
  // Seeds — and re-derives — which first-run chapter is open from the three
  // completion flags. Chapter order is Import setup → Privacy → Customize/tour,
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
    setShowPrivacy(importOnboarded && !privacyAcked)
    setShowOnboarding(importOnboarded && privacyAcked && !onboarded)
  }, [importOnboarded, privacyAcked, onboarded, themeBootReady])
  useEffect(() => {
    const replay = (event: Event) => {
      continueTourAfterImport.current =
        !!(event as CustomEvent<{ continueOnboarding?: boolean }>).detail?.continueOnboarding
      setShowOnboarding(false)
      setShowAgentImport(true)
    }
    window.addEventListener('mc-start-import', replay)
    return () => window.removeEventListener('mc-start-import', replay)
  }, [])
  return {
    showAgentImport, setShowAgentImport, showPrivacy, setShowPrivacy, showOnboarding, setShowOnboarding,
    continueTourAfterImport, privacyExit, endFirstRun,
  }
}

/** The three chapters, inside the one first-run chrome host. */
export function FirstRunChapters({ firstRun, onboarded, privacyAcked, markOnboarded, markImportOnboarded, markPrivacyAcked }: {
  firstRun: ReturnType<typeof useFirstRunChapters>
  onboarded: boolean
  privacyAcked: boolean
  markOnboarded: () => void
  markImportOnboarded: () => void
  markPrivacyAcked: () => void
}) {
  const {
    showAgentImport, setShowAgentImport, showPrivacy, setShowPrivacy, showOnboarding, setShowOnboarding,
    continueTourAfterImport, privacyExit, endFirstRun,
  } = firstRun
  return (
    <>
    {/* First-run modal chrome mounted ONCE (scrim + accent panel + floating
        mascots) so the import→customize hand-off swaps only the right-column
        content — the mascots never remount/replay, killing the transition
        glitch. Both flows portal their content into this single shell; each
        still renders standalone (its own chrome) when used outside a host. */}
    <OnboardingShellHost>
      {/* First-run chapter 1 — import gate. Existing users inherit the old
          onboarding marker, while new users reach Privacy (and then the
          feature tour) only after this flow. */}
      <AgentImportFlow
        initialOpen={showAgentImport}
        onComplete={() => {
          markImportOnboarded()
          setShowAgentImport(false)
          const wantsTour = !onboarded || continueTourAfterImport.current
          continueTourAfterImport.current = false
          if (!privacyAcked) {
            privacyExit.current = wantsTour ? 'customize' : 'finish'
            setShowPrivacy(true)
            return
          }
          if (wantsTour) setShowOnboarding(true)
        }}
        onSkipAll={() => {
          // Skip the rest of first run — but NOT the Privacy chapter, which is
          // mandatory: show it, and let its Continue mark onboarding done so
          // the user lands in the product (new chat) straight after it.
          markImportOnboarded()
          setShowAgentImport(false)
          continueTourAfterImport.current = false
          if (!privacyAcked) {
            privacyExit.current = 'finish'
            setShowPrivacy(true)
            return
          }
          markOnboarded()
          setShowOnboarding(false)
        }}
      />

      {/* First-run chapter 2 — Privacy. Mandatory and un-skippable: every path
          out of chapter 1 (finish, "Skip import", nothing to import, "Skip
          all") arrives here. */}
      <PrivacyChapter
        open={showPrivacy}
        onContinue={() => {
          markPrivacyAcked()
          setShowPrivacy(false)
          if (privacyExit.current === 'finish') markOnboarded()
          else setShowOnboarding(true)
        }}
      />

      {/* First-run chapter 3 — Customize + feature tour (theme → about you →
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
