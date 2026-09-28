import { describe, it, expect } from 'vitest'
import { updateAffordance } from '../utils/updateAffordance'

describe('updateAffordance', () => {
  it('offers an in-app apply only where the gateway can actually apply', () => {
    expect(updateAffordance({
      updateAvailable: true, canApply: true, canArm: true, command: 'curl … | sh',
    })).toBe('apply')
  })

  it('prefers host approval over exposing the raw installer command', () => {
    expect(updateAffordance({
      updateAvailable: true, canApply: false, canArm: true, command: 'curl … | sh',
    })).toBe('arm')
  })

  it('offers the command only where host approval is unavailable', () => {
    // A non-managed source install cannot use the nonce-backed update path,
    // but the installer remains a valid manual recovery action.
    expect(updateAffordance({
      updateAvailable: true, canApply: false, canArm: false, command: 'curl … | sh',
    })).toBe('command')
  })

  it('offers nothing when it cannot apply and has no command to give', () => {
    // A desktop bundle or a container: its own updater owns the bytes, and a
    // shell one-liner would not help.
    expect(updateAffordance({
      updateAvailable: true, canApply: false, canArm: false, command: '',
    })).toBe('none')
  })

  it('treats a missing verdict as no verdict, never as an available update', () => {
    for (const updateAvailable of [null, undefined] as const) {
      expect(updateAffordance({
        updateAvailable, canApply: true, canArm: true, command: 'x',
      })).toBe('none')
      expect(updateAffordance({
        updateAvailable, canApply: false, canArm: true, command: 'x',
      })).toBe('none')
    }
  })

  it('offers nothing when the verdict is a real negative', () => {
    expect(updateAffordance({
      updateAvailable: false, canApply: true, canArm: true, command: 'x',
    })).toBe('none')
  })

  it('an unknown capability is not a capability', () => {
    // A gateway that predates the field sends nothing; fail safe rather than
    // offering a button whose endpoint may refuse it.
    expect(updateAffordance({
      updateAvailable: true, canApply: undefined, canArm: undefined, command: 'x',
    })).toBe('command')
    expect(updateAffordance({
      updateAvailable: true, canApply: undefined, canArm: undefined, command: undefined,
    })).toBe('none')
  })
})
