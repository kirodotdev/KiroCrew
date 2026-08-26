// Spec-detail poll cadence: the Tasks panel is a progress view over tasks.md,
// which the agent and a hand editor write without going through lastSendAt.
import { describe, it, expect } from 'vitest'
import {
  specDetailPollMs,
  SPEC_DETAIL_FAST_POLL_MS,
  SPEC_DETAIL_DISPATCH_POLL_MS,
  SPEC_DETAIL_IDLE_POLL_MS,
  SPEC_DETAIL_FOLLOWUP_MS,
} from '../apps/spec-builder/api'

describe('specDetailPollMs', () => {
  it('stays fast while the worker is running or executing, even when idle otherwise', () => {
    expect(specDetailPollMs({ running: true })).toBe(SPEC_DETAIL_FAST_POLL_MS)
    expect(specDetailPollMs({ status: 'executing' })).toBe(SPEC_DETAIL_FAST_POLL_MS)
  })

  it('uses the dispatch window to catch the slot coming up after a POST', () => {
    expect(specDetailPollMs({ msSinceDispatch: 0 })).toBe(SPEC_DETAIL_DISPATCH_POLL_MS)
    expect(specDetailPollMs({ msSinceDispatch: SPEC_DETAIL_FOLLOWUP_MS - 1 }))
      .toBe(SPEC_DETAIL_DISPATCH_POLL_MS)
    expect(specDetailPollMs({ msSinceDispatch: SPEC_DETAIL_FOLLOWUP_MS }))
      .toBe(SPEC_DETAIL_IDLE_POLL_MS)
  })

  it('stays fast while the Tasks tab is open, which is the progress view', () => {
    expect(specDetailPollMs({ watchingTasks: true })).toBe(SPEC_DETAIL_FAST_POLL_MS)
    // A closed Tasks tab with no other signal is the idle case the bug lived in:
    // an agent flipping checkboxes never re-armed lastSendAt.
    expect(specDetailPollMs({ watchingTasks: false })).toBe(SPEC_DETAIL_IDLE_POLL_MS)
  })

  it('re-arms the fast window after tasks.md itself changes on disk', () => {
    expect(specDetailPollMs({ msSinceTasksChange: 0 })).toBe(SPEC_DETAIL_FAST_POLL_MS)
    expect(specDetailPollMs({ msSinceTasksChange: SPEC_DETAIL_FOLLOWUP_MS - 1 }))
      .toBe(SPEC_DETAIL_FAST_POLL_MS)
    expect(specDetailPollMs({ msSinceTasksChange: SPEC_DETAIL_FOLLOWUP_MS }))
      .toBe(SPEC_DETAIL_IDLE_POLL_MS)
  })

  it('prefers the worker/dispatch signals over the Tasks-tab cadence', () => {
    expect(specDetailPollMs({
      running: true,
      watchingTasks: true,
      msSinceDispatch: 0,
    })).toBe(SPEC_DETAIL_FAST_POLL_MS)
    expect(specDetailPollMs({
      watchingTasks: true,
      msSinceDispatch: 100,
    })).toBe(SPEC_DETAIL_DISPATCH_POLL_MS)
  })
})
