import { describe, it, expect } from 'vitest'
import { parseOptions } from '../app-sdk/protocol'
// The pattern is in-tree only — the barrel deliberately withholds it from the app surface.
import { labelsHaveUnmatchedOpener, stripOptionMarkers } from '../app-sdk/protocol/optionMarker'

// #9284: a label may legitimately carry a closer (`[OPTIONS: Alpha ] | Bravo ]]` is
// a supported, tested shape), so the body has to admit one — but admitting it
// UNCONDITIONALLY made the body run to the LAST closer in range instead of the first
// plausible one. An ordinary final line mentioning a bracket after the marker then
// matched across BOTH, and since the marker is removed by `replace`, the sentence
// vanished from the message and came back as a pill label.
//
// So a closer stays inside a label only where it is MATCHED by an earlier `[`, or
// where a separator or another closer follows it. Neither condition alone separates
// the three shapes that matter — continuation alone breaks `Fix [x] logging`, which
// the backend pins as supported; matching alone breaks `Alpha ] | Bravo ]]`. The
// second half is the rule `CONTINUES_LABELS_RE` already applied to the STREAMING
// probe.
//
// Every row in `overreach` matches on origin/main at 56f67aa43 (post-#9174) and
// deletes the prose shown. Two claims are asserted separately throughout, because
// they are different and only the second is what the user experiences: that the
// grammar does not MATCH, and that the visible text is UNCHANGED.
describe('OPTION_MARKER_RE label closers must be matched or continue the list (#9284)', () => {
  const overreach = [
    'Use [OPTIONS: A | B] then check arr[0]',
    'Pick [OPTIONS: A | B] and the type is dict[str, Any]',
    'All set [OPTIONS: Ship | Hold] before you diff src/app[0]',
    'Ready [OPTIONS: Yes | No] see the note in docs[2]',
    // The wrapped forms of the same shape, on #9174's leading-wrapper path.
    '`[OPTIONS: A | B] then check arr[0]`',
    '**[OPTIONS: Merge | Wait] then read CHANGELOG[1]**',
  ]

  it('declines a closer followed by ordinary words', () => {
    for (const text of overreach) {
      expect(parseOptions(text).options, text).toEqual([])
    }
  })

  it('and therefore deletes no prose', () => {
    for (const text of overreach) {
      expect(stripOptionMarkers(text), text).toBe(text)
    }
  })

  it('states the rule positively — a separator or another closer keeps it in', () => {
    expect(parseOptions('[OPTIONS: Alpha ] | Bravo ]]').options).toEqual(['Alpha ]', 'Bravo ]'])
    expect(parseOptions('[OPTIONS: Alpha ], Bravo]').options).toEqual(['Alpha ]', 'Bravo'])
  })

  it('applies the rule to every lookalike closer, not just ASCII', () => {
    for (const close of [']', '】', '］', '〕']) {
      const text = `[OPTIONS: Alpha ${close} | Bravo]`
      expect(parseOptions(text).options, text).toEqual([`Alpha ${close}`, 'Bravo'])
    }
  })

  it('leaves a bracket that ENDS a label alone — via the continuation half', () => {
    // The pair half is excluded here by its own trailing lookahead, precisely
    // because a `|` follows — which is what keeps the two disjoint, and is why
    // neither alternative is redundant: delete the continuation half and this
    // pinned shape regresses.
    expect(parseOptions('[OPTIONS: Fix arr[0] | Skip]').options).toEqual(['Fix arr[0]', 'Skip'])
  })

  it('keeps a MATCHED pair mid-label with words after it', () => {
    // Continuation alone would have broken these, and they are why the rule is a
    // union: the backend pins `Fix [x] logging` as a supported shape, and there
    // the closer is followed by an ordinary word rather than a separator.
    expect(parseOptions('[OPTIONS: Fix [x] logging | Skip]').options).toEqual([
      'Fix [x] logging',
      'Skip',
    ])
    expect(parseOptions('[OPTIONS: Fix arr[0] now | Skip]').options).toEqual([
      'Fix arr[0] now',
      'Skip',
    ])
  })

  it('refuses a stray opener, which is the one shape balanced labels cost', () => {
    // A stray `[` with no closer of its own used to be just a character in the label.
    // It cannot be: the shape is indistinguishable from a marker the model never
    // closed — in `[OPTIONS: A | B then check arr[0]` the only closer belongs to
    // `arr[0]`, and the body would run through the prose to reach it. Both have one
    // unmatched opener and a closer at the end anchor, so accepting either accepts
    // both, and accepting the second deletes a line. The marker now renders as
    // visible text; see `labelsHaveUnmatchedOpener` for the full argument.
    const text = '[OPTIONS: Fix [x logging | Skip]'
    expect(parseOptions(text).options).toEqual([])
    expect(parseOptions(text).text).toBe(text)
  })

  // Every shape the union rule gives up, enumerated rather than summarised. They
  // share one form — a closer that satisfies NEITHER half, with ordinary words
  // after it — but there is more than one way to be that closer, and all of them
  // parsed on the old body. Each fails toward a VISIBLE marker, and the assertion
  // on `.text` is what says so: nothing is deleted.
  it('accepts an UNMATCHED closer with words after it as the cost', () => {
    const text = '[OPTIONS: Fix ]x logging | Skip]'
    expect(parseOptions(text).options).toEqual([])
    expect(parseOptions(text).text).toBe(text)
  })

  it('accepts nesting deeper than one level as a cost too', () => {
    // The pair form is ONE level deep, so a closer whose nearest preceding `[` is
    // separated from it by another bracket has no pair parse either. Depth-general
    // matching is not something a regex can do; the boundary is named here rather
    // than left for a reader to discover.
    for (const text of [
      '[OPTIONS: Fix list[dict[str, Any]] now | Skip]',
      '[OPTIONS: Update arr[i[0]] then rerun | Skip]',
    ]) {
      expect(parseOptions(text).options, text).toEqual([])
      expect(parseOptions(text).text, text).toBe(text)
    }
    // ...and the same nesting with a SEPARATOR after it still parses, because then
    // the continuation half admits it. The cost is the tail, not the depth.
    expect(parseOptions('[OPTIONS: Fix list[dict[str, Any]] | Skip]').options).toEqual([
      'Fix list[dict[str, Any]]',
      'Skip',
    ])
  })

  it('parses a MATCHED lookalike pair like an ASCII `[...]` pair (#9375)', () => {
    // `MARKER_OPENERS` pairs each lookalike opener with its closer, so a matched
    // `【…】` / `［…］` / `〔…〕` inside a label renders buttons exactly as `[…]` does.
    for (const [open, close] of [
      ['【', '】'],
      ['［', '］'],
      ['〔', '〕'],
    ]) {
      const text = `[OPTIONS: Fix ${open}x${close} logging | Skip]`
      expect(parseOptions(text).options, text).toEqual([`Fix ${open}x${close} logging`, 'Skip'])
    }
    // A label may be entirely a lookalike pair, too. Common in Chinese output.
    expect(parseOptions('[OPTIONS: 【重要】修复 | 跳过】').options).toEqual(['【重要】修复', '跳过'])
  })

  it('declines a MISMATCHED lookalike pair as a cost (#9375)', () => {
    // `【` pairs with `】`, never with `]`, so `【 … ]` has no pair parse; the `]`
    // reads as unmatched and the marker declines rather than deleting prose.
    for (const text of [
      '[OPTIONS: 见【表1] 说明 | 跳过]',
      '[OPTIONS: Fix [x】 logging | Skip]',
      '[OPTIONS: Fix ［x〕 logging | Skip]',
    ]) {
      expect(parseOptions(text).options, text).toEqual([])
      expect(parseOptions(text).text, text).toBe(text)
    }
  })

  it('declines a mismatched pair even when its closer ends the label', () => {
    // The balance check pairs openers by TYPE. Before, every closer decremented one
    // shared count, so `【x]` read as closed and this candidate rendered buttons
    // from a label with a half-open lookalike pair. The `【` is still open at the
    // terminator — the bare-opener shape — so it declines and no prose moves.
    for (const text of [
      '[OPTIONS: A 【x] | B]',
      '[OPTIONS: A [x】 | B]',
      '[OPTIONS: A ［x〕 | B]',
      '[OPTIONS: A 【x] | B】',
    ]) {
      expect(labelsHaveUnmatchedOpener(text.slice('[OPTIONS:'.length, -1)), text).toBe(true)
      expect(parseOptions(text).options, text).toEqual([])
      expect(parseOptions(text).text, text).toBe(text)
    }
    // Typed pairing still accepts nesting across kinds and the unmatched closer.
    expect(parseOptions('[OPTIONS: A [x【y】] | B]').options).toEqual(['A [x【y】]', 'B'])
    expect(parseOptions('[OPTIONS: Alpha ] | Bravo ]]').options).toEqual(['Alpha ]', 'Bravo ]'])
  })

  it('still parses a citation like ref[1] in a label (#9375)', () => {
    expect(parseOptions('[OPTIONS: see ref[1] | Skip]').options).toEqual(['see ref[1]', 'Skip'])
  })

  it('still declines the #10058 bare-opener terminator', () => {
    const text = '[OPTIONS: A | B then check arr[0]'
    expect(parseOptions(text).options).toEqual([])
    expect(parseOptions(text).text).toBe(text)
  })

  it('declines a bare LOOKALIKE opener before the terminator the same way', () => {
    // A matched `【重要】` earlier in the label is fine; the bare `【` before the
    // final `】` is the opener whose partner would end the marker. The balance
    // gate counts every opener the grammar knows, so the line stays whole.
    for (const [o, c] of [['\u3010', '\u3011'], ['\uFF3B', '\uFF3D'], ['\u3014', '\u3015']]) {
      const text = `[OPTIONS: ${o}重要${c}修复 | B 详见${o}0${c}`
      expect(parseOptions(text).options, text).toEqual([])
      expect(parseOptions(text).text, text).toBe(text)
    }
  })

  it('never swallows a NESTED head into a label', () => {
    // The one place this rule could have been LOOSER than the body it replaced:
    // without `(?!OPTIONS?:)` on the pair form's opener, the pair alternative opens
    // on a nested head and pairs it with that head's own closer, so the OUTER head
    // matches and the pill's label is a raw protocol marker — echoed back as the
    // user's reply when tapped. The old body matched nothing here, nor does this one.
    const text = 'Note [OPTIONS: see [OPTIONS: x] below | Skip]'
    expect(parseOptions(text).options).toEqual([])
    expect(parseOptions(text).text).toBe(text)
  })

  it('declines the separator-tail form rather than truncating it', () => {
    // Not reachable by THIS rule: `], ` DOES continue the label list, by the very
    // rule that makes `[OPTIONS: Alpha ], Bravo]` legal, so no guard applied at the
    // internal closer can tell them apart. The terminator gate reaches it from the
    // other end — the `[` of `CHANGELOG[1]` is the opener whose partner would end
    // the marker — so the line stays whole instead of losing its tail to a pill
    // label reading `Wait], details in CHANGELOG[1`.
    const text = 'Done. [OPTIONS: Merge | Wait], details in CHANGELOG[1]'
    expect(parseOptions(text).options).toEqual([])
    expect(parseOptions(text).text).toBe(text)
  })
})

