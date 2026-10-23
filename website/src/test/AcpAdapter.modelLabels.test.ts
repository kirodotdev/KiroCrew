import { describe, it, expect, vi, beforeEach } from 'vitest'
import type { Mock } from 'vitest'

// Mock the API client so fetchAvailableModels reads our canned /api/models.
vi.mock('../api/client', () => ({
  api: {
    models: vi.fn(),
  },
}))

import { api } from '../api/client'
import { AcpAdapter, clearCachedModels } from '../providers/adapters/acp'
import { getModelLabel } from '../lib/modelLabels'

/** The /api/models rows these fixtures feed the adapter. The adapter's own
 *  RawModel is module-private, so mirror only the fields set here. */
type ModelRow = {
  model_name: string
  display_name?: string
  description?: string
  advertised_models_exclusive?: boolean
}

/** `api.models` as replaced by vi.mock above. */
type ModelsMock = Mock<() => Promise<ModelRow[] | { error: string }>>

describe('AcpAdapter.fetchAvailableModels display labels', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    localStorage.clear()
    clearCachedModels()
  })

  it('registers the display_name of a row as the label for its id', async () => {
    ;(api.models as ModelsMock).mockResolvedValue([
      { model_name: 'auto', description: '' },
      { model_name: 'qfmodel', display_name: 'Qwen3.8-Flash', description: '', advertised_models_exclusive: true },
    ])
    const models = await new AcpAdapter().fetchAvailableModels()
    expect(getModelLabel('qfmodel')).toBe('Qwen3.8-Flash')
    expect(models.find(m => m.name === 'qfmodel')?.label).toBe('Qwen3.8-Flash')
    expect(getModelLabel('auto')).toBeUndefined()
  })

  it('does not relabel a non-exclusive catalog row that carries provenance metadata', async () => {
    ;(api.models as ModelsMock).mockResolvedValue([
      { model_name: 'kiro-model', display_name: 'Kiro Display Name', description: '' },
    ])
    const models = await new AcpAdapter().fetchAvailableModels()
    expect(getModelLabel('kiro-model')).toBeUndefined()
    expect(models.find(m => m.name === 'kiro-model')?.label).toBeUndefined()
  })

  it('clears an earlier label when a later list row for that id carries none', async () => {
    ;(api.models as ModelsMock)
      .mockResolvedValueOnce([{ model_name: 'qfmodel', display_name: 'Qwen3.8-Flash', description: '', advertised_models_exclusive: true }])
      .mockResolvedValueOnce([{ model_name: 'qfmodel', description: '' }])
    const adapter = new AcpAdapter()
    await adapter.fetchAvailableModels()
    expect(getModelLabel('qfmodel')).toBe('Qwen3.8-Flash')
    const models = await adapter.fetchAvailableModels()
    expect(getModelLabel('qfmodel')).toBeUndefined()
    expect(models[0].label).toBeUndefined()
  })

  it('clearCachedModels drops the learned labels', async () => {
    ;(api.models as ModelsMock).mockResolvedValue([
      { model_name: 'qfmodel', display_name: 'Qwen3.8-Flash', description: '', advertised_models_exclusive: true },
    ])
    await new AcpAdapter().fetchAvailableModels()
    expect(getModelLabel('qfmodel')).toBe('Qwen3.8-Flash')
    clearCachedModels()
    expect(getModelLabel('qfmodel')).toBeUndefined()
  })

  it('seeds labels from the persisted list when the adapter module loads', async () => {
    ;(api.models as ModelsMock).mockResolvedValue([
      { model_name: 'qfmodel', display_name: 'Qwen3.8-Flash', description: '', advertised_models_exclusive: true },
    ])
    await new AcpAdapter().fetchAvailableModels()
    // A fresh module registry has no labels until acp.ts loads and reads the cache.
    vi.resetModules()
    const labels = await import('../lib/modelLabels')
    expect(labels.getModelLabel('qfmodel')).toBeUndefined()
    await import('../providers/adapters/acp')
    expect(labels.getModelLabel('qfmodel')).toBe('Qwen3.8-Flash')
  })
})
