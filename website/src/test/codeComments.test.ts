/**
 * Line-anchored code-block commenting: the pure half. `codeFenceSpans`
 * locates commentable code blocks by running the SAME parser the renderer
 * uses (`parseBlocks`), `routeCodeComments` decides which anchored comments
 * belong to which block (the in-span + slice-equality discriminator that
 * keeps prose rendered-text offsets from colliding with code source
 * offsets), and `buildCodeCommentAnchor` turns a gutter line range into a
 * source-space anchor. These carry the whole correctness story — the page
 * and CodeBlock are thin glue over them.
 */
import { describe, it, expect } from 'vitest'
import { codeFenceSpans, routeCodeComments, buildCodeCommentAnchor } from '../components/codeComments'
import type { ArtifactComment, CommentAnchor } from '../types'

const DOC = [
  'Intro prose line.',        // line 1
  '',                          // 2
  '```java',                   // 3  ← fence opens
  'class Foo {',               // 4  (content line 1)
  '  int x;',                  // 5  (content line 2)
  '}',                         // 6  (content line 3)
  '```',                       // 7  ← fence closes
  '',                          // 8
  'More prose with `}` code.', // 9
  '```',                       // 10 ← second fence
  'plain text body',           // 11
  '```',                       // 12
].join('\n')

function comment(id: string, anchor: CommentAnchor | null, extra?: Partial<ArtifactComment>): ArtifactComment {
  return {
    id,
    origin: 'local',
    scope: 'private',
    author: 'helena',
    is_agent: false,
    body: 'q',
    anchor,
    thread_id: id,
    parent_id: null,
    status: 'open',
    sync_state: 'local',
    ...extra,
  } as ArtifactComment
}

describe('codeFenceSpans', () => {
  it('locates code blocks with 1-based content line + content offsets', () => {
    const spans = codeFenceSpans(DOC)
    expect(spans).toHaveLength(2)
    const [a, b] = spans
    expect(a.contentStartLine).toBe(4)
    expect(DOC.slice(a.contentStart, a.contentEnd)).toBe('class Foo {\n  int x;\n}')
    expect(b.contentStartLine).toBe(11)
    expect(DOC.slice(b.contentStart, b.contentEnd)).toBe('plain text body')
  })

  it('an unclosed fence runs to end of input', () => {
    const src = 'x\n```py\nprint(1)\nprint(2)'
    const [s] = codeFenceSpans(src)
    expect(s.contentStartLine).toBe(3)
    expect(src.slice(s.contentStart, s.contentEnd)).toBe('print(1)\nprint(2)')
  })

  it('an empty fence has an empty content span', () => {
    const src = '```\n```\n'
    const [s] = codeFenceSpans(src)
    expect(s.contentEnd).toBe(s.contentStart)
  })

  it('excludes blocks that do not render as CodeBlock: diff and mermaid', () => {
    const src = [
      '```diff',
      '+added line',
      '-removed line',
      '```',
      '```mermaid',
      'graph TD; A-->B',
      '```',
      '```txt',
      'kept',
      '```',
    ].join('\n')
    const spans = codeFenceSpans(src)
    expect(spans).toHaveLength(1)
    expect(src.slice(spans[0].contentStart, spans[0].contentEnd)).toBe('kept')
  })

  it('matches the assembler, not CommonMark: tilde and indented fences are prose', () => {
    expect(codeFenceSpans('~~~\nnot a block\n~~~\n')).toHaveLength(0)
    expect(codeFenceSpans('  ```\nnot a block\n  ```\n')).toHaveLength(0)
  })
})

