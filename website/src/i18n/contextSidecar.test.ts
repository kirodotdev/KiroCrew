/**
 * Translator-context sidecar guards.
 *
 * `en.context.json` maps a catalog key to one sentence of translator context.
 * It exists because a short English string is often shorter than its meaning:
 * `"KB"` is kilobytes here and could read as "knowledge base" (a real feature in
 * this product) there; `"v"` is a version prefix; `"K"` is a physical keyboard
 * key that must not be translated at all; `"Get"` is a verb meaning install;
 * `"Run"` is a verb and not the noun several locales would otherwise pick. A
 * translator working from the catalog alone cannot tell.
 *
 * ## Why a sidecar and not an inline comment
 *
 * Of the mechanisms surveyed, inline context is the one that actively hurts.
 * VS Code's translation key is `message + "/" + comment`, so *adding or editing*
 * a comment orphans every existing translation of that string. GNOME/KDE reach
 * the same conclusion from the freeze side: "addition of a comment aimed for
 * translators" is exempt from a string freeze, but "adding a context marker
 * (msgctxt) is also subject to the freeze as it renders the affected string
 * 'fuzzy'". A separate file makes a description free to write, edit and improve
 * at any time — which is the only way descriptions actually get written.
 *
 * ## The opaque class is two defects wearing one shape
 *
 * Filtering the catalog to values of three characters or fewer finds 110 keys,
 * and they are not one problem:
 *
 *   - **Abbreviations, units and key caps** — `KB`, `PID`, `RSS`, `30s`, `Esc`,
 *     `v`. Legitimate strings whose English cannot carry its own meaning. These
 *     are what this file is for.
 *   - **Sentence fragments** — `of` (×7), `or` (×3), `at` (×2), `by` (×3), `· v`,
 *     `→ v`, `PR:`. These are D1: a sentence split across `{t()}` calls, pinning
 *     every target language to English clause order. The fix is to delete the
 *     key in Phase 3, not to describe it — a description would *legitimise* the
 *     fragment, and `qa.test.ts`'s bare-connector check already owns them.
 *
 * So the ratchet below excludes the fragment class and reports it separately.
 * Double-gating it here would make the two defects indistinguishable and push
 * the next contributor toward the wrong fix.
 *
 * ## Gated vs ratcheted
 *
 * Gated: no orphan entries (the failure a key rename leaves behind, and the
 * reason this file cannot become a write-only dump), descriptions are prose
 * rather than a restatement of the value, and no description is copy-pasted
 * across *different* values.
 *
 * Also gated, at zero: coverage of the opaque non-fragment class. Splitting the
 * fragments out left a set small enough to finish, so this is a hard gate rather
 * than the ratchet the plan budgeted for — every abbreviation, unit and key cap
 * in the catalog now carries context, and a new one cannot land without it. The
 * constant stays as a named number so the failure message can explain itself.
 *
 * Coverage is deliberately NOT required catalog-wide. Demanding 4048
 * descriptions produces 4048 restatements of the value, which is worse than
 * nothing: it buries the ones that carry information.
 */

import { describe, it, expect } from 'vitest'

import { CATALOGS as RUNTIME_CATALOGS } from './index'
import { DEFAULT_LANGUAGE } from './languages'
import contextSidecar from './en.context.json'

/** Flatten a nested catalog to dotted leaf paths → string values. */
function flatten(obj: unknown, prefix = ''): Record<string, string> {
  const out: Record<string, string> = {}
  if (obj === null || typeof obj !== 'object') return out
  for (const [key, value] of Object.entries(obj as Record<string, unknown>)) {
    const path = prefix ? `${prefix}.${key}` : key
    if (value !== null && typeof value === 'object') {
      Object.assign(out, flatten(value, path))
    } else {
      out[path] = String(value)
    }
  }
  return out
}

const EN = flatten(
  (RUNTIME_CATALOGS[DEFAULT_LANGUAGE] as { translation: unknown }).translation,
)

const ENTRIES: Record<string, string> = contextSidecar.entries

/**
 * English function words that only ever appear here as a fragment of a larger
 * sentence assembled in JSX. Owned by Phase 3 (delete the key) and by
 * `qa.test.ts`'s bare-connector check — not by this file.
 */
const CONNECTORS = new Set([
  'a', 'ago', 'an', 'and', 'as', 'at', 'by', 'for', 'from', 'in', 'is', 'of',
  'on', 'or', 'per', 'the', 'to', 'via', 'with',
])

/** Trimmed value with trailing/leading punctuation and decoration removed. */
function core(value: string): string {
  return value.replace(/[\s·→—–:,.·]+/g, ' ').trim()
}

function isFragment(value: string): boolean {
  // A trailing colon marks a label (`In:`, `Out:`), which is a complete unit of
  // its own — not a preposition dangling in front of the next JSX node.
  if (value.trim().endsWith(':')) return false
  // Leading OR trailing decoration (`· v`, `d ·`) means the value only reads as
  // part of a larger line assembled in JSX — a fragment by construction.
  if (/^[·→—–]|[·→—–]$/.test(value.trim())) return true
  const c = core(value)
  // A bare connector, or a connector plus decoration (`· v`, `→ v`, `PR:` is not
  // one — that is an abbreviation with a colon, caught as opaque below).
  return CONNECTORS.has(c.toLowerCase())
}

