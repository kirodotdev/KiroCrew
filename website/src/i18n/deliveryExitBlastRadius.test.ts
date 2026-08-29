/** The two failure-path exits differ in BLAST RADIUS, and only one of them says so.
 *
 *  `delivery_remove` drops the transcript bubble; `delivery_discard` also empties the composer, which
 *  is the user's only copy of an unsent prompt. When the two labels read as synonyms a user who met
 *  one cannot predict the other, and the wrong guess costs typed text -- so the destructive one must
 *  name the composer, in every locale, not merely use a different verb.
 */
import { describe, it, expect } from 'vitest'
import { readFileSync } from 'node:fs'
import { resolve } from 'node:path'

const L = 'src/i18n/locales/'
const read = (f: string) =>
  JSON.parse(readFileSync(resolve(process.cwd(), L + f), 'utf-8')) as Record<string, never>
const chatPage = (f: string) => {
  const d = read(f) as unknown as { pages?: { chatPage?: Record<string, string> } }
  return d.pages?.chatPage ?? {}
}

/** Longest common substring, so this works for locales that do not space their words. */
const shared = (a: string, b: string): number => {
  let best = 0
  for (let i = 0; i < a.length; i++) {
    for (let j = i + best + 1; j <= a.length; j++) {
      if (b.includes(a.slice(i, j))) best = Math.max(best, j - i)
      else break
    }
  }
  return best
}

// English is SPLIT across two files and is ONE logical catalogue.
const LOCALES = ['bn', 'de', 'es', 'fr', 'hi', 'it', 'ja', 'ko', 'pt', 'ru', 'zh-CN']
const strings = (loc: string) => {
  if (loc === 'en') return { ...chatPage('en.json'), ...chatPage('en.manual.json') }
  return chatPage(`${loc}.json`)
}

describe('the destructive exit names the composer it empties', () => {
  it('shares the composer wording with the clear label in every catalogue', () => {
    for (const loc of [...LOCALES, 'en']) {
      const s = strings(loc)
      expect(s.delivery_discard, `${loc}: premise: the labels exist`).toBeTruthy()
      expect(s.delivery_clear, `${loc}: premise: the clear label exists`).toBeTruthy()
      expect(shared(s.delivery_discard, s.delivery_clear),
        `${loc}: "${s.delivery_discard}" must name what "${s.delivery_clear}" names`)
        .toBeGreaterThanOrEqual(3)
    }
  })

  it('never lets the two exits read as bare synonyms', () => {
    for (const loc of [...LOCALES, 'en']) {
      const s = strings(loc)
      expect(s.delivery_discard, `${loc}: the exits are distinct`).not.toBe(s.delivery_remove)
      expect(s.delivery_discard.length,
        `${loc}: the wider blast radius needs the wider label`)
        .toBeGreaterThan(s.delivery_remove.length)
    }
  })
})
