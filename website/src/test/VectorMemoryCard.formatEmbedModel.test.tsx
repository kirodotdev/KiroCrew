import { describe, it, expect } from 'vitest'
import { formatEmbedModel } from '../pages/overview/VectorMemoryCard'

// The Memory tab discloses which embedding model runs locally. formatEmbedModel
// turns the backend's raw model_id into a friendly display name, and must
// degrade gracefully so a future model swap still discloses *something*.
describe('formatEmbedModel', () => {
  it('formats the shipped Qwen3 model id into a friendly name', () => {
    expect(formatEmbedModel('qwen3-embedding:0.6b')).toBe('Qwen3-Embedding-0.6B')
  })

  it('title-cases each hyphenated segment and upper-cases the size tag', () => {
    expect(formatEmbedModel('all-minilm:l6-v2')).toBe('All-Minilm-L6-V2')
  })

  it('handles an id with no size tag', () => {
    expect(formatEmbedModel('bge-base')).toBe('Bge-Base')
  })

  it('returns empty string for missing/blank input (no disclosure line)', () => {
    expect(formatEmbedModel(undefined)).toBe('')
    expect(formatEmbedModel('')).toBe('')
    expect(formatEmbedModel('   ')).toBe('')
  })
})
