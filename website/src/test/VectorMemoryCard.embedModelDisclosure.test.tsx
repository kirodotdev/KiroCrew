import { describe, it, expect } from 'vitest'
import { embedModelDisclosure } from '../pages/overview/VectorMemoryCard'

// A custom model's id is either operator-chosen (memory.embed_model_id) or
// derived as 'custom:<file>:<size>'. Neither reads well through
// formatEmbedModel, so the disclosure labels a custom model by FILENAME and puts
// the full path in the tooltip. The default path must be untouched.
describe('embedModelDisclosure', () => {
  it('labels the bundled model by its formatted id', () => {
    const d = embedModelDisclosure({ model_id: 'qwen3-embedding:0.6b', model_dim: 1024 })
    expect(d).not.toBeNull()
    expect(d!.label).toContain('Qwen3-Embedding-0.6B')
    expect(d!.title).not.toContain('—')
  })

  it('is byte-identical when model_source is absent (pre-change payload)', () => {
    const before = embedModelDisclosure({ model_id: 'qwen3-embedding:0.6b', model_dim: 1024 })
    const after = embedModelDisclosure({
      model_id: 'qwen3-embedding:0.6b',
      model_dim: 1024,
      model_source: 'default',
      model_path: '',
    })
    expect(after).toEqual(before)
  })

  it('labels a custom model by filename, not by its derived id', () => {
    const d = embedModelDisclosure({
      model_id: 'custom:bge-m3-q8_0.gguf:610000000',
      model_dim: 1024,
      model_source: 'custom',
      model_path: '/home/me/models/bge-m3-q8_0.gguf',
    })
    expect(d!.label).toContain('bge-m3-q8_0.gguf')
    expect(d!.label).not.toContain('Custom')
  })

  it('puts the full path in the tooltip so a misconfiguration is diagnosable', () => {
    const d = embedModelDisclosure({
      model_id: 'bge:local',
      model_dim: 1024,
      model_source: 'custom',
      model_path: '/home/me/models/bge-m3-q8_0.gguf',
    })
    expect(d!.title).toContain('/home/me/models/bge-m3-q8_0.gguf')
  })

  it('handles a Windows path separator', () => {
    const d = embedModelDisclosure({
      model_id: 'bge:local',
      model_dim: 1024,
      model_source: 'custom',
      model_path: 'C:\\models\\bge-m3.gguf',
    })
    expect(d!.label).toContain('bge-m3.gguf')
  })

  it('falls back to the formatted id when custom but no path is reported', () => {
    const d = embedModelDisclosure({
      model_id: 'bge:local',
      model_dim: 1024,
      model_source: 'custom',
      model_path: '',
    })
    expect(d!.label).toContain('Bge-LOCAL')
  })

  it('omits the disclosure entirely when nothing is known', () => {
    expect(embedModelDisclosure(null)).toBeNull()
    expect(embedModelDisclosure({})).toBeNull()
  })
})
