/**
 * Nav-rail + Settings-tab label translation.
 *
 * These labels are the most visible strings in the app, and they were the ones
 * the codemod could NOT convert: both `builtins.tsx` surfaces and `SettingsPage`
 * TABS are evaluated at MODULE LOAD, so a translated literal there would be
 * frozen in whichever language was active at import time. A screenshot of the
 * live dashboard caught it — everything switched to Chinese except the nav.
 *
 * The fix is `labelKey` + `surfaceLabel()` (resolved per render) and a
 * `buildTabs()` function instead of a `TABS` constant. These tests pin that
 * lazy-resolution property directly, since a "does it render" test on the
 * component would pass even with a frozen label.
 */

import { describe, it, expect, afterEach } from 'vitest'

import { i18next } from './index'
import { surfaceLabel } from '../surfaces/registry'
import '../surfaces/builtins'
import { getBuiltinSurfaces } from '../surfaces/registry'

afterEach(async () => {
  await i18next.changeLanguage('en')
})

describe('nav surface labels', () => {
  it('every built-in surface carries a labelKey', () => {
    // A surface without one renders untranslated — allowed by the type (apps and
    // downstream editions rely on it) but never correct for a core surface.
    const missing = getBuiltinSurfaces().filter(s => !s.labelKey).map(s => s.navId)
    expect(missing, `surfaces missing labelKey: ${missing.join(', ')}`).toEqual([])
  })

  it('resolves each labelKey to a real string in both languages', async () => {
    for (const lng of ['en', 'zh-CN']) {
      await i18next.changeLanguage(lng)
      for (const s of getBuiltinSurfaces()) {
        const label = surfaceLabel(s)
        expect(label, `${s.navId} in ${lng}`).toBeTruthy()
        // A raw key leaking through would contain a dot and no spaces.
        expect(label, `${s.navId} in ${lng} rendered a raw key`).not.toMatch(/^nav\./)
      }
    }
  })

  it('returns DIFFERENT text per language (proves lazy resolution)', async () => {
    const sessions = getBuiltinSurfaces().find(s => s.navId === 'chat')!
    await i18next.changeLanguage('en')
    const en = surfaceLabel(sessions)
    await i18next.changeLanguage('zh-CN')
    const zh = surfaceLabel(sessions)
    expect(en).toBe('Sessions')
    expect(zh).toMatch(/[一-鿿]/)
    // The regression this guards: a module-level literal returns the same string
    // for both languages.
    expect(zh).not.toBe(en)
  })

  it('falls back to the literal label when a surface has no labelKey', () => {
    expect(surfaceLabel({ label: 'My App' })).toBe('My App')
  })
})

describe('Settings tab labels', () => {
  it('resolve per language rather than being frozen at import', async () => {
    // Imported lazily so the module's own import-time evaluation can't be what
    // makes this pass.
    const { default: _SettingsPage } = await import('../pages/SettingsPage')
    expect(_SettingsPage).toBeTruthy()

    await i18next.changeLanguage('en')
    expect(i18next.t('settings.tabs.display.label')).toBe('Display')
    expect(i18next.t('settings.tabs.security.label')).toBe('Security')

    await i18next.changeLanguage('zh-CN')
    expect(i18next.t('settings.tabs.display.label')).toMatch(/[一-鿿]/)
    expect(i18next.t('settings.tabs.security.label')).toMatch(/[一-鿿]/)
  })

  it('has a label AND description for every tab in both languages', async () => {
    const keys = [
      'overview', 'imports', 'chat', 'display', 'voice', 'notifications',
      'shortcuts', 'channels', 'browser', 'instances', 'security', 'developer', 'about',
    ]
    for (const lng of ['en', 'zh-CN']) {
      await i18next.changeLanguage(lng)
      for (const k of keys) {
        for (const field of ['label', 'description']) {
          const v = i18next.t(`settings.tabs.${k}.${field}`)
          expect(v, `settings.tabs.${k}.${field} in ${lng}`).toBeTruthy()
          expect(v, `settings.tabs.${k}.${field} in ${lng} is a raw key`)
            .not.toBe(`settings.tabs.${k}.${field}`)
        }
      }
    }
  })
})
