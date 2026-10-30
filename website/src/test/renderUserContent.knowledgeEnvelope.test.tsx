/**
 * #18075: a row sent with knowledge comes back from history as its wire text,
 * which opens with the injected `[KNOWLEDGE CONTEXT ...]` block. The user
 * bubble removes exactly the block the send recorded (length + hash on
 * `meta.knowledge.envelope`) and never anything the user typed.
 */
import { describe, it, expect } from 'vitest'
import { render } from '@testing-library/react'
import { renderUserContent } from '../pages/chat/ChatPageMessageContent'
import { expandKnowledgeBlock, type KnowledgeBlock } from '../pages/chat/useKnowledgeFetch'
import { knowledgeEnvelopeEndInRow, knowledgeEnvelopeMark, stripKnowledgeEnvelope } from '../utils/knowledgeEnvelope'
import { buildOutgoingTurn } from '../chat-core/composer/outgoingTurn'
import { quoteBlock, stripQuoteBlock } from '../chat-core/composer/messageQuote'
import { findTokenRanges, formatToken, mergePreservedPastes, readStoredPaste, type PasteBlock } from '../utils/pasteTokens'

const block: KnowledgeBlock = {
  items: [{ id: 'k1', title: 'zzq-doc', source: 'zzq-src', match_type: 'kw', tokens: 5, summary: '', content: 'zzq-secret-kb-text' }],
  totalTokens: 5,
}
const mark = knowledgeEnvelopeMark(expandKnowledgeBlock(block))
const OPEN = '[KNOWLEDGE CONTEXT — injected by user from knowledge library]\n'

describe('knowledgeEnvelopeEndInRow', () => {
  it('finds the envelope after an exact quote prefix or at the unquoted head', () => {
    const quote = { role: 'assistant' as const, text: 'zzq quoted reply' }
    const wire = expandKnowledgeBlock(block) + '\nzzq typed body'
    const meta = { knowledge: { envelope: mark }, quote }
    expect(knowledgeEnvelopeEndInRow(wire, { knowledge: meta.knowledge })).toBe(mark.len)
    expect(knowledgeEnvelopeEndInRow(wire, meta)).toBe(mark.len)
    for (const gap of ['', '\n', '\n\n', '\n\n\n']) {
      const prefix = quoteBlock(quote) + gap
      expect(knowledgeEnvelopeEndInRow(prefix + wire, meta)).toBe(prefix.length + mark.len)
    }
    expect(knowledgeEnvelopeEndInRow(quoteBlock(quote) + '\n\n' + wire, undefined)).toBe(0)
    expect(knowledgeEnvelopeEndInRow(quoteBlock(quote) + '\n\n' + wire, { ...meta, quote: { ...quote, text: 'other' } })).toBe(0)
    expect(knowledgeEnvelopeEndInRow(quoteBlock(quote) + '\n\nzzq typed body', meta)).toBe(0)
  })
})

describe('stripKnowledgeEnvelope', () => {
  it('removes the recorded block from the wire text and keeps what was typed', () => {
    expect(stripKnowledgeEnvelope(expandKnowledgeBlock(block) + '\nzzq typed body', mark)).toBe('zzq typed body')
  })

  it('handles a block at the very end (backend trims trailing whitespace)', () => {
    expect(stripKnowledgeEnvelope(expandKnowledgeBlock(block).trimEnd(), mark)).toBe('')
  })

  it('keeps typed text after the block, even when it names the close marker', () => {
    const wire = expandKnowledgeBlock(block) + '\nbefore\n[END KNOWLEDGE CONTEXT]\nafter'
    expect(stripKnowledgeEnvelope(wire, mark)).toBe('before\n[END KNOWLEDGE CONTEXT]\nafter')
  })

  it('keeps any head that is not exactly the recorded block', () => {
    const cases = [
      'hi\n' + expandKnowledgeBlock(block),
      // Same title and text, the user's own words added inside the block.
      OPEN + '\n## zzq-doc (zzq-src)\nzzq-secret-kb-text\nThis is my literal example.\n\n[END KNOWLEDGE CONTEXT]\nrest',
      // A made-up source line.
      OPEN + '\n## zzq-doc (This is my literal example.)\nzzq-secret-kb-text\n\n[END KNOWLEDGE CONTEXT]\nrest',
      // The recorded length, but the next character does not end the block.
      expandKnowledgeBlock(block).trimEnd() + 'x\nrest',
    ]
    for (const c of cases) expect(stripKnowledgeEnvelope(c, mark)).toBe(c)
  })

  it('keeps a capped item followed by extra words (the length and hash cover the whole block)', () => {
    const long: KnowledgeBlock = { items: [{ ...block.items[0], content: 'x'.repeat(2000) }], totalTokens: 1 }
    const m = knowledgeEnvelopeMark(expandKnowledgeBlock(long))
    const typed = expandKnowledgeBlock(long).replace('x'.repeat(2000), 'x'.repeat(2000) + '\nThis is my literal example.') + '\nrest'
    expect(stripKnowledgeEnvelope(typed, m)).toBe(typed)
    expect(stripKnowledgeEnvelope(expandKnowledgeBlock(long) + '\nrest', m)).toBe('rest')
  })

  it('returns the text unchanged for a missing or malformed mark (older rows)', () => {
    const wire = expandKnowledgeBlock(block) + '\nzzq typed body'
    for (const bad of [undefined, null, {}, { len: mark.len }, { hash: mark.hash }, { len: 'x', hash: mark.hash }, { len: -1, hash: mark.hash }, { len: 1e9, hash: mark.hash }]) {
      expect(stripKnowledgeEnvelope(wire, bad)).toBe(wire)
    }
  })
})

