import { describe, it, expect } from 'vitest'
import { parseOptions } from '../app-sdk/protocol'
// The pattern is in-tree only — the barrel deliberately withholds it from the app surface.
import { stripOptionMarkers } from '../app-sdk/protocol/optionMarker'

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

  it('accepts a lookalike PAIR as a cost, because only ASCII `[` opens', () => {
    // The closer set was widened to the CJK lookalikes; there is no matching
    // OPENER set, so `【` is an ordinary character and the `】` after it reads as
    // unmatched. Common in Chinese output, hence stated explicitly.
    const text = '[OPTIONS: 【重要】修复 | 跳过】'
    expect(parseOptions(text).options).toEqual([])
    expect(parseOptions(text).text).toBe(text)
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
