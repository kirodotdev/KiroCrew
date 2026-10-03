/**
 * "Prompt as sent" — the developer view of a turn's exact text under the
 * Context Breakdown panel.
 *
 *  - adjacent same-label spans merge into one row; a gap never merges across.
 *  - a turn is matched to the record carrying its turn number (both writers stamp
 *    it); of several under one number, the one stamped nearest after the row.
 *  - the panel omits the section until the prompt trace has loaded, marks the
 *    turns that still have text with a dot, and says plainly when the selected
 *    turn has none.
 *  - a segment row opens to the raw slice of the prompt it names.
 */
import { describe, it, expect, afterEach, vi } from 'vitest'
import { render, screen, cleanup, fireEvent, within } from '@testing-library/react'

import { ContextBreakdownPanel, type ContextTrace, type ContextTurn } from '../pages/ContextBreakdownPanel'

vi.mock('../utils/clipboard', () => ({ copyToClipboard: vi.fn(async () => false) }))
import {
  promptForTurn,
  segmentNames,
  segmentsFor,
  spansToUtf16,
  type PromptRecord,
  type PromptTrace,
} from '../pages/PromptAsSentSection'

afterEach(cleanup)

const TEXT =
  '[CRITICAL RULES -- always follow these]\nrule\n[END CRITICAL RULES]\n\n' +
  '[CURRENT USER REQUEST -- respond to this]\nhello there\n\n(If presenting choices, end with x.)'

const HEADER = TEXT.indexOf('[CURRENT USER REQUEST')
const USER = TEXT.indexOf('hello there')

const record = (over: Partial<PromptRecord> = {}): PromptRecord => ({
  // Stamped a moment AFTER the turn row, and carrying the row's turn number (see promptForTurn).
  ts: '2026-08-04T00:00:30.400Z',
  turn: 1,
  chars: TEXT.length,
  assembled_chars: TEXT.length,
  text: TEXT,
  truncated: false,
  redacted: false,
  spans: [
    // One span per block, as the backend serves them (it coalesces a block's
    // body with the blank line after its closer before serving).
    { start: 0, end: HEADER, label: 'critical_rules' },
    { start: HEADER, end: USER, label: 'request_header' },
    { start: USER, end: USER + 'hello there'.length, label: 'your_message' },
    { start: USER + 'hello there'.length, end: TEXT.length, label: 'reply_format_rules' },
  ],
  ...over,
})

const turn = (over: Partial<ContextTurn> = {}): ContextTurn => ({
  ts: '2026-08-04T00:00:30Z',
  turn: 1,
  phase: 'per_turn',
  blocks: { request_header: 42, your_message: 11, critical_rules: 63, reply_format_rules: 40 },
  // Sized to the record by default, so a test is in the matched state unless it
  // says otherwise: under the wrong-match warning the text is withheld.
  total_chars: TEXT.length,
  context_used: 2000,
  context_window: 200000,
  model: 'auto',
  ...over,
})

const trace = (turns: ContextTurn[]): ContextTrace => ({
  slot: 'chat-1',
  turns,
  totals: {},
  injected_chars: 0,
  user_chars: 0,
  peak_context_used: 0,
  context_window: 0,
  window_days: 14,
})

const prompts = (turns: PromptRecord[], over: Partial<PromptTrace> = {}): PromptTrace => ({
  slot: 'chat-1',
  turns,
  dropped: 0,
  evicted: false,
  ...over,
})

describe('segmentsFor', () => {
  it('is one segment per served span, sized in code points', () => {
    const segs = segmentsFor(TEXT, record().spans)
    expect(segs.map(s => s.label)).toEqual([
      'critical_rules',
      'request_header',
      'your_message',
      'reply_format_rules',
    ])
    expect(segs[0]).toEqual({ label: 'critical_rules', start: 0, end: HEADER, chars: HEADER })
  })

  it('does not merge: the backend already coalesced, so two same-label spans are two rows', () => {
    // A repeated block (the reply-format rules sit at both ends) is two spans
    // with text between them; a merge here would be a second spelling of the
    // server's rule, and the ordinal the view adds needs them kept apart.
    const segs = segmentsFor('aaaaabbaa', [
      { start: 0, end: 5, label: 'a' },
      { start: 5, end: 7, label: 'b' },
      { start: 7, end: 9, label: 'a' },
    ])
    expect(segs).toHaveLength(3)
  })

  it('drops an empty span', () => {
    expect(segmentsFor('ab', [{ start: 0, end: 0, label: 'x' }, { start: 0, end: 2, label: 'y' }])).toHaveLength(1)
  })
})

