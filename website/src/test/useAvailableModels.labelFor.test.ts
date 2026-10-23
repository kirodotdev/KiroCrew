import { describe, it, expect } from 'vitest'

import { labelFor } from '../hooks/useAvailableModels'
import type { ModelInfo } from '../providers/types'

describe('labelFor', () => {
  const models: ModelInfo[] = [
    { name: 'auto', description: '' },
    { name: 'qfmodel', label: 'Qwen3.8-Flash', description: '0.00x Credit' },
    { name: 'claude-opus-4.8', description: 'Opus' },
  ]

  it('returns the backend label for an id that has one', () => {
    expect(labelFor(models, 'qfmodel')).toBe('Qwen3.8-Flash')
  })

  it('returns the id unchanged when the row has no label', () => {
    expect(labelFor(models, 'claude-opus-4.8')).toBe('claude-opus-4.8')
  })

  it('returns the id when the list does not advertise it', () => {
    expect(labelFor(models, 'retired-model')).toBe('retired-model')
  })

  it('returns the id when the list is empty', () => {
    expect(labelFor([], 'qfmodel')).toBe('qfmodel')
  })

  it('treats an empty label as absent', () => {
    expect(labelFor([{ name: 'qfmodel', label: '', description: '' }], 'qfmodel')).toBe('qfmodel')
  })

  it('returns an empty id as-is, so callers that test truthiness still see no model', () => {
    expect(labelFor(models, '')).toBe('')
  })
})
