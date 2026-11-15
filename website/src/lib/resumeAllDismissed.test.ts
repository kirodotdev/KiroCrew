import { describe, it, expect, beforeEach, vi } from 'vitest'
import { RESUME_ALL_DISMISSED_KEY, readResumeAllDismissed, writeResumeAllDismissed } from './resumeAllDismissed'

beforeEach(() => { localStorage.clear() })

describe('resume-all dismissed sessions', () => {
  it('reads nothing dismissed when the key is absent', () => {
    expect(readResumeAllDismissed().size).toBe(0)
  })

  it('round-trips a set of session keys', () => {
    expect(writeResumeAllDismissed(new Set(['a', 'b']))).toBe(true)
    expect([...readResumeAllDismissed()]).toEqual(['a', 'b'])
  })

  it('removes the entry when the set is empty', () => {
    writeResumeAllDismissed(new Set(['a']))
    expect(writeResumeAllDismissed(new Set())).toBe(true)
    expect(localStorage.getItem(RESUME_ALL_DISMISSED_KEY)).toBeNull()
  })

  it('returns false when storage rejects the write', () => {
    const setItem = vi.spyOn(Storage.prototype, 'setItem').mockImplementation(() => {
      throw new DOMException('Storage is disabled', 'SecurityError')
    })
    try {
      expect(writeResumeAllDismissed(new Set(['a']))).toBe(false)
    } finally {
      setItem.mockRestore()
    }
  })

  it('degrades to nothing dismissed on malformed or non-array data, and drops non-string entries', () => {
    localStorage.setItem(RESUME_ALL_DISMISSED_KEY, '{not json')
    expect(readResumeAllDismissed().size).toBe(0)
    localStorage.setItem(RESUME_ALL_DISMISSED_KEY, '{"a":1}')
    expect(readResumeAllDismissed().size).toBe(0)
    localStorage.setItem(RESUME_ALL_DISMISSED_KEY, JSON.stringify(['a', 3, null, 'b']))
    expect([...readResumeAllDismissed()]).toEqual(['a', 'b'])
  })
})
