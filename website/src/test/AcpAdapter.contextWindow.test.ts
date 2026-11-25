/**
 * The composer's context meter reads `provider.getContextWindow(slot.model)`
 * whenever the store has no live `{used, window}` for the slot — which is exactly
 * the window between a model switch (the backend broadcasts a `reset`, dropping
 * the previous model's counts) and the next turn's `usage_update`.
 *
 * The bundled `model_tokens.json` snapshot lists neither the models kiro serves
 * today (claude-opus-5, gpt-5.6-sol, glm-5, kimi-k3, grok-4.3, …) nor `auto`;
 * /api/models carries the backend's resolved window on each row a source knows.
 * These tests pin that the adapter learns and serves that window, and answers 0
 * (unknown) for an id neither knows rather than a made-up figure.
 */
import { describe, it, expect, vi, beforeEach, onTestFinished } from 'vitest'
import type { Mock } from 'vitest'

vi.mock('../api/client', () => ({
  api: {
    models: vi.fn(),
  },
}))

import { api } from '../api/client'
import { AcpAdapter } from '../providers/adapters/acp'

/** The /api/models rows these fixtures feed the adapter. The adapter's own
 *  RawModel is module-private, so mirror only the fields set here — both window
 *  spellings, since the two gateway branches disagree. */
type ModelRow = {
  model_name: string
  context_window?: number
  context_window_tokens?: number
}

/** `api.models` as replaced by vi.mock above. */
type ModelsMock = Mock<() => Promise<ModelRow[]>>

describe('AcpAdapter context-window resolution', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    localStorage.clear()
  })

  it('learns windows from the kiro branch field (context_window_tokens)', async () => {
    const adapter = new AcpAdapter()
    // Before the list loads there is nothing to learn — the bundled snapshot has
    // no entry for these ids, so neither window is known.
    expect(adapter.getContextWindow('claude-opus-5')).toBe(0)
    expect(adapter.getContextWindow('gpt-5.6-sol')).toBe(0)
    ;(api.models as ModelsMock).mockResolvedValue([
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
    ;(api.models as ModelsMock).mockResolvedValue([
      { model_name: 'kimi-k3', context_window: 1_000_000 },
    ])
    const adapter = new AcpAdapter()
    await adapter.fetchAvailableModels()
    expect(adapter.getContextWindow('kimi-k3')).toBe(1_000_000)
  })

  it('keeps the bundled snapshot for a row that reports no window', async () => {
    ;(api.models as ModelsMock).mockResolvedValue([
      { model_name: 'claude-opus-4.8' }, // gateway predating the enrichment
    ])
    const adapter = new AcpAdapter()
    const models = await adapter.fetchAvailableModels()
    expect(models[0].contextWindow).toBe(1_000_000) // from model_tokens.json
    expect(adapter.getContextWindow('claude-opus-4.8')).toBe(1_000_000)
  })

  it('ignores a non-positive window rather than overwriting a learned one', async () => {
    const adapter = new AcpAdapter()
    ;(api.models as ModelsMock).mockResolvedValue([
      { model_name: 'minimax-m2.5', context_window_tokens: 196_000 },
    ])
    await adapter.fetchAvailableModels()
    expect(adapter.getContextWindow('minimax-m2.5')).toBe(196_000)
    // A later list that cannot resolve the window must not clobber the good one.
    ;(api.models as ModelsMock).mockResolvedValue([
      { model_name: 'minimax-m2.5', context_window_tokens: 0 },
    ])
    await adapter.fetchAvailableModels()
    expect(adapter.getContextWindow('minimax-m2.5')).toBe(196_000)
  })

  it('serves the learned window for a model still unknown to the bundle', async () => {
    ;(api.models as ModelsMock).mockResolvedValue([
      { model_name: 'grok-4.3', context_window_tokens: 1_000_000 },
    ])
    const adapter = new AcpAdapter()
    await adapter.fetchAvailableModels()
    // A brand-new id the bundled map will never list resolves correctly with no
    // frontend release, which is the point of reading it from the backend.
    expect(adapter.getContextWindow('grok-4.3')).toBe(1_000_000)
    // An id nobody has reported has no known window.
    expect(adapter.getContextWindow('some-unreleased-model')).toBe(0)
  })

  it('knows no window for a Codex pick no source reports, not a 200K default', async () => {
    // codex-acp advertises model x effort pairs with no window, and the composer
    // shows the base model for an effort it groups, so neither id is known
    // until the first usage_update reports the served window.
    ;(api.models as ModelsMock).mockResolvedValue([
      { model_name: 'gpt-6.2-sol[medium]' },
      { model_name: 'gpt-6.2-sol[ultra]' },
    ])
    const adapter = new AcpAdapter()
    const models = await adapter.fetchAvailableModels()
    // The persisted rows carry no made-up window to seed the lookup on reload.
    expect(models.map(m => m.contextWindow)).toEqual([undefined, undefined])
    expect(adapter.getContextWindow('gpt-6.2-sol[ultra]')).toBe(0)
    expect(adapter.getContextWindow('gpt-6.2-sol')).toBe(0)
  })

  it('drops a window only the saved list gave once the live list names the id without one', async () => {
    // A list an earlier build saved, carrying the 1M stand-in for a Codex pair.
    vi.useFakeTimers({ toFake: ['Date'] })
    onTestFinished(() => { vi.useRealTimers() })
    vi.setSystemTime(new Date('2026-11-25T12:00:00Z'))
    localStorage.setItem('kc.acp.models.v1', JSON.stringify({
      ts: Date.now(),
      models: [{ name: 'gpt-6.2-sol[ultra]', contextWindow: 1_000_000 }],
    }))
    vi.resetModules()
    const fresh = await import('../providers/adapters/acp')
    const { api: freshApi } = await import('../api/client')
    const adapter = new fresh.AcpAdapter()
    // The cold start still reads the saved list before /api/models answers.
    expect(adapter.getContextWindow('gpt-6.2-sol[ultra]')).toBe(1_000_000)
    ;(freshApi.models as ModelsMock).mockResolvedValue([{ model_name: 'gpt-6.2-sol[ultra]' }])
    await adapter.fetchAvailableModels()
    expect(adapter.getContextWindow('gpt-6.2-sol[ultra]')).toBe(0)
  })

  it('reports the learned window for the auto sentinel in the degraded list', async () => {
    ;(api.models as ModelsMock).mockResolvedValue([
      { model_name: 'auto', context_window_tokens: 1_000_000 },
    ])
    const adapter = new AcpAdapter()
    await adapter.fetchAvailableModels()
    expect(adapter.getContextWindow('auto')).toBe(1_000_000)
    // The auto-only degraded fallback quotes the same learned figure instead of
    // hardcoding 200K (model_tokens.json has no 'auto' entry at all).
    ;(api.models as ModelsMock).mockRejectedValue(new Error('503'))
    localStorage.clear()
    const degraded = await adapter.fetchAvailableModels()
    expect(degraded).toHaveLength(1)
    expect(degraded[0].name).toBe('auto')
    expect(degraded[0].contextWindow).toBe(1_000_000)
  })
})
