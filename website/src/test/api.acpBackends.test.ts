import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { api } from '../api/client'

describe('api.acpBackends', () => {
  const originalFetch = globalThis.fetch

  beforeEach(() => {
    globalThis.fetch = vi.fn().mockResolvedValue({
      ok: true,
      status: 200,
      headers: new Headers(),
      json: async () => ({ backends: [] }),
    }) as unknown as typeof fetch
  })

  afterEach(() => {
    globalThis.fetch = originalFetch
  })

  it('omits ?probe=1 unless the caller asks', async () => {
    await api.acpBackends()
    expect(globalThis.fetch).toHaveBeenCalledWith('/api/acp-backends')
  })

  it('appends ?probe=1 only when probe is true', async () => {
    await api.acpBackends({ probe: true })
    expect(globalThis.fetch).toHaveBeenCalledWith('/api/acp-backends?probe=1')
  })
})
