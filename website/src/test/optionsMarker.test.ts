import { describe, it, expect } from 'vitest'
import { OPTION_MARKER_RE, stripPartialOptionMarker } from '../utils/optionsMarker'

/** What the reader sees for a given stream prefix: the finished-marker strip
 *  (OPTION_MARKER_RE, what parseOptions does) followed by the partial-marker
 *  strip. Mirrors AssistantMessage's streaming pipeline. */
const visible = (prefix: string) =>
  stripPartialOptionMarker(prefix.replace(OPTION_MARKER_RE, '').trim())

describe('stripPartialOptionMarker', () => {
  it('hides a marker whose closing bracket has not arrived yet', () => {
    expect(stripPartialOptionMarker('All done.\n\n[OPTIONS: Merge it now | Show me the d'))
      .toBe('All done.')
  })

  it('hides the head while it is still being typed, one char at a time', () => {
    for (const head of ['[', '[O', '[OP', '[OPT', '[OPTI', '[OPTIO', '[OPTION', '[OPTIONS', '[OPTIONS:', '[OPTION:']) {
      expect(stripPartialOptionMarker(`Done.\n\n${head}`)).toBe('Done.')
    }
  })

  it('is case-insensitive on the head, like OPTION_MARKER_RE', () => {
    expect(stripPartialOptionMarker('Done.\n[options: Merge it')).toBe('Done.')
    expect(stripPartialOptionMarker('Done.\n[Options: Merge it')).toBe('Done.')
  })

  // A half-typed head is ambiguous with prose: `[OPTION` can still become
  // `[OPTIONS:` or `[Optional]`. Casing is the only pre-colon signal, so the
  // mid-head probe demands a consistent casing and lets `[Optional]` go after
  // two characters. The colon resolves the ambiguity, and from there the probe
  // is case-insensitive again — so the mixed-case exposure never outlives the head.
  it('releases a mixed-case head early, and re-holds once its colon lands', () => {
    expect(stripPartialOptionMarker('Done.\n[O')).toBe('Done.')       // still ambiguous
    expect(stripPartialOptionMarker('Done.\n[Op')).toBe('Done.\n[Op') // released
    expect(stripPartialOptionMarker('Done.\n[Optional')).toBe('Done.\n[Optional')
    expect(stripPartialOptionMarker('Done.\n[Options:')).toBe('Done.')
  })

  // The canonical marker opens its own line, and the same-line variant the regex
  // also accepts still has a space before the `[`. Requiring that boundary takes
  // every in-word bracket out of scope without costing the marker anything.
  it('requires the bracket to open a line or follow whitespace', () => {
    expect(stripPartialOptionMarker('arr[')).toBe('arr[')
    expect(stripPartialOptionMarker('fn(x)[')).toBe('fn(x)[')
    expect(stripPartialOptionMarker('Pick one [OPTIONS: A')).toBe('Pick one')
    expect(stripPartialOptionMarker('[OPTIONS: A')).toBe('')
  })

  // Division of labour, not coverage of the probe: OPTION_MARKER_RE is what removes
  // a closed, line-final marker, for all four closers it accepts (ASCII plus the
  // CJK lookalikes). Stated explicitly because this case passes against a
  // `stripPartialOptionMarker` stub BY DESIGN — the probe has no closer-awareness
  // left to test. The two cases below are what lock in the new rule.
  it('removes a closed marker via OPTION_MARKER_RE (ASCII and CJK closers)', () => {
    for (const closer of [']', '\u3011', '\uFF3D', '\u3015']) {
      expect(visible(`Done.\n[OPTIONS: A | B${closer}`)).toBe('Done.')
    }
  })

  // The documented exception to "parseOptions already removed every complete
  // marker": OPTION_MARKER_RE's tempered body cannot match the earlier of two
  // markers on ONE line, so the first survives the strip and reaches the probe
  // complete. Hiding it mid-stream is still right — it is a marker, not prose.
  it('hides a same-line marker pair that survives parseOptions', () => {
    expect(visible('Pick:\n[OPTIONS: A] [OPTIONS: B]')).toBe('Pick:')
  })

  // A label may itself contain `]` — `[OPTIONS: Alpha ] | Bravo ]]` is a supported
  // shape (see the OPTION_MARKER_RE tests). Keying the cut on "has a closer
  // arrived?" would release the whole marker back into the prose at that inner
  // bracket, so the cut is unconditional once a head is present.
  it('keeps hiding a marker whose label contains a bracket', () => {
    const full = 'Pills:\n[OPTIONS: Alpha ] | Bravo ] | Charlie ]]'
    for (let n = 0; n <= full.length; n++) {
      expect(visible(full.slice(0, n))).not.toMatch(/\[OPTION/i)
    }
    expect(visible(full)).toBe('Pills:')
  })

  // The other side of that rule, and the reason it is not an unconditional cut:
  // a head whose marker closed and was followed by ordinary words is real prose.
  // OPTION_MARKER_RE declines to parse that shape, so nothing else would restore
  // it — hiding it would delete half a sentence for the rest of the turn.
  it('leaves a closed head followed by ordinary prose visible', () => {
    for (const s of [
      'Explain the literal [OPTIONS:] syntax here',
      'Done.\n[OPTIONS: A | B] see note',
      'The [OPTIONS: x] tag is the marker',
    ]) {
      expect(stripPartialOptionMarker(s)).toBe(s)
    }
  })

  // …and the discriminator itself: what follows the LAST closer decides. A
  // separator means the label list is still being written; words mean it ended.
  it('distinguishes a continuing label list from prose after the marker', () => {
    expect(stripPartialOptionMarker('P:\n[OPTIONS: Alpha ] | Bra')).toBe('P:')
    expect(stripPartialOptionMarker('P:\n[OPTIONS: Alpha ], Bra')).toBe('P:')
    expect(stripPartialOptionMarker('P:\n[OPTIONS: Alpha ] and then')).toBe('P:\n[OPTIONS: Alpha ] and then')
  })

  /** The whole point: no stream prefix may ever expose marker syntax. */
  it('never leaks marker syntax at any prefix of the reveal', () => {
    const full = 'Renamed the hook and reran the suite.\n\n[OPTIONS: Open the PR | Show me the diff | Skip it]'
    for (let n = 0; n <= full.length; n++) {
      expect(visible(full.slice(0, n))).not.toMatch(/\[OPTION/i)
    }
    // …and the prose is stable across the marker's arrival, never rewinding.
    const prose = 'Renamed the hook and reran the suite.'
    for (let n = prose.length; n <= full.length; n++) {
      expect(visible(full.slice(0, n))).toBe(prose)
    }
  })

  it('leaves text that merely contains a bracket alone', () => {
    for (const s of ['- [ ] a task', 'see [the docs](https://x)', 'arr[0] = 1', 'a [note] here']) {
      expect(stripPartialOptionMarker(s)).toBe(s)
    }
  })

  it('only looks at the tail — an earlier line is untouched', () => {
    expect(stripPartialOptionMarker('I mentioned [OPTIONS: legacy\nand then moved on'))
      .toBe('I mentioned [OPTIONS: legacy\nand then moved on')
  })

  it('returns text unchanged when there is no bracket at all', () => {
    expect(stripPartialOptionMarker('nothing to see')).toBe('nothing to see')
  })

  // Cost guard: this runs synchronously on every streaming frame, so the scan is
  // bounded to the last TAIL_SCAN (4096) chars rather than the whole buffer. The
  // observable consequence of that bound, asserted deterministically: a head that
  // has fallen further than the window behind the live edge is out of reach.
  it('bounds the probe to the tail window', () => {
    const near = `x\n[OPTIONS: ${'a'.repeat(4000)}`
    expect(stripPartialOptionMarker(near)).toBe('x')
    const far = `x\n[OPTIONS: ${'a'.repeat(5000)}`
    expect(stripPartialOptionMarker(far)).toBe(far)
  })

  it('stays cheap on a large buffer and on adversarial input', () => {
    const big = `${'prose prose prose\n'.repeat(60000)}[OPTIONS: A | B`
    const evil = '[OPTIONS:'.repeat(20000)
    // A newline-free buffer: if the `\n` search were not clipped to the window it
    // would scan all 4 MiB on every frame, which is the shape this bound exists for.
    const flat = `${'x'.repeat(4_000_000)} [OPTIONS: A | B`
    const start = Date.now()
    for (let i = 0; i < 50; i++) {
      stripPartialOptionMarker(big)
      stripPartialOptionMarker(evil)
      stripPartialOptionMarker(flat)
    }
    expect(Date.now() - start).toBeLessThan(2000)
  })
})
