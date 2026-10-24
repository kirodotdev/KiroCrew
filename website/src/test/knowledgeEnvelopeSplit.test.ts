import { describe, it, expect } from 'vitest'
import {
  expandKnowledgeBlock,
  splitKnowledgeEnvelope,
  stripKnowledgeEnvelope,
  type KnowledgeBlock,
} from '../pages/chat/useKnowledgeFetch'

const KB = (content: string): KnowledgeBlock => ({
  totalTokens: 3,
  items: [{ id: 'k1', title: 'Doc', source: null, match_type: 'x', tokens: 3, summary: '', content }],
})

describe('splitKnowledgeEnvelope', () => {
  it('splits what expandKnowledgeBlock produced from the text that follows it', () => {
    const wire = expandKnowledgeBlock(KB('body')) + '\n' + 'my question'
    const split = splitKnowledgeEnvelope(wire)
    expect(split?.rest).toBe('my question')
    expect(split!.head + split!.rest).toBe(wire)
    expect(stripKnowledgeEnvelope(wire)).toBe('my question')
  })

  it('keeps multi-line rest intact and handles an envelope with nothing after it', () => {
    const env = expandKnowledgeBlock(KB('body'))
    expect(stripKnowledgeEnvelope(env + '\nline1\n\nline2')).toBe('line1\n\nline2')
    expect(stripKnowledgeEnvelope(env.trimEnd())).toBe('')
  })

  it('does not end the envelope at a close marker that is not alone on its line', () => {
    const env = expandKnowledgeBlock(KB('see [END KNOWLEDGE CONTEXT] inline\n[END KNOWLEDGE CONTEXT] not a line end'))
    expect(stripKnowledgeEnvelope(env + '\nreal question')).toBe('real question')
  })

  it('refuses to split when more than one close-marker line could end the envelope', () => {
    const fromItem = expandKnowledgeBlock(KB('Example:\n[END KNOWLEDGE CONTEXT]\nKeep this paragraph.')) + '\nmy question'
    expect(splitKnowledgeEnvelope(fromItem)).toBeNull()
    expect(stripKnowledgeEnvelope(fromItem)).toBe(fromItem)
    const fromTyped = expandKnowledgeBlock(KB('body')) + '\nquote:\n[END KNOWLEDGE CONTEXT]\nwhat is that?'
    expect(splitKnowledgeEnvelope(fromTyped)).toBeNull()
  })

  it('returns null / the text unchanged when the text does not open with an envelope', () => {
    for (const text of ['plain', '', 'x [KNOWLEDGE CONTEXT — injected by user from knowledge library]\n', '[KNOWLEDGE CONTEXT — injected by user from knowledge library]\nno close']) {
      expect(splitKnowledgeEnvelope(text)).toBeNull()
      expect(stripKnowledgeEnvelope(text)).toBe(text)
    }
  })
})