describe('the #9284 temper leaves the rest of the grammar where it was', () => {
  it('still parses the plain marker', () => {
    expect(parseOptions('Done.\n\n[OPTIONS: Merge | Wait]').options).toEqual(['Merge', 'Wait'])
    expect(parseOptions('Done. [OPTIONS: Merge | Wait]').options).toEqual(['Merge', 'Wait'])
  })

  it('still parses every wrapper #9174 added', () => {
    for (const wrap of ['`', '``', '```', '*', '**', '_', '__', '***']) {
      const text = `Done.\n${wrap}[OPTIONS: Merge | Wait]${wrap}`
      expect(parseOptions(text).options, text).toEqual(['Merge', 'Wait'])
      expect(parseOptions(text).text, text).toBe('Done.')
    }
  })

  it('composes the two rules — a wrapped marker with a continuing closer parses', () => {
    expect(parseOptions('`[OPTIONS: Alpha ] | Bravo]`').options).toEqual(['Alpha ]', 'Bravo'])
  })

  it('still keeps [OPTION:] single-select', () => {
    expect(parseOptions('[OPTION: Ship | Hold]').multi).toBe(false)
    expect(parseOptions('`[OPTION: Ship | Hold]`').multi).toBe(false)
  })

  it('still never eats prose emphasis before the marker', () => {
    const text = '**Choose:** [OPTIONS: Merge | Wait]'
    expect(parseOptions(text).options).toEqual(['Merge', 'Wait'])
    expect(parseOptions(text).text).toBe('**Choose:**')
  })

  it('still composes with the markdown-link close tic', () => {
    expect(parseOptions('[OPTIONS: A | B](OPTIONS)').options).toEqual(['A', 'B'])
  })

  it('still declines a marker with same-line prose after it', () => {
    expect(parseOptions('[OPTIONS: A | B] see the note').options).toEqual([])
  })

  it('still cannot let a label span a line break', () => {
    expect(parseOptions('[OPTIONS: Alpha |\nBravo]').options).toEqual([])
  })
})

