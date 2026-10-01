import { describe, it, expect } from 'vitest'

import { buildOutgoingTurn, isEmptyTurn, type OutgoingTurnInput, type OutgoingTurnMode } from './outgoingTurn'
import { quoteBlock, type MessageQuote } from './messageQuote'
import type { PasteBlock } from '../../utils/pasteTokens'
import type { KnowledgeBlock } from '../../pages/chat/useKnowledgeFetch'
import { formatSessionRefLink } from '../../utils/sessionRefs'

const P1: PasteBlock = { id: 'p1', seq: 1, lines: 3, content: 'one\ntwo\nthree' }
const P2: PasteBlock = { id: 'p2', seq: 2, lines: 3, content: 'x\ny\nz' }
const T1 = '[ Paste #1 · 3 lines ]'
const T2 = '[ Paste #2 · 3 lines ]'
const QUOTE: MessageQuote = { role: 'assistant', text: 'the earlier answer' }
const Q = quoteBlock(QUOTE)
const REF = { key: 'dashboard:s-1', title: 'Old chat', messages: 2 }
const LINK = formatSessionRefLink(REF)
const KB = { totalTokens: 7, items: [{ id: 'k1', title: 'Doc', content: 'kb text', tokens: 7 }] } as unknown as KnowledgeBlock
const KB_TEXT = '[KNOWLEDGE CONTEXT — injected by user from knowledge library]\n\n## Doc\nkb text\n\n[END KNOWLEDGE CONTEXT]\n'

interface Row {
  name: string
  mode: OutgoingTurnMode
  input: OutgoingTurnInput
  wire: string
  bubble: string
  meta: Record<string, unknown>
  /** The blocks the turn expanded (what a refused submit hands back). */
  pastes?: PasteBlock[]
  stored?: boolean
  typedOnly?: boolean
  /** The typed text with only its files inlined (the main chat's failed-send hand-back). */
  inlined?: string
}

/* One row per rule of the order. Each `wire`/`bubble` is the exact text a host
   puts on the POST and in the optimistic bubble; `meta` is compared with its
   key order, which is the order the fields reach the request body. */
