/**
 * Guards on destructive-confirmation copy across every shipped language.
 *
 * A mistranslated count badge is cosmetic. A mistranslated *confirmation* is
 * not: it either blocks a user from completing an action they intend, or
 * describes a destructive action inaccurately enough that they consent to
 * something they did not mean. Both are real failure modes, so both are
 * asserted here.
 */

import { describe, it, expect } from 'vitest'

import { CATALOGS } from './index'
import { SUPPORTED_LANGUAGES, DEFAULT_LANGUAGE } from './languages'
import { BULK_DELETE_TOKEN } from '../pages/SchedulePage'
import { BULK_PR_CLOSE_TOKEN, SEQUENTIAL_MERGE_TOKEN } from '../apps/issue-radar/components/PrBulkBar'

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

  it('the PR bulk-close token is a code constant, never a catalog value', () => {
    // Same rule, second call site: Issue Radar's bulk close gates on a typed
    // token. The hazard is identical — a translated token locks every non-English
    // user out of the action — but the guard has to name this token explicitly,
    // because the assertion above only scans the `pages.schedulePage.` prefix.
    expect(BULK_PR_CLOSE_TOKEN).toBe('close prs')
    // It must not collide with ANY label in the bar, in any language: a token equal
    // to the button the user just pressed can be satisfied by copying that button,
    // which is not the deliberate second act a confirmation is for.
    for (const { code } of AUTHORED) {
      const offenders = Object.entries(FLAT[code])
        .filter(([k, v]) =>
          k.startsWith('apps.issueRadar.components.prBulkBar.')
          && v.trim().toLowerCase() === BULK_PR_CLOSE_TOKEN)
        .map(([k]) => k)
      expect(offenders, `${code} exposes the PR close token as copy: ${offenders.join(', ')}`)
        .toEqual([])
    }
  })

  it('the PR sequential-merge token is a code constant, never a catalog value', () => {
    // Third call site, same rule. This token guards the one IRREVERSIBLE action in the
    // bar, so a translation that happened to equal it would be the worst version of
    // this bug: the confirmation could be satisfied by copying a visible label.
    expect(SEQUENTIAL_MERGE_TOKEN).toBe('merge prs')
    // And it must differ from the close token, or typing one would arm the other —
    // two irreversibly different actions behind one phrase.
    expect(SEQUENTIAL_MERGE_TOKEN).not.toBe(BULK_PR_CLOSE_TOKEN)
    for (const { code } of AUTHORED) {
      const offenders = Object.entries(FLAT[code])
        .filter(([k, v]) =>
          k.startsWith('apps.issueRadar.components.prBulkBar.')
          && v.trim().toLowerCase() === SEQUENTIAL_MERGE_TOKEN)
        .map(([k]) => k)
      expect(offenders, `${code} exposes the PR merge token as copy: ${offenders.join(', ')}`)
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
    // Auto-Improvement's commit confirmation: it pushes to a real branch and a published
    // commit cannot be recalled, so an operator reading it in English they do not speak is
    // being asked to authorize an irreversible remote change they cannot evaluate.
    'autoImprovement.commitConfirm',
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
