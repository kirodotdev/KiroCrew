import { describe, it, expect, afterEach, vi } from 'vitest'
import { secureRandomId } from '../utils/secureId'

const UUID_RE = /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/

describe('secureRandomId', () => {
  afterEach(() => vi.unstubAllGlobals())

  it('returns a valid UUID v4 in a secure context (randomUUID present)', () => {
    const id = secureRandomId()
    expect(id).toMatch(UUID_RE)
  })

  it('produces unique ids', () => {
    const ids = new Set(Array.from({ length: 1000 }, () => secureRandomId()))
    expect(ids.size).toBe(1000)
  })

  it('falls back to getRandomValues when randomUUID is unavailable (non-secure context)', () => {
    // Simulate a non-secure context: randomUUID undefined, getRandomValues present.
    vi.stubGlobal('crypto', {
      getRandomValues: (arr: Uint8Array) => {
        for (let i = 0; i < arr.length; i++) arr[i] = (i * 37 + 11) & 0xff
        return arr
      },
    })
    const id = secureRandomId()
    // Still a well-formed v4 UUID (version nibble 4, variant bits 10xx).
    expect(id).toMatch(UUID_RE)
  })
})
