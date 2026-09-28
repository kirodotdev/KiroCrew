import { beforeEach, describe, expect, it } from 'vitest'

import {
  canShowStartupVideo,
  markStartupVideoHandled,
  resetStartupVideoLaunchGuardForTests,
  startupVideoHandledThisLaunch,
  type StartupVideoGateInput,
} from './startupVideoGate'

/**
 * The startup-video sequencing policy.
 *
 * Tested as a pure predicate rather than through `App`, because the thing worth
 * pinning down is the POLICY — "the video yields a whole launch to any other
 * startup interruption" — and asserting that through a mounted dashboard would
 * test the wiring while leaving the rule itself implicit.
 */

/** A launch with nothing in the way: every veto clause is off. */
const clear: StartupVideoGateInput = {
  interruptionShown: false,
  settled: true,
  memoryMode: 'persistent',
  handledThisLaunch: false,
}

beforeEach(() => {
  resetStartupVideoLaunchGuardForTests()
})

describe('canShowStartupVideo', () => {
  it('opens on a launch with no competing interruption', () => {
    expect(canShowStartupVideo(clear)).toBe(true)
  })

  it('yields the launch when another startup interruption was shown', () => {
    // The single most important clause: a changelog or update popup this launch
    // means the video waits for the NEXT launch, not for that dialog to close.
    expect(canShowStartupVideo({ ...clear, interruptionShown: true })).toBe(false)
  })

  it('stays closed while the interruption decisions are still settling', () => {
    // `interruptionShown: false` is not yet evidence of a free launch — the
    // changelog decides asynchronously. Opening on this reading is the race that
    // shows both dialogs, so an unsettled launch must veto even though nothing
    // is on screen.
    expect(canShowStartupVideo({ ...clear, settled: false, interruptionShown: false })).toBe(false)
  })

  it.each(['incognito', 'temporary'] as const)('skips a %s session', mode => {
    // A verdict is a durable per-user write; a session that keeps nothing must
    // not be asked for one.
    expect(canShowStartupVideo({ ...clear, memoryMode: mode })).toBe(false)
  })

  it('opens for a persistent session and for a slot with no mode reported', () => {
    expect(canShowStartupVideo({ ...clear, memoryMode: 'persistent' })).toBe(true)
    // An absent mode is the ordinary case before a slot is selected; it is not
    // an incognito signal and must not be treated as one.
    expect(canShowStartupVideo({ ...clear, memoryMode: undefined })).toBe(true)
  })

  it('does not open twice in one launch', () => {
    expect(canShowStartupVideo({ ...clear, handledThisLaunch: true })).toBe(false)
  })

  it('needs EVERY clause, not a majority of them', () => {
    // Guards against a future refactor that turns the vetoes into a score: each
    // one alone is sufficient to keep the video closed.
    const vetoes: Partial<StartupVideoGateInput>[] = [
      { interruptionShown: true },
      { settled: false },
      { memoryMode: 'incognito' },
      { memoryMode: 'temporary' },
      { handledThisLaunch: true },
    ]
    for (const veto of vetoes) {
      expect(canShowStartupVideo({ ...clear, ...veto })).toBe(false)
    }
  })
})

describe('per-launch guard', () => {
  it('starts unclaimed, latches once claimed, and never un-latches', () => {
    expect(startupVideoHandledThisLaunch()).toBe(false)
    markStartupVideoHandled()
    expect(startupVideoHandledThisLaunch()).toBe(true)
    // Idempotent: the caller marks on open and must not have to track whether it
    // already did.
    markStartupVideoHandled()
    expect(startupVideoHandledThisLaunch()).toBe(true)
  })

  it('closes the gate once the launch is claimed', () => {
    markStartupVideoHandled()
    expect(canShowStartupVideo({
      ...clear,
      handledThisLaunch: startupVideoHandledThisLaunch(),
    })).toBe(false)
  })
})
