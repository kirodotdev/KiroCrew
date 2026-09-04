/**
 * Vocabulary-collision pin for the stale-collapse expander (per locale).
 *
 * The sidebar carries THREE age-related surfaces whose wording must stay
 * distinguishable in every language:
 *   - `stale_collapse_row` — the per-folder expander hiding OPEN sessions in
 *     place (this feature; nothing is archived),
 *   - `older_sessions` — the bottom pane listing CLOSED, archived sessions,
 *   - the Clean Up dialog's inactive/archive wording
 *     (`no_inactive_sessions_to_archive`).
 *
 * The en.context.json entry states this constraint in prose, but prose does
 * not gate: the first translation pass converged onto the older-sessions
 * wording, and the second onto Clean Up's "inactive" register — each read
 * fine per locale and collided anyway. This test is the mechanical pin.
 */
import { describe, it, expect } from 'vitest'
import { CATALOGS } from '../i18n/catalogs'

interface Pages { chatSidebar?: Record<string, string> }

describe('stale-collapse wording stays distinct per locale', () => {
  for (const [tag, catalog] of Object.entries(CATALOGS)) {
    const cs = (catalog.translation as { pages?: Pages } | undefined)?.pages?.chatSidebar
    if (!cs?.stale_collapse_row) continue
    it(`${tag}: expander label collides with neither the Older Sessions pane nor Clean Up`, () => {
      const row = cs.stale_collapse_row
      expect(row).not.toBe(cs.older_sessions)
      expect(row).not.toBe(cs.older_sessions_2)
      const archive = cs.no_inactive_sessions_to_archive
      if (archive) expect(archive.includes(row)).toBe(false)
    })
  }
})
