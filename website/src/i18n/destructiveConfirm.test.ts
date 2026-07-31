/**
 * Guards on destructive-confirmation copy across every shipped language.
 *
 * A mistranslated count badge is cosmetic. A mistranslated *confirmation* is
 * not: it either blocks a user from completing an action they intend, or
 * describes a destructive action inaccurately enough that they consent to
 * something they did not mean. Both happened during this conversion, so both
 * are asserted here rather than left to review.
 */

import { describe, it, expect } from 'vitest'

import { CATALOGS } from './index'
import { SUPPORTED_LANGUAGES, DEFAULT_LANGUAGE } from './languages'
import { BULK_DELETE_TOKEN } from '../pages/SchedulePage'

function flatten(obj: unknown, prefix = ''): Record<string, string> {
  const out: Record<string, string> = {}
  if (obj === null || typeof obj !== 'object') return out
  for (const [key, value] of Object.entries(obj as Record<string, unknown>)) {
    const path = prefix ? `${prefix}.${key}` : key
    if (value !== null && typeof value === 'object') Object.assign(out, flatten(value, path))
    else out[path] = String(value)
  }
  return out
}

const FLAT: Record<string, Record<string, string>> = Object.fromEntries(
  Object.entries(CATALOGS).map(([code, bundle]) => [
    code,
    flatten((bundle as { translation: unknown }).translation),
  ]),
)

/**
 * Authored catalogs only. The pseudolocale is a mechanical transform of English, so its
 * confirmation token is accented by construction and asserting on it would test the
 * generator, not the copy.
 */
const AUTHORED = SUPPORTED_LANGUAGES.filter(l => !l.devOnly)

const NON_DEFAULT = AUTHORED.filter(l => l.code !== DEFAULT_LANGUAGE)

describe('bulk-delete confirmation token', () => {
  it('is a code constant, never a catalog value', () => {
    // The token is compared verbatim against user input, so it must not be
    // reachable by a translator. If it ever became a catalog key, every
    // non-English user would be locked out of bulk delete.
    expect(BULK_DELETE_TOKEN).toBe('delete')
    for (const { code } of AUTHORED) {
      const offenders = Object.entries(FLAT[code])
        .filter(([k, v]) => k.startsWith('pages.schedulePage.') && v.trim() === BULK_DELETE_TOKEN)
        .map(([k]) => k)
      expect(offenders, `${code} exposes the safety token as copy: ${offenders.join(', ')}`)
        .toEqual([])
    }
  })

  it('keeps the instruction verb separate from the "Type" column header', () => {
    // English "Type" is a noun in the table header and an imperative verb in
    // the confirmation. One shared key forced translators to pick one meaning,
    // and es/pt both picked the noun ("Tipo delete para confirmar"), which is
    // not an instruction. Two keys is the fix; this asserts they stay two.
    for (const { code } of AUTHORED) {
      expect(FLAT[code]['pages.schedulePage.type_verb_to_confirm'],
        `${code} is missing the verb form`).toBeTruthy()
      expect(FLAT[code]['pages.schedulePage.type'],
        `${code} is missing the column header`).toBeTruthy()
    }
  })

  it('does not reuse the column-header noun as the instruction verb', () => {
    // In English the two are legitimately the same word. In a language that
    // distinguishes them, an identical value means the noun leaked into the
    // instruction — the exact es/pt defect.
    const same = NON_DEFAULT.filter(({ code }) =>
      FLAT[code]['pages.schedulePage.type_verb_to_confirm']
        === FLAT[code]['pages.schedulePage.type'])
      .map(({ code }) => code)
    expect(same, `verb and noun forms are identical in: ${same.join(', ')} — `
      + 'the instruction likely reads as a noun').toEqual([])
  })
})

describe('destructive confirmations are translated', () => {
  /**
   * Keys whose copy authorizes irreversible loss. Left in English, a
   * non-English user is asked to approve deletion in a language they may not
   * read — the one place a missing translation is a safety issue rather than a
   * cosmetic one.
   */
  const DESTRUCTIVE = [
    'pages.schedulePage.this_permanently_removes_the_selected_job_one',
    'pages.schedulePage.this_permanently_removes_the_selected_job_other',
    'pages.schedulePage.and_their_run_history_this_action_cannot_be_undo',
  ]

  for (const { code } of NON_DEFAULT) {
    it(`${code} translates them`, () => {
      const en = FLAT[DEFAULT_LANGUAGE]
      const untranslated = DESTRUCTIVE
        .filter(k => en[k] !== undefined && FLAT[code][k] === en[k])
      expect(untranslated, `${code} left destructive copy in English: ${untranslated.join(', ')}`)
        .toEqual([])
    })
  }
})
