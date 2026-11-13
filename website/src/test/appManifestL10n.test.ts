import { afterEach, describe, expect, it } from 'vitest'
import { i18next } from '../i18n/all'
import { appDescription, appDisplayName, appHighlights, appPageLabel } from '../components/appstore/appManifest'

/**
 * An app's own `l10n` block localises its listing copy when the dashboard has no
 * catalog keys for it: the reader's locale, then its base language, then the
 * manifest's English field. Content, not trust, so any origin qualifies.
 */
const L10N = {
  'zh-CN': { displayName: '探针' },
  zh: { displayName: 'base', description: '描述' },
  pt: { displayName: 'Sonda', description: 'Descrição', pageLabel: 'Página', highlights: ['um', 'dois'] },
  de: { displayName: 'x'.repeat(121) },
  constructor: { displayName: 'never' },
}
const APP = {
  name: 'l10n-probe', origin: 'registry', displayName: 'Probe', description: 'English',
  highlights: ['one', 'two', 'three'], manifest: { l10n: L10N },
}

const previous = i18next.language
afterEach(async () => { await i18next.changeLanguage(previous) })

describe('manifest l10n fallback chain', () => {
  it('uses the exact locale, then the base language, then English', async () => {
    await i18next.changeLanguage('pt')
    expect(appDisplayName(APP)).toBe('Sonda')
    expect(appDescription(APP)).toBe('Descrição')
    expect(appHighlights(APP)).toEqual(['um', 'dois'])
    expect(appPageLabel(APP.name, 'English page', APP.displayName, APP.origin, L10N)).toBe('Página')
  })

  it('prefers the exact tag field by field, then the base language', async () => {
    await i18next.changeLanguage('zh-CN')
    expect(appDisplayName(APP)).toBe('探针')
    expect(appDescription(APP)).toBe('描述')
  })

  it('falls back to English for a locale the block does not carry', async () => {
    await i18next.changeLanguage('ja')
    expect(appDisplayName(APP)).toBe('Probe')
    expect(appDescription(APP)).toBe('English')
    expect(appHighlights(APP)).toEqual(['one', 'two', 'three'])
    expect(appPageLabel(APP.name, 'English page', APP.displayName, APP.origin, L10N)).toBe('English page')
  })

  it('ignores an over-cap value and reads a block spread onto a store row', async () => {
    await i18next.changeLanguage('de')
    expect(appDisplayName(APP)).toBe('Probe')
    expect(appDisplayName({ name: 'row', displayName: 'Row', l10n: { de: { displayName: 'Zeile' } } })).toBe('Zeile')
  })

  it('never lets the block override a catalog-keyed built-in', async () => {
    await i18next.changeLanguage('pt')
    const builtin = { name: 'projects', origin: 'builtin', displayName: 'X', manifest: { l10n: { pt: { displayName: 'Hijack' } } } }
    expect(appDisplayName(builtin)).not.toBe('Hijack')
  })
})