describe('routeCodeComments', () => {
  const spans = codeFenceSpans(DOC)
  /** Raw-source prefix as buildCodeCommentAnchor stores it (up to 32 chars). */
  const pfx = (doc: string, start: number) => doc.slice(Math.max(0, start - 32), start)

  it('claims a source-space anchor inside a block and computes its content line', () => {
    const start = DOC.indexOf('  int x;')
    const c = comment('c1', { quote: '  int x;', prefix: pfx(DOC, start), start_offset: start, end_offset: start + 8 })
    const { byFence, claimedThreadIds } = routeCodeComments(DOC, spans, [c])
    expect(claimedThreadIds.has('c1')).toBe(true)
    expect(byFence.get(4)).toEqual([{ id: 'c1', line: 2, count: 1, unread: false }])
  })

  it('does not route a resolved thread (chip disappears, matching the prose overlay)', () => {
    const spans = codeFenceSpans(DOC)
    const start = DOC.indexOf('  int x;')
    const c = comment(
      'c1',
      { quote: '  int x;', prefix: pfx(DOC, start), start_offset: start, end_offset: start + 8 },
      { status: 'resolved' },
    )
    const { byFence, claimedThreadIds } = routeCodeComments(DOC, spans, [c])
    expect(claimedThreadIds.size).toBe(0)
    expect(byFence.size).toBe(0)
  })

  it('counts the whole thread and carries unread state', () => {
    const start = DOC.indexOf('class Foo {')
    const root = comment('r', { quote: 'class Foo {', prefix: pfx(DOC, start), start_offset: start, end_offset: start + 11 })
    const reply = comment('r2', null, { thread_id: 'r', parent_id: 'r' })
    const { byFence } = routeCodeComments(DOC, spans, [root, reply], new Set(['r']))
    expect(byFence.get(4)).toEqual([{ id: 'r', line: 1, count: 2, unread: true }])
  })

  it('rejects an anchor whose offsets do not slice to its quote and whose context matches nowhere', () => {
    const start = DOC.indexOf('  int x;')
    const c = comment('c1', { quote: 'something else', start_offset: start, end_offset: start + 8, prefix: 'nope', suffix: 'nope' })
    const { claimedThreadIds } = routeCodeComments(DOC, spans, [c])
    expect(claimedThreadIds.size).toBe(0)
  })

  it('re-routes a drifted anchor (edit above the fence shifted offsets) via its context signature', () => {
    // Simulate an edit that prepends a paragraph AFTER the comment was
    // created: the stored offsets point at the old location, but the quote +
    // raw-source prefix/suffix still identify the line inside the fence.
    const start = DOC.indexOf('  int x;')
    const a = buildCodeCommentAnchor(DOC, spans[0], 2, 2)!
    const shifted = 'A new intro paragraph.\n\n' + DOC
    const shiftedSpans = codeFenceSpans(shifted)
    const c = comment('d1', { quote: a.quote, prefix: a.prefix, suffix: a.suffix, start_offset: a.startOffset, end_offset: a.endOffset })
    const { byFence, claimedThreadIds } = routeCodeComments(shifted, shiftedSpans, [c])
    expect(claimedThreadIds.has('d1')).toBe(true)
    // Fence now opens two lines later; the annotation stays on content line 2.
    expect(byFence.get(6)).toEqual([{ id: 'd1', line: 2, count: 1, unread: false }])
    expect(start).toBeGreaterThan(0) // guard: the original offset existed
  })

  it('does not drift-claim a prose anchor whose quote text also appears in a fence', () => {
    // `}` appears both in the fence and in prose. A prose comment carries no
    // raw-source prefix/suffix (native selections store none), so the drift
    // tier must not claim it even though the quote occurs inside the fence.
    const proseAt = DOC.indexOf('`}` code') + 1
    const c = comment('p2', { quote: '}', start_offset: proseAt - 5, end_offset: proseAt - 4 })
    const { claimedThreadIds } = routeCodeComments(DOC, spans, [c])
    expect(claimedThreadIds.size).toBe(0)
  })

  it('accepts a server-capped quote at its stored offsets (startsWith, not equality)', () => {
    const start = DOC.indexOf('class Foo {')
    // Server capped the stored quote to a prefix; offsets stored as sent.
    const c = comment('cap1', { quote: 'class Foo {\n  int', prefix: pfx(DOC, start), start_offset: start, end_offset: start + 20 })
    const { byFence } = routeCodeComments(DOC, spans, [c])
    expect(byFence.get(4)).toEqual([{ id: 'cap1', line: 1, count: 1, unread: false }])
  })

  it('does not exact-claim a prose anchor whose offset coincidentally startsWith inside a fence', () => {
    // A prose comment's rendered-text offset can land inside a fence span at
    // a position where the raw source begins with its quote (rendered text
    // repeats source fragments). Without a prefix (native prose selections
    // store none), tier 1 must refuse it — the thread belongs to prose.
    const start = DOC.indexOf('  int x;')
    const c = comment('prose-coincide', { quote: '  int x;', start_offset: start, end_offset: start + 8 })
    const { claimedThreadIds } = routeCodeComments(DOC, spans, [c])
    expect(claimedThreadIds.size).toBe(0)
  })

  it('does not exact-claim when the stored prefix does not match the raw source', () => {
    // Rendered-text context (what a prose overlay would store) differs from
    // raw source context, so even a prefix-bearing prose anchor is refused.
    const start = DOC.indexOf('  int x;')
    const c = comment('prose-ctx', { quote: '  int x;', prefix: 'rendered text context here', start_offset: start, end_offset: start + 8 })
    const { claimedThreadIds } = routeCodeComments(DOC, spans, [c])
    expect(claimedThreadIds.size).toBe(0)
  })

  it('re-routes a drifted anchor whose quote the server capped (suffix sits past the cap)', () => {
    // >2000-char selection: the server stored quote.slice(0, 2000) but kept
    // the offsets and the suffix captured after the ORIGINAL selection end.
    const fullQuote = 'a'.repeat(1200) + '\n' + 'b'.repeat(1200)
    const doc = '# A sufficiently long heading for context\n\n```\n' + fullQuote + '\ntail line\n```\n'
    const start = doc.indexOf('a'.repeat(10))
    const c = comment('big1', {
      quote: fullQuote.slice(0, 2000),
      prefix: doc.slice(start - 32, start),
      suffix: doc.slice(start + fullQuote.length, start + fullQuote.length + 32),
      start_offset: start,
      end_offset: start + fullQuote.length,
    })
    const shifted = 'Intro paragraph.\n\n' + doc
    const { byFence, claimedThreadIds } = routeCodeComments(shifted, codeFenceSpans(shifted), [c])
    expect(claimedThreadIds.has('big1')).toBe(true)
    expect(byFence.get(6)).toEqual([{ id: 'big1', line: 1, count: 1, unread: false }])
  })

  it('re-routes a drifted anchor with no suffix (selection reached EOF in an unclosed fence)', () => {
    // The backend nulls empty anchor strings, so an EOF selection in an
    // unclosed fence arrives with suffix undefined; the prefix alone gates.
    const doc = 'Intro paragraph long enough for prefix context.\n\n```py\nprint(1)\nlast line'
    const start = doc.indexOf('last line')
    const c = comment('eof1', {
      quote: 'last line',
      prefix: doc.slice(start - 32, start),
      start_offset: start,
      end_offset: start + 'last line'.length,
    })
    const shifted = 'A new paragraph above.\n\n' + doc
    const { byFence, claimedThreadIds } = routeCodeComments(shifted, codeFenceSpans(shifted), [c])
    expect(claimedThreadIds.has('eof1')).toBe(true)
    expect(byFence.get(6)).toEqual([{ id: 'eof1', line: 2, count: 1, unread: false }])
  })

  it('rejects a prose comment whose rendered-text offsets fall outside every block', () => {
    // A prose anchor near the top of the doc, where rendered offsets coincide
    // with source offsets — slice equality holds but it is in no code span.
    const c = comment('p1', { quote: 'Intro prose', start_offset: 0, end_offset: 11 })
    const { claimedThreadIds } = routeCodeComments(DOC, spans, [c])
    expect(claimedThreadIds.size).toBe(0)
  })

  it('ignores replies, anchor-less comments, and offset-less anchors', () => {
    const start = DOC.indexOf('  int x;')
    const reply = comment('c2', { quote: '  int x;', start_offset: start, end_offset: start + 8 }, { parent_id: 'c1', thread_id: 'c1' })
    const bare = comment('c3', null)
    const noOff = comment('c4', { quote: '  int x;' })
    const { claimedThreadIds } = routeCodeComments(DOC, spans, [reply, bare, noOff])
    expect(claimedThreadIds.size).toBe(0)
  })
})

