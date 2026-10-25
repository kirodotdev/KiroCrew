import { describe, it, expect, beforeEach } from 'vitest'

import { setModelLabel, getModelLabel, clearModelLabels } from '../lib/modelLabels'

describe('modelLabels', () => {
  beforeEach(() => {
    clearModelLabels()
  })

  it('returns the label recorded for an id', () => {
    setModelLabel('qfmodel', 'Qwen3.8-Flash')
    expect(getModelLabel('qfmodel')).toBe('Qwen3.8-Flash')
  })

  it('returns undefined for an id with no recorded label', () => {
    expect(getModelLabel('qfmodel')).toBeUndefined()
  })

  it('replaces an earlier label for the same id', () => {
    setModelLabel('qfmodel', 'Old name')
    setModelLabel('qfmodel', 'Qwen3.8-Flash')
    expect(getModelLabel('qfmodel')).toBe('Qwen3.8-Flash')
  })

  it('removes the label when set to undefined', () => {
    setModelLabel('qfmodel', 'Qwen3.8-Flash')
    setModelLabel('qfmodel', undefined)
    expect(getModelLabel('qfmodel')).toBeUndefined()
  })

  it('treats an empty label as absent', () => {
    setModelLabel('qfmodel', 'Qwen3.8-Flash')
    setModelLabel('qfmodel', '')
    expect(getModelLabel('qfmodel')).toBeUndefined()
  })

  it('ignores an empty id', () => {
    setModelLabel('', 'Nameless')
    expect(getModelLabel('')).toBeUndefined()
  })

  it('clearModelLabels forgets every label', () => {
    setModelLabel('qfmodel', 'Qwen3.8-Flash')
    setModelLabel('claude-opus-4.8', 'Opus')
    clearModelLabels()
    expect(getModelLabel('qfmodel')).toBeUndefined()
    expect(getModelLabel('claude-opus-4.8')).toBeUndefined()
  })
})
