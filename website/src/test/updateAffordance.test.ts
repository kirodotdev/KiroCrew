import { describe, it, expect } from 'vitest'
import { updateAffordance } from '../utils/updateAffordance'

describe('updateAffordance', () => {
  it('offers an in-app apply only where the gateway can actually apply', () => {
    expect(updateAffordance({ updateAvailable: true, canApply: true, command: '' })).toBe('apply')
  })

  it('offers the command instead where the gateway cannot apply', () => {
    // The defect this replaces: availability alone rendered an Update button that
    // POSTs a git-only endpoint, so a wheel install got a guaranteed 400/409.
    expect(updateAffordance({ updateAvailable: true, canApply: false, command: 'curl … | sh' }))
      .toBe('command')
  })

  it('offers nothing when it cannot apply and has no command to give', () => {
    // A desktop bundle or a container: its own updater owns the bytes, and a
    // shell one-liner would not help.
    expect(updateAffordance({ updateAvailable: true, canApply: false, command: '' })).toBe('none')
  })

  it('treats a missing verdict as no verdict, never as an available update', () => {
    for (const updateAvailable of [null, undefined] as const) {
      expect(updateAffordance({ updateAvailable, canApply: true, command: 'x' })).toBe('none')
      expect(updateAffordance({ updateAvailable, canApply: false, command: 'x' })).toBe('none')
    }
  })

  it('offers nothing when the verdict is a real negative', () => {
    expect(updateAffordance({ updateAvailable: false, canApply: true, command: 'x' })).toBe('none')
  })

  it('an unknown capability is not a capability', () => {
    // A gateway that predates the field sends nothing; fail safe rather than
    // offering a button whose endpoint may refuse it.
    expect(updateAffordance({ updateAvailable: true, canApply: undefined, command: 'x' }))
      .toBe('command')
    expect(updateAffordance({ updateAvailable: true, canApply: undefined, command: undefined }))
      .toBe('none')
  })
})