const ROWS: Row[] = [
  { name: 'plain text is sent as typed, trimmed', mode: 'send', input: { text: '  hello  ' }, wire: 'hello', bubble: 'hello', meta: {}, typedOnly: true },
  { name: 'a staged file becomes a marker on the wire only', mode: 'send', input: { text: 'read @a.txt', files: ['/r/a.txt'] }, wire: 'read [attached_file 1] /r/a.txt', bubble: 'read @a.txt', meta: { files: ['/r/a.txt'] }, typedOnly: true, inlined: 'read [attached_file 1] /r/a.txt' },
  { name: 'an image opens both texts as markdown and is the turn\'s attachment', mode: 'send', input: { text: 'look', files: ['/t/i.png'] }, wire: '![image](/t/i.png)\n\nlook', bubble: '![image](/t/i.png)\n\nlook', meta: { images: ['/t/i.png'] }, typedOnly: true },
  { name: 'an image-only send is its markdown line, never an empty message', mode: 'send', input: { text: '', files: ['/t/only.png'] }, wire: '![image](/t/only.png)', bubble: '![image](/t/only.png)', meta: { images: ['/t/only.png'] }, typedOnly: true },
  { name: 'images ride meta.images and stay off meta.files, whose order the markers index', mode: 'send', input: { text: 'both', files: ['/t/a.png', '/t/n.txt', '/t/b c.pdf'] }, wire: '![image](/t/a.png)\n\nboth\n[attached_file 1] /t/n.txt\n[attached_file 2] /t/b c.pdf', bubble: '![image](/t/a.png)\n\nboth', meta: { images: ['/t/a.png'], files: ['/t/n.txt', '/t/b c.pdf'] }, typedOnly: true },
  { name: 'meta.dirs[N-1] is marker N, resolved against the project', mode: 'send', input: { text: 'see @src/ and @/abs/d/ and @src/', project: '/p' }, wire: 'see [attached_dir 1] /p/src and [attached_dir 2] /abs/d and [attached_dir 1] /p/src', bubble: 'see @src/ and @/abs/d/ and @src/', meta: { dirs: ['/p/src', '/abs/d'] }, typedOnly: true, inlined: 'see @src/ and @/abs/d/ and @src/' },
  { name: 'steer never emits a folder marker', mode: 'steer', input: { text: 'see @src/', project: '/p' }, wire: 'see @src/', bubble: 'see @src/', meta: {}, typedOnly: true },
  { name: 'session links are appended to both texts', mode: 'send', input: { text: 'about this', sessionRefs: [REF] }, wire: `about this\n\n${LINK}`, bubble: `about this\n\n${LINK}`, meta: {}, typedOnly: false, inlined: 'about this' },
  { name: 'steer carries no session links', mode: 'steer', input: { text: 'now', sessionRefs: [REF] }, wire: 'now', bubble: 'now', meta: {}, typedOnly: true },
  { name: 'a paste expands on the wire; the bubble keeps the token and the block', mode: 'send', input: { text: `a ${T1} b`, pastes: [P1] }, wire: 'a one\ntwo\nthree b', bubble: `a ${T1} b`, meta: { pastes: [P1] }, pastes: [P1], stored: true, typedOnly: false, inlined: `a ${T1} b` },
  { name: 'a block whose token left the text is pruned from both texts', mode: 'send', input: { text: `only ${T2}`, pastes: [P1, P2] }, wire: 'only x\ny\nz', bubble: `only ${T2}`, meta: { pastes: [P2] }, pastes: [P2], stored: true, typedOnly: false },
  // A block whose token left the text expands nowhere -- not even inside a
  // staged FILE PATH that happens to spell its token, where expanding it would
  // turn the `[attached_file N]` marker into the paste's content.
  { name: 'a file path spelling a staged token whose token left the text stays a verbatim marker', mode: 'send', input: { text: 'see attached', files: [`/tmp/${T1}.txt`], pastes: [P1] }, wire: `see attached\n[attached_file 1] /tmp/${T1}.txt`, bubble: 'see attached', meta: { files: [`/tmp/${T1}.txt`] }, pastes: [], typedOnly: true },
  { name: 'a project path spelling a staged token whose token left the text stays a verbatim folder marker', mode: 'send', input: { text: 'look in @src/', project: `/srv/${T1}`, pastes: [P1] }, wire: `look in [attached_dir 1] /srv/${T1}/src`, bubble: 'look in @src/', meta: { dirs: [`/srv/${T1}/src`] }, pastes: [], typedOnly: true },
  { name: 'a token with no staged block stays literal', mode: 'send', input: { text: `keep ${T2}`, pastes: [P1] }, wire: `keep ${T2}`, bubble: `keep ${T2}`, meta: {}, pastes: [], typedOnly: true },
  { name: 'steer expands a paste in both texts and stores nothing', mode: 'steer', input: { text: `a ${T1}`, pastes: [P1] }, wire: 'a one\ntwo\nthree', bubble: 'a one\ntwo\nthree', meta: {}, pastes: [P1], typedOnly: false },
  { name: 'pasted content is never read as a token of this turn', mode: 'send', input: { text: T1, pastes: [{ ...P1, content: `@src/ @a.txt ${T2}` }, P2], files: ['/r/a.txt'], project: '/p' }, wire: `@src/ @a.txt ${T2}\n[attached_file 1] /r/a.txt`, bubble: T1, meta: { files: ['/r/a.txt'], pastes: [{ ...P1, content: `@src/ @a.txt ${T2}` }] }, pastes: [{ ...P1, content: `@src/ @a.txt ${T2}` }], stored: true, typedOnly: false },
  { name: 'knowledge opens the wire and rides meta', mode: 'send', input: { text: 'q', knowledge: KB }, wire: `${KB_TEXT}\nq`, bubble: 'q', meta: { knowledge: { items: 1, tokens: 7, titles: ['Doc'], content: [{ title: 'Doc', text: 'kb text' }] } }, typedOnly: false, inlined: 'q' },
  { name: 'steer carries no knowledge', mode: 'steer', input: { text: 'q', knowledge: KB }, wire: 'q', bubble: 'q', meta: {}, typedOnly: true },
  { name: 'the quote opens both texts, after every token pass', mode: 'send', input: { text: `@src/ ${T1}`, pastes: [P1], quote: { ...QUOTE, text: `has ${T1} and @src/` }, project: '/p' }, wire: `${quoteBlock({ ...QUOTE, text: `has ${T1} and @src/` })}\n\n[attached_dir 1] /p/src one\ntwo\nthree`, bubble: `${quoteBlock({ ...QUOTE, text: `has ${T1} and @src/` })}\n\n@src/ ${T1}`, meta: { dirs: ['/p/src'], pastes: [P1], quote: { ...QUOTE, text: `has ${T1} and @src/` } }, pastes: [P1], stored: true, typedOnly: false, inlined: `@src/ ${T1}` },
  { name: 'the quote opens the wire before knowledge', mode: 'send', input: { text: 'q', knowledge: KB, quote: QUOTE }, wire: `${Q}\n\n${KB_TEXT}\nq`, bubble: `${Q}\n\nq`, meta: { quote: QUOTE, knowledge: { items: 1, tokens: 7, titles: ['Doc'], content: [{ title: 'Doc', text: 'kb text' }] } }, typedOnly: false },
  { name: 'a quote alone is a whole message', mode: 'send', input: { text: '', quote: QUOTE }, wire: Q, bubble: Q, meta: { quote: QUOTE }, typedOnly: true },
  { name: 'steer quote opens the one text', mode: 'steer', input: { text: 'go', quote: QUOTE, files: ['/r/a.txt'] }, wire: `${Q}\n\ngo\n[attached_file 1] /r/a.txt`, bubble: `${Q}\n\ngo\n[attached_file 1] /r/a.txt`, meta: { files: ['/r/a.txt'], quote: QUOTE }, typedOnly: true },
  { name: 'meta keys keep wire order: files, dirs, pastes, quote, knowledge', mode: 'send', input: { text: `@a.txt @src/ ${T1}`, files: ['/r/a.txt'], pastes: [P1], quote: QUOTE, knowledge: KB, project: '/p' }, wire: `${Q}\n\n${KB_TEXT}\n[attached_file 1] /r/a.txt [attached_dir 1] /p/src one\ntwo\nthree`, bubble: `${Q}\n\n@a.txt @src/ ${T1}`, meta: { files: ['/r/a.txt'], dirs: ['/p/src'], pastes: [P1], quote: QUOTE, knowledge: { items: 1, tokens: 7, titles: ['Doc'], content: [{ title: 'Doc', text: 'kb text' }] } }, pastes: [P1], stored: true, typedOnly: false, inlined: `[attached_file 1] /r/a.txt @src/ ${T1}` },
]

