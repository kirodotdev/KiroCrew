import { describe, expect, it } from 'vitest'
import {
  HARDCODED_PLURAL_PATTERNS,
  findHardcodedPluralSites,
} from '../../scripts/lib/hardcoded-plural.mjs'

/**
 * The hardcoded-plural detector behind the `[plurals-hardcoded]` ceiling.
 *
 * What it must catch is the plural-glue defect with NO `i18nT()` anywhere in
 * it — the shape the i18nT-anchored hard-zero patterns are structurally blind
 * to, because they key on an adjacent translate call that these sites simply
 * do not have. What it must NOT catch is pinned just as hard: a false positive
 * here fails CI on a healthy line, and a ceiling gate is only trustworthy when
 * a match is a defect every time.
 */
describe('findHardcodedPluralSites — true positives', () => {
  it('catches the count-adjacent template-literal shape', () => {
    // The reference shape (a real aria-label in this codebase): interpolated
    // count, literal noun, glued conditional suffix — all hardcoded English.
    const src = 'aria-label={`Retry ${failedIds.length} failed subagent${failedIds.length > 1 ? \'s\' : \'\'}`}'
    const sites = findHardcodedPluralSites(src)
    expect(sites).toHaveLength(1)
    expect(sites[0].text).toContain('failed subagent')
  })

  it.each([
    ['greater-than-1', '`${n} session${n > 1 ? \'s\' : \'\'}`'],
    ['not-equals-1', '`${res.failed.length} session${res.failed.length !== 1 ? \'s\' : \'\'}`'],
    ['equals-1 inverted', '`${hiddenCount} more app${hiddenCount === 1 ? \'\' : \'s\'}`'],
  ])('catches the %s spelling', (_name, src) => {
    expect(findHardcodedPluralSites(src)).toHaveLength(1)
  })

  it.each([
    ['compact, no spaces', '`${n} item${n>1?\'s\':\'\'}`'],
    ['double quotes', '`${n} item${n > 1 ? "s" : ""}`'],
    ['line break inside the ternary', '`${n} item${n > 1\n  ? \'s\'\n  : \'\'}`'],
  ])('catches a reformatted spelling: %s', (_name, src) => {
    // The defect is the same however a formatter (or an evader) spells it, so
    // whitespace and quote style must not be load-bearing in the patterns.
    expect(findHardcodedPluralSites(src)).toHaveLength(1)
  })

  it('does not mix quote styles within one ternary arm pair', () => {
    // A backreference pins the closing quote to the opening one; mixed quotes
    // are not valid JS string literals and must not be counted.
    expect(findHardcodedPluralSites('`${n} item${n > 1 ? \'s" : \'\'}`')).toHaveLength(0)
  })

  it('catches a parenthesised count expression', () => {
    const src = '`${wfActive?.count ?? 0} workflow${(wfActive?.count ?? 0) > 1 ? \'s\' : \'\'}`'
    expect(findHardcodedPluralSites(src)).toHaveLength(1)
  })

  it('matches when the suffix is driven by a DIFFERENT variable than the count', () => {
    // Still the defect (a plural form chosen in JS), plus a count/form
    // disagreement on top — requiring the expressions to be equal would hide
    // the buggier variant of the same class.
    const src = '`${shown} item${total > 1 ? \'s\' : \'\'}`'
    expect(findHardcodedPluralSites(src)).toHaveLength(1)
  })

  it('reports a 1-based line number an editor can jump to', () => {
    const src = 'const a = 1\nconst b = 2\nconst label = `${n} file${n === 1 ? \'\' : \'s\'}`\n'
    const sites = findHardcodedPluralSites(src)
    expect(sites).toHaveLength(1)
    expect(sites[0].line).toBe(3)
  })

  it('counts every site in a file, sorted by position', () => {
    const src = [
      'const a = `${x} tool${x > 1 ? \'s\' : \'\'}`',
      'const b = `${y} agent${y === 1 ? \'\' : \'s\'}`',
      'const c = `${z} run${z !== 1 ? \'s\' : \'\'}`',
    ].join('\n')
    const sites = findHardcodedPluralSites(src)
    expect(sites.map(s => s.line)).toEqual([1, 2, 3])
  })
})

describe('findHardcodedPluralSites — pinned NON-matches', () => {
  it('ignores a plural handled INSIDE i18nT with a count', () => {
    // The correct form this gate steers people toward must never trip it.
    const src = [
      "{i18nT('pages.chat.subagentProgressBar.retry_failed_count', { count: failedIds.length })}",
      '`${i18nT(\'pages.overview.session\', { count: n })}`',
    ].join('\n')
    expect(findHardcodedPluralSites(src)).toHaveLength(0)
  })

  it('ignores a ternary yielding non-plural text', () => {
    // A conditional that appends something other than a bare plural marker is
    // ordinary conditional copy, not plural glue.
    const src = '`${n} item${open ? \' (open)\' : \'\'}` and `${n} thing${n > 1 ? \'!\' : \'\'}`'
    expect(findHardcodedPluralSites(src)).toHaveLength(0)
  })

  it('ignores the i18nT-adjacent form owned by the hard-zero tier', () => {
    // Double-counting would bill one site to two checks with different
    // enforcement. The JSX form is not a template literal at all, and in the
    // template form the translate call leaves no literal noun for the suffix
    // to glue to.
    const src = [
      "{n} {i18nT('pages.overview.memoryTab.session')}{n === 1 ? '' : 's'}",
      "`${i18nT('pages.overview.memoryTab.session')}${n === 1 ? '' : 's'}`",
    ].join('\n')
    expect(findHardcodedPluralSites(src)).toHaveLength(0)
  })

  it('does not match across template-literal boundaries', () => {
    // Two adjacent healthy literals must not be stitched into one finding.
    const src = 'const a = `${n} done`; const b = `ready${flag > 1 ? \'s\' : \'\'}`'
    expect(findHardcodedPluralSites(src)).toHaveLength(0)
  })
})

describe('the pattern set itself', () => {
  it('covers exactly the three comparison spellings, all global', () => {
    // `matchAll` throws on a non-global regex, and a fourth spelling should be
    // added consciously (with its true-positive test), not discovered here.
    expect(HARDCODED_PLURAL_PATTERNS).toHaveLength(3)
    for (const re of HARDCODED_PLURAL_PATTERNS) expect(re.flags).toContain('g')
  })
})
