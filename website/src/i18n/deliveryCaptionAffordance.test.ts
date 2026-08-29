/** UX Review at 9a26ecb74: the shared unconfirmed caption promised a "Dismiss" affordance that two of
 *  its three render sites do not have -- the steer-path notice row, and the arm whose button reads
 *  "Discard message". The consequence sentence is its own key now, rendered only on the Dismiss arm.
 *
 *  Pinned per catalog rather than in English alone: the split was applied to twelve translations, and
 *  a re-merge that folded the sentence back would otherwise only surface as a UX report.
 */
import { describe, it, expect } from 'vitest'
import { readFileSync, readdirSync } from 'node:fs'
import { join } from 'node:path'

const DIR = join(__dirname, 'locales')
const chatPage = (name: string): Record<string, string> => {
  const d = JSON.parse(readFileSync(join(DIR, name), 'utf-8'))
  return (d?.pages?.chatPage ?? {}) as Record<string, string>
}

// The clause each locale uses for the dismiss affordance, so the assertion reads the real translation
// rather than looking for an English word inside it.
const DISMISS_CLAUE: Record<string, string> = {
  'en.manual.json': 'removing it',
  'de.json': 'Entfernen',
  'es.json': 'eliminarlo',
  'fr.json': 'le retirer',
  'it.json': 'rimuoverlo',
  'pt.json': 'removê-la',
  'ru.json': '\u0443\u0434\u0430\u043b\u0435\u043d\u0438\u0435',
  'ja.json': '\u524a\u9664',
  'ko.json': '\u2020',
  'zh-CN.json': '\u79fb\u9664',
  'bn.json': '\u2020',
  'hi.json': '\u2020',
}

describe('the unconfirmed caption does not promise an affordance its render site lacks', () => {
  const catalogs = readdirSync(DIR).filter(f => f.endsWith('.json') && f !== 'en-XA.json')

  it('carries the consequence sentence in a SEPARATE key, in every catalog that has the caption', () => {
    let checked = 0
    for (const f of catalogs) {
      const p = chatPage(f)
      if (!p.delivery_unconfirmed_resend) continue
      checked++
      expect(p.delivery_unconfirmed_dismiss_note, `${f} lost the split note`).toBeTruthy()
      const clause = DISMISS_CLAUE[f]
      if (clause && clause !== '\u2020') {
        expect(p.delivery_unconfirmed_dismiss_note, `${f} note must carry the clause`).toContain(clause)
        expect(p.delivery_unconfirmed_resend, `${f} caption must NOT mention dismissing`).not.toContain(clause)
      }
    }
    expect(checked, 'positive control: the caption was actually found').toBeGreaterThanOrEqual(12)
  })

  it('names the control by the SAME term its own buttons use', () => {
    // English is the source of the wording, so it is the one that can be asserted verbatim.
    const en = chatPage('en.manual.json')
    expect(en.delivery_unconfirmed_resend).toBe(
      'Delivery unconfirmed \u2014 your text is back in the composer; resending may send it twice.',
    )
    expect(en.delivery_clear).toContain('composer')
    expect(en.delivery_unconfirmed_resend,
      'one strip must not give one control two names').not.toContain('text box')
    expect(en.delivery_unconfirmed_dismiss_note).toBe(
      'If this message never reached the server, removing it takes it out of this conversation; its text returns to the composer.',
    )
  })
})
