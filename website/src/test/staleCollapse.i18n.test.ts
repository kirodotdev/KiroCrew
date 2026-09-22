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
 * The expander label is a pluralised sentence, so the pin runs over every
 * plural form the locale ships, with the `{{count}}` placeholder stripped —
 * a translation that reads "Older sessions" plus a number is still the
 * collision this file exists to catch.
 */
import { describe, it, expect } from 'vitest'
import { CATALOGS } from '../i18n/catalogs'

interface Pages { chatSidebar?: Record<string, string> }

const EXPANDER_BASES = ['stale_collapse_row_hidden', 'stale_collapse_row_shown'] as const

/** Every shipped plural form of the two expander labels, placeholder removed. */
function expanderForms(cs: Record<string, string>): string[] {
  return Object.entries(cs)
    .filter(([k]) => EXPANDER_BASES.some(base => k.startsWith(`${base}_`)))
    .map(([, v]) => v.replace(/\{\{count\}\}/g, '').replace(/\s+/g, ' ').trim())
}

describe('stale-collapse wording stays distinct per locale', () => {
  for (const [tag, catalog] of Object.entries(CATALOGS)) {
    const cs = (catalog.translation as { pages?: Pages } | undefined)?.pages?.chatSidebar
    if (!cs) continue
    const forms = expanderForms(cs)
    if (forms.length === 0) continue
    it(`${tag}: expander label collides with neither the Older Sessions pane nor Clean Up`, () => {
      const archive = cs.no_inactive_sessions_to_archive
      for (const row of forms) {
        expect(row).not.toBe('')
        expect(row).not.toBe(cs.older_sessions)
        expect(row).not.toBe(cs.older_sessions_2)
        if (archive) expect(archive.includes(row)).toBe(false)
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
