/** What an earlier page load persisted is read back as untrusted data: each surface keeps the
 *  fields its own restore takes and refuses only a value with nothing to put back. */
import { describe, it, expect } from 'vitest'
import { parsePaneSent } from '../components/useChatPaneSend'
import { parseSentTurn } from '../pages/chat/page/composerSend'
import { prependQuote, readMessageQuote, QUOTE_TEXT_MAX } from '../chat-core/composer/messageQuote'

const paste = { id: 'p1', seq: 1, lines: 4, content: 'a\nb\nc\nd' }
const quote = { role: 'assistant' as const, text: 'quoted', mid: 'm1' }
const withQuote = (text: string) => prependQuote(text, quote)

describe('parseSentTurn', () => {
  it('keeps every field the page restore takes, with the quote carried in the text', () => {
    const knowledge = [{ id: 'k1', title: 'Doc', source: null, match_type: 'exact', tokens: 3, summary: 's', content: 'c' }]
    expect(parseSentTurn({
      inlined: 'hello', pastes: [paste], sessionRefs: [{ key: 'chat-1', title: 'Other' }], quote,
      slotTokens: { '@a': ['/x/a.txt'] }, knowledge,
    })).toEqual({
      inlined: withQuote('hello'), pastes: [paste], sessionRefs: [{ key: 'chat-1', title: 'Other', messages: undefined }], quote: null,
      slotTokens: { '@a': ['/x/a.txt'] }, knowledge,
    })
  })

  it('drops malformed members rather than the whole send', () => {
    expect(parseSentTurn({ inlined: 'hi', pastes: [{ id: 1 }], sessionRefs: 'x', quote: { role: 'bot' }, knowledge: [{ id: 'k' }] }))
      .toEqual({ inlined: 'hi', pastes: [], sessionRefs: [], quote: null })
  })

  it('accepts a quote-only send and a session-ref-only send, which isEmptyTurn lets through', () => {
    expect(parseSentTurn({ inlined: '', quote })).toEqual({ inlined: withQuote(''), pastes: [], sessionRefs: [], quote: null })
    expect(parseSentTurn({ inlined: '', sessionRefs: [{ key: 'chat-2', title: 'Ref' }] }))
      .toEqual({ inlined: '', pastes: [], sessionRefs: [{ key: 'chat-2', title: 'Ref', messages: undefined }], quote: null })
  })

  it('reads the quote through the transcript reader, which caps its text', () => {
    const long = { role: 'user', text: 'x'.repeat(QUOTE_TEXT_MAX * 4) }
    expect(parseSentTurn({ inlined: 'hi', quote: long })?.inlined).toBe(prependQuote('hi', readMessageQuote({ quote: long })!))
  })

  it('refuses a value with nothing to put back', () => {
    expect(parseSentTurn({ inlined: '  ' })).toBeNull()
    expect(parseSentTurn({ inlined: '', quote: { role: 'user', text: '  ' } })).toBeNull()
    expect(parseSentTurn('hello')).toBeNull()
    expect(parseSentTurn(null)).toBeNull()
  })
})

describe('parsePaneSent', () => {
  it('keeps text, files and pastes, with the quote carried in the text', () => {
    expect(parsePaneSent({ text: 'see', files: ['/a.png', 3], pastes: [paste], quote }))
      .toEqual({ text: withQuote('see'), files: ['/a.png'], pastes: [paste], quote: null })
  })

  it('accepts a files-only or quote-only send, and refuses one with nothing at all', () => {
    expect(parsePaneSent({ text: '', files: ['/a.png'] })).toEqual({ text: '', files: ['/a.png'], pastes: [], quote: null })
    expect(parsePaneSent({ text: '', files: [], quote })).toEqual({ text: withQuote(''), files: [], pastes: [], quote: null })
    expect(parsePaneSent({ text: '', files: [] })).toBeNull()
    expect(parsePaneSent({ files: ['/a.png'] })).toBeNull()
  })
})
