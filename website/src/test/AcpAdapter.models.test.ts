import { describe, it, expect, vi, beforeEach, type Mock } from 'vitest'

// Mock the API client so fetchAvailableModels reads our canned /api/models.
vi.mock('../api/client', () => ({
  api: {
    models: vi.fn(),
  },
}))

import { api } from '../api/client'
import { AcpAdapter } from '../providers/adapters/acp'
import {
  markModelsDegraded,
  modelsDegraded,
  modelListRefetchInterval,
} from '../providers/modelListHealth'

const apiModelsMock = api.models as Mock

describe('AcpAdapter.fetchAvailableModels', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    localStorage.clear()
  })

  it('returns backend-advertised models on success', async () => {
    ;apiModelsMock.mockResolvedValue([
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

  it('falls back to AUTO-ONLY when API returns non-array (e.g. error object)', async () => {
    ;apiModelsMock.mockResolvedValue({ error: 'Token required' })
    const models = await new AcpAdapter().fetchAvailableModels()
    // Never surface canonical registry keys (opus-4.8-1m, fable-5-1m, …): the
    // ACP CLI rejects them as model ids (-32603). Only 'auto' is safe.
    expect(models).toHaveLength(1)
    expect(models[0].name).toBe('auto')
    expect(models.some(m => m.name.includes('-1m') || m.name === 'opus-4.8')).toBe(false)
  })

  it('falls back to AUTO-ONLY when API returns empty array', async () => {
    ;apiModelsMock.mockResolvedValue([])
    const models = await new AcpAdapter().fetchAvailableModels()
    expect(models).toHaveLength(1)
    expect(models[0].name).toBe('auto')
  })

  it('falls back to AUTO-ONLY when API throws (timeout, network error)', async () => {
    ;apiModelsMock.mockRejectedValue(new Error('fetch timeout'))
    const models = await new AcpAdapter().fetchAvailableModels()
    expect(models).toHaveLength(1)
    expect(models[0].name).toBe('auto')
  })

  it('auto-only fallback carries a sensible context window', async () => {
    ;apiModelsMock.mockRejectedValue(new Error('boom'))
    const models = await new AcpAdapter().fetchAvailableModels()
    expect(models[0].name).toBe('auto')
    expect(models[0].contextWindow).toBeGreaterThan(0)
  })

  it('persists a good live list to localStorage', async () => {
    ;apiModelsMock.mockResolvedValue([
      { model_name: 'auto', description: 'a' },
      { model_name: 'claude-opus-4.8', description: 'b' },
    ])
    await new AcpAdapter().fetchAvailableModels()
    const raw = localStorage.getItem('kc.acp.models.v1')
    expect(raw).toBeTruthy()
    const cached = JSON.parse(raw as string) as {
      models: Array<{ name: string }>
      ts: unknown
    }
    expect(cached.models.map(m => m.name)).toEqual(['auto', 'claude-opus-4.8'])
    expect(typeof cached.ts).toBe('number')
  })

  it('serves the last-good cached list (not auto-only) when the API throws', async () => {
    // Prime the cache with a good live fetch.
    ;apiModelsMock.mockResolvedValueOnce([
      { model_name: 'auto', description: 'a' },
      { model_name: 'claude-opus-4.8', description: 'b' },
      { model_name: 'claude-fable-5', description: 'c' },
    ])
    const adapter = new AcpAdapter()
    await adapter.fetchAvailableModels()
    // Next fetch fails transiently — should degrade to the cached 3, not auto-only.
    ;apiModelsMock.mockRejectedValue(new Error('503'))
    const models = await adapter.fetchAvailableModels()
    expect(models).toHaveLength(3)
    expect(models.map(m => m.name)).toContain('claude-fable-5')
  })

  it('falls back to auto-only when the API throws and there is no cache', async () => {
    ;apiModelsMock.mockRejectedValue(new Error('503'))
    const models = await new AcpAdapter().fetchAvailableModels()
    expect(models).toHaveLength(1)
    expect(models[0].name).toBe('auto')
  })

  it('does not overwrite the cache with an empty/failed result', async () => {
    ;apiModelsMock.mockResolvedValueOnce([
      { model_name: 'auto', description: 'a' },
      { model_name: 'claude-opus-4.8', description: 'b' },
    ])
    const adapter = new AcpAdapter()
    await adapter.fetchAvailableModels()
    ;apiModelsMock.mockResolvedValue([]) // empty success must not clobber cache
    await adapter.fetchAvailableModels()
    const cached = JSON.parse(localStorage.getItem('kc.acp.models.v1') as string)
    expect(cached.models).toHaveLength(2)
  })

  it('uses the {models, backend} envelope a non-kiro backend returns', async () => {
    // The kiro path answers with a bare array; a spec adapter answers an
    // object. Reading only the array form treated this good list as a non-array
    // "empty success" and fell through to the cache, serving kiro ids.
    ;apiModelsMock.mockResolvedValue({
      models: [{ model_name: 'claude-opus-5', description: 'x' }],
      backend: 'claude',
    })
    const models = await new AcpAdapter().fetchAvailableModels()
    expect(models.map(m => m.name)).toEqual(['claude-opus-5'])
  })

  it('refuses a kiro-stamped cache after a bodyless failure on a spec adapter', async () => {
    // Prime the cache from kiro (bare array) — these ids are kiro-namespace.
    ;apiModelsMock.mockResolvedValueOnce([
      { model_name: 'auto', description: 'a' },
      { model_name: 'gpt-5.6-sol', description: 'b' },
    ])
    const adapter = new AcpAdapter()
    await adapter.fetchAvailableModels()

    // A spec adapter identifies itself once...
    ;apiModelsMock.mockResolvedValueOnce({ models: [], backend: 'claude' })
    await adapter.fetchAvailableModels()

    // ...then the network dies, so the failure carries NO backend at all. The
    // cache is stamped kiro, the active namespace is claude, so it must not be
    // served even though serving something would look friendlier.
    ;apiModelsMock.mockRejectedValue(new Error('network down'))
    expect(await adapter.fetchAvailableModels()).toEqual([])
  })

  it('serves NO rows when the backend namespace is unavailable, even with a warm cache', async () => {
    // Prime the cache from a kiro session — these ids are kiro-namespace.
    ;apiModelsMock.mockResolvedValueOnce([
      { model_name: 'auto', description: 'a' },
      { model_name: 'gpt-5.6-sol', description: 'b' },
    ])
    const adapter = new AcpAdapter()
    await adapter.fetchAvailableModels()
    expect(localStorage.getItem('kc.acp.models.v1')).toBeTruthy()

    // The operator has now switched to a spec adapter and no session has
    // advertised its namespace. The cached kiro rows must NOT be offered: a
    // plausible `gpt-5.6-sol` row is worse than none, because it reads as a
    // real option and is rejected at the wire.
    ;apiModelsMock.mockRejectedValue(
      Object.assign(new Error('503'), {
        body: JSON.stringify({ code: 'acp_backend_models_unavailable' }),
      })
    )
    expect(await adapter.fetchAvailableModels()).toEqual([])
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
    ;apiModelsMock.mockRejectedValue(new Error('503'))
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
    ;apiModelsMock.mockRejectedValue(new Error('503'))
    const models = await new AcpAdapter().fetchAvailableModels()
    expect(models).toHaveLength(1)
    expect(models[0].name).toBe('auto')
  })
})

describe('model-list liveness (self-heal signal)', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    localStorage.clear()
    markModelsDegraded('acp', false)
    markModelsDegraded('acp', false, 'config:')
    markModelsDegraded('acp', false, 'config:codex')
    markModelsDegraded('acp', false, 'slot:chat-1')
  })

  it('marks degraded on failure and clears it on a live success', async () => {
    ;apiModelsMock.mockRejectedValue(new Error('503'))
    const adapter = new AcpAdapter()
    await adapter.fetchAvailableModels()
    expect(modelsDegraded('acp')).toBe(true)
    // Poll continues while degraded, regardless of served list length.
    expect(modelListRefetchInterval({ queryKey: ['available-models', 'acp'] })).toBe(8_000)

    ;apiModelsMock.mockResolvedValue([
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
    ;apiModelsMock.mockResolvedValueOnce([
      { model_name: 'auto', description: 'a' },
      { model_name: 'claude-opus-4.8', description: 'b' },
      { model_name: 'claude-fable-5', description: 'c' },
    ])
    const adapter = new AcpAdapter()
    await adapter.fetchAvailableModels()
    ;apiModelsMock.mockRejectedValue(new Error('503'))
    const served = await adapter.fetchAvailableModels()
    expect(served.length).toBeGreaterThan(1) // multi-entry cached list
    expect(modelsDegraded('acp')).toBe(true)
    expect(modelListRefetchInterval({ queryKey: ['available-models', 'acp'] })).toBe(8_000)
  })

  it('does not poll an unmarked/unknown provider', () => {
    expect(modelListRefetchInterval({ queryKey: ['available-models', 'other'] })).toBe(false)
  })

  it('does not let a config-namespace 503 restart a live-slot poll', async () => {
    ;apiModelsMock.mockRejectedValue(new Error('503'))
    const adapter = new AcpAdapter()
    await adapter.fetchAvailableModels({ scope: 'config:codex' })
    expect(modelsDegraded('acp')).toBe(false)
    expect(modelsDegraded('acp', 'slot:chat-1')).toBe(false)
    expect(modelListRefetchInterval({ queryKey: ['available-models', 'acp', 'slot:chat-1'] })).toBe(
      false,
    )
    expect(modelListRefetchInterval({ queryKey: ['available-models', 'acp', 'config:codex'] })).toBe(
      8_000,
    )
  })

  it('marks a slot-scoped 503 on that slot without flapping config', async () => {
    ;apiModelsMock.mockRejectedValue(new Error('503'))
    const adapter = new AcpAdapter()
    await adapter.fetchAvailableModels({ slot: 'chat-1', scope: 'slot:chat-1' })
    expect(modelsDegraded('acp', 'slot:chat-1')).toBe(true)
    expect(modelsDegraded('acp')).toBe(true)
    expect(modelListRefetchInterval({ queryKey: ['available-models', 'acp', 'slot:chat-1'] })).toBe(
      8_000,
    )
    expect(modelListRefetchInterval({ queryKey: ['available-models', 'acp', 'config:'] })).toBe(
      false,
    )
  })
})

describe('AcpAdapter.fetchAvailableModels slot scope', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    localStorage.clear()
  })

  it('passes ?slot= through to GET /api/models', async () => {
    ;apiModelsMock.mockResolvedValue([
      { model_name: 'auto', description: 'a' },
      { model_name: 'gpt-5.6-sol', description: 'b' },
    ])
    await new AcpAdapter().fetchAvailableModels({ slot: 'chat-1' })
    expect(api.models).toHaveBeenCalledWith({ slot: 'chat-1' })
  })

  it('does not rewrite the config-namespace cache from a session-scoped fetch', async () => {
    // Prime the config cache as Codex so a kiro live-slot fetch must not
    // stamp lastKnown / the picker cache back to kiro.
    ;apiModelsMock.mockResolvedValueOnce({
      models: [{ model_name: 'gpt-5.2[high]', description: 'x' }],
      backend: 'codex',
      serves_auto: false,
    })
    const adapter = new AcpAdapter()
    await adapter.fetchAvailableModels()
    expect(localStorage.getItem('kc.acp.backend.v1')).toBe('codex')
    expect(localStorage.getItem('kc.acp.servesAuto.v1')).toBe('0')

    ;apiModelsMock.mockResolvedValueOnce([
      { model_name: 'auto', description: 'a' },
      { model_name: 'gpt-5.6-sol', description: 'b' },
    ])
    const live = await adapter.fetchAvailableModels({ slot: 'chat-kiro' })
    expect(live.map(m => m.name)).toEqual(['auto', 'gpt-5.6-sol'])
    expect(localStorage.getItem('kc.acp.backend.v1')).toBe('codex')
    expect(localStorage.getItem('kc.acp.servesAuto.v1')).toBe('0')
    const cached = JSON.parse(localStorage.getItem('kc.acp.models.v1') as string)
    expect(cached.backend).toBe('codex')
    expect(cached.models.map((m: { name: string }) => m.name)).toEqual(['gpt-5.2[high]'])
  })
})
