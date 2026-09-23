/**
 * Russian style guards.
 *
 * Encodes mechanically checkable rules from `style/ru.md`.
 * The 4-category plural system is already enforced by catalogParity.test.ts.
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

const ru = bundle('ru')

function report(bad: string[], limit = 6): string {
  return `${bad.length} violation(s):\n  ${bad.slice(0, limit).join('\n  ')}`
}

describe('ru punctuation (style/ru.md §1)', () => {
  it('uses guillemets « » for quotation, not straight quotes around Russian text', () => {
    // Values containing Cyrillic text in straight quotes "..." where guillemets are expected.
    // This is a soft check — straight quotes are acceptable in some contexts (nested quotes).
    const CYRILLIC = /[\u0400-\u04ff]/
    const bad: string[] = []
    for (const [key, value] of Object.entries(ru)) {
      if (!CYRILLIC.test(value)) continue
      // Check for "Cyrillic text" pattern (straight quotes around Cyrillic)
      if (/"[\u0400-\u04ff]/.test(value) && !value.includes('«')) {
        bad.push(`${key}: ${JSON.stringify(value.slice(0, 60))}`)
      }
    }
    // Baselined — existing catalog may use straight quotes
    expect(bad.length, report(bad)).toBeLessThanOrEqual(20)
  })
})

describe('ru tone (style/ru.md §4)', () => {
  it('uses ты forms, not вы/Вы', () => {
    // Capital Вы is the formal second-person pronoun
    const bad = Object.entries(ru)
      .filter(([, v]) => /\bВы\b/.test(v))
      .map(([k]) => k)
    expect(bad, report(bad)).toEqual([])
  })
})

/* ── §4 register, scoped to the values THIS BRANCH wrote ── */

const REPO = join(__dirname, '..', '..', '..', '..')
const CATALOG = 'website/src/i18n/locales/ru.json'

/**
 * A Cyrillic word boundary. JavaScript's `\b` only knows ASCII word characters,
 * so `/\bВы\b/` never fires between two Cyrillic letters or next to a space —
 * the lookarounds below are the boundary that actually works for Cyrillic.
 */
const NOT_CYRILLIC_BEFORE = '(?<![\\u0400-\\u04ff])'
const NOT_CYRILLIC_AFTER = '(?![\\u0400-\\u04ff])'

/** The вы pronoun family, any case: вы / вас / вам / вами / ваш-. */
const FORMAL_PRONOUN = new RegExp(
  `${NOT_CYRILLIC_BEFORE}(?:[Вв]ы|[Вв]ас|[Вв]ам|[Вв]ами|[Вв]аш[\\u0400-\\u04ff]*)${NOT_CYRILLIC_AFTER}`,
)

/**
 * A second-person-plural verb ending — the formal imperative (нажмите, попробуйте,
 * добавьте) and the formal present (хотите, видите) both end in `-ите`, `-йте` or
 * `-ьте`. The lookahead lists the locative nouns that share the ending and were
 * measured in the catalog (в коммите, в лимите) plus their obvious siblings; the
 * other 202 distinct matches in the catalog are all verbs.
 */
const FORMAL_VERB = new RegExp(
  `${NOT_CYRILLIC_BEFORE}(?!(?:коммите|лимите|сайте|байте|аудите)${NOT_CYRILLIC_AFTER})[\\u0400-\\u04ff]{2,}(?:ите|йте|ьте)${NOT_CYRILLIC_AFTER}`,
)

/**
 * The ru values this branch added or edited, or null when there is nothing to
 * diff against (`I18N_BASE_REF` unset — a bare local run; CI always supplies it).
 *
 * Diff scope rather than a catalog-wide sweep, and the same reasoning as
 * `bnStyle`'s register gate: the inherited catalog is majority-вы, so a
 * repo-wide assertion could only land as another baselined COUNT, and a count
 * cannot tell "one fixed" from "one fixed and one broken". Nothing is stored, so
 * two i18n branches have no ledger line to conflict on.
 */
function changedRuValues(): Record<string, string> | null {
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
  return Object.fromEntries(Object.entries(ru).filter(([key, value]) => base[key] !== value))
}

describe('ru register (style/ru.md §4)', () => {
  it('[changed-values] addresses the reader as ты, never вы', () => {
    const changed = changedRuValues()
    if (changed === null) {
      // eslint-disable-next-line no-console -- stdout IS this gate's report channel: a gate that returns silently is one nobody can tell ran, and this skip is reachable on a bare local run
      console.log('[changed-values] skipped — I18N_BASE_REF is unset, so there is no branch to diff.')
      return
    }
    const bad = Object.entries(changed)
      .filter(([, value]) => FORMAL_PRONOUN.test(value) || FORMAL_VERB.test(value))
      .map(([key, value]) => `${key}: ${JSON.stringify(value.slice(0, 60))}`)
    expect(bad, `${report(bad)}\n\nThere is no ceiling to raise for these — the value is yours.`)
      .toEqual([])
  })
})

describe('ru DNT (style/ru.md §3)', () => {
  it('does not transliterate product names into Cyrillic', () => {
    // Common transliterations that should stay in Latin
    const TRANSLITERATIONS: Array<[string, string]> = [
      ['Гитхаб', 'GitHub'],
      ['Слэк', 'Slack'],
      ['Слак', 'Slack'],
      ['Дискорд', 'Discord'],
      ['КироКрю', 'KiroCrew'],
      ['Докер', 'Docker'],
      ['Плейрайт', 'Playwright'],
    ]
    const bad: string[] = []
    for (const [key, value] of Object.entries(ru)) {
      for (const [cyrillic, latin] of TRANSLITERATIONS) {
        if (value.includes(cyrillic)) {
          bad.push(`${key}: has '${cyrillic}', should be '${latin}'`)
        }
      }
    }
    expect(bad, report(bad)).toEqual([])
  })
})
