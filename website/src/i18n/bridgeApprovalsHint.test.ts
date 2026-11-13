/**
 * The `bridge_approvals` feature preview's hint says how a chat app answers the
 * note it sends (#34821): Slack by the note's buttons, Telegram and Discord by a
 * reply. Every shipped catalog names all three, and English never says the note
 * has no buttons.
 */
import { describe, it, expect } from 'vitest'

import { CATALOGS } from './catalogs'
import { SUPPORTED_LANGUAGES } from './languages'

const GENERATED = new Set(SUPPORTED_LANGUAGES.filter(l => l.devOnly).map(l => l.code))

function hint(lang: string): unknown {
  const tab = (CATALOGS[lang].translation.pages as Record<string, Record<string, Record<string, unknown>>>)
    .developer.featurePreviewsTab
  return tab.bridge_approvals_desc
}

describe('bridge_approvals preview hint', () => {
  it('en never says the note has no buttons', () => {
    expect(hint('en')).not.toMatch(/no buttons/)
  })

  for (const lang of Object.keys(CATALOGS).filter(code => !GENERATED.has(code))) {
    it(`${lang} names Slack, Telegram and Discord`, () => {
      for (const app of ['Slack', 'Telegram', 'Discord']) expect(hint(lang)).toContain(app)
    })
  }
})
