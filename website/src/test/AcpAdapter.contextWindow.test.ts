/**
 * The composer's context meter reads `provider.getContextWindow(slot.model)`
 * whenever the store has no live `{used, window}` for the slot — which is exactly
 * the window between a model switch (the backend broadcasts a `reset`, dropping
 * the previous model's counts) and the next turn's `usage_update`.
 *
 * That lookup used to consult ONLY the bundled `model_tokens.json` snapshot,
 * which lists neither the models kiro serves today (claude-opus-5, gpt-5.6-sol,
 * glm-5, kimi-k3, grok-4.3, …) nor `auto`. Every one of them fell through to the
 * 200K default, so switching to a 1M model showed "0% of 200K" until a message
 * was sent. /api/models already carries the backend's resolved window per row;
 * these tests pin that the adapter learns it and serves it.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'

vi.mock('../api/client', () => ({
  api: {
    models: vi.fn(),
  },
}))

import { api } from '../api/client'
import { AcpAdapter } from '../providers/adapters/acp'

describe('AcpAdapter context-window resolution', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    localStorage.clear()
  })

  it('learns windows from the kiro branch field (context_window_tokens)', async () => {
    const adapter = new AcpAdapter()
    // Before the list loads there is nothing to learn — the bundled snapshot has
    // no entry for these ids, so both read as the reference default.
    expect(adapter.getContextWindow('claude-opus-5')).toBe(200_000)
    expect(adapter.getContextWindow('gpt-5.6-sol')).toBe(200_000)
    ;(api.models as any).mockResolvedValue([
      { model_name: 'claude-opus-5', context_window_tokens: 1_000_000 },
      { model_name: 'gpt-5.6-sol', context_window_tokens: 272_000 },
    ])
    const models = await adapter.fetchAvailableModels()
    expect(models.map(m => m.contextWindow)).toEqual([1_000_000, 272_000])
    // …and the per-model lookup the context meter uses now agrees.
    expect(adapter.getContextWindow('claude-opus-5')).toBe(1_000_000)
    expect(adapter.getContextWindow('gpt-5.6-sol')).toBe(272_000)
  })

  it('learns windows from the claude_code branch field (context_window)', async () => {
    ;(api.models as any).mockResolvedValue([
      { model_name: 'kimi-k3', context_window: 1_000_000 },
    ])
    const adapter = new AcpAdapter()
    await adapter.fetchAvailableModels()
    expect(adapter.getContextWindow('kimi-k3')).toBe(1_000_000)
  })

  it('keeps the bundled snapshot for a row that reports no window', async () => {
    ;(api.models as any).mockResolvedValue([
      { model_name: 'claude-opus-4.8' }, // gateway predating the enrichment
    ])
    const adapter = new AcpAdapter()
    const models = await adapter.fetchAvailableModels()
    expect(models[0].contextWindow).toBe(1_000_000) // from model_tokens.json
    expect(adapter.getContextWindow('claude-opus-4.8')).toBe(1_000_000)
  })

  it('ignores a non-positive window rather than overwriting a learned one', async () => {
    const adapter = new AcpAdapter()
    ;(api.models as any).mockResolvedValue([
      { model_name: 'minimax-m2.5', context_window_tokens: 196_000 },
    ])
    await adapter.fetchAvailableModels()
    expect(adapter.getContextWindow('minimax-m2.5')).toBe(196_000)
    // A later list that cannot resolve the window must not clobber the good one.
    ;(api.models as any).mockResolvedValue([
      { model_name: 'minimax-m2.5', context_window_tokens: 0 },
    ])
    await adapter.fetchAvailableModels()
    expect(adapter.getContextWindow('minimax-m2.5')).toBe(196_000)
  })

  it('serves the learned window for a model still unknown to the bundle', async () => {
    ;(api.models as any).mockResolvedValue([
      { model_name: 'grok-4.3', context_window_tokens: 1_000_000 },
    ])
    const adapter = new AcpAdapter()
    await adapter.fetchAvailableModels()
    // A brand-new id the bundled map will never list resolves correctly with no
    // frontend release, which is the point of reading it from the backend.
    expect(adapter.getContextWindow('grok-4.3')).toBe(1_000_000)
    // An id nobody has reported still degrades to the reference default.
    expect(adapter.getContextWindow('some-unreleased-model')).toBe(200_000)
  })

  it('reports the learned window for the auto sentinel in the degraded list', async () => {
    ;(api.models as any).mockResolvedValue([
      { model_name: 'auto', context_window_tokens: 1_000_000 },
    ])
    const adapter = new AcpAdapter()
    await adapter.fetchAvailableModels()
    expect(adapter.getContextWindow('auto')).toBe(1_000_000)
    // The auto-only degraded fallback quotes the same learned figure instead of
    // hardcoding 200K (model_tokens.json has no 'auto' entry at all).
    ;(api.models as any).mockRejectedValue(new Error('503'))
    localStorage.clear()
    const degraded = await adapter.fetchAvailableModels()
    expect(degraded).toHaveLength(1)
    expect(degraded[0].name).toBe('auto')
    expect(degraded[0].contextWindow).toBe(1_000_000)
  })
})
