import { describe, it, expect } from 'vitest'
import { QUIET_RECONNECT_GRACE_MS, classifyClose, isQuietClose } from './relayClose'

describe('classifyClose', () => {
  it('names each relay case and leaves every other code offline', () => {
    expect(classifyClose(4401)).toBe('token_renewal')
    expect(classifyClose(1012)).toBe('relay_drain')
    expect(classifyClose(4400)).toBe('refused')
    expect(classifyClose(4429)).toBe('refused')
    expect(classifyClose(4499)).toBe('refused')
    expect(classifyClose(1006)).toBe('offline')
    expect(classifyClose(1011)).toBe('offline')
    expect(classifyClose(4500)).toBe('offline')
    expect(classifyClose(undefined)).toBe('offline')
  })

  it('reconnects quietly only on a token renewal or a drain', () => {
    expect(isQuietClose('token_renewal')).toBe(true)
    expect(isQuietClose('relay_drain')).toBe(true)
    expect(isQuietClose('refused')).toBe(false)
    expect(isQuietClose('offline')).toBe(false)
  })

  it('keeps the grace the reconnect test assumes', () => {
    expect(QUIET_RECONNECT_GRACE_MS).toBe(5000)
  })
})
