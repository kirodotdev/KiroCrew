import { describe, it, expect } from 'vitest'

import { fileLandingSlot } from '../utils/uploadRouting'

describe('fileLandingSlot (cross-slot capture routing)', () => {
  it('lands in the live composer when the initiating slot is still active', () => {
    expect(fileLandingSlot('slot-a', 'slot-a')).toEqual({ target: 'pending' })
  })

  it('lands in the INITIATING slot draft after a mid-capture slot switch', () => {
    // Regression: a snip started in A, cropped after switching to B, must attach
    // to A — not whatever slot is active when the async capture resolves.
    expect(fileLandingSlot('slot-a', 'slot-b')).toEqual({ target: 'draft', slot: 'slot-a' })
  })

  it('drops when there is no initiating slot', () => {
    expect(fileLandingSlot(null, 'slot-b')).toEqual({ target: 'drop' })
    expect(fileLandingSlot(undefined, undefined)).toEqual({ target: 'drop' })
  })
})
