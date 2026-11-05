/**
 * Issue Radar's PR bulk-bar confirmation tokens are code constants, never catalog
 * values, in every shipped language.
 *
 * The core holds its own bulk-delete token to the same rule in
 * `i18n/destructiveConfirm.test.ts`. A token is compared verbatim against what the
 * user types, so a translator must not be able to reach it.
 */

import { describe, it, expect } from 'vitest'

import { CATALOGS } from '../../i18n/catalogs'
import { SUPPORTED_LANGUAGES } from '../../i18n/languages'
import { BULK_PR_CLOSE_TOKEN, SEQUENTIAL_MERGE_TOKEN } from './components/PrBulkBar'

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

/** Authored catalogs only: the pseudolocale is a mechanical transform of English. */
const AUTHORED = SUPPORTED_LANGUAGES.filter(l => !l.devOnly)

describe('PR bulk-bar confirmation tokens', () => {
  it('the PR bulk-close token is a code constant, never a catalog value', () => {
    // Issue Radar's bulk close gates on a typed token, the rule the core's bulk
    // delete token is held to in `i18n/destructiveConfirm.test.ts`: a translated
    // token locks every non-English user out of the action.
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
    // Same rule. This token guards the one IRREVERSIBLE action in the
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
})
