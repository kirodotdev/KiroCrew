/**
 * Guards for the translation driver.
 *
 * The driver is the only thing standing between "1767 new English keys" and
 * "1767 keys x 11 locales landing in one commit", and `catalogParity.test.ts`
 * gives no partial credit — so the driver's checks have to be right before the
 * translation run, not after.
 *
 * Several rules here are deliberately RELATIVE to the English rather than
 * absolute, and the comments say which measurement forced that. Run in absolute
 * form against the already-approved ru/de/zh-CN catalogs, the whitespace, bracket
 * and plural rules produced 142, 150 and 115 findings respectively — every one
 * inherited from the English source or from a slug that merely looks plural. A
 * check that cries wolf on approved work is a check somebody switches off.
 */

import { describe, it, expect } from 'vitest'
import fs from 'node:fs'
import path from 'node:path'

import {
  PHASE1_CATEGORIES,
  PHASE6_CATEGORIES,
  parseLanguages,
  flatten,
  placeholders,
  checkValue,
  passthroughRatio,
  localiseShard,
  maskedRanges,
  countOutside,
  mergeCatalog,
  sortDeep,
  renderPrompt,
  extractPromptBody,
} from '../../scripts/i18n-translate.mjs'
import { SUPPORTED_LANGUAGES, DEFAULT_LANGUAGE } from './languages'

const SRC = path.resolve(__dirname)
const PROMPT_DOC = fs.readFileSync(path.join(SRC, 'TRANSLATION-PROMPT.md'), 'utf-8')
const LANGUAGES_SRC = fs.readFileSync(path.join(SRC, 'languages.ts'), 'utf-8')

/** Rule names from a findings array, which is all most assertions care about. */
const rules = (findings: { rule: string }[]) => findings.map(f => f.rule)

describe('locale list derivation', () => {
  /**
   * The driver regex-parses `languages.ts` instead of keeping its own list. That
   * is the right call — a duplicated list is a second thing to forget when
   * language #13 ships — but it only stays right while the parse agrees with the
   * real export. If someone reformats `SUPPORTED_LANGUAGES`, this fails here
   * rather than by quietly translating ten locales out of eleven.
   */
  it('parses exactly the shipped, non-devOnly languages', () => {
    const parsed = parseLanguages(LANGUAGES_SRC)
    const expected = SUPPORTED_LANGUAGES.filter(l => !('devOnly' in l && l.devOnly))
    expect(parsed.map((l: { code: string }) => l.code)).toEqual(expected.map(l => l.code))
    expect(parsed.map((l: { label: string }) => l.label)).toEqual(expected.map(l => l.label))
  })

  it('excludes the pseudolocale, which is a detector and not a translation target', () => {
    expect(parseLanguages(LANGUAGES_SRC).map((l: { code: string }) => l.code)).not.toContain('en-XA')
  })

  it('leaves 11 translation targets once English is removed', () => {
    // A literal, NOT SUPPORTED_LANGUAGES.length - 1: the test above already
    // asserts the parse equals that export, so deriving the count from it here
    // would compare the parse against itself and pass for any fan-out size.
    const parsed = parseLanguages(LANGUAGES_SRC)
    expect(parsed.filter((l: { code: string }) => l.code !== DEFAULT_LANGUAGE)).toHaveLength(11)
  })

  it('throws rather than guessing when the export cannot be found', () => {
    expect(() => parseLanguages('export const SOMETHING_ELSE = []')).toThrow(/SUPPORTED_LANGUAGES/)
  })
})

describe('phase categories', () => {
  it('partition the classifier buckets with no overlap', () => {
    expect(PHASE1_CATEGORIES.filter((c: string) => PHASE6_CATEGORIES.includes(c))).toEqual([])
  })

  /**
   * Cross-check against the classifier itself, so a new bucket added there cannot
   * land in neither phase list and quietly vanish from every scope report.
   *
   * `check-i18n-strings.mjs` arrives with the Phase 0 gates, so it is legitimately
   * absent on a tree that predates it — absent-tolerant rather than skipped, so the
   * assertion becomes mandatory the moment the file exists.
   */
  it('cover every bucket check-i18n-strings.mjs can emit', () => {
    const classifier = path.resolve(SRC, '../../scripts/check-i18n-strings.mjs')
    if (!fs.existsSync(classifier)) {
      expect([...PHASE1_CATEGORIES, ...PHASE6_CATEGORIES].length).toBeGreaterThan(0)
      return
    }
    const buckets = [...fs.readFileSync(classifier, 'utf-8').matchAll(/return '([a-z-]+)'/g)].map(m => m[1])
    expect(buckets.length).toBeGreaterThan(0)
    for (const b of buckets) {
      expect([...PHASE1_CATEGORIES, ...PHASE6_CATEGORIES]).toContain(b)
    }
  })
})

