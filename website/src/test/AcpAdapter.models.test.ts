import { describe, it, expect, vi, beforeEach } from 'vitest'
import type { Mock } from 'vitest'

// Mock the API client so fetchAvailableModels reads our canned /api/models.
vi.mock('../api/client', () => ({
  api: {
    models: vi.fn(),
  },
}))

import { api } from '../api/client'
import { AcpAdapter, clearCachedModels, noteKiroAccount } from '../providers/adapters/acp'
import {
  markModelsCold,
  markModelsDegraded,
  modelsDegraded,
  modelListRefetchInterval,
} from '../providers/modelListHealth'
import type { ModelInfo } from '../providers/types'

/** The /api/models rows these fixtures feed the adapter. The adapter's own
 *  RawModel is module-private, so mirror only the fields set here. */
type ModelRow = { model_name: string; display_name?: string; description?: string; cold?: boolean }

/** `api.models` as replaced by vi.mock above. The resolved type is a union
 *  because the endpoint has both shapes: rows on success, and the error object
 *  a refusing gateway returns, which the non-array fallback test pins. */
type ModelsMock = Mock<() => Promise<ModelRow[] | { error: string }>>

describe('AcpAdapter.fetchAvailableModels', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    localStorage.clear()
  })

  it('returns backend-advertised models on success', async () => {
    ;(api.models as ModelsMock).mockResolvedValue([
      { model_name: 'auto', description: 'Let the provider pick' },
      { model_name: 'claude-opus-4.8', description: 'Most capable' },
      { model_name: 'claude-sonnet-4.6', description: 'Everyday tasks' },
    ])
    const models = await new AcpAdapter().fetchAvailableModels()
    expect(models.length).toBe(3)
    expect(models[0].name).toBe('auto')
    expect(models[1].name).toBe('claude-opus-4.8')
    expect(models[2].description).toBe('Everyday tasks')
  })

  it('keeps the catalog label for the auto row, never a backend display name', async () => {
    ;(api.models as ModelsMock).mockResolvedValue([
      { model_name: 'auto', display_name: 'Auto', description: 'Let the provider pick' },
      { model_name: 'claude-opus-4.8', display_name: 'Opus 5.5', description: 'Most capable' },
    ])
    const models = await new AcpAdapter().fetchAvailableModels()
    expect(models[0].name).toBe('auto')
    expect(models[0].displayName).toBeUndefined()
    expect(models[1].displayName).toBe('Opus 5.5')
  })

  it('falls back to AUTO-ONLY when API returns non-array (e.g. error object)', async () => {
    ;(api.models as ModelsMock).mockResolvedValue({ error: 'Token required' })
    const models = await new AcpAdapter().fetchAvailableModels()
    // Never surface canonical registry keys (opus-4.8-1m, fable-5-1m, …): the
    // ACP CLI rejects them as model ids (-32603). Only 'auto' is safe.
    expect(models).toHaveLength(1)
    expect(models[0].name).toBe('auto')
    expect(models.some(m => m.name.includes('-1m') || m.name === 'opus-4.8')).toBe(false)
  })

  it('falls back to AUTO-ONLY when API returns empty array', async () => {
    ;(api.models as ModelsMock).mockResolvedValue([])
    const models = await new AcpAdapter().fetchAvailableModels()
    expect(models).toHaveLength(1)
    expect(models[0].name).toBe('auto')
  })

  it('falls back to AUTO-ONLY when API throws (timeout, network error)', async () => {
    ;(api.models as ModelsMock).mockRejectedValue(new Error('fetch timeout'))
    const models = await new AcpAdapter().fetchAvailableModels()
    expect(models).toHaveLength(1)
    expect(models[0].name).toBe('auto')
  })

  it('auto-only fallback carries a sensible context window', async () => {
    ;(api.models as ModelsMock).mockRejectedValue(new Error('boom'))
    const models = await new AcpAdapter().fetchAvailableModels()
    expect(models[0].name).toBe('auto')
    expect(models[0].contextWindow).toBeGreaterThan(0)
  })

  it('persists a good live list to localStorage', async () => {
    ;(api.models as ModelsMock).mockResolvedValue([
      { model_name: 'auto', description: 'a' },
      { model_name: 'claude-opus-4.8', description: 'b' },
    ])
    await new AcpAdapter().fetchAvailableModels()
    const raw = localStorage.getItem('kc.acp.models.v1')
    expect(raw).toBeTruthy()
    const cached = JSON.parse(raw as string)
    expect(cached.models.map((m: ModelInfo) => m.name)).toEqual(['auto', 'claude-opus-4.8'])
    expect(typeof cached.ts).toBe('number')
  })

  it('serves the last-good cached list (not auto-only) when the API throws', async () => {
    // Prime the cache with a good live fetch.
    ;(api.models as ModelsMock).mockResolvedValueOnce([
      { model_name: 'auto', description: 'a' },
      { model_name: 'claude-opus-4.8', description: 'b' },
      { model_name: 'claude-fable-5', description: 'c' },
    ])
    const adapter = new AcpAdapter()
    await adapter.fetchAvailableModels()
    // Next fetch fails transiently — should degrade to the cached 3, not auto-only.
    ;(api.models as ModelsMock).mockRejectedValue(new Error('503'))
    const models = await adapter.fetchAvailableModels()
    expect(models).toHaveLength(3)
    expect(models.map(m => m.name)).toContain('claude-fable-5')
  })

  it('falls back to auto-only when the API throws and there is no cache', async () => {
    ;(api.models as ModelsMock).mockRejectedValue(new Error('503'))
    const models = await new AcpAdapter().fetchAvailableModels()
    expect(models).toHaveLength(1)
    expect(models[0].name).toBe('auto')
  })

  it('clearCachedModels drops the last-good list so a failing fetch degrades to auto-only', async () => {
    // A backend switch: the cache holds the OLD backend's ids. After the drop,
    // a failing first fetch on the new backend must not resurrect them.
    ;(api.models as ModelsMock).mockResolvedValueOnce([
      { model_name: 'auto', description: 'a' },
      { model_name: 'claude-opus-4.8', description: 'b' },
    ])
    const adapter = new AcpAdapter()
    await adapter.fetchAvailableModels()
    expect(localStorage.getItem('kc.acp.models.v1')).not.toBeNull()
    clearCachedModels()
    expect(localStorage.getItem('kc.acp.models.v1')).toBeNull()
    ;(api.models as ModelsMock).mockRejectedValue(new Error('503'))
    const models = await adapter.fetchAvailableModels()
    expect(models).toHaveLength(1)
    expect(models[0].name).toBe('auto')
  })

  it('does not overwrite the cache with an empty/failed result', async () => {
    ;(api.models as ModelsMock).mockResolvedValueOnce([
      { model_name: 'auto', description: 'a' },
      { model_name: 'claude-opus-4.8', description: 'b' },
    ])
    const adapter = new AcpAdapter()
    await adapter.fetchAvailableModels()
    ;(api.models as ModelsMock).mockResolvedValue([]) // empty success must not clobber cache
    await adapter.fetchAvailableModels()
    const cached = JSON.parse(localStorage.getItem('kc.acp.models.v1') as string)
    expect(cached.models).toHaveLength(2)
  })

  it('ignores a cache older than the TTL (bounds -32603 exposure)', async () => {
    // Write a stale cache (25h old) directly.
    localStorage.setItem(
      'kc.acp.models.v1',
      JSON.stringify({
        ts: Date.now() - 25 * 60 * 60 * 1000,
        models: [{ name: 'auto' }, { name: 'stale-model' }],
      }),
    )
    ;(api.models as ModelsMock).mockRejectedValue(new Error('503'))
    const models = await new AcpAdapter().fetchAvailableModels()
    // Too stale to trust → auto-only, not the stale cached list.
    expect(models).toHaveLength(1)
    expect(models[0].name).toBe('auto')
  })

  it('ignores a cache with a future timestamp (clock skew)', async () => {
    localStorage.setItem(
      'kc.acp.models.v1',
      JSON.stringify({
        ts: Date.now() + 60 * 60 * 1000, // 1h in the future
        models: [{ name: 'auto' }, { name: 'skewed-model' }],
      }),
    )
    ;(api.models as ModelsMock).mockRejectedValue(new Error('503'))
    const models = await new AcpAdapter().fetchAvailableModels()
    expect(models).toHaveLength(1)
    expect(models[0].name).toBe('auto')
  })
})

