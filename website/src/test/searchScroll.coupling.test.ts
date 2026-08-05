// Feature: chat-virtualizer — cross-module timing coupling.
//
// The settle-poll constants in `searchScroll.ts` are
// hand-tuned against a widget-build delay owned by `WidgetFrame.tsx`: two
// magic numbers in different modules whose relationship is load-bearing but
// invisible from either side. Rather than create a runtime dependency from a
// util to a React component (which would drag the component graph into the
// util), the relationship is asserted here, so drift in EITHER constant fails
// CI and points at the other one.
//
// The relationship: a jump to a widget row must keep converging until the
// iframe has finished growing. If the poll can settle first, the late resize
// pushes the target off-centre — the exact first-click miss the condition-based
// poll was introduced to remove.

import { describe, it, expect } from 'vitest'
import { MIN_QUIET_MS, CONVERGE_MAX_MS } from '../utils/searchScroll'
import { PROGRAMMATIC_BUILD_DELAY_MS, MAX_WIDGET_BUILD_WAIT_MS, MAX_STAGGER_SLOTS, staggeredBuildWait } from '../components/WidgetFrame'

describe('searchScroll <-> WidgetFrame timing coupling', () => {
  it('the settle quiet window outlasts the WORST-CASE widget build wait', () => {
    // Not just the base delay: a jump can mount a batch of widgets whose builds
    // are staggered, so the last one to build is the deadline that matters.
    expect(
      MIN_QUIET_MS,
      `MIN_QUIET_MS (${MIN_QUIET_MS}ms) must exceed MAX_WIDGET_BUILD_WAIT_MS ` +
        `(${MAX_WIDGET_BUILD_WAIT_MS}ms = base ${PROGRAMMATIC_BUILD_DELAY_MS}ms + ` +
        'capped stagger), or a jump to a widget in a later stagger slot settles ' +
        'before its iframe grows and lands off-target.',
    ).toBeGreaterThan(MAX_WIDGET_BUILD_WAIT_MS)
  })

  it('the worst-case build wait is BOUNDED (stagger is capped)', () => {
    // If the stagger slot were unbounded, no finite quiet window could be
    // correct. This pins the fact that it is capped.
    expect(MAX_WIDGET_BUILD_WAIT_MS).toBeGreaterThan(PROGRAMMATIC_BUILD_DELAY_MS)
    expect(MAX_WIDGET_BUILD_WAIT_MS).toBeLessThan(CONVERGE_MAX_MS)
  })

  it('the wall-clock backstop still leaves room for the quiet window', () => {
    // The backstop terminates a poll whose target never settles. It has to be
    // comfortably larger than the quiet window, or every poll would time out
    // instead of settling.
    expect(CONVERGE_MAX_MS).toBeGreaterThan(MIN_QUIET_MS)
  })

  it('the backstop also covers mount + build in the worst case', () => {
    expect(CONVERGE_MAX_MS).toBeGreaterThan(MAX_WIDGET_BUILD_WAIT_MS)
  })

  // The assertions above compare CONSTANTS, so they would stay green if the
  // `Math.min` that applies the cap were removed — late-slot widgets would
  // again build after convergence settled while CI reported no problem. These
  // exercise the arithmetic itself.
  it('the stagger delay plateaus at the cap instead of growing per widget', () => {
    const base = 100
    // Below the cap the delay grows one stagger step per slot.
    expect(staggeredBuildWait(base, 0)).toBe(base)
    expect(staggeredBuildWait(base, 1)).toBeGreaterThan(staggeredBuildWait(base, 0))
    expect(staggeredBuildWait(base, MAX_STAGGER_SLOTS)).toBeGreaterThan(
      staggeredBuildWait(base, MAX_STAGGER_SLOTS - 1),
    )
    // At and beyond the cap it must stop growing, however large the batch.
    const capped = staggeredBuildWait(base, MAX_STAGGER_SLOTS)
    expect(staggeredBuildWait(base, MAX_STAGGER_SLOTS + 1)).toBe(capped)
    expect(staggeredBuildWait(base, 500)).toBe(capped)
  })

  it('the worst-case wait bound is the one the poll is calibrated against', () => {
    // Ties MAX_WIDGET_BUILD_WAIT_MS to the function that produces the real
    // delay, so the published bound cannot drift away from actual behaviour.
    expect(staggeredBuildWait(PROGRAMMATIC_BUILD_DELAY_MS, 10_000)).toBe(
      MAX_WIDGET_BUILD_WAIT_MS,
    )
    expect(MIN_QUIET_MS).toBeGreaterThan(staggeredBuildWait(PROGRAMMATIC_BUILD_DELAY_MS, 10_000))
  })
})
