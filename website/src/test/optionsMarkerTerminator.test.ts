import { describe, expect, it, vi } from 'vitest'

import {
  findLastOptionMarker,
  findOptionMarkers,
  stripOptionMarkers,
  labelsHaveUnmatchedOpener,
  OPTION_MARKER_PATTERN_SOURCE,
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

describe('a same-line chain whose SIBLING does not balance', () => {
  /**
   * REGRESSION introduced by `SIBLING_CHAIN`: measured on 59baad44ab these shapes deleted
   * nothing, because the tail required end-of-line and the leading marker could not match.
   *
   * The lookahead admits a sibling through the LOOSE tempered body while acceptance re-checks
   * each marker's OWN balance, so the first marker was stripped and the second left as prose —
   * the deleted span came back as a chip nobody wrote, which is the one failure direction this
   * grammar refuses everywhere else. All-or-nothing is therefore decided per LINE.
   */
  const CHAINS: [string, string][] = [
    ['content pair', '[OPTIONS: A] [OPTIONS: B then arr[0]'],
    ['three, last malformed', '[OPTIONS: A] [OPTIONS: B] [OPTIONS: C then arr[0]'],
  ]

  it('offers nothing and deletes no prose', () => {
    for (const [name, raw] of CHAINS) {
      expect(findOptionMarkers(raw), name).toEqual([])
      expect(parseOptions(raw).text, name).toBe(raw)
      expect(stripOptionMarkers(raw), name).toBe(raw)
    }
  })

  it('NEGATIVE CONTROL: a BALANCED chain still parses, the last marker winning', () => {
    // Differs from the first row only by the `]` closing `arr[0`, so over-rejection fails here.
    const ok = '[OPTIONS: A] [OPTIONS: B then arr[0]]'
    expect(parseOptions(ok).options).toEqual(['B then arr[0]'])
    expect(parseOptions(ok).text).toBe('')
  })

  it('NEGATIVE CONTROL: a broken chain does not poison a marker on ANOTHER line', () => {
    const two = '[OPTIONS: A] [OPTIONS: B then arr[0]\n[OPTIONS: C]'
    expect(parseOptions(two).options).toEqual(['C'])
    expect(parseOptions(two).text).toBe('[OPTIONS: A] [OPTIONS: B then arr[0]')
  })
})

describe('two markers on ONE line', () => {
  it('strips both, and the LAST supplies the options', () => {
    const { options, text } = parseOptions('Body [OPTIONS: A] [OPTIONS: B]')
    expect(options).toEqual(['B'])
    expect(text).toBe('Body')
  })

  it('still refuses a marker followed by ordinary prose on the same line', () => {
    const raw = 'See [OPTIONS: A | B] for details'
    expect(parseOptions(raw)).toEqual({ text: raw, options: [], multi: true })
  })

  it.each([
    ['two markers', '[OPTIONS: A] [OPTIONS: B] in docs'],
    ['three markers', '[OPTIONS: A] [OPTIONS: B] [OPTIONS: C] see above'],
    ['a non-marker bracket after it', '[OPTIONS: A | B] [OPTION-ACTIONS: close=Done]'],
  ])('a chain that does not reach the line end parses NOTHING — %s', (_name, raw) => {
    // All-or-nothing: the first marker must not be deleted while the rest stays prose.
    expect(parseOptions(raw).options, raw).toEqual([])
    expect(parseOptions(raw).text, raw).toBe(raw)
  })

  it('looks only ONE head ahead, so a long line costs linear work', () => {
    // A lookahead that re-tiled the rest of the line ran once per candidate: markers times
    // line length on one untrusted line. The tiling is decided after matching instead.
    expect(OPTION_MARKER_PATTERN_SOURCE).not.toMatch(/\(\?=\(\?:\\\[/)
    expect(OPTION_MARKER_PATTERN_SOURCE).toContain('(?=\\[(?:OPTIONS?:))')
  })

  it('leaves a fenced example of a same-line pair as written, offering no pills', () => {
    const raw = 'Emit it like this:\n```\n[OPTIONS: A | B] [OPTION-ACTIONS: close=Done]\n```'
    expect(parseOptions(raw).options).toEqual([])
    expect(stripOptionMarkers(raw)).toBe(raw)
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

  it('refuses an over-cap same-line chain WHOLE, without running a marker pattern', () => {
    const unit = '[OPTIONS: A] '
    // `parseOptions` cannot witness this: its probes are guarded, so it skips the strip entirely.
    // The cost is counted as work: over the cap, the head count is the only scan that runs.
    const scanned = (src: string): Set<string> => {
      const sources = new Set<string>()
      const realMatchAll = String.prototype.matchAll
      const realReplace = String.prototype.replace
      const matchAll = vi.spyOn(String.prototype, 'matchAll').mockImplementation(function (this: string, re: RegExp) {
        sources.add(re.source)
        return realMatchAll.call(this, re)
      })
      const replace = vi.spyOn(String.prototype, 'replace').mockImplementation(function (this: string, re: string | RegExp, by: never) {
        if (re instanceof RegExp) sources.add(re.source)
        return realReplace.call(this, re, by)
      })
      try {
        stripOptionMarkers(src)
      } finally {
        matchAll.mockRestore()
        replace.mockRestore()
      }
      return sources
    }
    const over = unit.repeat(257).trimEnd()
    expect(stripOptionMarkers(over)).toBe(over)
    expect(scanned(over).size, 'only the head count runs over the cap').toBe(1)
    // Under the cap the marker pattern does run, so the count above can tell the two apart.
    expect(scanned(unit.repeat(2).trimEnd()).size).toBeGreaterThan(1)
  })
})