describe('placeholders', () => {
  it('finds interpolation, tags and nesting references', () => {
    expect(placeholders('{{count}} of <0>{{total}}</0> via $t(a.b)')).toEqual(
      ['$t(a.b)', '</0>', '<0>', '{{count}}', '{{total}}'].sort(),
    )
  })

  it('is order-insensitive so a placeholder may move for target grammar', () => {
    expect(placeholders('{{a}} then {{b}}')).toEqual(placeholders('{{b}} then {{a}}'))
  })
})

describe('checkValue — structural rules', () => {
  it('accepts a clean translation', () => {
    expect(checkValue({ key: 'k', en: 'Open {{count}} PR', tr: '打开 {{count}} 个 PR' })).toEqual([])
  })

  it('rejects an empty or whitespace value', () => {
    expect(rules(checkValue({ key: 'k', en: 'Save', tr: '   ' }))).toContain('empty')
  })

  it('rejects a non-string value', () => {
    expect(rules(checkValue({ key: 'k', en: 'Save', tr: 42 }))).toContain('not-a-string')
  })

  it('rejects a dropped placeholder', () => {
    expect(rules(checkValue({ key: 'k', en: '{{count}} items', tr: '若干项目' }))).toContain('placeholder-parity')
  })

  /**
   * The real defect this caught in the shipped German catalog: `<path>` became
   * `<pfad>` and `<your-host>` became `<ihr-host>`. The name inside a placeholder
   * is an identifier, so translating it silently breaks interpolation at runtime.
   */
  it('rejects a TRANSLATED placeholder name', () => {
    expect(rules(checkValue({ key: 'k', en: 'at <path>', tr: 'bei <pfad>' }))).toContain('placeholder-parity')
    expect(rules(checkValue({ key: 'k', en: '{{count}} items', tr: '{{数量}} 项' }))).toContain('placeholder-parity')
  })

  it('accepts a placeholder moved to a different position', () => {
    expect(rules(checkValue({ key: 'k', en: 'Delete {{name}} now', tr: '现在删除 {{name}}' }))).toEqual([])
  })

  it('rejects a changed newline count', () => {
    expect(rules(checkValue({ key: 'k', en: 'a\nb', tr: 'a b' }))).toContain('newline-count')
  })

  it('does not count brackets inside placeholders or tags', () => {
    expect(rules(checkValue({ key: 'k', en: 'a <0>b</0> {{c}}', tr: 'a <0>乙</0> {{c}}' }))).toEqual([])
  })
})

describe('checkValue — do-not-translate terms', () => {
  it('rejects a translated do-not-translate term', () => {
    const f = checkValue({ key: 'k', en: 'Open in GitHub', tr: '在 吉特哈布 中打开', dnt: ['GitHub'] })
    expect(rules(f)).toContain('dnt-missing')
  })

  it('does not demand a DNT term the English never used', () => {
    const f = checkValue({ key: 'k', en: 'Open the file', tr: '打开文件', dnt: ['GitHub'] })
    expect(rules(f)).not.toContain('dnt-missing')
  })

  it('matches on word boundaries, not substrings', () => {
    // `Git` must not count as present just because `Gitea` is.
    const f = checkValue({ key: 'k', en: 'Use Gitea here', tr: '在 Gitea 中使用', dnt: ['Git'] })
    expect(rules(f)).not.toContain('dnt-missing')
  })

  it('treats a Korean 조사 as a boundary, not a continuation of the term', () => {
    // Korean agglutinates: `GitHub에서` IS the term, verbatim, with a particle
    // attached. A plain letter-boundary called all 54 such values a dropped term.
    const f = checkValue({
      key: 'k', en: 'Open in GitHub', tr: 'GitHub에서 열기', dnt: ['GitHub'], locale: 'ko',
    })
    expect(rules(f)).not.toContain('dnt-missing')
  })

  it('still rejects a term the Korean dropped, particle or not', () => {
    const f = checkValue({
      key: 'k', en: 'Open in GitHub', tr: '깃허브에서 열기', dnt: ['GitHub'], locale: 'ko',
    })
    expect(rules(f)).toContain('dnt-missing')
  })

  it('keeps the strict boundary for every other locale', () => {
    // The relaxation is Korean orthography, not a general loosening: `ja.md` §1.1
    // asks for a space at the script boundary, so `GitHubで` is a real finding
    // there and must stay one.
    const f = checkValue({
      key: 'k', en: 'Open in GitHub', tr: 'GitHubで開く', dnt: ['GitHub'], locale: 'ja',
    })
    expect(rules(f)).toContain('dnt-missing')
  })

  it('stays strict when no locale is supplied', () => {
    const f = checkValue({ key: 'k', en: 'Open in GitHub', tr: 'GitHub에서 열기', dnt: ['GitHub'] })
    expect(rules(f)).toContain('dnt-missing')
  })
})

