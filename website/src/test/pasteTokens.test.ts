import { describe, it, expect, beforeEach } from 'vitest'
import {
  shouldCollapse,
  countLines,
  formatToken,
  makePasteId,
  nextSeq,
  findTokenRanges,
  tokenRangeAt,
  pruneBlocks,
  expandAll,
  recollapsePastes,
  mergePreservedPastes,
  saveStoredPaste,
  readStoredPaste,
  STORE_KEY,
  STORE_CAP,
  STORE_TTL_MS,
  STORE_MAX_BYTES,
  PASTE_TOKEN_REGEX,
  type PasteBlock,
} from '../utils/pasteTokens'

const block = (overrides: Partial<PasteBlock> = {}): PasteBlock => ({
  id: overrides.id ?? makePasteId(),
  seq: overrides.seq ?? 1,
  lines: overrides.lines ?? 3,
  content: overrides.content ?? 'a\nb\nc',
})

describe('pasteTokens', () => {
  describe('shouldCollapse', () => {
    it('collapses 3-line paste', () => { expect(shouldCollapse('a\nb\nc')).toBe(true) })
    it('collapses 200+ char one-liner', () => { expect(shouldCollapse('x'.repeat(250))).toBe(true) })
    it('does not collapse 2-line short paste', () => { expect(shouldCollapse('a\nb')).toBe(false) })
    it('does not collapse empty', () => { expect(shouldCollapse('')).toBe(false) })
  })

  describe('countLines', () => {
    it('1 for no newlines', () => { expect(countLines('hello')).toBe(1) })
    it('trailing newline counts', () => { expect(countLines('a\nb\n')).toBe(3) })
    it('empty = 0', () => { expect(countLines('')).toBe(0) })
  })

  describe('formatToken + regex', () => {
    it('produces canonical token', () => {
      expect(formatToken(block({ seq: 3, lines: 42 }))).toBe('[ Paste #3 · 42 lines ]')
    })
    it('regex captures seq and lines', () => {
      const b = block({ seq: 7, lines: 12 })
      const s = `hey ${formatToken(b)} there`
      PASTE_TOKEN_REGEX.lastIndex = 0
      const m = PASTE_TOKEN_REGEX.exec(s)
      expect(m?.[1]).toBe('7')
      expect(m?.[2]).toBe('12')
    })
  })

  describe('nextSeq', () => {
    it('starts at 1 when empty', () => { expect(nextSeq([])).toBe(1) })
    it('is max+1 regardless of gaps', () => {
      expect(nextSeq([block({ seq: 1 }), block({ seq: 4 })])).toBe(5)
    })
  })

  describe('findTokenRanges', () => {
    it('pairs by seq', () => {
      const b1 = block({ id: 'a', seq: 1, lines: 3, content: 'X' })
      const b2 = block({ id: 'b', seq: 2, lines: 9, content: 'Y' })
      const text = `${formatToken(b1)} mid ${formatToken(b2)}`
      const r = findTokenRanges(text, [b1, b2])
      expect(r.map(x => x.block.id)).toEqual(['a', 'b'])
    })

    it('preserves document order even with seq gaps', () => {
      const b1 = block({ id: 'a', seq: 5 })
      const b2 = block({ id: 'b', seq: 2 })
      const text = `${formatToken(b2)} ${formatToken(b1)}`
      const r = findTokenRanges(text, [b1, b2])
      expect(r.map(x => x.block.id)).toEqual(['b', 'a'])
    })

    it('ignores tokens whose seq is unknown', () => {
      const known = block({ id: 'k', seq: 1 })
      const unknown = block({ id: 'u', seq: 99 })
      const text = `${formatToken(known)} ${formatToken(unknown)}`
      const r = findTokenRanges(text, [known])
      expect(r).toHaveLength(1)
      expect(r[0].block.id).toBe('k')
    })
  })

  describe('tokenRangeAt', () => {
    it('returns range when caret is inside', () => {
      const b = block({ seq: 1 })
      const text = `xx ${formatToken(b)} yy`
      const caret = text.indexOf('[') + 4
      expect(tokenRangeAt(text, [b], caret)?.block.id).toBe(b.id)
    })
    it('returns null outside', () => {
      expect(tokenRangeAt(`a ${formatToken(block())} b`, [block()], 0)).toBeNull()
    })
  })

  describe('pruneBlocks', () => {
    it('drops blocks without a surviving token', () => {
      const keep = block({ id: 'k', seq: 1 })
      const drop = block({ id: 'd', seq: 2 })
      expect(pruneBlocks(`${formatToken(keep)}`, [keep, drop])).toEqual([keep])
    })
    it('returns same ref if unchanged', () => {
      const b = block({ seq: 1 })
      const input = [b]
      expect(pruneBlocks(formatToken(b), input)).toBe(input)
    })
  })

  describe('expandAll', () => {
    it('inlines content', () => {
      const b1 = block({ id: 'a', seq: 1, content: 'AAA' })
      const b2 = block({ id: 'b', seq: 2, content: 'BBB' })
      expect(expandAll(`x ${formatToken(b1)} y ${formatToken(b2)} z`, [b1, b2]))
        .toBe('x AAA y BBB z')
    })
    it('leaves unknown-seq tokens alone', () => {
      const known = block({ seq: 1, content: 'K' })
      const unknown = block({ seq: 99 })
      expect(expandAll(`${formatToken(known)} and ${formatToken(unknown)}`, [known]))
        .toBe(`K and ${formatToken(unknown)}`)
    })
  })

  describe('recollapsePastes', () => {
    it('folds a whole-message paste back to its token', () => {
      const b = block({ id: 'a', seq: 1, lines: 3, content: 'l1\nl2\nl3' })
      expect(recollapsePastes('l1\nl2\nl3', [b])).toBe('[ Paste #1 · 3 lines ]')
    })

    it('is the inverse of expandAll for a chip embedded in surrounding text', () => {
      const b = block({ id: 'a', seq: 2, lines: 3, content: 'A\nB\nC' })
      const display = `before ${formatToken(b)} after`
      const expanded = expandAll(display, [b])
      expect(recollapsePastes(expanded, [b])).toBe(display)
    })

    it('collapses multiple blocks in document order', () => {
      const b1 = block({ id: 'a', seq: 1, lines: 1, content: 'AAA' })
      const b2 = block({ id: 'b', seq: 2, lines: 1, content: 'BBB' })
      expect(recollapsePastes('x AAA y BBB z', [b2, b1]))
        .toBe(`x ${formatToken(b1)} y ${formatToken(b2)} z`)
    })

    it('returns content unchanged when no block content is found', () => {
      const b = block({ id: 'a', seq: 1, content: 'AAA' })
      expect(recollapsePastes('nothing here', [b])).toBe('nothing here')
    })

    it('claims non-overlapping occurrences when one block is a substring of another', () => {
      const outer = block({ id: 'o', seq: 1, lines: 1, content: 'AAABBB' })
      const inner = block({ id: 'i', seq: 2, lines: 1, content: 'AAA' })
      // 'AAABBB' then a standalone 'AAA'. Outer claims the first region; inner
      // must land on the later standalone 'AAA', not overlap the outer.
      const out = recollapsePastes('AAABBB then AAA', [outer, inner])
      expect(out).toBe(`${formatToken(outer)} then ${formatToken(inner)}`)
    })

    it('does not exceed the byte size of the input (huge paste collapses small)', () => {
      const huge = 'x\n'.repeat(30_000)
      const b = block({ id: 'a', seq: 1, lines: 30_001, content: huge })
      const out = recollapsePastes(huge, [b])
      expect(out).toBe(formatToken(b))
      expect(out.length).toBeLessThan(64)
    })

    // The backend strips trailing whitespace from the stored message, but the
    // block content is stored verbatim. A paste that was the LAST thing in the
    // message therefore keeps a trailing newline in block.content that the
    // stored content no longer has — verbatim indexOf misses. The trimEnd()
    // fallback recovers it (else the huge paste falls through to raw markdown).
    it('matches a tail block whose trailing whitespace the backend stripped', () => {
      const b = block({ id: 'a', seq: 1, lines: 3, content: 'l1\nl2\nl3\n' })
      // Stored content: block text minus its trailing newline (backend trimEnd).
      const stored = 'l1\nl2\nl3'
      expect(recollapsePastes(stored, [b])).toBe('[ Paste #1 · 3 lines ]')
    })

    it('collapses a whitespace-stripped tail block embedded after prefix text', () => {
      const b = block({ id: 'a', seq: 2, lines: 2, content: 'A\nB  \n' })
      const stored = 'hi A\nB' // prefix + block with trailing "  \n" stripped
      expect(recollapsePastes(stored, [b])).toBe('hi [ Paste #2 · 2 lines ]')
    })

    it('prefers the verbatim match for an interior block whose trailing whitespace is preserved', () => {
      // Interior block keeps its trailing newline in the stored content (text
      // follows it), so the verbatim match must win — the trimEnd() fallback
      // must not fire and swallow the trailing newline into the token.
      const b = block({ id: 'a', seq: 1, lines: 2, content: 'X\nY\n' })
      const stored = 'X\nY\nafter'
      expect(recollapsePastes(stored, [b])).toBe('[ Paste #1 · 2 lines ]after')
    })
  })

  describe('mergePreservedPastes', () => {
    it('re-attaches tokens + pastes when expansion matches incoming content', () => {
      const b = block({ id: 'x', seq: 1, content: 'line1\nline2\nline3' })
      const existing = [
        { role: 'user', content: `prefix ${formatToken(b)} suffix`, meta: { pastes: [b] } },
        { role: 'assistant', content: 'ok' },
      ]
      const incoming = [
        { role: 'user', content: 'prefix line1\nline2\nline3 suffix' }, // backend-expanded
        { role: 'assistant', content: 'ok' },
      ]
      const out = mergePreservedPastes(existing, incoming)
      expect(out[0].content).toBe(existing[0].content)
      expect((out[0].meta as { pastes: PasteBlock[] }).pastes).toEqual([b])
      expect(out[1].content).toBe('ok')
    })

    it('returns incoming unchanged when no existing pastes', () => {
      const incoming = [{ role: 'user', content: 'hi' }]
      expect(mergePreservedPastes([{ role: 'user', content: 'hi' }], incoming)).toBe(incoming)
    })

    it('consumes FIFO when multiple user messages have pastes', () => {
      const b1 = block({ id: 'a', seq: 1, content: 'AAA' })
      const b2 = block({ id: 'b', seq: 1, content: 'BBB' })
      const existing = [
        { role: 'user', content: `X ${formatToken(b1)}`, meta: { pastes: [b1] } },
        { role: 'assistant', content: 'r1' },
        { role: 'user', content: `Y ${formatToken(b2)}`, meta: { pastes: [b2] } },
      ]
      const incoming = [
        { role: 'user', content: 'X AAA' },
        { role: 'assistant', content: 'r1' },
        { role: 'user', content: 'Y BBB' },
      ]
      const out = mergePreservedPastes(existing, incoming)
      expect(out[0].content).toBe(existing[0].content)
      expect(out[2].content).toBe(existing[2].content)
    })

    it('leaves message alone when no existing entry matches its expanded content', () => {
      const b = block({ id: 'a', seq: 1, content: 'AAA' })
      const existing = [{ role: 'user', content: `X ${formatToken(b)}`, meta: { pastes: [b] } }]
      const incoming = [
        { role: 'user', content: 'some other text' }, // no match
      ]
      const out = mergePreservedPastes(existing, incoming)
      expect(out[0].content).toBe('some other text')
      expect(out[0].meta).toBeUndefined()
    })

    // Fallback 3: a fresh-tab load has no optimistic bubble and (for a big
    // paste) no side-table entry, but the backend re-serves meta.pastes with
    // the expanded content. Re-collapse from the message's own blocks so the
    // huge string never reaches state/render expanded.
    it('self-collapses a backend message carrying its own pastes but no token', () => {
      const b = block({ id: 'a', seq: 1, lines: 3, content: 'l1\nl2\nl3' })
      const incoming = [{ role: 'user', content: 'l1\nl2\nl3', meta: { pastes: [b] } }]
      const out = mergePreservedPastes([], incoming)
      expect(out[0].content).toBe('[ Paste #1 · 3 lines ]')
      expect((out[0].meta as { pastes: PasteBlock[] }).pastes).toEqual([b])
    })

    it('does not re-collapse a message whose token is already present', () => {
      const b = block({ id: 'a', seq: 1, lines: 3, content: 'l1\nl2\nl3' })
      const already = { role: 'user', content: `hi ${formatToken(b)}`, meta: { pastes: [b] } }
      const incoming = [already]
      const out = mergePreservedPastes([], incoming)
      expect(out[0].content).toBe(already.content)
    })

    it('returns incoming unchanged (same ref) when no message needs any collapse', () => {
      const incoming = [{ role: 'user', content: 'plain, no pastes' }]
      expect(mergePreservedPastes([], incoming)).toBe(incoming)
    })
  })

  // The scroll-position chip flip (#15996): a resolved paste chip must not
  // revert to raw text when a later reconcile can no longer re-collapse it via
  // the FIFO queue (consumed) or the localStorage side table (evicted). Once an
  // existing row carries meta.pastes AND a stable meta.sendId, carry the pastes
  // forward to any incoming row with the same sendId, independent of both.
  describe('mergePreservedPastes — id-keyed carry-forward', () => {
    beforeEach(() => { localStorage.clear() })

    it('re-collapses a redacted echo by sendId instead of leaving it expanded', () => {
      const b = block({ id: 'x', seq: 1, content: 'line1\nline2\nline3' })
      // Existing row already resolved to a chip, tagged with its sendId.
      const existing = [
        { role: 'user', content: `prefix ${formatToken(b)} suffix`, meta: { pastes: [b], sendId: 's1' } },
      ]
      // Backend re-serves the SAME send expanded, no pastes — and the content
      // does NOT match fallback 2's expansion key verbatim (trailing redaction),
      // so only the id path can save it. No localStorage entry exists. The paste
      // body is still present as a substring, so the id branch MUST re-collapse
      // it to a token rather than return the raw expanded text (which would
      // leave the paste permanently expanded in state and preempt the side
      // table). Verifies the Opus-5 finding is fixed.
      const incoming = [
        { role: 'user', content: 'prefix line1\nline2\nline3 suffix REDACTED', meta: { sendId: 's1' } },
      ]
      const out = mergePreservedPastes(existing, incoming)
      expect((out[0].meta as { pastes: PasteBlock[] }).pastes).toEqual([b])
      // Re-collapsed in place: the chip token replaces the paste body, trailing
      // redaction preserved — never the raw expanded body.
      expect(out[0].content).toBe(`prefix ${formatToken(b)} suffix REDACTED`)
      expect(out[0].content).not.toContain('line1\nline2\nline3')
    })

    it('restores the collapsed token content when the echo is the plain expansion', () => {
      const b = block({ id: 'x', seq: 1, content: 'line1\nline2\nline3' })
      const existing = [
        { role: 'user', content: `prefix ${formatToken(b)} suffix`, meta: { pastes: [b], sendId: 's2' } },
      ]
      // Expanded equivalent of the held content (trailing whitespace stripped
      // by the backend), same sendId, no side-table entry.
      const incoming = [
        { role: 'user', content: 'prefix line1\nline2\nline3 suffix', meta: { sendId: 's2' } },
      ]
      const out = mergePreservedPastes(existing, incoming)
      expect(out[0].content).toBe(existing[0].content) // kept collapsed → chip
      expect((out[0].meta as { pastes: PasteBlock[] }).pastes).toEqual([b])
    })

    it('is stable across repeated reconciles (no flip on remount/refresh)', () => {
      const b = block({ id: 'x', seq: 1, content: 'l1\nl2\nl3' })
      const first = [
        { role: 'user', content: `${formatToken(b)}`, meta: { pastes: [b], sendId: 's3' } },
      ]
      const server = [{ role: 'user', content: 'l1\nl2\nl3', meta: { sendId: 's3' } }]
      // Reconcile once (optimistic → server), then AGAIN against the resolved
      // state (what a scroll-driven refresh/slot-switch does). The second pass
      // must keep the pastes.
      const once = mergePreservedPastes(first, server)
      const twice = mergePreservedPastes(once, server)
      expect((twice[0].meta as { pastes: PasteBlock[] }).pastes).toEqual([b])
      expect(twice[0].content).toBe('[ Paste #1 · 3 lines ]')
    })

    it('carries meta.files forward alongside pastes by sendId', () => {
      const b = block({ id: 'x', seq: 1, content: 'line1\nline2\nline3' })
      const existing = [
        { role: 'user', content: `${formatToken(b)}`, meta: { pastes: [b], files: ['/a b.png'], sendId: 's4' } },
      ]
      const incoming = [
        { role: 'user', content: 'line1\nline2\nline3 X', meta: { sendId: 's4' } },
      ]
      const out = mergePreservedPastes(existing, incoming)
      expect((out[0].meta as { files: string[] }).files).toEqual(['/a b.png'])
    })

    it('does not carry forward without a matching sendId', () => {
      const b = block({ id: 'x', seq: 1, content: 'line1\nline2\nline3' })
      const existing = [
        { role: 'user', content: `${formatToken(b)}`, meta: { pastes: [b], sendId: 's5' } },
      ]
      // Different sendId, non-matching content, no side table → stays raw.
      const incoming = [
        { role: 'user', content: 'line1\nline2\nline3 Z', meta: { sendId: 'OTHER' } },
      ]
      const out = mergePreservedPastes(existing, incoming)
      expect(out[0].meta?.pastes).toBeUndefined()
      expect(out[0].content).toBe('line1\nline2\nline3 Z')
    })

    it('matches by sendId BEFORE FIFO so a repeated paste keeps its own files', () => {
      // Same paste text sent twice, each with a DIFFERENT file. The two
      // optimistic rows expand to byte-identical text, so the FIFO (expansion
      // equality) match is ambiguous and would consume the FIRST row's entry
      // for the SECOND send — surfacing the older send's attachment. The id
      // path must win so each send keeps its own files. Verifies the GPT-5.6
      // finding (identity matching running after ambiguous FIFO) is fixed.
      const body = 'line1\nline2\nline3'
      const b1 = block({ id: 'p1', seq: 1, content: body })
      const b2 = block({ id: 'p2', seq: 2, content: body })
      const existing = [
        { role: 'user', content: `${formatToken(b1)}`, meta: { pastes: [b1], files: ['/first.png'], sendId: 'sa' } },
        { role: 'user', content: `${formatToken(b2)}`, meta: { pastes: [b2], files: ['/second.png'], sendId: 'sb' } },
      ]
      // Backend re-serves both, expanded, no pastes/files — distinguished only
      // by sendId (the expanded bodies are identical).
      const incoming = [
        { role: 'user', content: body, meta: { sendId: 'sa' } },
        { role: 'user', content: body, meta: { sendId: 'sb' } },
      ]
      const out = mergePreservedPastes(existing, incoming)
      expect((out[0].meta as { files: string[] }).files).toEqual(['/first.png'])
      expect((out[1].meta as { files: string[] }).files).toEqual(['/second.png'])
      expect((out[0].meta as { pastes: PasteBlock[] }).pastes).toEqual([b1])
      expect((out[1].meta as { pastes: PasteBlock[] }).pastes).toEqual([b2])
    })

    it('re-collapses when the wire text carries attached-file markers the bubble lacks', () => {
      // A paste sent together with a file: the LLM-facing wire text appends an
      // `[attached_file N] path` marker that the display bubble never had, so
      // the wire text diverges from expandAll(displayContent). FIFO misses, the
      // side table is keyed on the wire text (not yet present here), and the id
      // branch must re-collapse the paste body in the wire text — NOT return it
      // raw and leave the paste permanently expanded. Verifies the Opus-5
      // permanent-expansion finding is fixed.
      const b = block({ id: 'f', seq: 1, content: 'line1\nline2\nline3' })
      const existing = [
        { role: 'user', content: `${formatToken(b)}`, meta: { pastes: [b], files: ['/doc.pdf'], sendId: 'sf' } },
      ]
      const incoming = [
        { role: 'user', content: 'line1\nline2\nline3\n[attached_file 1] /doc.pdf', meta: { sendId: 'sf' } },
      ]
      const out = mergePreservedPastes(existing, incoming)
      expect((out[0].meta as { pastes: PasteBlock[] }).pastes).toEqual([b])
      expect((out[0].meta as { files: string[] }).files).toEqual(['/doc.pdf'])
      // Paste body folded to a token; the attached-file marker is preserved and
      // the raw body is gone (no permanent expansion in state).
      expect(out[0].content).toBe(`${formatToken(b)}\n[attached_file 1] /doc.pdf`)
      expect(out[0].content).not.toContain('line1\nline2\nline3')
    })

    it('consumes the held entry on an id hit so a later id-less row cannot inherit it', () => {
      // GPT-5.6 finding: an id-bearing paste row, then an id-LESS channel/echo
      // row with IDENTICAL expanded content. The id row is matched by sendId;
      // its entry must be removed from the FIFO queue so the id-less row does
      // NOT then match it by expansion-equality (fallback 2) and inherit the
      // first send's pastes/files. The id-less row must stay raw.
      const b = block({ id: 'g', seq: 1, content: 'line1\nline2\nline3' })
      const existing = [
        { role: 'user', content: `${formatToken(b)}`, meta: { pastes: [b], files: ['/leak.png'], sendId: 'sg' } },
      ]
      const body = 'line1\nline2\nline3'
      const incoming = [
        // The id-bearing send, re-served expanded (plain expansion of the chip).
        { role: 'user', content: body, meta: { sendId: 'sg' } },
        // A SEPARATE id-less row with the same expanded content (e.g. a channel
        // echo / a different message that happens to match). Must NOT inherit.
        { role: 'user', content: body },
      ]
      const out = mergePreservedPastes(existing, incoming)
      // First row: carried by id, chip restored.
      expect((out[0].meta as { pastes: PasteBlock[] }).pastes).toEqual([b])
      expect((out[0].meta as { files: string[] }).files).toEqual(['/leak.png'])
      // Second row: NO leak — no pastes, no files, content untouched.
      expect(out[1].meta?.pastes).toBeUndefined()
      expect(out[1].meta?.files).toBeUndefined()
      expect(out[1].content).toBe(body)
    })

    it('restores the stored display text (not recollapsed wire) when the wire carries a knowledge block', () => {
      // GPT-5.6 finding: a paste sent WITH selected knowledge context — the
      // send path prepends a `[KNOWLEDGE CONTEXT …]` block to the LLM wire text
      // only, never to the display bubble. On reconcile heldExpanded !== wire,
      // so the divergence branch runs. Reconstructing from the wire via
      // recollapsePastes would fold the paste body but leave the knowledge
      // block visible in the bubble (an info leak). The side table (keyed on
      // the exact wire text) holds the TRUE displayTxt — prefer it.
      const b = block({ id: 'k', seq: 1, content: 'line1\nline2\nline3' })
      const displayTxt = `${formatToken(b)}` // what the user actually sees
      const wire = `[KNOWLEDGE CONTEXT]\nsecret internal doc\n[/KNOWLEDGE CONTEXT]\nline1\nline2\nline3`
      // Composer saved the wire→display mapping at send time.
      saveStoredPaste(wire, displayTxt, [b], ['/f.png'])
      const existing = [
        { role: 'user', content: displayTxt, meta: { pastes: [b], files: ['/f.png'], sendId: 'sk' } },
      ]
      const incoming = [
        { role: 'user', content: wire, meta: { sendId: 'sk' } },
      ]
      const out = mergePreservedPastes(existing, incoming)
      // Display restored from the side table — chip only, NO knowledge block.
      expect(out[0].content).toBe(displayTxt)
      expect(out[0].content).not.toContain('KNOWLEDGE CONTEXT')
      expect(out[0].content).not.toContain('secret internal doc')
      expect((out[0].meta as { pastes: PasteBlock[] }).pastes).toEqual([b])
      expect((out[0].meta as { files: string[] }).files).toEqual(['/f.png'])
    })

    it('strips only the knowledge envelope and re-collapses held pastes (side table evicted)', () => {
      // GPT-5.6 follow-up (pasteTokens.ts knowledge branch): a knowledge send
      // whose side-table entry was evicted. The wire is the REAL envelope the
      // app prepends (expandKnowledgeBlock + '\n' + llmTxt). The branch must
      // strip only the envelope and re-collapse the held paste blocks over the
      // remainder — never leave the `[KNOWLEDGE CONTEXT …]` block in the bubble,
      // and never wholesale-drop surrounding text.
      const b = block({ id: 'kv', seq: 1, content: 'line1\nline2\nline3' })
      const displayTxt = `${formatToken(b)}`
      // Real envelope format (see useKnowledgeFetch.expandKnowledgeBlock).
      const envelope = '[KNOWLEDGE CONTEXT — injected by user from knowledge library]\n\n## doc\nsecret internal doc\n\n[END KNOWLEDGE CONTEXT]\n'
      const wire = `${envelope}\nline1\nline2\nline3`
      // Side table intentionally left EMPTY (entry evicted) — no saveStoredPaste.
      const existing = [
        { role: 'user', content: displayTxt, meta: { pastes: [b], files: ['/f.png'], sendId: 'ske', knowledge: { items: 1, tokens: 4, titles: ['doc'], content: [{ title: 'doc', text: 'secret internal doc' }] } } },
      ]
      const incoming = [
        { role: 'user', content: wire, meta: { sendId: 'ske', knowledge: { items: 1, tokens: 4, titles: ['doc'], content: [{ title: 'doc', text: 'secret internal doc' }] } } },
      ]
      const out = mergePreservedPastes(existing, incoming)
      // Envelope stripped, paste body re-collapsed to its chip.
      expect(out[0].content).toBe(displayTxt)
      expect(out[0].content).not.toContain('KNOWLEDGE CONTEXT')
      expect(out[0].content).not.toContain('secret internal doc')
      expect((out[0].meta as { pastes: PasteBlock[] }).pastes).toEqual([b])
      expect((out[0].meta as { files: string[] }).files).toEqual(['/f.png'])
    })

    it('preserves other merged-row text when stripping a knowledge send (busy-drain fold)', () => {
      // GPT-5.6 UPHELD finding: a busy-drain folds several queued messages into
      // ONE reconciled row and unions their meta — so the row carries one send's
      // sendId + knowledge, yet its content holds BOTH the knowledge send and a
      // second queued message. The old wholesale `held.content` return dropped
      // the second message permanently. The branch must strip only the envelope
      // and re-collapse the paste, keeping the second message intact.
      const b = block({ id: 'km', seq: 1, content: 'line1\nline2\nline3' })
      const displayTxt = `${formatToken(b)}`
      const envelope = '[KNOWLEDGE CONTEXT — injected by user from knowledge library]\n\n## doc\nsecret internal doc\n\n[END KNOWLEDGE CONTEXT]\n'
      const secondMessage = 'a different second queued message'
      // Merged wire: envelope + paste-send body + the folded-in second message.
      const wire = `${envelope}\nline1\nline2\nline3\n${secondMessage}`
      const existing = [
        { role: 'user', content: displayTxt, meta: { pastes: [b], sendId: 'skm', knowledge: { items: 1, tokens: 4, titles: ['doc'], content: [{ title: 'doc', text: 'secret internal doc' }] } } },
      ]
      const incoming = [
        { role: 'user', content: wire, meta: { sendId: 'skm', knowledge: { items: 1, tokens: 4, titles: ['doc'], content: [{ title: 'doc', text: 'secret internal doc' }] } } },
      ]
      const out = mergePreservedPastes(existing, incoming)
      // The knowledge envelope is gone, the paste folded to a chip, and the
      // second queued message is STILL present (not dropped).
      expect(out[0].content).not.toContain('KNOWLEDGE CONTEXT')
      expect(out[0].content).not.toContain('secret internal doc')
      expect(out[0].content).toContain(secondMessage)
      expect(out[0].content).toContain(formatToken(b))
      expect((out[0].meta as { pastes: PasteBlock[] }).pastes).toEqual([b])
    })

    it('skips id carry-forward when both rows have different server mids (reused sendId)', () => {
      // GPT-5.6 finding: a reused client-supplied sendId naming two DIFFERENT
      // persisted rows (distinct server mids) must not let one row inherit the
      // other's pastes/files. The incoming content is the PLAIN EXPANSION of the
      // held pastes, so WITHOUT the mid guard the id branch would carry forward
      // (leak); WITH it the differing mids prove distinct rows and the id path
      // is skipped, and no other route matches (empty side table), so the row
      // stays raw.
      const b = block({ id: 'm', seq: 1, content: 'line1\nline2\nline3' })
      const existing = [
        { role: 'user', content: `${formatToken(b)}`, meta: { pastes: [b], files: ['/owner.png'], sendId: 'dup', mid: 'MID-A' } },
      ]
      const incoming = [
        // Same (reused) sendId, DIFFERENT server mid, content == expansion of b.
        { role: 'user', content: 'line1\nline2\nline3', meta: { sendId: 'dup', mid: 'MID-B' } },
      ]
      const out = mergePreservedPastes(existing, incoming)
      expect(out[0].meta?.pastes).toBeUndefined()
      expect(out[0].meta?.files).toBeUndefined()
      expect(out[0].content).toBe('line1\nline2\nline3')
    })

    it('still carries forward by sendId when the incoming row has no mid yet (optimistic window)', () => {
      // The mid guard must NOT disable the carry-forward in the pre-confirm
      // window where the server mid has not landed on the incoming row yet —
      // that window is exactly where the chip-flip bug lives. A missing mid on
      // either side leaves the sendId match intact.
      const b = block({ id: 'n', seq: 1, content: 'l1\nl2\nl3' })
      const existing = [
        { role: 'user', content: `${formatToken(b)}`, meta: { pastes: [b], sendId: 'opt', mid: 'MID-X' } },
      ]
      const incoming = [
        { role: 'user', content: 'l1\nl2\nl3', meta: { sendId: 'opt' } }, // no mid yet
      ]
      const out = mergePreservedPastes(existing, incoming)
      expect((out[0].meta as { pastes: PasteBlock[] }).pastes).toEqual([b])
      expect(out[0].content).toBe('[ Paste #1 · 3 lines ]')
    })

    it('does not strand the held entry on a mid-conflict: the true owner row still gets its pastes', () => {
      // GPT-5.6 UPHELD finding (pasteTokens.ts mid-conflict branch): on a
      // reused sendId with differing mids, the conflicting row must NOT evict
      // the held entry — the entry's TRUE owner (a later row carrying the
      // matching mid) must still receive its pastes. Here the conflicting row
      // (MID-B) is reconciled BEFORE the owner row (MID-A). The old eviction
      // dropped the entry, so the owner rendered expanded; now the owner keeps
      // its chip. The conflicting row must also NOT inherit the entry (no FIFO
      // leak) even though its content equals the held expansion.
      const b = block({ id: 'o', seq: 1, content: 'line1\nline2\nline3' })
      const existing = [
        { role: 'user', content: `${formatToken(b)}`, meta: { pastes: [b], files: ['/owner.png'], sendId: 'dup', mid: 'MID-A' } },
      ]
      const incoming = [
        // Conflicting row first (reused sendId, different mid), content == expansion.
        { role: 'user', content: 'line1\nline2\nline3', meta: { sendId: 'dup', mid: 'MID-B' } },
        // The true owner row (matching mid), served expanded by the backend.
        { role: 'user', content: 'line1\nline2\nline3', meta: { sendId: 'dup', mid: 'MID-A' } },
      ]
      const out = mergePreservedPastes(existing, incoming)
      // Conflicting row did NOT inherit the entry.
      expect(out[0].meta?.pastes).toBeUndefined()
      expect(out[0].content).toBe('line1\nline2\nline3')
      // Owner row DID get its pastes/files back (chip, not expanded).
      expect((out[1].meta as { pastes: PasteBlock[] }).pastes).toEqual([b])
      expect((out[1].meta as { files: string[] }).files).toEqual(['/owner.png'])
      expect(out[1].content).toBe('[ Paste #1 · 3 lines ]')
    })
  })

  // mc-paste-store-v1 localStorage side table: byte/entry/TTL bounding so it
  // cannot grow unbounded and exhaust the localStorage quota.
  describe('saveStoredPaste / readStoredPaste store bounding', () => {
    beforeEach(() => { localStorage.clear() })

    const storeSize = (): number => {
      const raw = localStorage.getItem(STORE_KEY)
      return raw ? Object.keys(JSON.parse(raw)).length : 0
    }

    it('round-trips a saved paste by expanded content key', () => {
      const b = block({ id: 'a', seq: 1, content: 'AAA' })
      saveStoredPaste('expanded AAA text', 'display [ Paste #1 · 3 lines ]', [b])
      const got = readStoredPaste('expanded AAA text')
      expect(got?.displayTxt).toBe('display [ Paste #1 · 3 lines ]')
      expect(got?.pastes).toEqual([b])
    })

    it('ignores empty pastes / empty content', () => {
      saveStoredPaste('', 'd', [block()])
      saveStoredPaste('key', 'd', [])
      expect(storeSize()).toBe(0)
    })

    it('caps total serialized bytes, keeping only the newest entries that fit', () => {
      // Each entry holds ~50 KB of content; STORE_MAX_BYTES (2 MB) admits far
      // fewer than the number we write, so the byte-aware LRU must evict.
      const big = 'x'.repeat(50_000)
      const total = Math.ceil(STORE_MAX_BYTES / 50_000) + 20
      for (let i = 0; i < total; i++) {
        saveStoredPaste(`key-${i}`, 'd', [block({ seq: 1, content: big })])
      }
      const raw = localStorage.getItem(STORE_KEY)!
      expect(raw.length).toBeLessThanOrEqual(STORE_MAX_BYTES)
      // Newest write must survive; an early (evicted) one must be gone.
      expect(readStoredPaste(`key-${total - 1}`)).not.toBeNull()
      expect(readStoredPaste('key-0')).toBeNull()
    })

    it('keeps the newest entry even when it alone exceeds the byte budget', () => {
      const huge = 'y'.repeat(STORE_MAX_BYTES + 100_000)
      saveStoredPaste('huge-key', 'd', [block({ seq: 1, content: huge })])
      expect(readStoredPaste('huge-key')).not.toBeNull()
      expect(storeSize()).toBe(1)
    })

    it('caps entry count at STORE_CAP, evicting oldest', () => {
      // Small entries so the byte cap never trips — isolate the count cap.
      for (let i = 0; i < STORE_CAP + 10; i++) {
        saveStoredPaste(`k-${i}`, 'd', [block({ seq: 1, content: 's' })])
      }
      expect(storeSize()).toBe(STORE_CAP)
      expect(readStoredPaste(`k-${STORE_CAP + 9}`)).not.toBeNull() // newest kept
      expect(readStoredPaste('k-0')).toBeNull()                    // oldest evicted
    })

    it('drops entries older than the TTL on read', () => {
      const stale = {
        'old-key': { displayTxt: 'd', pastes: [block()], savedAt: Date.now() - STORE_TTL_MS - 1 },
        'fresh-key': { displayTxt: 'd', pastes: [block()], savedAt: Date.now() },
      }
      localStorage.setItem(STORE_KEY, JSON.stringify(stale))
      expect(readStoredPaste('old-key')).toBeNull()
      expect(readStoredPaste('fresh-key')).not.toBeNull()
    })

    it('breaks same-millisecond savedAt ties by seq — newer seq survives eviction', () => {
      // The seq tiebreaker is the headline correctness mechanism: content-
      // addressed keys can be numeric, so insertion-order / stable-sort LRU is
      // unsafe. This test isolates it. Seed two entries with an IDENTICAL
      // savedAt but different seq, each large enough that only ONE fits under
      // STORE_MAX_BYTES alongside a newer trigger entry. savedAt alone cannot
      // order the pair — only seq can — so this test FAILS if the
      // `(b.seq ?? 0) - (a.seq ?? 0)` tiebreaker is removed from writeStore.
      const T = Date.now()
      const big = 'x'.repeat(1_200_000) // ~1.2 MB; two cannot coexist under the 2 MB cap
      localStorage.setItem(STORE_KEY, JSON.stringify({
        'tie-old': { displayTxt: 'd', pastes: [block({ seq: 1, content: big })], savedAt: T, seq: 1 },
        'tie-new': { displayTxt: 'd', pastes: [block({ seq: 1, content: big })], savedAt: T, seq: 2 },
      }))
      // A tiny fresh save triggers writeStore's byte-aware eviction; being the
      // newest it always survives, so the eviction boundary falls between the
      // two tied entries and only seq decides which one is kept.
      saveStoredPaste('trigger-key', 'd', [block({ seq: 1, content: 's' })])
      expect(readStoredPaste('tie-new')).not.toBeNull() // newer seq kept
      expect(readStoredPaste('tie-old')).toBeNull()     // older seq evicted
    })
  })
})
