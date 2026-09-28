import { describe, it, expect } from 'vitest'
import { stripPartialOptionMarker } from '../app-sdk/protocol'
// The pattern is in-tree only — the barrel deliberately withholds it from the app surface.
import { findLastOptionMarker, stripOptionMarkers } from '../app-sdk/protocol/optionMarker'

/** What the reader sees for a given stream prefix: the finished-marker strip
 *  (OPTION_MARKER_RE, what parseOptions does) followed by the partial-marker
 *  strip. Mirrors AssistantMessage's streaming pipeline. */
const visible = (prefix: string) =>
  stripPartialOptionMarker(stripOptionMarkers(prefix).trim())

// #9110: a model sometimes wraps the whole marker line in inline code or
// emphasis — `` `[OPTIONS: A | B]` `` / `**[OPTIONS: A | B]**`. The wrapper
// character lands AFTER the closer, which used to break the end-of-line
// anchor: the marker was neither parsed nor stripped, the turn lost its pills,
// and the raw marker leaked as literal (code-styled) text.
describe('OPTION_MARKER_RE with Markdown wrappers (#9110)', () => {
  const wrapped = [
    '`[OPTIONS: Alpha | Beta]`', // backtick-wrapped (the reported shape)
    '**[OPTIONS: Alpha | Beta]**', // emphasis-wrapped
    '_[OPTIONS: Alpha | Beta]_', // underscore emphasis
    '**[OPTIONS: Alpha | Beta]', // leading wrapper only (nothing to steal)
    '***[OPTIONS: Alpha | Beta]***', // longest legal run
    '  `[OPTIONS: Alpha | Beta]`', // indented, wrapper still at line start
    '`[OPTIONS: Alpha | Beta](OPTIONS)`', // wrapper around the paren tic too
  ]

  it('parses the wrapped marker and keeps the labels clean', () => {
    for (const line of wrapped) {
      let last: RegExpMatchArray | null = null
      last = findLastOptionMarker(`Done.\n${line}`)
      expect(last, line).not.toBeNull()
      const labels = last![2].split('|').map(s => s.trim())
      expect(labels, line).toEqual(['Alpha', 'Beta'])
    }
  })

  it('strips the wrapper together with the marker', () => {
    for (const line of wrapped) {
      expect(stripOptionMarkers(`Done.\n${line}`).trim(), line).toBe('Done.')
    }
  })

  it('keeps the negative rows negative — real prose still fails the anchor', () => {
    for (const line of [
      '[OPTIONS: Alpha | Beta].', // trailing prose: a period
      '[OPTIONS: Alpha | Beta] pick one', // trailing prose: words
      '[OPTIONS: Alpha | Beta] **', // wrapper must ABUT the closer
      '[OPTIONS: Alpha | Beta]****', // a 4+ run is not a wrapper
      '[OPTIONS: Alpha | Beta]` and more', // wrapper then prose
      // The trailing tolerance requires the marker to have OPENED a wrapper
      // (nonempty leading run): a run the marker did not open belongs to the
      // enclosing Markdown and must survive the strip.
      '`Use [OPTIONS: Alpha | Beta]`', // inline code span quoting a marker
      'Pick **[OPTIONS: Alpha | Beta]**', // mid-line emphasis-wrapped marker
      'x [OPTIONS: Alpha | Beta]**', // mid-line marker, trailing wrapper
      '[OPTIONS: Alpha | Beta]**', // trailing-only: nothing opened it, prose
    ]) {
      const text = `Done.\n${line}`
      expect(stripOptionMarkers(text), line).toBe(text)
    }
  })

  it('does not consume a multiline emphasis closer', () => {
    // Emphasis opened on a prior line, closed abutting the marker's closer:
    // the marker did not open that run, so nothing matches and the pair
    // survives intact.
    const text = '**Choose one\n[OPTIONS: Alpha | Beta]**'
    expect(stripOptionMarkers(text)).toBe(text)
  })

  it('never eats emphasis belonging to preceding prose', () => {
    // The leading wrapper is only legal at line start: here the `**` pair
    // closes real emphasis, and only the marker itself is stripped.
    expect(stripOptionMarkers('**Choose one:** [OPTIONS: A | B]'))
      .toBe('**Choose one:** ')
  })

  it('still parses a mid-line BARE marker (the pre-widening grammar)', () => {
    expect(stripOptionMarkers('Pick one [OPTIONS: A | B]')).toBe('Pick one ')
  })

  it('branch group pairs: (1,2) anchored, (3,4) mid-line — exactly one defined', () => {
    let last: RegExpMatchArray | null = null
    last = findLastOptionMarker('`[OPTION: Solo]`')
    expect(last![1]).toBeUndefined() // singular [OPTION:], anchored branch
    expect(last![2].trim()).toBe('Solo')
    expect(last![4]).toBeUndefined()
    last = null
    last = findLastOptionMarker('Pick one [OPTIONS: Solo]')
    expect(last![3]).toBe('S') // mid-line branch
    expect(last![4].trim()).toBe('Solo')
    expect(last![2]).toBeUndefined()
  })
})

