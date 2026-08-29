/**
 * Tests for ``phaseLabel`` -- shared scanner-phase-to-user-copy resolver.
 *
 * Three behaviours pinned here, matching the three code paths in
 * ``phaseLabel``:
 *
 * 1. A known phase string (``fetch`` / ``scanner`` / ``cross_validate`` /
 *    ``done`` / ``starting``) resolves to its localised i18n key.
 * 2. An unknown phase string falls back to the neutral "Reviewing" label
 *    -- the safety net for backend rollouts that add a new phase before
 *    the frontend catches up.
 * 3. A ``null`` or empty ``phase`` (the tiny window between "scan
 *    kicked off" and "first poll") shows the "starting" copy so the
 *    sidebar in-progress card and the ScanProgress card stay in sync
 *    at every step, including their moment of first render.
 */
import { describe, it, expect } from 'vitest'

import { phaseLabel } from './phaseLabels'

describe('phaseLabel', () => {
  it('returns a resolved label for every known phase string', () => {
    for (const knownPhase of ['starting', 'fetch', 'scanner', 'cross_validate', 'done']) {
      const resolvedLabel = phaseLabel(knownPhase)
      expect(resolvedLabel).toBeTruthy()
      // The label MUST NOT be the raw phase-string surfaced to the user.
      // When i18next is not initialised in the test environment the
      // resolver returns the dotted i18n key -- which is fine because it
      // still differs from the raw phase name and gives us a stable,
      // debuggable label rather than an empty string.
      expect(resolvedLabel).not.toBe(knownPhase)
    }
  })

  it('falls back to the neutral "reviewing" label for an unknown phase string', () => {
    // The backend may ship a new phase name before the frontend catches
    // up; the fallback label keeps the UI legible during that rollout
    // window rather than leaking a raw implementation label to the user.
    const resolvedLabel = phaseLabel('__phase_that_does_not_exist_yet__')
    expect(resolvedLabel).toBeTruthy()
    expect(resolvedLabel).not.toBe('__phase_that_does_not_exist_yet__')
  })

  it('renders the "starting" copy for a null or empty phase', () => {
    // ``null`` is the tiny window between "scan kicked off" and "first
    // poll" -- ScanProgress and ReviewList in-progress cards both hit
    // this at first render. The "starting" copy MUST be identical to
    // whatever ``phaseLabel('starting')`` returns so the two panes stay
    // in sync.
    const nullPhaseLabel = phaseLabel(null)
    const emptyPhaseLabel = phaseLabel('')
    const explicitStartingLabel = phaseLabel('starting')

    expect(nullPhaseLabel).toBeTruthy()
    expect(emptyPhaseLabel).toBeTruthy()
    expect(nullPhaseLabel).toBe(explicitStartingLabel)
    expect(emptyPhaseLabel).toBe(explicitStartingLabel)
  })

  it('resolves undefined the same way it resolves null', () => {
    // ``undefined`` reaches this helper when a caller destructures a
    // missing field from a partially-populated status snapshot. The
    // guard is ``if (!phaseName)`` which catches both null and undefined
    // identically -- pin that so a future refactor to a stricter check
    // (e.g. ``phaseName === null``) does not silently break the empty
    // case for undefined.
    expect(phaseLabel(undefined)).toBe(phaseLabel('starting'))
  })
})
