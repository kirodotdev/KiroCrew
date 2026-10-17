import { describe, expect, it } from 'vitest'
import {
  CONCAT_PLURAL_PATTERNS,
  HARDCODED_PLURAL_PATTERNS,
  JSX_TEXT_PLURAL_PATTERNS,
  WHOLE_WORD_PLURAL_PATTERNS,
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

  it('the sibling-spelling groups are sized consciously and global too', () => {
    // JSX-text and concat mirror the three comparison spellings one-to-one;
    // whole-word carries two morphologies (+s and y→ies) per comparison.
    expect(JSX_TEXT_PLURAL_PATTERNS).toHaveLength(3)
    expect(CONCAT_PLURAL_PATTERNS).toHaveLength(3)
    expect(WHOLE_WORD_PLURAL_PATTERNS).toHaveLength(6)
    for (const re of [
      ...JSX_TEXT_PLURAL_PATTERNS,
      ...CONCAT_PLURAL_PATTERNS,
      ...WHOLE_WORD_PLURAL_PATTERNS,
    ]) expect(re.flags).toContain('g')
  })
})

describe('JSX-text glue — true positives', () => {
  it.each([
    ['greater-than-1', "<span>{n} agent{n > 1 ? 's' : ''}</span>"],
    ['not-equals-1', "<span>{res.failed.length} session{res.failed.length !== 1 ? 's' : ''}</span>"],
    ['equals-1 inverted', "<span>{hiddenCount} more app{hiddenCount === 1 ? '' : 's'}</span>"],
  ])('catches the %s spelling in JSX text', (_name, src) => {
    // The React-idiomatic spelling of the same defect: no template literal,
    // no i18nT, the suffix chosen directly between two JSX expressions.
    expect(findHardcodedPluralSites(src)).toHaveLength(1)
  })

  it.each([
    ['compact, no spaces', "<p>{n} item{n>1?'s':''}</p>"],
    ['double quotes', '<p>{n} item{n > 1 ? "s" : ""}</p>'],
  ])('catches a reformatted JSX spelling: %s', (_name, src) => {
    expect(findHardcodedPluralSites(src)).toHaveLength(1)
  })

  it('matches when the JSX suffix is driven by a different variable', () => {
    expect(findHardcodedPluralSites("<p>{shown} item{total > 1 ? 's' : ''}</p>")).toHaveLength(1)
  })
})

describe('JSX-text glue — pinned NON-matches', () => {
  it('still ignores the i18nT-adjacent JSX form owned by the hard-zero tier', () => {
    // The translate call sits flush against the ternary brace, so there is no
    // literal noun letter for the JSX pattern to anchor on — exactly the
    // property that keeps one site from being billed to two tiers.
    const src = "{n} {i18nT('pages.overview.memoryTab.session')}{n === 1 ? '' : 's'}"
    expect(findHardcodedPluralSites(src)).toHaveLength(0)
  })

  it('ignores JSX conditional copy that is not a bare plural marker', () => {
    const src = "<p>{n} item{open ? ' (open)' : ''}</p> <p>{n} thing{n > 1 ? '!' : ''}</p>"
    expect(findHardcodedPluralSites(src)).toHaveLength(0)
  })

  it('does not match across a tag boundary (known limit, kept deliberate)', () => {
    // The noun run may not contain angle brackets, so a count wrapped in its
    // own element is unseen. A miss keeps the count low, never fails anyone.
    const src = "<b>{n}</b> item{n > 1 ? 's' : ''}"
    expect(findHardcodedPluralSites(src)).toHaveLength(0)
  })

  it('does not mix quote styles within one JSX ternary arm pair', () => {
    expect(findHardcodedPluralSites('<p>{n} item{n > 1 ? \'s" : \'\'}</p>')).toHaveLength(0)
  })
})

describe('string concatenation — true positives', () => {
  it.each([
    ['greater-than-1, parenthesised', "const label = 'agent' + (n > 1 ? 's' : '')"],
    ['not-equals-1, bare', "const label = 'session' + count !== 1 ? 's' : ''"],
    ['equals-1 inverted', "const label = 'app' + (hiddenCount === 1 ? '' : 's')"],
    ['double quotes', 'const label = "item" + (n > 1 ? "s" : "")'],
    ['compact', "const label = 'item'+(n>1?'s':'')"],
  ])('catches the %s spelling', (_name, src) => {
    expect(findHardcodedPluralSites(src)).toHaveLength(1)
  })
})

describe('string concatenation — pinned NON-matches', () => {
  it('ignores concatenation whose ternary is not a bare plural marker', () => {
    const src = "const a = 'item' + (open ? ' (open)' : ''); const b = 'thing' + (n > 1 ? '!' : '')"
    expect(findHardcodedPluralSites(src)).toHaveLength(0)
  })

  it('ignores ordinary string joins with no ternary', () => {
    expect(findHardcodedPluralSites("const a = 'item' + suffix")).toHaveLength(0)
  })

  it('does not pair a noun with a suffix across a statement boundary', () => {
    // The condition may not cross `;`/`,`/`:` or another string, so a distant
    // healthy literal cannot be stitched to an unrelated ternary.
    const src = "f('item', x); const y = n > 1 ? 's' : ''"
    expect(findHardcodedPluralSites(src)).toHaveLength(0)
  })

  it('does not mix quote styles within one concat ternary arm pair', () => {
    expect(findHardcodedPluralSites('const a = \'item\' + (n > 1 ? \'s" : \'\')')).toHaveLength(0)
  })
})

