/**
 * Vocabulary-collision pin for the stale-collapse expander (per locale).
 *
 * The sidebar carries THREE age-related surfaces whose wording must stay
 * distinguishable in every language:
 *   - `stale_collapse_row_hidden` / `stale_collapse_row_shown` — the
 *     per-folder expander hiding OPEN sessions in place (this feature; nothing
 *     is archived),
 *   - `older_sessions` — the bottom pane listing CLOSED, archived sessions,
 *   - the Clean Up dialog's inactive/archive wording
 *     (`no_inactive_sessions_to_archive`).
 *
 * The en.context.json entry states this constraint in prose, but prose does
 * not gate: the first translation pass converged onto the older-sessions
 * wording, and the second onto Clean Up's "inactive" register — each read
 * fine per locale and collided anyway. This test is the mechanical pin.
 *
 * The expander label is a pluralised SENTENCE, so exact equality with the
 * neighbouring phrases would never fire again ("Older sessions hidden" is not
 * equal to "Older sessions"). The pin therefore checks CONTAINMENT of the
 * neighbours' distinctive vocabulary inside every plural form of both labels:
 *   - the whole `older_sessions` phrase,
 *   - its modifier — the phrase with the plain session noun removed
 *     ("Older", "Ältere", "以前の"), so a sentence that borrows the adjective
 *     alone is caught too,
 *   - every word of the Clean Up phrase other than the session noun and
 *     short function words ("inactive", "archive", "非アクティブ…").
 * The session noun itself is shared by all three surfaces on purpose and is
 * never a collision.
 */
import { describe, it, expect } from 'vitest'
import { CATALOGS } from '../i18n/catalogs'

interface Pages { chatSidebar?: Record<string, string> }

const EXPANDER_BASES = ['stale_collapse_row_hidden', 'stale_collapse_row_shown'] as const

const norm = (s: string) => s.replace(/\{\{\w+\}\}/g, ' ').replace(/\s+/g, ' ').trim().toLowerCase()

/** Every shipped plural form of the two expander labels, placeholder removed. */
function expanderForms(cs: Record<string, string>): string[] {
  return Object.entries(cs)
    .filter(([k]) => EXPANDER_BASES.some(base => k.startsWith(`${base}_`)))
    .map(([, v]) => norm(v))
}

/** The plain session noun in every plural form the locale ships, as tokens. */
function nounTokens(cs: Record<string, string>): string[] {
  return Object.entries(cs)
    .filter(([k]) => /^session(_(one|two|few|many|other|zero))?$/.test(k))
    .flatMap(([, v]) => norm(v).split(' '))
    .filter(Boolean)
}

/** `phrase` with every session-noun token removed, e.g. "Older sessions" → "older". */
function withoutNoun(phrase: string, nouns: string[]): string {
  let out = norm(phrase)
  for (const n of nouns) out = out.split(n).join(' ')
  return out.replace(/\s+/g, ' ').trim()
}

/**
 * Distinctive words of a phrase: split on the session noun, then on
 * whitespace, drop punctuation and short function words (≤ 3 chars). For
 * scripts without word spaces the pieces between the noun survive whole.
 */
function distinctiveTokens(phrase: string, nouns: string[]): string[] {
  return withoutNoun(phrase, nouns)
    .split(' ')
    .map(t => t.replace(/^[\p{P}\p{S}]+|[\p{P}\p{S}]+$/gu, ''))
    .filter(t => t.length > 3)
}

describe('stale-collapse wording stays distinct per locale', () => {
  for (const [tag, catalog] of Object.entries(CATALOGS)) {
    const cs = (catalog.translation as { pages?: Pages } | undefined)?.pages?.chatSidebar
    if (!cs) continue
    const forms = expanderForms(cs)
    if (forms.length === 0) continue
    const nouns = nounTokens(cs)

    it(`${tag}: expander label collides with neither the Older Sessions pane nor Clean Up`, () => {
      expect(nouns.length).toBeGreaterThan(0)
      const older = [cs.older_sessions, cs.older_sessions_2].filter(Boolean).map(norm)
      const olderModifiers = older.map(o => withoutNoun(o, nouns)).filter(Boolean)
      // The modifier must exist, or the containment check below is vacuous.
      expect(olderModifiers.length).toBe(older.length)
      const archive = cs.no_inactive_sessions_to_archive ? norm(cs.no_inactive_sessions_to_archive) : ''
      const archiveWords = archive ? distinctiveTokens(archive, nouns) : []
      for (const row of forms) {
        expect(row).not.toBe('')
        for (const o of older) {
          expect(row, `contains the Older Sessions phrase "${o}"`).not.toContain(o)
        }
        for (const m of olderModifiers) {
          expect(row, `borrows the Older Sessions modifier "${m}"`).not.toContain(m)
        }
        if (archive) {
          expect(archive.includes(row), `is a fragment of the Clean Up phrase`).toBe(false)
          for (const w of archiveWords) {
            expect(row, `borrows the Clean Up word "${w}"`).not.toContain(w)
          }
        }
      }
    })

    it(`${tag}: the collapsed label says the rows are hidden, not just that they exist`, () => {
      // The whole point of the sentence: a reader must be told the rest of the
      // folder's count is *in here*. The two states may not share a label.
      const hidden = Object.entries(cs).filter(([k]) => k.startsWith('stale_collapse_row_hidden_')).map(([, v]) => v)
      const shown = Object.entries(cs).filter(([k]) => k.startsWith('stale_collapse_row_shown_')).map(([, v]) => v)
      expect(hidden.length).toBeGreaterThan(0)
      expect(shown.length).toBe(hidden.length)
      for (const h of hidden) expect(shown).not.toContain(h)
    })
  }
})
