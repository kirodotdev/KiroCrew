import { describe, expect, it } from 'vitest'

import {
  findLastOptionMarker,
  findOptionMarkers,
  stripOptionMarkers,
  labelsHaveUnmatchedOpener,
} from '../app-sdk/protocol/optionMarker'
import { parseOptions } from '../app-sdk/protocol/options'

/** Labels of the LAST accepted marker, or null when none is accepted. */
function labelsOf(text: string): string | null {
  const m = findLastOptionMarker(text)
  return m ? ((m[2] ?? m[4]) ?? '') : null
}

/** Bracket/separator structure, every other run collapsed to `w`. */
function skeleton(text: string): string {
  const body = text.startsWith('[OPTIONS:') ? text.slice('[OPTIONS:'.length) : text
  const out: string[] = []
  for (const ch of body) {
    if ('[]|,】］〕'.includes(ch)) out.push(ch)
    else if (out.length === 0 || out[out.length - 1] !== 'w') out.push('w')
  }
  return out.join('')
}

/**
 * The terminating closer must not be an unmatched opener's PARTNER.
 *
 * The body admits a bare `[` on purpose, so a stray opener in a label does not sink
 * the marker (`[OPTIONS: Fix [x logging | Skip]` parses, and is pinned). That same
 * alternative admitted the opener in a marker the model never closed:
 *
 *   [OPTIONS: A | B then check arr[0]
 *
 * The only closer on that line belongs to `arr[0]`, so the body ran on through the
 * prose, that `]` became the terminator, and since the marker is removed by `replace`
 * the line left the message and came back as the pill label `B then check arr[0`.
 *
 * The pattern cannot decide this. Reduced to their bracket structure the shape to
 * REFUSE and the shape to ACCEPT are the same string, and a lookahead sees only one
 * nesting level — `list[dict[str, int]]` defeats a one-level rule, `a[b[c[d]]]` a
 * two-level one. The condition is bracket BALANCE, so `labelsHaveUnmatchedOpener` decides
 * it and the pattern is module-private to stop the two being applied separately.
 */
describe('an unterminated [OPTIONS: does not consume its line', () => {
  it.each([
    '[OPTIONS: A | B then check arr[0]',
    '[OPTIONS: Ship | Hold and read docs[4]',
    '[OPTIONS: Merge | Wait then diff src/app[3]',
    // The wrapper and stray-tic forms reach the same terminator.
    '**[OPTIONS: A | B then check arr[0]**',
    '[OPTIONS: A | B then check arr[0](OPTIONS)',
    // A comma inside the terminal bracket is ordinary punctuation, not a boundary.
    '[OPTIONS: A | B then inspect dict[str, int]',
    '[OPTIONS: Ship | Hold then arr[i, j]',
    // NESTED terminal brackets, at the depths a pattern-level rule could not reach.
    '[OPTIONS: A | B then inspect list[dict[str, int]]',
    '[OPTIONS: A | B then inspect a[b[c[d]]]',
    '[OPTIONS: A | B see a[b[c[d[e]]]]',
  ])('refuses it and deletes nothing: %s', (text) => {
    expect(labelsOf(text)).toBeNull()
    const { options, text: kept } = parseOptions(text)
    expect(options).toEqual([])
    expect(kept).toBe(text)
  })

  it('names the corruption it prevents, so the reason cannot be optimised away', () => {
    expect(parseOptions('[OPTIONS: A | B then check arr[0]').options).not.toEqual([
      'A',
      'B then check arr[0',
    ])
  })

  it('still refuses a marker with no closer anywhere', () => {
    expect(labelsOf('[OPTIONS: A | B then check arr')).toBeNull()
  })

  it('refuses the separator-tail form rather than truncating it', () => {
    // `], ` genuinely continues the list, so no guard at the INTERNAL closer can tell
    // this apart. Counting from the other end decides it.
    const text = 'Done. [OPTIONS: Merge | Wait], details in CHANGELOG[1]'
    expect(labelsOf(text)).toBeNull()
    expect(parseOptions(text).text).toBe(text)
  })
})

describe('the decision cannot live in the pattern', () => {
  it('the refused and accepted shapes share a skeleton', () => {
    expect(skeleton('[OPTIONS: A | B then check arr[0]')).toBe('w|w[w]')
    expect(skeleton('[OPTIONS: Fix | Skip [x logging]')).toBe('w|w[w]')
  })

  it('and a `|` after the opener cannot save it either', () => {
    // Why the rule is TOTAL rather than carrying an escape hatch. The hatch was "an
    // unmatched opener is label text if a `|` follows it", and a `|` INSIDE the
    // unmatched bracket satisfies that — so `dict[str | int]` at the end of a line
    // readmitted the whole defect. The shape it protected and the shape it readmitted
    // differ only in where a `|` sits relative to an opener that has no closer.
    for (const text of [
      '[OPTIONS: Fix [x logging | Skip]', // the hatch protected this
      '[OPTIONS: A | B then inspect dict[str | int]', // ...and readmitted this
    ]) {
      expect(labelsOf(text), text).toBeNull()
      expect(parseOptions(text).text, text).toBe(text)
    }
  })
})