describe('whole-word ternary — true positives', () => {
  it.each([
    // The three live sites this widening was measured against, verbatim shapes.
    ['PastedChip template form', "`${expanded ? 'Collapse' : 'Expand'} pasted ${block.lines} ${block.lines === 1 ? 'line' : 'lines'}`"],
    ['SecurityPanel helper form', "return `${n} ${n === 1 ? 'rule' : 'rules'}`"],
    ['AgentImportFlow JSX y→ies form', "{selection.categories.length} {selection.categories.length === 1 ? 'category' : 'categories'}"],
  ])('catches the %s', (_name, src) => {
    expect(findHardcodedPluralSites(src)).toHaveLength(1)
  })

  it.each([
    ['not-equals-1, plural first', "const w = n !== 1 ? 'lines' : 'line'"],
    ['greater-than-1, plural first', "const w = n > 1 ? 'files' : 'file'"],
    ['not-equals-1, y→ies', "const w = n !== 1 ? 'categories' : 'category'"],
    ['greater-than-1, y→ies', "const w = n > 1 ? 'entries' : 'entry'"],
    ['double quotes', 'const w = n === 1 ? "rule" : "rules"'],
    ['compact', "const w = n===1?'rule':'rules'"],
  ])('catches the %s spelling', (_name, src) => {
    expect(findHardcodedPluralSites(src)).toHaveLength(1)
  })
})

describe('whole-word ternary — pinned NON-matches', () => {
  it('requires the plural arm to be the s-form of the singular arm', () => {
    // A ternary choosing two UNRELATED words is conditional copy, not a
    // plural pair — this constraint is what makes the variant trustworthy.
    const src = [
      "const a = flag === 1 ? 'on' : 'off'",
      "const b = n === 1 ? 'line' : 'rules'",
      "const c = mode !== 1 ? 'tabs' : 'window'",
    ].join('\n')
    expect(findHardcodedPluralSites(src)).toHaveLength(0)
  })

  it('ignores a ternary on a non-count condition', () => {
    // No `=== 1` / `!== 1` / `> 1` comparison means nothing marks this as a
    // count, even when the arms happen to be an s-pair.
    expect(findHardcodedPluralSites("const a = expanded ? 'Collapse' : 'Expand'")).toHaveLength(0)
    expect(findHardcodedPluralSites("const b = isPlural ? 'lines' : 'line'")).toHaveLength(0)
  })

  it('ignores comparisons against numbers other than 1', () => {
    const src = "const a = n === 12 ? 'line' : 'lines'; const b = n > 10 ? 'lines' : 'line'"
    expect(findHardcodedPluralSites(src)).toHaveLength(0)
  })

  it('does not mix quote styles within one whole-word arm pair', () => {
    expect(findHardcodedPluralSites('const a = n === 1 ? \'rule" : \'rules\'')).toHaveLength(0)
  })

  it('does not double-count a glued suffix as a whole-word pair', () => {
    // `'s'`/`''` arms have an empty side the whole-word patterns forbid, so a
    // template-glue site is billed exactly once, to the template pattern.
    const src = "`${n} item${n > 1 ? 's' : ''}`"
    expect(findHardcodedPluralSites(src)).toHaveLength(1)
  })

  it('ignores the shape quoted inside a comment', () => {
    // The whole-word spelling is plain enough to appear verbatim in prose, so
    // documenting the defect must never be counted as committing it — with the
    // ceiling pinned to the live count, one such comment would redden CI.
    const src = [
      "// e.g. n === 1 ? 'line' : 'lines' is the shape this gate catches",
      '/* block form: count !== 1 ? "rules" : "rule" */',
      '/**',
      " * JSDoc form: total > 1 ? 'files' : 'file'",
      ' */',
    ].join('\n')
    expect(findHardcodedPluralSites(src)).toHaveLength(0)
  })

  it('ignores the shape quoted inside a string or template text', () => {
    const src = [
      'const doc = "choose n === 1 ? \'line\' : \'lines\' by count"',
      "const tpl = `never write n === 1 ? 'rule' : 'rules'`",
    ].join('\n')
    expect(findHardcodedPluralSites(src)).toHaveLength(0)
  })

  it('still counts a site that only shares a line with a comment', () => {
    const src = "const w = n === 1 ? 'rule' : 'rules' // convert me"
    expect(findHardcodedPluralSites(src)).toHaveLength(1)
  })

  it('ignores arrow and shift operators that end in >', () => {
    // The `>` spelling must not read `=>` or `>>` as a count comparison.
    const src = "const f = () => 1 ? 'lines' : 'line'; const g = n >> 1 ? 'files' : 'file'"
    expect(findHardcodedPluralSites(src)).toHaveLength(0)
  })
})

describe('comment handling for the glue spellings', () => {
  it('ignores JSX-text and concat samples quoted inside comments', () => {
    const src = [
      "// JSX form: {n} agent{n > 1 ? 's' : ''}",
      "/* concat form: 'agent' + (n > 1 ? 's' : '') */",
    ].join('\n')
    expect(findHardcodedPluralSites(src)).toHaveLength(0)
  })
})

describe('scan performance', () => {
  it('stays linear on a long non-matching line', () => {
    // The whole-word patterns are anchored on their comparison operators
    // precisely so an unanchored lazy prefix cannot retry from every character
    // of a long line. Quadratic behavior here took seconds at 32 KiB — a
    // linear scan of 64 KiB is single-digit milliseconds, so a generous
    // absolute ceiling separates the two regimes without flaking on a slow
    // CI runner.
    const line = 'const someIdentifier = call(arg) + other.member[index] '.repeat(1200)
    const started = performance.now()
    expect(findHardcodedPluralSites(line)).toHaveLength(0)
    expect(performance.now() - started).toBeLessThan(1000)
  })
})
