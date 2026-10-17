/**
 * The paper-name and namespace-name refusal copy, as structure in every catalog.
 *
 * A refused LETTER is named with its script and code point, because a Cyrillic or
 * Greek capital renders exactly like the Latin one the rule allows: `“О”` alone
 * shows a character that looks fine. A refused non-letter is named as typed. Both
 * messages then give the rule as one clause, so the 320px Code Review Sage rail
 * shows a few lines of red text rather than a paragraph.
 *
 * Asserted as relationships rather than literals, so a catalog can reword its
 * sentence freely: the placeholders each key needs, the one-clause rule, the four
 * script labels, and a length ceiling on the rendered message.
 */

import { describe, it, expect } from 'vitest'

import { CATALOGS } from './catalogs'

const LETTER_KEYS = [
  'apps.papyrus.page.invalid_project_name_letter',
  'apps.codeReviewSage.components.learningRail.invalid_namespace_name_letter',
]
const CHAR_KEYS = [
  'apps.papyrus.page.invalid_project_name_char',
  'apps.codeReviewSage.components.learningRail.invalid_namespace_name_char',
]
const SCRIPT_LABELS = ['latin', 'greek', 'cyrillic', 'other'] as const

/**
 * The rule's allowed-punctuation tail, joined so it never wraps mid-list: a
 * no-break space between the characters, plus a word joiner after the hyphen,
 * because a line may still break between a hyphen and a no-break space.
 */
const JOINED_TAIL = '.\u00a0-\u2060\u00a0_'

/**
 * The longest rendered message a catalog may carry. The full multi-clause rule ran
 * 250 to 380 characters and wrapped to about seven lines in the rail; the one-clause
 * form fits in a little over a hundred in the widest language.
 */
const MAX_RENDERED = 130

/** Authored catalogs only: the pseudolocale pads every string on purpose. */
const AUTHORED = Object.entries(CATALOGS).filter(([code]) => code !== 'en-XA')

function leaf(catalog: Record<string, unknown>, key: string): string {
  let node: unknown = catalog
  for (const part of key.split('.')) node = (node as Record<string, unknown> | undefined)?.[part]
  if (typeof node !== 'string') throw new Error(`missing ${key}`)
  return node
}

function render(value: string, params: Record<string, string>): string {
  return value.replace(/\{\{(\w+)\}\}/g, (_m, name: string) => params[name] ?? `{{${name}}}`)
}

describe.each(AUTHORED)('name-refusal copy in %s', (_code, { translation }) => {
  const cyrillic = leaf(translation, 'lib.scriptLabel.cyrillic')

  it('carries a distinct, non-empty label for every script it names', () => {
    const labels = SCRIPT_LABELS.map(name => leaf(translation, `lib.scriptLabel.${name}`))
    for (const label of labels) expect(label.trim()).not.toBe('')
    expect(new Set(labels).size).toBe(labels.length)
  })

  it.each(LETTER_KEYS)('names the script and code point of a refused letter (%s)', (key) => {
    const value = leaf(translation, key)
    for (const placeholder of ['{{char}}', '{{script}}', '{{code}}']) expect(value).toContain(placeholder)
    const text = render(value, { char: 'О', script: cyrillic, code: 'U+041E' })
    expect(text).toContain('U+041E')
    expect(text).toContain(cyrillic)
    expect(text.length).toBeLessThanOrEqual(MAX_RENDERED)
  })

  it.each(CHAR_KEYS)('names a refused non-letter as typed (%s)', (key) => {
    const value = leaf(translation, key)
    expect(value).toContain('{{char}}')
    expect(value).not.toContain('{{script}}')
    expect(value).not.toContain('{{code}}')
    expect(render(value, { char: '/' }).length).toBeLessThanOrEqual(MAX_RENDERED)
  })

  it.each([...LETTER_KEYS, ...CHAR_KEYS])('states the rule as one clause (%s)', (key) => {
    const value = leaf(translation, key)
    for (const part of ['a–z', '0–9', JOINED_TAIL]) expect(value).toContain(part)
    expect(value).not.toMatch(/\. +- +_/)
    // The start, end and length constraints belong to the message shown when no
    // single character is to blame.
    expect(value).not.toMatch(/64/)
  })
})

/**
 * Each catalog's own word for "lowercase", as its full name rule spells it. A
 * capital Latin letter is refused at the letter too, so the one-clause rule has to
 * say lowercase or "Use a–z" reads as allowing "R".
 */
const LOWERCASE_WORD: Record<string, string> = {
  en: 'lowercase',
  de: 'Kleinbuchstaben',
  es: 'minúsculas',
  fr: 'minuscules',
  it: 'minuscole',
  pt: 'minúsculas',
  ru: 'строчные',
  bn: 'ছোট হাতের',
  hi: 'छोटे',
  ja: '小文字',
  ko: '소문자',
  'zh-CN': '小写',
}

describe.each(AUTHORED)('lowercase in the name-refusal rule in %s', (code, { translation }) => {
  const word = LOWERCASE_WORD[code]

  it('has a lowercase word recorded for this catalog', () => {
    expect(word).toBeTruthy()
  })

  it.each([...LETTER_KEYS, ...CHAR_KEYS])('says lowercase in the one-clause rule, as the full rule does (%s)', (key) => {
    const full = leaf(translation, key.replace(/_(letter|char)$/, ''))
    expect(full).toContain(word)
    expect(leaf(translation, key)).toContain(word)
  })
})
