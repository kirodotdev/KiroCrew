import { describe, it, expect } from 'vitest'
import { mergeSentTurns, type SentTurnRestore } from '../pages/chat/page/composerSend'
import { mergePaneSends, type PaneSentRestore } from './useChatPaneSend'
import { formatToken, type PasteBlock } from '../utils/pasteTokens'
import { prependQuote, type MessageQuote } from '../chat-core/composer/messageQuote'
import type { SessionRef } from '../utils/sessionRefs'
import type { KnowledgeResult } from '../pages/chat/useKnowledgeFetch'

const paste = (id: string, content: string): PasteBlock => ({ id, seq: 1, lines: 4, content })
const quoteA: MessageQuote = { role: 'assistant', text: 'first answer' }
const quoteB: MessageQuote = { role: 'assistant', text: 'second answer' }
const knowledge = (id: string): KnowledgeResult =>
  ({ id, title: id, source: null, match_type: 'fts', tokens: 1, summary: '', content: id }) as KnowledgeResult

describe('mergeSentTurns', () => {
  it('keeps both texts, renumbers a clashing paste, and unions the context', () => {
    const p1 = paste('p-1', 'one\ntwo\nthree\nfour')
    const p2 = paste('p-2', 'five\nsix\nseven\neight')
    const earlier: SentTurnRestore = {
      inlined: `first ${formatToken(p1)}`, pastes: [p1], sessionRefs: [{ key: 'chat-1' } as SessionRef],
      quote: quoteA, slotTokens: { 'a.txt': ['@a'] }, knowledge: [knowledge('k1')],
    }
    const later: SentTurnRestore = {
      inlined: `second ${formatToken(p2)}`, pastes: [p2], sessionRefs: [{ key: 'chat-2' } as SessionRef],
      quote: quoteB, slotTokens: { 'a.txt': ['@a2'] }, knowledge: [knowledge('k1'), knowledge('k2')],
    }

    const merged = mergeSentTurns(earlier, later)

    expect(merged.inlined).toContain('first')
    expect(merged.inlined).toContain(prependQuote('second', quoteB).split('\n')[0])
    expect(merged.pastes.map(b => b.id)).toEqual(['p-1', 'p-2'])
    expect(new Set(merged.pastes.map(b => b.seq)).size).toBe(2)
    expect(merged.inlined).toContain(formatToken(merged.pastes[1]))
    expect(merged.sessionRefs.map(r => r.key)).toEqual(['chat-1', 'chat-2'])
    expect(merged.quote).toBe(quoteA)
    expect(merged.slotTokens).toEqual({ 'a.txt': ['@a', '@a2'] })
    expect(merged.knowledge?.map(k => k.id)).toEqual(['k1', 'k2'])
  })

  it('carries the only quote across without folding it into text', () => {
    const merged = mergeSentTurns(
      { inlined: 'first', pastes: [], sessionRefs: [], quote: null },
      { inlined: 'second', pastes: [], sessionRefs: [], quote: quoteB },
    )
    expect(merged.quote).toBe(quoteB)
    expect(merged.inlined).not.toContain(quoteB.text)
    expect(merged.slotTokens).toBeUndefined()
  })
})

describe('mergePaneSends', () => {
  it('keeps both texts and files, and folds a second quote into its text', () => {
    const earlier: PaneSentRestore = { text: 'first', files: ['a.txt'], pastes: [], quote: quoteA }
    const later: PaneSentRestore = { text: 'second', files: ['a.txt', 'b.txt'], pastes: [], quote: quoteB }

    const merged = mergePaneSends(earlier, later)

    expect(merged.text).toContain('first')
    expect(merged.text).toContain('second')
    expect(merged.text).toContain(quoteB.text)
    expect(merged.files).toEqual(['a.txt', 'b.txt'])
    expect(merged.quote).toBe(quoteA)
  })
})