describe('buildCodeCommentAnchor', () => {
  const spans = codeFenceSpans(DOC)

  it('builds a source-space anchor for a line range, with document line', () => {
    const a = buildCodeCommentAnchor(DOC, spans[0], 1, 2)
    expect(a).not.toBeNull()
    expect(a!.quote).toBe('class Foo {\n  int x;')
    expect(DOC.slice(a!.startOffset, a!.endOffset)).toBe(a!.quote)
    expect(a!.line).toBe(4) // document line of `class Foo {`
    expect(a!.prefix.endsWith('```java\n')).toBe(true)
  })

  it('round-trips through routeCodeComments (create → route back to the same block/line)', () => {
    const a = buildCodeCommentAnchor(DOC, spans[0], 3, 3)!
    const c = comment('rt', { quote: a.quote, prefix: a.prefix, suffix: a.suffix, start_offset: a.startOffset, end_offset: a.endOffset })
    const { byFence } = routeCodeComments(DOC, spans, [c])
    expect(byFence.get(4)).toEqual([{ id: 'rt', line: 3, count: 1, unread: false }])
  })

  it('clamps an out-of-range request and swaps a reversed range', () => {
    const a = buildCodeCommentAnchor(DOC, spans[0], 99, 2)!
    expect(a.quote).toBe('  int x;\n}')
  })

  it('returns null for a blank selection', () => {
    const src = '```\n\n   \n```\n'
    const [s] = codeFenceSpans(src)
    expect(buildCodeCommentAnchor(src, s, 1, 2)).toBeNull()
  })
})