describe('promptForTurn', () => {
  // Both sides carry the assembler's turn number; the row (`context/composed`)
  // is stamped as the prompt is assembled, the record a moment later.
  const p1 = record({ ts: '2026-08-04T00:00:00.400Z', text: 'one', turn: 1 })
  const p2 = record({ ts: '2026-08-04T00:01:00.400Z', text: 'two', turn: 2 })
  const p3 = record({ ts: '2026-08-04T00:02:00.400Z', text: 'three', turn: 3 })

  it('joins by the turn number, never by time', () => {
    expect(promptForTurn([p1, p2, p3], { ts: '2026-08-04T00:01:00Z', turn: 2 })?.text).toBe('two')
    // A record inside the row's time window but under another number is not this turn's.
    expect(promptForTurn([p1, p3], { ts: '2026-08-04T00:01:00Z', turn: 2 })).toBeNull()
    // A record under this number is this turn's wherever its timestamp fell.
    expect(promptForTurn([record({ ts: '2026-08-04T00:09:00Z', text: 'late', turn: 2 })], { ts: '2026-08-04T00:01:00Z', turn: 2 })?.text).toBe('late')
  })

  it('prefers the attempt stamped nearest after the row when a turn wrote several records', () => {
    const early = record({ ts: '2026-08-04T00:00:59Z', text: 'before the row', turn: 2 })
    const own = record({ ts: '2026-08-04T00:01:00.400Z', text: 'own', turn: 2 })
    const retry = record({ ts: '2026-08-04T00:01:30Z', text: 'retry', turn: 2 })
    expect(promptForTurn([retry, early, own], { ts: '2026-08-04T00:01:00Z', turn: 2 })?.text).toBe('own')
    // A retried turn's second row (stamped at the retry) gets the retry's record.
    expect(promptForTurn([retry, early, own], { ts: '2026-08-04T00:01:29Z', turn: 2 })?.text).toBe('retry')
    // Only records stamped before the row: the nearest of them, rather than nothing.
    expect(promptForTurn([early], { ts: '2026-08-04T00:01:00Z', turn: 2 })?.text).toBe('before the row')
  })

  it('finds nothing for a row without a number, and nothing when no record carries the number', () => {
    expect(promptForTurn([p1, p2, p3], { ts: '2026-08-04T00:01:00Z' })).toBeNull()
    expect(promptForTurn([p1, p2, p3], { ts: '2026-08-04T00:01:00Z', turn: null })).toBeNull()
    expect(promptForTurn([p1, p2, p3], { ts: '2026-08-04T00:03:00Z', turn: 4 })).toBeNull()
  })
})

describe('spansToUtf16', () => {
  it('shifts every offset after a non-BMP character by its extra UTF-16 unit', () => {
    // "a😀b" is 3 code points but 4 UTF-16 units; the backend counts code points.
    const text = 'a😀b|tail'
    const spans = spansToUtf16(text, [
      { start: 0, end: 3, label: 'x' },
      { start: 3, end: 8, label: 'y' },
    ])
    expect(text.slice(spans[0].start, spans[0].end)).toBe('a😀b')
    expect(text.slice(spans[1].start, spans[1].end)).toBe('|tail')
  })

  it('sizes a segment in code points even after its offsets are re-addressed for slicing', () => {
    const text = 'a😀b|tail'
    const segs = segmentsFor(text, [
      { start: 0, end: 3, label: 'x' },
      { start: 3, end: 8, label: 'y' },
    ])
    expect(segs[0].chars).toBe(3)
    expect(text.slice(segs[0].start, segs[0].end)).toBe('a😀b')
    expect(segs[1].chars).toBe(5)
  })

  it('is the identity on BMP-only text and clamps an offset past the end', () => {
    expect(spansToUtf16('abc', [{ start: 0, end: 3, label: 'x' }])).toEqual([{ start: 0, end: 3, label: 'x' }])
    expect(spansToUtf16('abc', [{ start: 1, end: 9, label: 'x' }])).toEqual([{ start: 1, end: 3, label: 'x' }])
  })
})

describe('segmentNames', () => {
  const name = (label: string) => label
  it('numbers a label that appears more than once and leaves single ones alone', () => {
    const segs = [
      { label: 'a', start: 0, end: 1, chars: 1 },
      { label: 'b', start: 1, end: 2, chars: 1 },
      { label: 'a', start: 2, end: 3, chars: 1 },
    ]
    expect(segmentNames(segs, name)).toEqual(['a · 1 of 2', 'b', 'a · 2 of 2'])
  })
})

