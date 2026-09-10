import { describe, expect, it } from 'vitest'

import { OPTION_MARKER_RE } from '../app-sdk/protocol/optionMarker'
import { parseOptions } from '../app-sdk/protocol/options'

/** Labels of the LAST marker, or null when the grammar declines the line. */
function labelsOf(text: string): string | null {
  const m = [...text.matchAll(new RegExp(OPTION_MARKER_RE))].pop()
  return m ? (m[2] ?? m[4]) : null
}

/**
 * #9375: a closer is readmitted to a label when an opener earlier in the label
 * MATCHES it. "Matches" was decided against ASCII `[` alone, so a label written
 * wholly in CJK punctuation had a `】` with no recognised opener -- the body ended
 * there and the whole marker was declined, leaking as literal text. Each opener
 * now pairs with its own closer.
 *
 * The pairing is the load-bearing half. A single branch taking any opener and any
 * closer would make `【表1]` a pair, which is #9284's greedy body back through a
 * side door: the label would end at a closer whose opener never appeared.
 */
describe('OPTION_MARKER_RE lookalike bracket pairs inside a label (#9375)', () => {
  it.each([
    ['CJK', '[OPTIONS: 见【表1】说明 | 跳过]', ' 见【表1】说明 | 跳过'],
    ['fullwidth', '[OPTIONS: 见［表1］说明 | 跳过]', ' 见［表1］说明 | 跳过'],
    ['tortoise-shell', '[OPTIONS: 见〔表1〕说明 | 跳过]', ' 见〔表1〕说明 | 跳过'],
  ])('parses a %s pair inside a label', (_kind, text, want) => {
    expect(labelsOf(text)).toBe(want)
  })

  it('parses a marker whose closer is a lookalike AND whose label carries a pair', () => {
    expect(labelsOf('[OPTIONS: 【重要】修复 | 跳过】')).toBe(' 【重要】修复 | 跳过')
  })

  it('renders the CJK pair as real options, not as text', () => {
    const { options, text } = parseOptions('请选择：\n[OPTIONS: 见【表1】说明 | 跳过]')
    expect(options).toEqual(['见【表1】说明', '跳过'])
    expect(text).toBe('请选择：')
  })

  it.each([
    ['opener with the wrong closer', '[OPTIONS: 见【表1] 说明 | 跳过]'],
    ['closer with the wrong opener', '[OPTIONS: 见[表1】 说明 | 跳过]'],
  ])('declines a MISMATCHED pair (%s)', (_kind, text) => {
    expect(labelsOf(text)).toBeNull()
    // Declining must not delete: the whole line survives the strip.
    expect(text.replace(OPTION_MARKER_RE, '')).toBe(text)
  })

  it('still declines #9284: a closer that is neither matched nor continuing', () => {
    const text = '见 [OPTIONS: 保留 | 丢弃] 然后看 arr[0]'
    expect(labelsOf(text)).toBeNull()
    expect(text.replace(OPTION_MARKER_RE, '')).toBe(text)
  })

  it('treats a STRAY lookalike opener as ordinary label text', () => {
    // No partner closer, so no pair -- the bare-opener branch consumes it and the
    // label list is unaffected.
    expect(labelsOf('[OPTIONS: Fix 【 now | Skip]')).toBe(' Fix 【 now | Skip')
  })

  it('keeps every pre-existing supported shape', () => {
    expect(labelsOf('[OPTIONS: Read arr[0] now | Skip it]')).toBe(' Read arr[0] now | Skip it')
    expect(labelsOf('[OPTIONS: See [1] above | Skip]')).toBe(' See [1] above | Skip')
    expect(labelsOf('[OPTIONS: Alpha ] | Bravo ]]')).toBe(' Alpha ] | Bravo ]')
    expect(labelsOf('[OPTIONS: Alpha ], Bravo]')).toBe(' Alpha ], Bravo')
    expect(labelsOf('**[OPTIONS: Yes | No]**')).toBe(' Yes | No')
  })

  it('still refuses a NESTED head, which the pair guard is what preserves', () => {
    // Only ASCII `[` can begin a head, so only its branch carries the guard --
    // but that is the branch a nested `[OPTIONS:` would open on.
    expect(labelsOf('Note [OPTIONS: see [OPTIONS: x] below | Skip]')).toBeNull()
  })

  it.each([
    '[OPTIONS: See [a【b] ref | Skip]',
    '[OPTIONS: See [a［b] ref | Skip]',
    '[OPTIONS: 见【表[1】说明 | 跳过]',
  ])('declines a pair whose INTERIOR holds another bracket kind: %s', (text) => {
    // The cost the opener set pays for. The interior excludes EVERY bracket, so a
    // pair holding a different kind has no pair parse. Admitting `【` in the ASCII
    // branch's interior would let it consume a character the `【` branch could also
    // open on, giving a span two parses — and single-parse-per-span is what the
    // linearity argument reads off the pattern. Fails toward a VISIBLE marker.
    expect(labelsOf(text)).toBeNull()
    expect(text.replace(OPTION_MARKER_RE, '')).toBe(text)
  })

  it('parses that same interior when the list CONTINUES after the closer', () => {
    // The cost is the tail, not the interior.
    expect(labelsOf('[OPTIONS: See [a【b] | Skip]')).toBe(' See [a【b] | Skip')
  })

  it.each([
    '[OPTIONS: 见【表1|表2】说明 | 跳过]',
    '[OPTIONS: 见［表1|表2］说明 | 跳过]',
    // The comma shapes with NO pipe anywhere. `parseOptions` picks its delimiter as
    // `labels.includes('|') ? '|' : ','`, so the comma is load-bearing exactly when
    // no pipe is present — the case a pipe-only test cannot reach.
    '[OPTIONS: 见【表1,表2】说明, 跳过]',
    '[OPTIONS: 见〔表1,表2〕说明, 跳过]',
    // ...and with a pipe present too, so the rule does not depend on which
    // delimiter the consumer happens to pick.
    '[OPTIONS: 见【表1,表2】说明 | 跳过]',
  ])('declines a LOOKALIKE pair whose interior holds a separator: %s', (text) => {
    // About the CONSUMER, not the grammar: the splitter has no notion of nesting, so
    // admitting a pair that carries a separator would tear it into fragments with
    // unbalanced brackets — echoed back as the user's reply when tapped. Declining is
    // affordable; corrupting is not.
    expect(labelsOf(text)).toBeNull()
    const { options, text: kept } = parseOptions(text)
    expect(options).toEqual([])
    expect(kept).toBe(text)
  })

  it.each([
    ['[OPTIONS: Fix dict[str, Any] now | Skip]', ['Fix dict[str, Any] now', 'Skip']],
    ['[OPTIONS: Refactor arr[i, j] now | Skip]', ['Refactor arr[i, j] now', 'Skip']],
  ])('keeps the ASCII interior admitting separators: %s', (text, options) => {
    // The asymmetry, pinned as the thing that keeps it. Applying the exclusion to the
    // ASCII branch too declines these — labels a model writes constantly, which parse
    // today. The lookalike branches carry no such history to protect.
    expect(parseOptions(text as string).options).toEqual(options)
  })

  it('is a MITIGATION, not a guarantee — the continuation path is unchanged', () => {
    // A closer followed by a separator is admitted by continuation regardless of what
    // preceded it, so a bracket run holding `|` still reaches the splitter that way.
    // Pinned so the interior rule is not read as closing the whole class; removing it
    // needs a nesting-aware splitter. Same as the behaviour before this change.
    expect(parseOptions('[OPTIONS: 见【表1|表2】 | 跳过]').options).toEqual([
      '见【表1',
      '表2】',
      '跳过',
    ])
  })

  it.each([
    '[OPTIONS: 见【表1|表2】说明 | 跳过]',
    '[OPTIONS: 见【表1,表2】说明, 跳过]',
  ])('names the corruption it prevents, so the reason cannot be optimised away: %s', (text) => {
    expect(parseOptions(text).options).not.toEqual(['见【表1', '表2】说明', '跳过'])
  })

  it('admits anything in a pair that is neither a bracket nor a separator', () => {
    // The interior is otherwise opaque: spaces, digits and punctuation no splitter
    // looks at pass through as ordinary label text.
    expect(parseOptions('[OPTIONS: 见【表 1：注释】说明 | 跳过]').options).toEqual([
      '见【表 1：注释】说明',
      '跳过',
    ])
  })

  it('does not backtrack catastrophically on lookalike-heavy adversarial input', () => {
    const src = `[OPTIONS:${'【a】 | '.repeat(4000)}`
    const started = Date.now()
    expect(labelsOf(src)).toBeNull()
    expect(Date.now() - started).toBeLessThan(1000)
  })
})
