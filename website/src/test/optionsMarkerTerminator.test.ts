import { describe, expect, it } from 'vitest'

import { OPTION_MARKER_RE } from '../app-sdk/protocol/optionMarker'
import { parseOptions } from '../app-sdk/protocol/options'

/** Labels of the LAST marker, or null when the grammar declines the line. */
function labelsOf(text: string): string | null {
  const m = [...text.matchAll(new RegExp(OPTION_MARKER_RE))].pop()
  return m ? (m[2] ?? m[4]) : null
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
 * A bare opener may not be the one whose partner closer ENDS the marker.
 *
 * The bare-`[` alternative exists so a stray opener does not sink a whole marker
 * (`[OPTIONS: Fix [x logging | Skip]` parses, and is pinned). But it also admitted
 * the opener in a marker the model never closed:
 *
 *   [OPTIONS: A | B then check arr[0]
 *
 * The only closer on that line belongs to `arr[0]`, so the body ran on through the
 * prose, that `]` became the terminator, and since the marker is removed by
 * `replace` the whole line vanished from the message and came back as the pill
 * label `B then check arr[0`.
 *
 * No rule over bracket structure can separate the two: reduced to skeletons they
 * are the same string. The discriminator is where the opener sits relative to the
 * END — the bare form is refused when nothing but ordinary text lies between it and
 * a closer at the end anchor. A separator clears the gate (the opener is inside a
 * label), and so does another bracket (some other form owns that closer).
 */
describe('an unterminated [OPTIONS: no longer consumes its line', () => {
  it.each([
    '[OPTIONS: A | B then check arr[0]',
    '[OPTIONS: Ship | Hold and read docs[4]',
    '[OPTIONS: Merge | Wait then diff src/app[3]',
    // The wrapper and stray-tic forms reach the same terminator, so the gate has to
    // account for both or the shape returns wearing one.
    '**[OPTIONS: A | B then check arr[0]**',
    '[OPTIONS: A | B then check arr[0](OPTIONS)',
    // A COMMA INSIDE the terminal bracket. The scan has to cross it to reach the
    // partner closer — a comma is only the fallback separator and inside brackets is
    // ordinary punctuation, so a scan that stopped there cleared the gate.
    '[OPTIONS: A | B then inspect dict[str, int]',
    '[OPTIONS: Ship | Hold then arr[i, j]',
  ])('declines it and deletes nothing: %s', (text) => {
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

  it('still declines a marker with no closer anywhere', () => {
    expect(labelsOf('[OPTIONS: A | B then check arr')).toBeNull()
  })
})

describe('the discriminator is not bracket structure', () => {
  it('the refused and accepted shapes share a skeleton', () => {
    // Pinned as the reason the gate is shaped this way: if someone replaces it with
    // a bracket-balance rule, this is what says that cannot work.
    expect(skeleton('[OPTIONS: A | B then check arr[0]')).toBe('w|w[w]')
    expect(skeleton('[OPTIONS: Fix | Skip [x logging]')).toBe('w|w[w]')
  })

  it('and the pinned stray-opener shape is the one a separator saves', () => {
    expect(skeleton('[OPTIONS: Fix [x logging | Skip]')).toBe('w[w|w]')
    expect(parseOptions('[OPTIONS: Fix [x logging | Skip]').options).toEqual([
      'Fix [x logging',
      'Skip',
    ])
  })
})

describe('the terminator gate leaves the rest of the grammar where it was', () => {
  it.each([
    // A closer admitted by CONTINUATION, so its opener is not the terminator's
    // partner. The gate must not reach these.
    ['[OPTIONS: Fix arr[0] | Skip]', ['Fix arr[0]', 'Skip']],
    ['[OPTIONS: a[1] | b[2]]', ['a[1]', 'b[2]']],
    ['[OPTIONS: Fix list[dict[str, Any]] | Skip]', ['Fix list[dict[str, Any]]', 'Skip']],
    // Matched pairs own their own closer.
    ['[OPTIONS: Fix [x] logging | Skip]', ['Fix [x] logging', 'Skip']],
    ['[OPTIONS: Read arr[0] now | Skip it]', ['Read arr[0] now', 'Skip it']],
    ['[OPTIONS: See [1] above | Skip]', ['See [1] above', 'Skip']],
    ['[OPTIONS: Fix dict[str, Any] now | Skip]', ['Fix dict[str, Any] now', 'Skip']],
    // No opener at all, so no gate applies.
    ['[OPTIONS: Alpha ] | Bravo ]]', ['Alpha ]', 'Bravo ]']],
    ['[OPTIONS: Yes | No]', ['Yes', 'No']],
    ['**[OPTIONS: Yes | No]**', ['Yes', 'No']],
    ['[OPTIONS: A | B](OPTIONS)', ['A', 'B']],
    // Prose ending in a closer with no opener: that closer genuinely IS the
    // marker's, so this parses as before.
    ['[OPTIONS: A | B then check arr]', ['A', 'B then check arr']],
  ])('still parses %s', (text, options) => {
    expect(parseOptions(text as string).options).toEqual(options)
  })

  it.each([
    'Use [OPTIONS: A | B] then check arr[0]',
    '[OPTIONS: Fix ]x logging | Skip]',
    '[OPTIONS: Fix list[dict[str, Any]] now | S]',
    'Note [OPTIONS: see [OPTIONS: x] below | Skip]',
  ])('still declines %s', (text) => {
    expect(labelsOf(text)).toBeNull()
  })
})

describe('what the gate gives up', () => {
  it.each([
    '[OPTIONS: Fix | Skip [x logging]',
    '[OPTIONS: Fix [x logging]',
    // The comma variant joins the same family: only a comma follows the stray
    // opener, and a comma no longer clears the gate. That is the price of seeing
    // through `dict[str, int]` to its closer.
    '[OPTIONS: Fix [x logging, Skip]',
  ])(
    'a stray opener in the FINAL label, with nothing deleted: %s',
    (text) => {
      // The one family given up: no separator follows to clear the gate. It fails
      // toward a VISIBLE marker, which is the direction every cost in this grammar
      // fails in, and is what makes it affordable.
      expect(labelsOf(text)).toBeNull()
      expect(parseOptions(text).text).toBe(text)
    },
  )
})

describe('the gate stays linear', () => {
  it('does not backtrack catastrophically on many failing openers', () => {
    // The adversarial shape for this gate specifically: one lookahead entered per
    // opener, on an unterminated marker so every one of them fails.
    const src = `[OPTIONS:${'a[b'.repeat(20_000)}`
    const started = Date.now()
    expect(labelsOf(src)).toBeNull()
    expect(Date.now() - started).toBeLessThan(1000)
  })
})
