/**
 * French style guards.
 *
 * Encodes mechanically checkable rules from `style/fr.md`. The critical rule —
 * narrow no-break space before double punctuation — is checked here as a
 * warning-level baseline rather than a hard gate, since the existing catalog
 * was not authored with it.
 */

import { describe, it, expect } from 'vitest'
import { execFileSync } from 'node:child_process'
import { join } from 'node:path'
import { CATALOGS as RUNTIME_CATALOGS } from '../catalogs'

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

const bundle = (code: string) =>
  flatten((RUNTIME_CATALOGS as Record<string, { translation: unknown }>)[code].translation)

const fr = bundle('fr')

function report(bad: string[], limit = 6): string {
  return `${bad.length} violation(s):\n  ${bad.slice(0, limit).join('\n  ')}`
}

describe('fr punctuation (style/fr.md §1)', () => {
  it('double punctuation is not glued to the preceding word', () => {
    // French requires a space (ideally U+202F) before ; : ? !
    // This test catches the worst case: NO space at all before these marks.
    // A regular space is imperfect but acceptable; no space is wrong.
    const bad: string[] = []
    for (const [key, value] of Object.entries(fr)) {
      // Skip very short values (likely single-char or symbols)
      if (value.length < 3) continue
      // Match a letter directly followed by ; : ? ! (no space at all)
      if (/[a-zA-Zàâéèêëïîôùûüÿçœæ][;:?!]/.test(value)) {
        // Exclude URLs, code-like patterns
        if (/https?:\/\//.test(value)) continue
        if (/\{\{.*\}\}/.test(value)) continue
        // Colons in time patterns like 10:30 are fine
        if (/\d:\d/.test(value)) continue
        bad.push(`${key}: ${JSON.stringify(value.slice(0, 60))}`)
      }
    }
    // Baselined: existing catalog was not authored with this rule
    expect(bad.length, report(bad)).toBeLessThanOrEqual(50)
  })

  it('uses tu/toi forms, not vous', () => {
    // Check for formal vous where it clearly addresses the user
    const bad = Object.entries(fr)
      .filter(([, v]) => /\bVous\b/.test(v) && !/\bvous\b/.test(v))
      .map(([k]) => k)
    // "vous" lowercase can appear in many contexts; only flag uppercase "Vous" at sentence start
    expect(bad.length, report(bad)).toBeLessThanOrEqual(11)
  })
})

/* ── §1 spacing, scoped to the values THIS BRANCH wrote ── */

const REPO = join(__dirname, '..', '..', '..', '..')
const CATALOG = 'website/src/i18n/locales/fr.json'

/**
 * A letter, then a plain space or nothing, then `;` `:` `?` `!`.
 *
 * The baselined check above accepts U+0020 ("imperfect but acceptable"), so it
 * can only catch a MISSING space and the exact defect §1 names — the wrong space
 * — is invisible to it. Zero tolerance is affordable on the values a branch
 * writes even though the inherited catalog is mostly U+0020.
 */
const WRONG_DOUBLE_SPACE = /[a-zA-Zàâéèêëïîôùûüÿçœæ][\u0020]?[;:?!]/

/**
 * The fr values this branch added or edited, or null when there is nothing to
 * diff against (`I18N_BASE_REF` unset — a bare local run; CI always supplies it).
 *
 * Diff scope rather than a catalog-wide sweep, and the same reasoning as
 * `bnStyle`'s register gate: the inherited catalog predates the rule, so a
 * repo-wide assertion could only land as another baselined COUNT, and a count
 * cannot tell "one fixed" from "one fixed and one broken". Nothing is stored, so
 * two i18n branches have no ledger line to conflict on.
 */
function changedFrValues(): Record<string, string> | null {
  const baseRef = process.env.I18N_BASE_REF
  if (!baseRef) return null
  const git = (args: string[]) =>
    execFileSync('git', args, { cwd: REPO, encoding: 'utf-8', maxBuffer: 64 * 1024 * 1024 })
  // A ref that IS configured but cannot be resolved throws: a gate that cannot
  // run must fail, never pass quietly.
  git(['rev-parse', '--verify', `${baseRef}^{commit}`])
  let from: string
  try {
    from = git(['merge-base', baseRef, 'HEAD']).trim()
  } catch {
    // CI checks out at depth 1 and fetches the base at depth 1 too, so there is
    // no shared history to find a merge base in; the base tip needs only the two
    // trees.
    from = baseRef
  }
  let base: Record<string, string> = {}
  try {
    base = flatten(JSON.parse(git(['show', `${from}:${CATALOG}`])))
  } catch {
    // No catalog at the base ref: every value in it is this branch's.
  }
  return Object.fromEntries(Object.entries(fr).filter(([key, value]) => base[key] !== value))
}

describe('fr spacing (style/fr.md §1)', () => {
  it('[changed-values] puts U+202F before double punctuation, never a plain space', () => {
    const changed = changedFrValues()
    if (changed === null) {
      // eslint-disable-next-line no-console -- stdout IS this gate's report channel: a gate that returns silently is one nobody can tell ran, and this skip is reachable on a bare local run
      console.log('[changed-values] skipped — I18N_BASE_REF is unset, so there is no branch to diff.')
      return
    }
    const bad = Object.entries(changed)
      .filter(([, value]) => {
        // Mask code tokens rather than skip the whole value, so a real §1
        // colon-spacing error elsewhere in the same value is still caught.
        // Strip any URI-scheme token (https://…, secret://…) and a d:d time —
        // the `:` inside a URI scheme or a time is a code token, not French
        // punctuation. A Windows drive-letter path (`D:\`, `D:\chemin\vers`)
        // is likewise a literal the user types, not a French colon: strip that
        // token too and test the REST of the value, so prose beside a path or
        // URL is still held to §1 rather than riding in on the code token.
        const masked = value
          .replace(/[a-z][a-z0-9+.-]*:\/\/\S*/gi, ' ')
          .replace(/\b[A-Za-z]:[\\/]\S*/g, ' ')
          .replace(/\d:\d/g, ' ')
        return WRONG_DOUBLE_SPACE.test(masked)
      })
      .map(([key, value]) => `${key}: ${JSON.stringify(value.slice(0, 60))}`)
    expect(bad, `${report(bad)}\n\nThere is no ceiling to raise for these — the value is yours.`)
      .toEqual([])
  })
})

describe('fr accents (style/fr.md §7)', () => {
  it('capitals have their accents', () => {
    // Common violations: "Etat" should be "État", "A propos" should be "À propos"
    const MUST_ACCENT: Array<[string, string]> = [
      ['Etat', 'État'],
      ['Ecran', 'Écran'],
      ['Element', 'Élément'],
      ['Evenement', 'Événement'],
    ]
    const bad: string[] = []
    for (const [key, value] of Object.entries(fr)) {
      for (const [wrong, correct] of MUST_ACCENT) {
        if (value.includes(wrong) && !value.includes(correct)) {
          bad.push(`${key}: has '${wrong}' not '${correct}'`)
        }
      }
    }
    expect(bad.length, report(bad)).toBeLessThanOrEqual(11)
  })
})
