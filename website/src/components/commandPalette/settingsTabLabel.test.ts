import { readFileSync } from 'node:fs'
import path from 'node:path'

import { describe, expect, it } from 'vitest'

import en from '../../i18n/locales/en.json'
import enManual from '../../i18n/locales/en.manual.json'
import { SETTINGS_REGISTRY } from './settingsRegistry.gen'
import { settingsTabLabelKey } from './settingsTabLabel'

const lookup = (catalog: unknown, key: string): unknown =>
  key.split('.').reduce<unknown>((node, part) => {
    if (node && typeof node === 'object') return (node as Record<string, unknown>)[part]
    return undefined
  }, catalog)

const resolves = (key: string): boolean =>
  typeof lookup(en, key) === 'string' || typeof lookup(enManual, key) === 'string'

describe('settingsTabLabelKey', () => {
  it('resolves to a real catalog string for EVERY tab in the registry', () => {
    // The guarantee this test exists for: no settings row can render a raw machine
    // key as its tab name. A new tab, or a tab whose catalog key moves, lands here
    // rather than in front of a user reading a non-English dashboard.
    const tabs = [...new Set(SETTINGS_REGISTRY.map(e => e.tab))].sort()
    expect(tabs.length).toBeGreaterThan(0)
    const unresolved = tabs.filter(tab => !resolves(settingsTabLabelKey(tab)))
    expect(unresolved).toEqual([])
  })

  it('derives the regular shape and overrides only the irregular tabs', () => {
    expect(settingsTabLabelKey('browser')).toBe('settings.tabs.browser.label')
    // Kebab tab key, camelCase catalog segment.
    expect(settingsTabLabelKey('computer-use')).toBe('settings.tabs.computerUse.label')
    // Lives outside the settings tab block entirely.
    expect(settingsTabLabelKey('privacy')).toBe('privacyDisclosure.settingsLabel')
  })

  it('does not invent an override for an unknown tab', () => {
    expect(settingsTabLabelKey('not-a-tab')).toBe('settings.tabs.not-a-tab.label')
  })

  it('is the single source both palette surfaces read', () => {
    // The legacy provider used to capitalize the tab key, which rendered
    // "Computer-use" for `computer-use` and, in any non-English locale, the English
    // machine key for every tab. Two surfaces showing different names for one tab is
    // the drift this resolver exists to remove, so the sibling is asserted here
    // rather than left to be rediscovered.
    const src = readFileSync(
      path.join(__dirname, 'providers', 'settingsProvider.ts'),
      'utf-8',
    )
    expect(src).toContain('settingsTabLabel(entry.tab)')
    expect(src).not.toMatch(/entry\.tab\.charAt\(0\)\.toUpperCase\(\)/)
  })
})