describe('renderUserContent with meta.knowledge', () => {
  it('history row: draws the typed body, never the block', () => {
    const turn = buildOutgoingTurn({ text: 'zzq typed body', knowledge: block }, 'send')
    expect(turn.wire.startsWith('[KNOWLEDGE CONTEXT')).toBe(true)
    const { container } = render(<>{renderUserContent({ content: turn.wire, meta: { ...turn.meta } })}</>)
    expect(container).toHaveTextContent('zzq typed body')
    expect(container.textContent).not.toContain('KNOWLEDGE CONTEXT')
    expect(container.textContent).not.toContain('zzq-secret-kb-text')
  })

  it('optimistic bubble: a typed block example stays visible beside the chip', () => {
    const typed = OPEN + 'zzq example\n[END KNOWLEDGE CONTEXT]\nzzq question'
    const turn = buildOutgoingTurn({ text: typed, knowledge: block }, 'send')
    const { container } = render(<>{renderUserContent({ content: turn.bubble, meta: { ...turn.meta } })}</>)
    expect(container).toHaveTextContent('zzq example')
    expect(container).toHaveTextContent('zzq question')
  })

  it('older row without a mark keeps today\'s rendering', () => {
    const wire = expandKnowledgeBlock(block) + '\nzzq typed body'
    const legacy = { items: 1, tokens: 5, titles: ['zzq-doc'], content: [{ title: 'zzq-doc', text: 'zzq-secret-kb-text' }] }
    const { container } = render(<>{renderUserContent({ content: wire, meta: { knowledge: legacy } })}</>)
    expect(container.textContent).toContain('KNOWLEDGE CONTEXT')
  })

  it('quote + knowledge: the bubble drops the quote block, then the block under it', () => {
    const quote = { role: 'assistant' as const, text: 'zzq quoted reply' }
    const turn = buildOutgoingTurn({ text: 'zzq typed body', knowledge: block, quote }, 'send')
    // UserMessage strips the carried quote block before it renders the body.
    const body = stripQuoteBlock(turn.wire, quote)
    const { container } = render(<>{renderUserContent({ content: body, meta: { ...turn.meta } })}</>)
    expect(container).toHaveTextContent('zzq typed body')
    expect(container.textContent).not.toContain('KNOWLEDGE CONTEXT')
  })

  it.each([
    { route: 'sendId carry-forward', quoted: false },
    { route: 'self-contained fallback', quoted: false },
    { route: 'sendId carry-forward', quoted: true },
    { route: 'self-contained fallback', quoted: true },
  ])('shared knowledge/paste excerpt after side-table eviction: $route (quoted=$quoted)', ({ route, quoted }) => {
    const excerpt = 'zzq-shared-first\nzzq-shared-second\nzzq-shared-third'
    const paste: PasteBlock = { id: 'p1', seq: 1, content: excerpt, lines: 3 }
    const knowledge: KnowledgeBlock = { ...block, items: [{ ...block.items[0], content: `Context:\n${excerpt}\nEnd.` }] }
    const quote = quoted ? { role: 'assistant' as const, text: 'zzq quoted reply' } : undefined
    const turn = buildOutgoingTurn({ text: 'zzq typed body ' + formatToken(paste), pastes: [paste], knowledge, quote }, 'send')
    localStorage.clear()
    expect(readStoredPaste(turn.wire.trimEnd())).toBeNull()
    const existing = [{ role: 'user', content: turn.bubble, meta: { ...turn.meta, sendId: 's1' } }]
    const incoming = [{ role: 'user', content: turn.wire.trimEnd(), meta: { knowledge: turn.meta.knowledge, quote: turn.meta.quote, sendId: 's1' } }]
    const [row] = route === 'sendId carry-forward'
      ? mergePreservedPastes(existing, incoming)
      : mergePreservedPastes([], [{ ...incoming[0], meta: { ...incoming[0].meta, pastes: turn.meta.pastes } }])
    const body = quote ? stripQuoteBlock(row.content, quote) : row.content
    const pastes = row.meta.pastes as PasteBlock[]
    const ranges = findTokenRanges(body, pastes)
    expect(ranges).toHaveLength(1)
    expect(ranges[0].start).toBeGreaterThanOrEqual(turn.meta.knowledge!.envelope.len)
    const { container, getAllByRole } = render(<>{renderUserContent({ content: body, meta: row.meta })}</>)
    expect(container.textContent).not.toContain('KNOWLEDGE CONTEXT')
    expect(container.textContent).not.toContain('zzq-shared-first')
    expect(container).toHaveTextContent('zzq typed body')
    expect(getAllByRole('button', { name: 'Expand pasted 3 lines' })).toHaveLength(1)
  })

  it('reconcile after side-table eviction: paste chip comes back, block stays hidden', () => {
    const paste: PasteBlock = { id: 'p1', seq: 1, content: 'zzq-paste-line\n'.repeat(40), lines: 40 }
    const turn = buildOutgoingTurn({ text: 'look ' + formatToken(paste), pastes: [paste], knowledge: block }, 'send')
    // History re-serves the wire text with the persisted meta (no optimistic
    // bubble, no side-table entry).
    const [row] = mergePreservedPastes([], [{ role: 'user', content: turn.wire.trimEnd(), meta: { ...turn.meta } as Record<string, unknown> }])
    const { container } = render(<>{renderUserContent({ content: row.content, meta: row.meta })}</>)
    expect(container.textContent).not.toContain('KNOWLEDGE CONTEXT')
    expect(container).toHaveTextContent('look')
  })
})