describe('labelsHaveUnmatchedOpener, directly', () => {
  it.each([
    [' A | B then check arr[0', true],
    [' A | B then inspect list[dict[str, int]', true],
    [' Merge | Wait], details in CHANGELOG[1', true],
    [' A | B then inspect dict[str | int', true], // a `|` inside the bracket
    [' Fix [x logging | Skip', true], // a `|` after it changes nothing
    [' Fix arr[0] | Skip', false], // balanced
    [' a[1] | b[2]', false], // balanced
    [' Alpha ] | Bravo ]', false], // unmatched CLOSERS say nothing
    [' Yes | No', false], // no brackets
    [' A | B then check arr', false], // no opener
    [' 见【表1】说明 | 跳过', false], // a lookalike closer closes nothing here
  ])('%s -> %s', (labels, nested) => {
    expect(labelsHaveUnmatchedOpener(labels as string)).toBe(nested)
  })

  it('mirrors the backend rule, which the cross-surface differential checks', () => {
    // Both surfaces must agree or a marker becomes buttons on one and text on the
    // other; the shared cases above are the ones the differential compares.
    expect(labelsHaveUnmatchedOpener(' A | B then see a[b[c[d]]')).toBe(true)
  })
})

describe('the check leaves the rest of the grammar where it was', () => {
  it.each([
    // A closer admitted by CONTINUATION, so its opener is closed by the labels' end.
    ['[OPTIONS: Fix arr[0] | Skip]', ['Fix arr[0]', 'Skip']],
    ['[OPTIONS: a[1] | b[2]]', ['a[1]', 'b[2]']],
    ['[OPTIONS: Fix list[dict[str, Any]] | Skip]', ['Fix list[dict[str, Any]]', 'Skip']],
    // Matched pairs own their own closer.
    ['[OPTIONS: Fix [x] logging | Skip]', ['Fix [x] logging', 'Skip']],
    ['[OPTIONS: Read arr[0] now | Skip it]', ['Read arr[0] now', 'Skip it']],
    ['[OPTIONS: See [1] above | Skip]', ['See [1] above', 'Skip']],
    ['[OPTIONS: Fix dict[str, Any] now | Skip]', ['Fix dict[str, Any] now', 'Skip']],
    // No opener at all, so nothing to be unbalanced.
    ['[OPTIONS: Alpha ] | Bravo ]]', ['Alpha ]', 'Bravo ]']],
    ['[OPTIONS: Yes | No]', ['Yes', 'No']],
    ['**[OPTIONS: Yes | No]**', ['Yes', 'No']],
    ['[OPTIONS: A | B](OPTIONS)', ['A', 'B']],
    // Prose ending in a closer with no opener: that closer genuinely IS the marker's.
    ['[OPTIONS: A | B then check arr]', ['A', 'B then check arr']],
  ])('still parses %s', (text, options) => {
    expect(parseOptions(text as string).options).toEqual(options)
  })

  it.each([
    'Use [OPTIONS: A | B] then check arr[0]',
    '[OPTIONS: Fix ]x logging | Skip]',
    '[OPTIONS: Fix list[dict[str, Any]] now | S]',
    'Note [OPTIONS: see [OPTIONS: x] below | Skip]',
  ])('still refuses %s', (text) => {
    expect(labelsOf(text)).toBeNull()
  })

  it('strips accepted markers and leaves refused candidates in place', () => {
    expect(stripOptionMarkers('Done.\n[OPTIONS: A | B]')).toBe('Done.\n')
    const refused = 'Done.\n[OPTIONS: A | B then arr[0]'
    expect(stripOptionMarkers(refused)).toBe(refused)
  })

  it('finds every accepted marker in order', () => {
    const found = findOptionMarkers('[OPTIONS: A | B]\n[OPTIONS: C | D]')
    expect(found.map((m) => (m[2] ?? m[4]) ?? '')).toEqual([' A | B', ' C | D'])
  })

  it('does not let a refused candidate hide a later accepted one', () => {
    // The body refuses a nested `[OPTION(S):`, so a candidate never contains another
    // head — which is what makes filtering candidates safe.
    const found = findOptionMarkers('[OPTIONS: A then arr[0]\n[OPTIONS: C | D]')
    expect(found.map((m) => (m[2] ?? m[4]) ?? '')).toEqual([' C | D'])
  })
})

describe('what the check gives up', () => {
  it.each([
    '[OPTIONS: Fix | Skip [x logging]',
    '[OPTIONS: Fix [x logging]',
    // Only a comma follows the stray opener, and a comma is not enough.
    '[OPTIONS: Fix [x logging, Skip]',
  ])('a stray opener in the FINAL label, with nothing deleted: %s', (text) => {
    expect(labelsOf(text)).toBeNull()
    expect(parseOptions(text).text).toBe(text)
  })
})

describe('cost', () => {
  it('stays linear on many failing openers', () => {
    // The adversarial shape: an unterminated marker of bare openers, so the pattern
    // scans it all and the check walks it all.
    const src = `[OPTIONS:${'a[b'.repeat(20_000)}`
    const started = Date.now()
    expect(labelsOf(src)).toBeNull()
    expect(Date.now() - started).toBeLessThan(1000)
  })

  it('walks a long label run in one pass', () => {
    const started = Date.now()
    expect(labelsHaveUnmatchedOpener('a['.repeat(200_000))).toBe(true)
    expect(Date.now() - started).toBeLessThan(1000)
  })
})