describe('checkValue — whitespace, relative to the English', () => {
  it('rejects whitespace the translation introduces', () => {
    expect(rules(checkValue({ key: 'k', en: 'Save', tr: ' 保存' }))).toContain('edge-whitespace')
    expect(rules(checkValue({ key: 'k', en: 'Save now', tr: '立即  保存' }))).toContain('doubled-space')
  })

  /**
   * The English catalog itself carries 77 doubled spaces — JSX text extraction kept
   * the source indentation — and one edge-space value. Billing those to the
   * translator accounted for 78 of the 142 false findings.
   */
  it('does not bill the translation for whitespace the English already has', () => {
    expect(rules(checkValue({ key: 'k', en: 'namespaces. New  learnings', tr: '命名空间。新  经验' })))
      .not.toContain('doubled-space')
    expect(rules(checkValue({ key: 'k', en: ' trailing ', tr: ' 尾随 ' }))).not.toContain('edge-whitespace')
  })
})

describe('checkValue — full-width forms', () => {
  it('rejects full-width Latin letters and digits', () => {
    expect(rules(checkValue({ key: 'k', en: 'PR 12', tr: 'ＰＲ 12' }))).toContain('fullwidth-latin')
    expect(rules(checkValue({ key: 'k', en: '12 items', tr: '１２ 项' }))).toContain('fullwidth-latin')
  })

  it('accepts full-width PUNCTUATION, which is correct CJK typography', () => {
    // Only letters and digits are banned. A whole-block U+FF01-FF5E check would
    // flag `，` and `（`, fighting the very style guide it implements.
    expect(rules(checkValue({ key: 'k', en: 'Save, then close.', tr: '保存，然后关闭。' }))).toEqual([])
  })
})

describe('checkValue — brackets, pooled across widths', () => {
  /**
   * Two calibrations in one rule. DELTA against the English, because the D1
   * fragment keys (`'Findings ('` + N + `')'`) are deliberately unbalanced at
   * source — demanding balance flagged 64 approved values. POOLED across widths,
   * because zh-CN correctly rewrites `(` as `（`, which its own style guide asks
   * for — comparing per width flagged 60 more. What is left as a defect is MIXING
   * both widths in one value, which is the pair `qa.test.ts` rejects.
   */
  it('accepts a bracket the English leaves open', () => {
    expect(rules(checkValue({ key: 'k', en: 'Findings (', tr: '发现 (' }))).toEqual([])
    expect(rules(checkValue({ key: 'k', en: 'remaining)', tr: '剩余)' }))).toEqual([])
  })

  it('accepts a bracket consistently converted to full-width', () => {
    expect(rules(checkValue({ key: 'k', en: 'Findings (3)', tr: '发现（3）' }))).toEqual([])
    expect(rules(checkValue({ key: 'k', en: 'Findings (', tr: '发现（' }))).toEqual([])
  })

  it('rejects one value using both widths of a pair', () => {
    expect(rules(checkValue({ key: 'k', en: 'Findings (3)', tr: '发现 (3）' }))).toContain('mixed-width-bracket')
  })

  it('rejects a bracket dropped from a balanced English pair', () => {
    expect(rules(checkValue({ key: 'k', en: 'Findings (3)', tr: '发现 (3' }))).toContain('unbalanced-bracket')
  })

  it('rejects a bracket the English never had', () => {
    expect(rules(checkValue({ key: 'k', en: 'Findings 3', tr: '发现 (3' }))).toContain('unbalanced-bracket')
  })
})