describe('stripPartialOptionMarker with Markdown wrappers (#9110)', () => {
  it('never leaks marker syntax at any prefix of a wrapped reveal', () => {
    for (const full of [
      'Done.\n\n`[OPTIONS: Open the PR | Show me the diff]`',
      'Done.\n\n**[OPTIONS: Open the PR | Show me the diff]**',
    ]) {
      for (let n = 0; n <= full.length; n++) {
        expect(visible(full.slice(0, n))).not.toMatch(/\[OPTION/i)
      }
      expect(visible(full)).toBe('Done.')
    }
  })

  it('cuts a line-leading wrapper together with the streaming head', () => {
    expect(stripPartialOptionMarker('Done.\n**[OPTIONS: Merge it')).toBe('Done.')
    expect(stripPartialOptionMarker('Done.\n`[OPT')).toBe('Done.')
    expect(stripPartialOptionMarker('Done.\n  `[OPTIONS: A ] | Br')).toBe('Done.')
  })

  it('hides a mid-line head behind a wrapper but keeps the wrapper', () => {
    // A mid-line head after a wrapper can still complete as a BARE mid-line
    // marker (`Pick one **[OPTIONS: A]`), so it is held like any other head;
    // the wrapper itself is prose and stays, matching the completed grammar.
    expect(stripPartialOptionMarker('Pick one **[OPTIONS: A')).toBe('Pick one **')
    expect(stripPartialOptionMarker('Pick one **[OPT')).toBe('Pick one **')
  })

  it('leaves an in-word bracket behind a glued wrapper run alone', () => {
    for (const s of ['arr**[', 'x`[', 'a__[0']) {
      expect(stripPartialOptionMarker(s)).toBe(s)
    }
  })

  it('leaves a closed wrapped head followed by prose visible', () => {
    const s = 'Done.\n`[OPTIONS: A | B]` see note'
    expect(stripPartialOptionMarker(s)).toBe(s)
  })

  it('does not treat a 4+ run as a wrapper', () => {
    // Four backticks open a fence, not an inline span. The complete head is
    // still cut (a complete head is unambiguous), but the cut stops AT the
    // head — the run itself stays visible, exactly as the completed regex
    // would leave it.
    expect(stripPartialOptionMarker('Done.\n````[OPTIONS: A')).toBe('Done.\n````')
  })

  it('keeps prose stable once the wrapped marker starts arriving', () => {
    const prose = 'Renamed the hook and reran the suite.'
    const full = `${prose}\n\n**[OPTIONS: Open the PR | Skip it]**`
    // From the `[` onward the visible prose never rewinds nor regrows.
    const from = full.indexOf('[OPTIONS')
    for (let n = from + 1; n <= full.length; n++) {
      expect(visible(full.slice(0, n))).toBe(prose)
    }
  })
})
