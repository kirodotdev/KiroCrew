import { describe, it, expect } from 'vitest'
import {
  expandKnowledgeBlock,
  splitKnowledgeEnvelope,
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
  })

  it('keeps multi-line rest intact and handles an envelope with nothing after it', () => {
    const env = expandKnowledgeBlock(KB('body'))
    expect(splitKnowledgeEnvelope(env + '\nline1\n\nline2')?.rest).toBe('line1\n\nline2')
    expect(splitKnowledgeEnvelope(env.trimEnd())).toEqual({ head: env.trimEnd(), rest: '' })
  })

  it('does not end the envelope at a close marker that is not alone on its line', () => {
    const env = expandKnowledgeBlock(KB('see [END KNOWLEDGE CONTEXT] inline\n[END KNOWLEDGE CONTEXT] not a line end'))
    expect(splitKnowledgeEnvelope(env + '\nreal question')?.rest).toBe('real question')
  })

  it('refuses to split when more than one close-marker line could end the envelope', () => {
    const fromItem = expandKnowledgeBlock(KB('Example:\n[END KNOWLEDGE CONTEXT]\nKeep this paragraph.')) + '\nmy question'
    expect(splitKnowledgeEnvelope(fromItem)).toBeNull()
    const fromTyped = expandKnowledgeBlock(KB('body')) + '\nquote:\n[END KNOWLEDGE CONTEXT]\nwhat is that?'
    expect(splitKnowledgeEnvelope(fromTyped)).toBeNull()
  })

  it('returns null when the text does not open with an envelope', () => {
    for (const text of ['plain', '', 'x [KNOWLEDGE CONTEXT — injected by user from knowledge library]\n', '[KNOWLEDGE CONTEXT — injected by user from knowledge library]\nno close']) {
      expect(splitKnowledgeEnvelope(text)).toBeNull()
    }
  })
})