describe('checkValue — plurals, gated on the registry', () => {
  it('rejects a plural form the locale cannot select', () => {
    const f = checkValue({
      key: 'items_one', en: '1 item', tr: '1 项',
      categories: ['other'], pluralBases: ['items'],
    })
    expect(rules(f)).toContain('impossible-plural')
  })

  it('accepts a plural form the locale does have', () => {
    const f = checkValue({
      key: 'items_other', en: '{{count}} items', tr: '{{count}} 项',
      categories: ['other'], pluralBases: ['items'],
    })
    expect(rules(f)).not.toContain('impossible-plural')
  })

  /**
   * The slug generator ends a key with the sentence's last word, so `"…to add one"`
   * becomes `..._add_one`. Judging the suffix alone called three such approved
   * zh-CN values impossible plurals. Only a key whose BASE is registered in
   * `pluralKeys.json` is a plural.
   */
  it('does not treat a key that merely ends in "one" as a plural form', () => {
    const f = checkValue({
      key: 'pages.chatSidebar.add_column_after_this_one',
      en: 'Add column after this one',
      tr: '在此列后添加一列',
      categories: ['other'],
      pluralBases: ['components.appstore.categoryRail.app'],
    })
    expect(rules(f)).not.toContain('impossible-plural')
  })

  it('stays silent when no registry is supplied rather than guessing', () => {
    const f = checkValue({ key: 'items_one', en: '1 item', tr: '1 项', categories: ['other'] })
    expect(rules(f)).not.toContain('impossible-plural')
  })
})

describe('localiseShard', () => {
  /**
   * The prompt must ask each locale for the plural forms it can actually select.
   * Sending the English shard verbatim asks for exactly the wrong set: zh-CN gets
   * an `_one` it can never select — and `checkValue` then rejects the compliant
   * answer — while ru is never asked for `_few`/`_many` at all, so the pipeline
   * cannot produce a correct Russian plural.
   */
  it('drops a plural form the locale cannot select', () => {
    const out = localiseShard({ items_one: '1 item', items_other: '{{count}} items' }, ['items'], ['other'])
    expect(Object.keys(out)).toEqual(['items_other'])
  })

  it('ADDS the forms the locale needs but English does not carry', () => {
    const out = localiseShard(
      { items_one: '1 item', items_other: '{{count}} items' },
      ['items'],
      ['one', 'few', 'many', 'other'],
    )
    expect(Object.keys(out).sort()).toEqual(['items_few', 'items_many', 'items_one', 'items_other'])
    // `other` seeds a form English lacks: it is the one category every locale has.
    expect(out.items_few).toBe('{{count}} items')
    expect(out.items_many).toBe('{{count}} items')
    // An existing form is preserved rather than overwritten by the seed.
    expect(out.items_one).toBe('1 item')
  })

  it('leaves non-plural keys untouched', () => {
    const out = localiseShard({ save: 'Save', 'a.b': 'X' }, ['items'], ['other'])
    expect(out).toEqual({ 'a.b': 'X', save: 'Save' })
  })

  it('does not treat an unregistered plural-shaped key as a plural', () => {
    // `"…to add one"` slugs to `..._add_one`, which is a real key, not a form.
    const out = localiseShard({ add_one: 'Add one' }, [], ['other'])
    expect(out).toEqual({ add_one: 'Add one' })
  })

  it('is deterministic in key order so a rendered prompt is reproducible', () => {
    const a = localiseShard({ b: '2', a: '1' }, [], ['other'])
    const b = localiseShard({ a: '1', b: '2' }, [], ['other'])
    expect(JSON.stringify(a)).toBe(JSON.stringify(b))
  })
})

