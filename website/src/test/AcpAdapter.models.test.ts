import { describe, it, expect, vi, beforeEach } from 'vitest'

// Mock the API client so fetchAvailableModels reads our canned /api/models.
vi.mock('../api/client', () => ({
  api: {
    models: vi.fn(),
  },
}))

import { api } from '../api/client'
import { AcpAdapter } from '../providers/adapters/acp'

describe('AcpAdapter.fetchAvailableModels', () => {
  beforeEach(() => vi.clearAllMocks())

  it('returns backend-advertised models on success', async () => {
    ;(api.models as any).mockResolvedValue([
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
    ;(api.models as any).mockResolvedValue({ error: 'Token required' })
    const models = await new AcpAdapter().fetchAvailableModels()
    // Never surface canonical registry keys (opus-4.8-1m, fable-5-1m, …): the
    // ACP CLI rejects them as model ids (-32603). Only 'auto' is safe.
    expect(models).toHaveLength(1)
    expect(models[0].name).toBe('auto')
    expect(models.some(m => m.name.includes('-1m') || m.name === 'opus-4.8')).toBe(false)
  })

  it('falls back to AUTO-ONLY when API returns empty array', async () => {
    ;(api.models as any).mockResolvedValue([])
    const models = await new AcpAdapter().fetchAvailableModels()
    expect(models).toHaveLength(1)
    expect(models[0].name).toBe('auto')
  })

  it('falls back to AUTO-ONLY when API throws (timeout, network error)', async () => {
    ;(api.models as any).mockRejectedValue(new Error('fetch timeout'))
    const models = await new AcpAdapter().fetchAvailableModels()
    expect(models).toHaveLength(1)
    expect(models[0].name).toBe('auto')
  })

  it('auto-only fallback carries a sensible context window', async () => {
    ;(api.models as any).mockRejectedValue(new Error('boom'))
    const models = await new AcpAdapter().fetchAvailableModels()
    expect(models[0].name).toBe('auto')
    expect(models[0].contextWindow).toBeGreaterThan(0)
  })
})
