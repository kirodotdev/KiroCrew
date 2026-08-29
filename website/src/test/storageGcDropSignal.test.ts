import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { __resetSessionIdentities, noteLiveSessionInstances, setSessionScopedItem, sessionOwnerStamp } from '../utils/storageGc'

const KEY = 'mc-panel-tabs:chat-7'

afterEach(() => {
  __resetSessionIdentities()
  localStorage.clear()
  vi.restoreAllMocks()
})

describe('a dropped session-scoped write is not silent', () => {
  beforeEach(() => localStorage.clear())

  it('warns in dev when the captured owner is no longer current', () => {
    vi.stubEnv('DEV', true)
    const warn = vi.spyOn(console, 'warn').mockImplementation(() => {})
    noteLiveSessionInstances([{ key: 'chat-7', created: '2026-01-01T00:00:00Z' }])
    const staleOwner = sessionOwnerStamp('chat-7')
    __resetSessionIdentities()
    noteLiveSessionInstances([{ key: 'chat-7', created: '2026-02-02T00:00:00Z' }])

    setSessionScopedItem(localStorage, KEY, 'x', staleOwner)

    expect(localStorage.getItem(KEY)).toBeNull()
    expect(warn).toHaveBeenCalledOnce()
    expect(warn.mock.calls[0][0]).toMatch(/dropped session-scoped write/)
  })
})
