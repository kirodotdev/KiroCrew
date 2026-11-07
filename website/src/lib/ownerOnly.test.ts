import { describe, expect, it } from 'vitest'
import { isOwnerOnlySchedule } from './ownerOnly'

describe('isOwnerOnlySchedule', () => {
  it('is true for an auto-approved, script or command schedule', () => {
    expect(isOwnerOnlySchedule({ approval_mode: 'auto' })).toBe(true)
    expect(isOwnerOnlySchedule({ script: 'x.py:f' })).toBe(true)
    expect(isOwnerOnlySchedule({ command: 'ls' })).toBe(true)
  })

  it('is false for an attended agent schedule', () => {
    expect(isOwnerOnlySchedule({ approval_mode: '' })).toBe(false)
    expect(isOwnerOnlySchedule({})).toBe(false)
  })
})