describe('last-good model list is scoped to the signed-in account', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    localStorage.clear()
  })

  const opusList = [
    { model_name: 'auto', description: 'a' },
    { model_name: 'claude-opus-5', description: 'b' },
  ]

  async function primeFor(account: string, adapter: AcpAdapter) {
    noteKiroAccount(account)
    ;(api.models as ModelsMock).mockResolvedValueOnce(opusList)
    await adapter.fetchAvailableModels()
  }

  it('a switch to another account drops the list, so a server blip shows auto-only', async () => {
    const adapter = new AcpAdapter()
    await primeFor('acct-free', adapter)
    expect(noteKiroAccount('acct-power')).toBe(true)
    expect(localStorage.getItem('kc.acp.models.v1')).toBeNull()
    ;(api.models as ModelsMock).mockRejectedValue(new Error('503'))
    expect((await adapter.fetchAvailableModels()).map(m => m.name)).toEqual(['auto'])
  })

  it('the same account, or no tag at all, keeps the list and asks for no refetch', async () => {
    const adapter = new AcpAdapter()
    await primeFor('acct-a', adapter)
    expect(noteKiroAccount('acct-a')).toBe(false)
    expect(noteKiroAccount(undefined)).toBe(false)
    expect(noteKiroAccount('')).toBe(false)
    ;(api.models as ModelsMock).mockRejectedValue(new Error('503'))
    expect((await adapter.fetchAvailableModels()).map(m => m.name)).toEqual(['auto', 'claude-opus-5'])
  })

  it('a list cached for another account is not served after a reload', async () => {
    // Written by an earlier page for account A; storage now names B.
    await primeFor('acct-a', new AcpAdapter())
    localStorage.setItem('kc.acp.account.v1', 'acct-b')
    ;(api.models as ModelsMock).mockRejectedValue(new Error('503'))
    expect((await new AcpAdapter().fetchAvailableModels()).map(m => m.name)).toEqual(['auto'])
  })

  it('the first tag a page sees re-reads a list fetched before any account was known', async () => {
    vi.resetModules()
    const fresh = await import('../providers/adapters/acp')
    const { api: freshApi } = await import('../api/client')
    ;(freshApi.models as ModelsMock).mockResolvedValueOnce(opusList)
    await new fresh.AcpAdapter().fetchAvailableModels()
    expect(fresh.noteKiroAccount('acct-b')).toBe(true)
    // Once known, the same tag asks for nothing more.
    expect(fresh.noteKiroAccount('acct-b')).toBe(false)
  })

  it('a new tab re-reads its first list when another tab handled a switch meanwhile', async () => {
    localStorage.setItem('kc.acp.account.v1', 'acct-a')
    vi.resetModules()
    const fresh = await import('../providers/adapters/acp')
    const { api: freshApi } = await import('../api/client')
    ;(freshApi.models as ModelsMock).mockResolvedValueOnce(opusList)
    await new fresh.AcpAdapter().fetchAvailableModels()
    localStorage.setItem('kc.acp.account.v1', 'acct-b')
    localStorage.removeItem('kc.acp.models.v1')
    expect(fresh.noteKiroAccount('acct-b')).toBe(true)
  })

  it('a second tab still learns of the switch after the first tab handled it', async () => {
    await primeFor('acct-a', new AcpAdapter())
    // The other tab's poll saw B first: it wrote B and dropped the stored list.
    localStorage.setItem('kc.acp.account.v1', 'acct-b')
    localStorage.removeItem('kc.acp.models.v1')
    expect(noteKiroAccount('acct-b')).toBe(true)
  })

  it('a list that was in flight across the switch is not cached', async () => {
    const adapter = new AcpAdapter()
    noteKiroAccount('acct-free')
    let resolve: (rows: ModelRow[]) => void = () => {}
    ;(api.models as ModelsMock).mockReturnValueOnce(new Promise(r => { resolve = r }))
    const inFlight = adapter.fetchAvailableModels()
    noteKiroAccount('acct-power')
    // The request that replaced it failed and marked the list degraded.
    markModelsDegraded('acp', true)
    resolve([{ model_name: 'auto' }])
    await inFlight
    expect(localStorage.getItem('kc.acp.models.v1')).toBeNull()
    // The obsolete success must not clear that and stop the recovery polling.
    expect(modelsDegraded('acp')).toBe(true)
    markModelsDegraded('acp', false)
  })
})