describe('maskedRanges / countOutside', () => {
  /**
   * These exist instead of `replace(/<[^>]+>/g, '')`. That shape is a
   * multi-character sanitizer — CodeQL flags it high severity, correctly in
   * general, since removing `<…>` once can splice a new `<script` out of
   * `<scr<x>ipt`. Counting by offset never rewrites the string, so the dangerous
   * shape does not exist to get wrong.
   */
  it('covers interpolation, tags and nesting references', () => {
    expect(maskedRanges('a {{b}} c').length).toBe(1)
    expect(maskedRanges('<0>x</0>').length).toBe(2)
    expect(maskedRanges('$t(a.b)').length).toBe(1)
  })

  it('ignores characters inside a masked range', () => {
    expect(countOutside('({{a(b}})', c => c === '(')).toBe(1)
    expect(countOutside('<a(b>', c => c === '(')).toBe(0)
  })

  it('counts characters outside every masked range', () => {
    expect(countOutside('(a) {{b}} (c)', c => c === '(')).toBe(2)
  })

  it('does not rewrite the input', () => {
    const s = '<scr<x>ipt>alert(1)</script>'
    expect(countOutside(s, c => c === '(')).toBeGreaterThanOrEqual(0)
    expect(s).toBe('<scr<x>ipt>alert(1)</script>')
  })
})

describe('mergeCatalog', () => {
  /**
   * Why `merge` exists instead of `i18n-shard.mjs join`. `join` rewrites a catalog
   * from shards keyed off the ENGLISH corpus, so any form the locale has and
   * English does not is dropped. Measured on a real round-trip: `ru.json` loses 108
   * lines and each of es/fr/pt/it loses 45 keys, all `_few`/`_many` CLDR plural
   * forms.
   */
  it('preserves a locale-specific plural form the English corpus cannot carry', () => {
    const existing = { items_other: 'предметов', items_few: 'предмета', items_one: 'предмет' }
    const { catalog } = mergeCatalog(existing, { newKey: 'новый' })
    expect(catalog.items_few).toBe('предмета')
    expect(catalog.items_one).toBe('предмет')
    expect(catalog.newKey).toBe('новый')
  })

  /**
   * INSERT-ONLY by default. The catalogs are AI-generated plus human correction, so
   * an existing value may carry a contributor edit. Sharding the full corpus by
   * mistake and then merging would otherwise replace every one of ~4000 values —
   * discarding exactly the work that is hardest to reproduce.
   */
  it('leaves an existing translation alone and reports it as skipped', () => {
    const { catalog, merged, skipped } = mergeCatalog({ a: '译文' }, { a: 'new', b: '新' })
    expect(catalog.a).toBe('译文')
    expect(catalog.b).toBe('新')
    expect(merged).toBe(1)
    expect(skipped).toEqual(['a'])
  })

  it('replaces an existing translation only when overwrite is asked for', () => {
    const { catalog, merged, skipped } = mergeCatalog({ a: '译文' }, { a: 'new' }, { overwrite: true })
    expect(catalog.a).toBe('new')
    expect(merged).toBe(1)
    expect(skipped).toEqual([])
  })

  it('treats a nested existing key as present, not just a top-level one', () => {
    const { catalog, skipped } = mergeCatalog({ pages: { a: '旧' } }, { 'pages.a': 'new' })
    expect(catalog.pages.a).toBe('旧')
    expect(skipped).toEqual(['pages.a'])
  })

  it('nests a dotted key into the existing tree', () => {
    const { catalog } = mergeCatalog({ pages: { a: '1' } }, { 'pages.b': '2' })
    expect(catalog).toEqual({ pages: { a: '1', b: '2' } })
  })

  it('creates intermediate objects for a wholly new path', () => {
    expect(mergeCatalog({}, { 'a.b.c': 'x' }).catalog).toEqual({ a: { b: { c: 'x' } } })
  })

  it('does not mutate its input', () => {
    const before = { a: { b: '1' } }
    mergeCatalog(before, { 'a.c': '2' })
    expect(before).toEqual({ a: { b: '1' } })
  })

  it('refuses a prototype-polluting key segment, in any position', () => {
    expect(() => mergeCatalog({}, { '__proto__.x': 'bad' })).toThrow(/unsafe object-key segment/)
    expect(() => mergeCatalog({}, { 'a.constructor': 'bad' })).toThrow(/unsafe object-key segment/)
    expect(() => mergeCatalog({}, { prototype: 'bad' })).toThrow(/unsafe object-key segment/)
  })

  it('replaces a leaf that blocks a deeper path rather than throwing', () => {
    expect(mergeCatalog({ a: 'leaf' }, { 'a.b': 'x' }).catalog).toEqual({ a: { b: 'x' } })
  })
})