describe('buildOutgoingTurn', () => {
  it.each(ROWS)('$mode: $name', (row) => {
    const turn = buildOutgoingTurn(row.input, row.mode)
    expect(turn.wire).toBe(row.wire)
    expect(turn.bubble).toBe(row.bubble)
    expect(JSON.stringify(turn.meta)).toBe(JSON.stringify(row.meta))
    expect(turn.pastes).toEqual(row.pastes ?? [])
    if (row.stored) {
      expect(turn.storedPaste).toEqual({ expanded: turn.wire, display: turn.bubble, pastes: row.pastes, files: (row.meta.files as string[] | undefined) ?? [] })
    } else {
      expect(turn.storedPaste).toBeUndefined()
    }
    if (row.typedOnly !== undefined) expect(turn.typedOnly).toBe(row.typedOnly)
    if (row.inlined !== undefined) expect(turn.inlined).toBe(row.inlined)
  })

  it('caps each knowledge item at 2,000 characters on meta, never on the wire', () => {
    const long = { totalTokens: 1, items: [{ id: 'k', title: 'L', content: 'z'.repeat(2500), tokens: 1 }] } as unknown as KnowledgeBlock
    const turn = buildOutgoingTurn({ text: 'q', knowledge: long }, 'send')
    expect(turn.meta.knowledge?.content[0].text).toHaveLength(2000)
    expect(turn.wire).toContain('z'.repeat(2500))
  })
})

describe('isEmptyTurn', () => {
  it.each([
    [{ text: '' }, true],
    [{ text: '  \n ' }, true],
    [{ text: '', knowledge: KB, project: '/p', pastes: [P1] }, true],
    [{ text: 'x' }, false],
    [{ text: '', quote: QUOTE }, false],
    [{ text: '', files: ['/r/a.txt'] }, false],
    [{ text: '', sessionRefs: [REF] }, false],
  ] as Array<[OutgoingTurnInput, boolean]>)('%j is empty: %s', (input, empty) => {
    expect(isEmptyTurn(input)).toBe(empty)
  })
})
