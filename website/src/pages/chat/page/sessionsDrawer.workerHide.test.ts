import { describe, expect, it } from 'vitest'
import { workerHideRestore } from './sessionsDrawer'

describe('workerHideRestore -- leaving a Crew Mode worker gives the sidebar pin back', () => {
  it('restores the pin the hide saved while the list is still as the hide left it', () => {
    expect(workerHideRestore(true, false)).toBe(true)
    expect(workerHideRestore(false, false)).toBe(false)
  })
  it('leaves an explicit Show sessions alone: a pin no longer false is the person\'s choice', () => {
    // Open a worker (list hidden), press Show sessions, then open a plain chat:
    // the list stays open, and the persisted preference agrees with the screen.
    expect(workerHideRestore(true, true)).toBeNull()
    expect(workerHideRestore(false, true)).toBeNull()
  })
  it('does nothing when the worker hid nothing', () => {
    expect(workerHideRestore(null, false)).toBeNull()
    expect(workerHideRestore(null, true)).toBeNull()
  })
})