describe('the #9284 temper stays linear', () => {
  // The temper adds a lookahead inside a quantified body. A quadratic
  // implementation wedges rather than failing an assertion, so the bound is
  // generous and the shapes are the adversarial ones.
  it('is linear over a long trailing whitespace run', () => {
    const text = `[OPTIONS: A ]${'\t'.repeat(100_000)}`
    const start = Date.now()
    expect(parseOptions(text).options).toEqual(['A'])
    expect(Date.now() - start).toBeLessThan(2000)
  })

  it('is linear over a long FAILING continuation scan', () => {
    // The closer is INSIDE the body, so it enters the lookahead, whose whitespace
    // scan runs to the end of a 100k-tab run and then fails on `x`.
    const text = `[OPTIONS: A ]${'\t'.repeat(100_000)}x]`
    const start = Date.now()
    expect(parseOptions(text).options).toEqual([])
    expect(Date.now() - start).toBeLessThan(2000)
  })

  it('is linear over many failing closers', () => {
    const text = `[OPTIONS: ${'] x '.repeat(5_000)}`
    const start = Date.now()
    parseOptions(text)
    expect(Date.now() - start).toBeLessThan(2000)
  })

  it('is linear over many CONTINUING closers then a long tail', () => {
    const text = `[OPTIONS: ${'] | '.repeat(30_000)}${'\t'.repeat(30_000)}`
    const start = Date.now()
    parseOptions(text)
    expect(Date.now() - start).toBeLessThan(2000)
  })

  it('costs a run of any opener about what a run of ASCII `[` costs', () => {
    // A repeated-token degeneration after a head: 20,000 copies of one opener
    // with no partner. Every negated class in the body excludes EVERY bracket,
    // so each pair attempt fails on the next character and the bare-opener
    // alternative takes it — one step per character for every opener kind. An
    // interior that admitted the lookalikes would rescan the remaining run at
    // each position instead, and the ratio would be in the hundreds.
    const cost = (opener: string) => {
      const text = `[OPTIONS: A | B ${opener.repeat(20_000)}`
      const start = performance.now()
      parseOptions(text)
      return performance.now() - start
    }
    const ascii = Math.max(cost('['), 1)
    for (const opener of ['\u3010', '\uFF3B', '\u3014']) {
      expect(cost(opener), opener).toBeLessThan(8 * ascii)
    }
  })

  it('cannot blow up where the two bracket alternatives meet', () => {
    // THE shape that would be exponential if the matched-pair and continuation
    // alternatives could consume the same span: N blocks that look like both,
    // then a tail that fails the whole match, forcing the engine to exhaust
    // every combination it believes exists. They are disjoint by what follows
    // the closer, so there is only one.
    for (const block of ['[x] ', '[x] | ', '[a[b] ', '[a] ]a ', '[x', '[] ']) {
      const text = `[OPTIONS: ${block.repeat(20_000)}z`
      const start = Date.now()
      parseOptions(text)
      expect(Date.now() - start, block).toBeLessThan(2000)
    }
  })
})