describe('ContextBreakdownPanel with a prompt trace', () => {
  it('omits the section while the prompt trace is not loaded', () => {
    render(<ContextBreakdownPanel trace={trace([turn()])} />)
    expect(screen.queryByTestId('prompt-as-sent')).toBeNull()
  })

  it('shows the selected turn text by segment and marks the turn with a dot', () => {
    const { container } = render(<ContextBreakdownPanel trace={trace([turn()])} prompts={prompts([record()])} />)
    const section = screen.getByTestId('prompt-as-sent')
    expect(within(section).getByText('Prompt as sent')).toBeTruthy()
    // The user's text was carved by the backend, so it has a row of its own and
    // the header row does not claim it.
    expect(within(section).getByText('The same pieces as above, in the order they were sent — the bar shows them to scale.')).toBeTruthy()
    expect(within(section).queryByText(/includes your/)).toBeNull()
    const rows = section.querySelectorAll('[data-prompt-segment]')
    expect(Array.from(rows).map(r => r.getAttribute('data-prompt-segment'))).toEqual([
      'critical_rules',
      'request_header',
      'your_message',
      'reply_format_rules',
    ])
    expect(container.querySelectorAll('[data-prompt-dot]').length).toBe(1)
    expect(screen.getByTestId('prompt-kept-legend')).toBeTruthy()
    // No bar-level tooltip: the helper line names the bar in text a reader
    // meets; each segment keeps its own title (name and size) for hover.
    expect(screen.getByTestId('prompt-segment-bar').getAttribute('title')).toBeNull()
  })

  it('says where the message sits when the backend could not carve it', () => {
    // A record scanned without a user span: the header's span runs through
    // the user's text, so the header row says it includes the message.
    const uncarved = record({
      spans: [
        { start: 0, end: HEADER, label: 'critical_rules' },
        { start: HEADER, end: USER + 'hello there'.length, label: 'request_header' },
        { start: USER + 'hello there'.length, end: TEXT.length, label: 'reply_format_rules' },
      ],
    })
    render(<ContextBreakdownPanel trace={trace([turn()])} prompts={prompts([uncarved])} />)
    const section = screen.getByTestId('prompt-as-sent')
    // The row's NAME says it carries the message, so its larger total is
    // explained before any aside; the aside then carries the message's own
    // count (the turn fixture says 11), so the reader is not left to subtract
    // the summary total from the row's.
    expect(within(section).getByText(/^Header added before your message \(built-in\) \+ your message · 11 of its \d+ characters are your message$/)).toBeTruthy()
    // The helper is the same one sentence either way: where the message sits is
    // said on the row suffix and the summary aside, not a third time here.
    expect(within(section).getByText('The same pieces as above, in the order they were sent — the bar shows them to scale.')).toBeTruthy()
    expect(section.querySelector('[data-prompt-segment="your_message"]')).toBeNull()
    // The summary's own "Your message" total says where its count sits below,
    // naming the number, so the two lists do not read as counting one thing
    // two ways.
    const userRow = document.querySelector('[data-category-row="message"]')!
    expect(userRow.textContent).toMatch(/no row of its own below — your message wasn't marked separately in this turn; its 11 characters sit inside Header added before your message \(built-in\)/)
    // The chevron stays beside the name; the aside follows it, on its own
    // full-width line (a block), so it cannot squeeze the name into a
    // mid-word wrap.
    const chevron = userRow.querySelector('svg')!
    const aside = within(userRow as HTMLElement).getByText(/no row of its own below/)
    expect(chevron.compareDocumentPosition(aside) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy()
    expect(aside.className).toMatch(/\bblock\b/)
    expect(aside.parentElement).toBe(userRow.querySelector('button'))
  })

  it('does not claim a message on a turn that sent none', () => {
    // A quick prompt (/plain, an @prompt turn) has no user text: its record has
    // no user span and the totals credit the user 0 characters, so neither the
    // header row nor the totals aside may say the message is folded in.
    const quick = record({
      spans: [
        { start: 0, end: HEADER, label: 'critical_rules' },
        { start: HEADER, end: USER + 'hello there'.length, label: 'request_header' },
        { start: USER + 'hello there'.length, end: TEXT.length, label: 'reply_format_rules' },
      ],
    })
    const t = turn({ total_chars: TEXT.length, blocks: { request_header: 53, critical_rules: 63, reply_format_rules: 40 } })
    render(<ContextBreakdownPanel trace={trace([t])} prompts={prompts([quick])} />)
    const section = screen.getByTestId('prompt-as-sent')
    expect(within(section).queryByText(/\+ your message/)).toBeNull()
    expect(document.querySelector('[data-category-row="message"]')).toBeNull()
  })

  it('does not annotate the summary row when the message has its own prompt row', () => {
    render(<ContextBreakdownPanel trace={trace([turn()])} prompts={prompts([record()])} />)
    expect(document.querySelector('[data-category-row="message"]')!.textContent).not.toMatch(/no row of its own below/)
  })

  it('never repeats a size in the footer: the heading or a note already carries it', () => {
    render(<ContextBreakdownPanel trace={trace([turn({ total_chars: TEXT.length })])} prompts={prompts([record()])} />)
    expect(screen.getByTestId('prompt-matched-at').textContent).not.toMatch(/characters/)
    cleanup()
    // A substituted receipt: the assembled-vs-sent note names the sent size,
    // and the footer does not say it again under another word.
    render(
      <ContextBreakdownPanel
        trace={trace([turn({ total_chars: TEXT.length + 400 })])}
        prompts={prompts([record({ assembled_chars: TEXT.length + 400 })])}
      />,
    )
    expect(screen.getByTestId('prompt-assembled-vs-sent').textContent).toContain(String(TEXT.length))
    expect(screen.getByTestId('prompt-matched-at').textContent).not.toMatch(/characters/)
    // The rows do not match the totals here, so the helper does not promise
    // they do: it names the count the rows sum to instead.
    expect(screen.queryByText(/^The same pieces as above/)).toBeNull()
    expect(screen.getByText(`The pieces cover the ${TEXT.length} characters kept here, in the order they were sent — the bar shows them to scale.`)).toBeTruthy()
  })

  it('opens a segment to the raw slice of the prompt', () => {
    render(<ContextBreakdownPanel trace={trace([turn()])} prompts={prompts([record()])} />)
    const row = screen.getByTestId('prompt-as-sent').querySelector('[data-prompt-segment="your_message"] button')
    expect(row).not.toBeNull()
    fireEvent.click(row!)
    expect(screen.getByText('hello there')).toBeTruthy()
  })

  it('says how many earlier turns fell out, and when the whole session was evicted', () => {
    render(<ContextBreakdownPanel trace={trace([turn()])} prompts={prompts([record()], { dropped: 3 })} />)
    expect(screen.getByTestId('prompt-dropped').textContent).toMatch(/^Trimmed for newer turns: prompt text for 3 earlier turns .* removed/)
    cleanup()
    // The 13th turn drops exactly one: a plural key, so it does not say "1 earlier turns".
    render(<ContextBreakdownPanel trace={trace([turn()])} prompts={prompts([record()], { dropped: 1 })} />)
    expect(screen.getByTestId('prompt-dropped').textContent).toMatch(/for 1 earlier turn of this session/)
    cleanup()
    render(<ContextBreakdownPanel trace={trace([turn()])} prompts={prompts([], { evicted: true })} />)
    // Each loss line opens with its cause: the per-session cap ("Trimmed for
    // newer turns"), the global budget ("Cleared for other sessions").
    expect(
      within(screen.getByTestId('prompt-as-sent')).getByText(
        /^Cleared to make room for other chats' prompts\. The next turn's text will be kept\.$/,
      ),
    ).toBeTruthy()
    expect(within(screen.getByTestId('prompt-as-sent')).queryByText(/Trimmed for newer turns/)).toBeNull()
  })

  it('says when only a prefix of the prompt is kept, counting code points', () => {
    // An emoji in the kept prefix: one code point, two UTF-16 units. The note
    // must agree with the rows above it, which count code points.
    const emojiText = TEXT.replace('hello there', 'hello 🙂here')
    const emojiSpans = record().spans.map(sp => ({ ...sp }))
    render(
      <ContextBreakdownPanel
        trace={trace([turn({ total_chars: 9_000_000 })])}
        prompts={prompts([
          record({ truncated: true, chars: 9_000_000, assembled_chars: 9_000_000, text: emojiText, spans: emojiSpans }),
        ])}
      />,
    )
    const truncated = screen.getByTestId('prompt-truncated')
    const codePoints = Array.from(emojiText).length
    expect(codePoints).toBe(emojiText.length - 1)
    expect(truncated.textContent).toContain(String(codePoints))
    expect(truncated.textContent).not.toContain(String(emojiText.length))
    // Bridges the rows (kept part) to the totals above (whole prompt).
    expect(truncated.textContent).toContain('The pieces below cover only that kept part.')
    // Said before the rows, whose sizes sum to the kept text and not the heading.
    const firstRow = screen.getByTestId('prompt-as-sent').querySelector('[data-prompt-segment]')!
    expect(truncated.compareDocumentPosition(firstRow) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy()
  })

  it('says on the totals side that the rows below add up to them, when they are this turn\'s', () => {
    // Two lists with the same names and numbers: the relation is said on the
    // totals heading too, not only in the helper under the second list.
    render(<ContextBreakdownPanel trace={trace([turn({ total_chars: TEXT.length })])} prompts={prompts([record()])} />)
    expect(screen.getByTestId('totals-by-kind').textContent).toBe('Totals by kind· the pieces below, added up')
    cleanup()
    // With nothing recorded there is nothing below to add up.
    render(<ContextBreakdownPanel trace={trace([turn({ total_chars: TEXT.length })])} prompts={prompts([])} />)
    expect(screen.getByTestId('totals-by-kind').textContent).toBe('Totals by kind')
  })

  it('does not claim the rows add up to the totals when the record was cut or substituted', () => {
    // A truncated record's rows cover only the kept part; a receipt-swapped
    // record's rows sum to the sent size, not the built one the totals measure.
    render(
      <ContextBreakdownPanel
        trace={trace([turn({ total_chars: TEXT.length })])}
        prompts={prompts([record({ truncated: true })])}
      />,
    )
    expect(screen.getByTestId('totals-by-kind').textContent).toBe('Totals by kind')
    cleanup()
    render(
      <ContextBreakdownPanel
        trace={trace([turn({ total_chars: TEXT.length + 400 })])}
        prompts={prompts([record({ assembled_chars: TEXT.length + 400 })])}
      />,
    )
    expect(screen.getByTestId('totals-by-kind').textContent).toBe('Totals by kind')
  })

  it('names each bar segment on hover, since two kinds share a purple', () => {
    render(<ContextBreakdownPanel trace={trace([turn({ total_chars: TEXT.length })])} prompts={prompts([record()])} />)
    const bar = screen.getByTestId('prompt-segment-bar')
    const titles = Array.from(bar.querySelectorAll('i')).map(el => el.getAttribute('title'))
    expect(titles).toHaveLength(4)
    expect(titles[2]).toMatch(/^Your message · 11$/)
  })

  it('announces the session-start row by its ordinal, the number printed on it', () => {
    // The accessible name must say the same turn number the visible label
    // does: the backend ordinal, not the window-local index the chart keys by.
    const start = turn({ phase: 'session_start', ordinal: 41, ts: '2026-08-04T00:00:30Z', total_chars: TEXT.length })
    render(<ContextBreakdownPanel trace={trace([start])} prompts={prompts([record()])} />)
    const row = document.querySelector('[data-start-row]') as HTMLElement
    expect(row.textContent).toContain('Turn 41 · session start')
    expect(row.getAttribute('aria-label')).toMatch(/^Turn 41 · session start/)
    expect(row.getAttribute('aria-label')).toMatch(/prompt text available/)
  })

  it('joins a row to the record carrying its turn number, not the one nearest in time', () => {
    // Both sides are stamped by the assembler with the same turn number, so the
    // join is by id: a record stamped INSIDE this turn's time window but carrying
    // another number is not this turn's, and a record carrying this number is,
    // wherever its timestamp fell. The footer says the match was by turn number.
    const t6 = turn({ ordinal: 6, turn: 6, ts: '2026-08-04T00:00:30Z', total_chars: TEXT.length })
    const t7 = turn({ ordinal: 7, turn: 7, ts: '2026-08-04T00:01:30Z', total_chars: TEXT.length + 2 })
    const r6 = record({ ts: '2026-08-04T00:00:30.400Z', turn: 6 })
    // Turn 7's own record was stamped late (a slow transport), long after its row.
    const r7 = record({ ts: '2026-08-04T00:03:00.000Z', turn: 7, text: TEXT + '!!', chars: TEXT.length + 2, assembled_chars: TEXT.length + 2 })
    const stray = record({ ts: '2026-08-04T00:01:31.000Z', turn: 6 }) // a retry of turn 6, landing in turn 7's window
    render(<ContextBreakdownPanel trace={trace([t6, t7])} prompts={prompts([r6, stray, r7])} />)
    // Turn 7 (newest, selected): its record by id, despite the stray in its window.
    expect(screen.getByTestId('prompt-matched-at').textContent).toMatch(/^Sent /)
    expect(within(screen.getByTestId('prompt-as-sent')).getByText(/^Copy all$/)).toBeTruthy()
    expect(within(screen.getByTestId('prompt-as-sent')).queryByText(/^Not kept:/)).toBeNull()
    // Turn 6: of its two records, the one stamped nearest after its row.
    fireEvent.click(document.querySelector('button[data-turn="1"]') as HTMLElement)
    expect(promptForTurn([r6, stray, r7], t6)).toBe(r6)
    // A row without a number has nothing to join and says the text is not kept.
    const numberless = turn({ ordinal: 8, turn: null, ts: '2026-08-04T00:04:30Z' })
    cleanup()
    render(<ContextBreakdownPanel trace={trace([t6, t7, numberless])} prompts={prompts([r6, stray, r7])} />)
    expect(within(screen.getByTestId('prompt-as-sent')).getByText(/^Not kept:/)).toBeTruthy()
  })

  it('compares against the ASSEMBLED size and explains a shorter wire text instead of warning', () => {
    // A member session in its acknowledged steady state: the essentials
    // envelope is dropped between the size measurement and the transport write,
    // so the usage row (assembled) and the record (sent) legitimately differ.
    const assembled = TEXT.length + 400
    render(
      <ContextBreakdownPanel
        trace={trace([turn({ total_chars: assembled })])}
        prompts={prompts([record({ assembled_chars: assembled })])}
      />,
    )
    const line = screen.getByTestId('prompt-assembled-vs-sent').textContent ?? ''
    expect(line).toContain(String(assembled))
    expect(line).toContain(String(TEXT.length))
  })

  it('says when the served text had a secret masked', () => {
    render(<ContextBreakdownPanel trace={trace([turn()])} prompts={prompts([record({ redacted: true })])} />)
    expect(screen.getByTestId('prompt-redacted').textContent).toMatch(/masked/)
    cleanup()
    render(<ContextBreakdownPanel trace={trace([turn()])} prompts={prompts([record()])} />)
    expect(screen.queryByTestId('prompt-redacted')).toBeNull()
  })

  it('says plainly when the selected turn has no text kept', () => {
    const t1 = turn({ ts: '2026-08-04T00:00:30Z', turn: 1 })
    const t2 = turn({ ts: '2026-08-04T00:05:30Z', turn: 2 })
    // Only the SECOND turn's prompt is still in the ring.
    const kept = record({ ts: '2026-08-04T00:05:30.400Z', turn: 2 })
    const { container } = render(<ContextBreakdownPanel trace={trace([t1, t2])} prompts={prompts([kept])} />)
    expect(container.querySelectorAll('[data-prompt-dot]').length).toBe(1)
    // Newest is selected by default and has text; pick turn 1.
    fireEvent.click(container.querySelector('button[data-turn="1"]')!)
    // Each text-gone note leads with its consequence; this one is not coming back.
    expect(within(screen.getByTestId('prompt-as-sent')).getByText(/^Not kept: no prompt text is held for this turn/)).toBeTruthy()
  })
  it('drops a copy failure when the selected turn changes', async () => {
    // The clipboard mock refuses, so Copy all raises the notice. Selecting
    // another turn re-renders the section in place with a different record;
    // the notice belongs to the turn it happened on and must not follow.
    const t1 = turn({ ts: '2026-08-04T00:00:30Z' })
    const t2 = turn({ ts: '2026-08-04T00:05:30Z' })
    const r1 = record({ ts: '2026-08-04T00:00:30.400Z' })
    const r2 = record({ ts: '2026-08-04T00:05:30.400Z', text: TEXT.replace('hello there', 'second turn') })
    render(<ContextBreakdownPanel trace={trace([t1, t2])} prompts={prompts([r1, r2])} />)
    const section = screen.getByTestId('prompt-as-sent')
    fireEvent.click(within(section).getByRole('button', { name: /Copy all/ }))
    expect(await within(section).findByText(/Couldn't copy/)).toBeTruthy()
    fireEvent.click(document.querySelector('button[data-turn="1"]')!)
    expect(within(screen.getByTestId('prompt-as-sent')).queryByText(/Couldn't copy/)).toBeNull()
  })

})
