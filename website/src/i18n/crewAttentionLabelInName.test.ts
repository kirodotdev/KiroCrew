/**
 * The crew group header's attention button keeps its visible text as its
 * accessible name, in every shipped locale (WCAG 2.5.3, Label in Name).
 *
 * The button shows a target phrase (`crew_attention_go_*`, "Open approval chat")
 * and then its counts. Its `aria-label` is `crew_attention_name`, which fills
 * `{{target}}` and `{{counts}}` with those same strings. So a translation of that
 * template may add only the pause between the two, never words of its own: a
 * word the button does not show is one a speech-input user cannot say, and the
 * name has to read target first, as the button does.
 *
 * The generated pseudolocale is excluded: it pads every value with its own
 * filler, and it regenerates from `en.json`.
 */

import { readFileSync } from 'node:fs'
import { join } from 'node:path'

import { describe, it, expect } from 'vitest'

import { CATALOGS } from './catalogs'
import { SUPPORTED_LANGUAGES } from './languages'

const NAME_KEY = 'pages.chatSidebar.crew_attention_name'
const TARGET_KEYS = [
  'pages.chatSidebar.crew_attention_go_approval',
  'pages.chatSidebar.crew_attention_go_question',
  'pages.chatSidebar.crew_attention_go_working',
]

const GENERATED = new Set(SUPPORTED_LANGUAGES.filter(l => l.devOnly).map(l => l.code))

function resolve(catalog: Record<string, unknown>, dotted: string): unknown {
  let node: unknown = catalog
  for (const part of dotted.split('.')) {
    if (typeof node !== 'object' || node === null) return undefined
    node = (node as Record<string, unknown>)[part]
  }
  return node
}

describe('crew group attention button: its name is its visible text', () => {
  const shipped = Object.entries(CATALOGS).filter(([code]) => !GENERATED.has(code))

  it.each(shipped.map(([code]) => code))('%s: the name template adds only a pause between target and counts', code => {
    const catalog = (CATALOGS[code] as { translation: Record<string, unknown> }).translation
    const template = resolve(catalog, NAME_KEY)
    expect(typeof template, `${code} is missing ${NAME_KEY}`).toBe('string')
    const text = template as string
    const target = text.indexOf('{{target}}')
    const counts = text.indexOf('{{counts}}')
    expect(target, `${code}: ${NAME_KEY} must hold {{target}}, got "${text}"`).toBeGreaterThanOrEqual(0)
    expect(counts, `${code}: ${NAME_KEY} must hold {{counts}} after {{target}}, got "${text}"`).toBeGreaterThan(target)
    const rest = text.replace('{{target}}', '').replace('{{counts}}', '')
    expect(/^[\p{P}\s]*$/u.test(rest), `${code}: ${NAME_KEY} may add only punctuation, got "${text}"`).toBe(true)
    for (const key of TARGET_KEYS) {
      const phrase = resolve(catalog, key)
      expect(typeof phrase === 'string' && phrase.trim().length > 0, `${code} is missing ${key}`).toBe(true)
    }
  })

  it('the header draws its text and its name from these keys', () => {
    // Liveness: if the button stopped reading these keys, the checks above would
    // keep passing against catalog entries nothing renders.
    const source = readFileSync(join(__dirname, '..', 'pages', 'chat-sidebar', 'CrewGroups.tsx'), 'utf-8')
    for (const key of [NAME_KEY, ...TARGET_KEYS]) expect(source).toContain(`'${key}'`)
  })
})