/** Values of three visible characters or fewer that contain a letter. */
const SHORT = Object.entries(EN).filter(([, v]) => {
  const t = v.trim()
  return t.length > 0 && t.length <= 3 && /[A-Za-z]/.test(t) && !t.includes('{{')
})

const OPAQUE = SHORT.filter(([, v]) => !isFragment(v)).map(([k]) => k)
const FRAGMENTS = SHORT.filter(([, v]) => isFragment(v)).map(([k]) => k)

/**
 * Opaque keys allowed to lack a description. Zero: the class is fully covered,
 * so a new abbreviation, unit or key cap must arrive with its context. Never
 * raise this to make a build pass — add the entry instead. It is a one-line
 * sentence, and the alternative is a translator guessing.
 */
const UNDESCRIBED_OPAQUE_BASELINE = 0

describe('en.context.json shape', () => {
  it('is a sidecar, so no description can orphan a translation', () => {
    // The point of the whole mechanism: descriptions live outside the catalogs
    // the runtime loads, so editing one can never change a translation key.
    expect(Object.keys(EN)).not.toContain('_comment')
    expect(Object.keys(EN)).not.toContain('entries')
  })

  it('has no orphan entries', () => {
    const orphans = Object.keys(ENTRIES).filter(k => !(k in EN))
    expect(
      orphans,
      `renamed or deleted keys whose description remains — update or drop it:\n  ${orphans.join('\n  ')}`,
    ).toEqual([])
  })

  it('describes rather than restates', () => {
    const bad: string[] = []
    for (const [key, description] of Object.entries(ENTRIES)) {
      const value = EN[key] ?? ''
      const d = description.trim()
      if (d.length < 20) bad.push(`${key}: too short to say anything (${d.length} chars)`)
      else if (d.toLowerCase() === value.trim().toLowerCase()) bad.push(`${key}: restates the value`)
      else if (!/[.!?]$/.test(d)) bad.push(`${key}: not a sentence — end it with a period`)
    }
    expect(bad, `\n  ${bad.join('\n  ')}`).toEqual([])
  })

  it('does not reuse one description across different strings', () => {
    // Sharing a description across keys that hold the *same* value is correct —
    // `"v"` means the same thing in all 12 places it appears (which is its own
    // defect, D6, not this file's problem). Sharing one across *different*
    // values is copy-paste, and makes the description wrong for at least one.
    const byDescription = new Map<string, Set<string>>()
    for (const [key, description] of Object.entries(ENTRIES)) {
      const norm = description.trim().toLowerCase()
      if (!byDescription.has(norm)) byDescription.set(norm, new Set())
      // Case-insensitive: `pod` and `Pod` are the same string wearing two capitalisations.
      byDescription.get(norm)!.add((EN[key] ?? '').trim().toLowerCase())
    }
    const bad = [...byDescription.entries()]
      .filter(([, values]) => values.size > 1)
      .map(([d, values]) => `${[...values].map(v => JSON.stringify(v)).join(' / ')} share: ${d.slice(0, 60)}…`)
    expect(bad, `\n  ${bad.join('\n  ')}`).toEqual([])
  })
})

describe('opaque-key coverage', () => {
  it('finds both short-string classes', () => {
    // Guard against the ratchet silently passing because a filter broke.
    expect(OPAQUE.length).toBeGreaterThan(50)
    expect(FRAGMENTS.length).toBeGreaterThan(5)
  })

  it('leaves no opaque key without translator context', () => {
    const undescribed = OPAQUE.filter(k => !(k in ENTRIES)).sort()
    expect(
      undescribed.length,
      undescribed.length > UNDESCRIBED_OPAQUE_BASELINE
        ? `New short string(s) with no translator context. Three characters or fewer cannot ` +
          `carry their own meaning — add an entry to en.context.json naming the surface and the ` +
          `sense meant.\n  ${undescribed.map(k => `${k} = ${JSON.stringify(EN[k])}`).join('\n  ')}`
        : `Baseline is stale: ${undescribed.length} undescribed opaque keys remain but the ` +
          `baseline still says ${UNDESCRIBED_OPAQUE_BASELINE}. Lower it to ${undescribed.length}.`,
    ).toBe(UNDESCRIBED_OPAQUE_BASELINE)
  })

  it('does not describe sentence fragments', () => {
    // Phase 3 deletes these keys. A description here would argue for keeping them.
    const described = FRAGMENTS.filter(k => k in ENTRIES)
    expect(
      described,
      `these are D1 fragments, not abbreviations — delete the key in Phase 3 instead of ` +
        `describing it:\n  ${described.map(k => `${k} = ${JSON.stringify(EN[k])}`).join('\n  ')}`,
    ).toEqual([])
  })
})
