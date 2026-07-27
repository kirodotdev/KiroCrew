import { useCallback, useEffect, useRef, useState } from 'react'
import { useTheme } from './useTheme'

/**
 * First-run onboarding gate: sequences the agent-import flow and the theme /
 * feature tour, and decides which is visible.
 *
 * The two flows have independent completion flags on the server
 * (`dashboard.import_onboarded` and `dashboard.onboarded`), but they are shown
 * as one first-run experience: import first, then the tour. This hook owns the
 * visibility state and the hand-off between them.
 *
 * Option A hand-off rule: dismissing the FIRST-RUN import (via "Skip for now",
 * the header Skip, or finishing an import) always flows into the remaining
 * onboarding — regardless of the `onboarded` flag. This prevents "Skip for now"
 * from looking like it skipped every step when the user is in an
 * `onboarded=true` / `import_onboarded=false` state (e.g. re-testing just the
 * importer). A Settings/slash replay is NOT a first run: it only continues into
 * the tour when the replay explicitly asked to (`continueOnboarding`), so
 * re-importing from Settings never re-opens the theme tour.
 *
 * The E2E Playwright suite depends on this gate: playwright/auth.setup.ts seeds
 * localStorage['mc-onboarded']='1' so the first-run "Choose your look" modal
 * never overlays the shell and intercepts every spec's interactions. If this
 * flag is renamed or the modal moves off localStorage, update auth.setup.ts.
 */
export function useOnboardingGate() {
  const {
    onboarded,
    importOnboarded,
    themeBootReady,
    markOnboarded,
    markImportOnboarded,
  } = useTheme()

  const locallyImportOnboarded =
    !!localStorage.getItem('mc-import-onboarded') || !!localStorage.getItem('mc-onboarded')
  const [showAgentImport, setShowAgentImport] = useState(false)
  const [showOnboarding, setShowOnboarding] = useState(
    () => locallyImportOnboarded && !localStorage.getItem('mc-onboarded'),
  )

  // Whether the currently-open import gate was opened as the first run (from
  // boot) rather than a Settings/slash replay. First-run import always chains
  // into the tour on completion; a replay only chains when it asked to.
  const importFirstRun = useRef(false)
  const continueTourAfterImport = useRef(false)
  // Seed visibility from server boot state exactly once. Kept to a single run
  // so a later `importOnboarded` flip (from finishing/skipping the importer)
  // can't re-run this effect and stomp the tour the completion handler opened.
  const bootSeeded = useRef(false)

  // Dismiss onboarding when the server reports the user is already onboarded
  // (handles the race: boot fetch completes after the useState initializer ran).
  useEffect(() => {
    if (onboarded) setShowOnboarding(false)
  }, [onboarded])

  useEffect(() => {
    if (!themeBootReady || bootSeeded.current) return
    bootSeeded.current = true
    const firstRunImport = !importOnboarded
    importFirstRun.current = firstRunImport
    setShowAgentImport(firstRunImport)
    setShowOnboarding(importOnboarded && !onboarded)
  }, [themeBootReady, importOnboarded, onboarded])

  // Settings / slash-command replay of the importer.
  useEffect(() => {
    const replay = (event: Event) => {
      continueTourAfterImport.current =
        !!(event as CustomEvent<{ continueOnboarding?: boolean }>).detail?.continueOnboarding
      // A replay is never the first-run chain: only continue into the tour
      // when the replay explicitly requested it.
      importFirstRun.current = false
      setShowOnboarding(false)
      setShowAgentImport(true)
    }
    window.addEventListener('mc-start-import', replay)
    return () => window.removeEventListener('mc-start-import', replay)
  }, [])

  const onImportComplete = useCallback(() => {
    markImportOnboarded()
    setShowAgentImport(false)
    if (importFirstRun.current || continueTourAfterImport.current) {
      setShowOnboarding(true)
    }
    continueTourAfterImport.current = false
    importFirstRun.current = false
  }, [markImportOnboarded])

  const onOnboardingComplete = useCallback(() => {
    markOnboarded()
    setShowOnboarding(false)
  }, [markOnboarded])

  return { showAgentImport, showOnboarding, onImportComplete, onOnboardingComplete }
}