describe('model-list liveness (self-heal signal)', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    localStorage.clear()
    markModelsDegraded('acp', false)
    markModelsCold('acp', false)
  })

  it('a cold answer is shown but not saved over the last-good list, and keeps polling with no failure', async () => {
    ;(api.models as ModelsMock).mockResolvedValueOnce([
      { model_name: 'auto', description: 'a' },
      { model_name: 'claude-opus-4.8', description: 'b' },
    ])
    const adapter = new AcpAdapter()
    await adapter.fetchAvailableModels()
    // The gateway restarts: a failed read, then no session has recorded this account's list yet.
    ;(api.models as ModelsMock).mockRejectedValueOnce(new Error('503'))
    await adapter.fetchAvailableModels()
    expect(modelsDegraded('acp')).toBe(true)
    ;(api.models as ModelsMock).mockResolvedValueOnce([
      { model_name: 'auto', description: 'Backend default', cold: true },
    ])
    const served = await adapter.fetchAvailableModels()
    expect(served.map(m => m.name)).toEqual(['auto'])
    const cached = JSON.parse(localStorage.getItem('kc.acp.models.v1') as string)
    expect(cached.models.map((m: ModelInfo) => m.name)).toEqual(['auto', 'claude-opus-4.8'])
    expect(modelsDegraded('acp')).toBe(false)
    expect(modelListRefetchInterval({ queryKey: ['available-models', 'acp'] })).toBe(8_000)
    // The first recorded list ends the cold state, and the polling with it.
    ;(api.models as ModelsMock).mockResolvedValueOnce([
      { model_name: 'auto', description: 'a' },
      { model_name: 'claude-opus-4.8', description: 'b' },
    ])
    await adapter.fetchAvailableModels()
    expect(modelListRefetchInterval({ queryKey: ['available-models', 'acp'] })).toBe(false)
  })

  it('marks degraded on failure and clears it on a live success', async () => {
    ;(api.models as ModelsMock).mockRejectedValue(new Error('503'))
    const adapter = new AcpAdapter()
    await adapter.fetchAvailableModels()
    expect(modelsDegraded('acp')).toBe(true)
    // Poll continues while degraded, regardless of served list length.
    expect(modelListRefetchInterval({ queryKey: ['available-models', 'acp'] })).toBe(8_000)

    ;(api.models as ModelsMock).mockResolvedValue([
      { model_name: 'auto', description: 'a' },
      { model_name: 'claude-opus-4.8', description: 'b' },
    ])
    await adapter.fetchAvailableModels()
    expect(modelsDegraded('acp')).toBe(false)
    // Live success → stop polling.
    expect(modelListRefetchInterval({ queryKey: ['available-models', 'acp'] })).toBe(false)
  })

  it('keeps polling on a degraded CACHED multi-model list (the -32603/stale bug)', async () => {
    // Prime a good live list, then fail: the served list is multi-entry but
    // degraded — polling MUST continue.
    ;(api.models as ModelsMock).mockResolvedValueOnce([
      { model_name: 'auto', description: 'a' },
      { model_name: 'claude-opus-4.8', description: 'b' },
      { model_name: 'claude-fable-5', description: 'c' },
    ])
    const adapter = new AcpAdapter()
    await adapter.fetchAvailableModels()
    ;(api.models as ModelsMock).mockRejectedValue(new Error('503'))
    const served = await adapter.fetchAvailableModels()
    expect(served.length).toBeGreaterThan(1) // multi-entry cached list
    expect(modelsDegraded('acp')).toBe(true)
    expect(modelListRefetchInterval({ queryKey: ['available-models', 'acp'] })).toBe(8_000)
  })

  it('does not poll an unmarked/unknown provider', () => {
    expect(modelListRefetchInterval({ queryKey: ['available-models', 'other'] })).toBe(false)
  })
})