describe('sortDeep', () => {
  it('sorts keys at every level so a merge produces a stable diff', () => {
    expect(JSON.stringify(sortDeep({ b: '1', a: { d: '2', c: '3' } })))
      .toBe(JSON.stringify({ a: { c: '3', d: '2' }, b: '1' }))
  })

  it('leaves non-objects alone', () => {
    expect(sortDeep('x')).toBe('x')
    expect(sortDeep(null)).toBe(null)
  })
})

describe('passthroughRatio', () => {
  it('is 1 when nothing was translated', () => {
    expect(passthroughRatio({ a: 'Save', b: 'Open' }, { a: 'Save', b: 'Open' })).toBe(1)
  })

  it('is 0 when everything was', () => {
    expect(passthroughRatio({ a: 'Save', b: 'Open' }, { a: '保存', b: '打开' })).toBe(0)
  })

  it('tolerates a proper noun surviving translation', () => {
    expect(passthroughRatio({ a: 'KiroCrew', b: 'Open' }, { a: 'KiroCrew', b: '打开' })).toBe(0.5)
  })

  it('is 0 for an empty corpus rather than NaN', () => {
    expect(passthroughRatio({}, {})).toBe(0)
  })
})

describe('flatten', () => {
  it('produces dotted leaf paths', () => {
    expect(flatten({ a: { b: 'x' }, c: 'y' })).toEqual({ 'a.b': 'x', c: 'y' })
  })
})

describe('the committed prompt', () => {
  it('has the BEGIN/END markers the driver extracts between', () => {
    expect(() => extractPromptBody(PROMPT_DOC)).not.toThrow()
    expect(extractPromptBody(PROMPT_DOC).length).toBeGreaterThan(500)
  })

  it('throws on a doc missing its markers instead of sending the whole file', () => {
    expect(() => extractPromptBody('# just prose\n')).toThrow(/PROMPT BEGIN/)
  })

  /**
   * `renderPrompt` throws on a leftover slot, so a slot added to the doc and not to
   * the driver would break a translation run at emit time. Catch it in CI instead.
   */
  it('declares only slots the driver supplies', () => {
    const declared = new Set(
      [...extractPromptBody(PROMPT_DOC).matchAll(/\{\{([A-Z_]+)\}\}/g)].map(m => m[1]),
    )
    const supplied = new Set([
      'LOCALE', 'LANGUAGE_LABEL', 'PLURAL_CATEGORIES', 'STYLE_GUIDE',
      'DNT_TERMS', 'CONTEXT', 'SHARD_JSON', 'EXAMPLES',
    ])
    expect([...declared].filter(d => !supplied.has(d))).toEqual([])
  })

  it('documents every slot it declares in the slot table', () => {
    const declared = [...extractPromptBody(PROMPT_DOC).matchAll(/\{\{([A-Z_]+)\}\}/g)].map(m => m[1])
    for (const slot of new Set(declared)) {
      expect(PROMPT_DOC).toContain(`\`{{${slot}}}\``)
    }
  })

  it('states the output contract the driver relies on', () => {
    const body = extractPromptBody(PROMPT_DOC)
    expect(body).toMatch(/exactly one JSON object/i)
    expect(body).toMatch(/non-empty string/i)
  })
})

describe('renderPrompt', () => {
  it('substitutes every occurrence of a slot', () => {
    expect(renderPrompt('{{A}} and {{A}} and {{B}}', { A: 'x', B: 'y' })).toBe('x and x and y')
  })

  it('throws on an unfilled slot rather than sending a literal {{SLOT}}', () => {
    expect(() => renderPrompt('{{A}} {{MISSING}}', { A: 'x' })).toThrow(/MISSING/)
  })

  it('does not treat i18next interpolation in a shard as a prompt slot', () => {
    // Slots are UPPER_SNAKE; `{{count}}` inside the embedded shard JSON must survive.
    expect(renderPrompt('{{A}}', { A: 'value is {{count}} items' })).toBe('value is {{count}} items')
  })
})
