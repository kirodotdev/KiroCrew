import { describe, it, expect } from 'vitest'

import { extractFromSource } from '../../scripts/settingsExtract'

/**
 * Guards the i18n awareness of the settings extractor.
 *
 * The extractor feeds `SETTINGS_REGISTRY`, which powers command-palette settings
 * search. It must resolve `label={t('...')}` calls, not just string literals: a
 * label matched only as a string literal leaves a t()-wrapped setting silently
 * un-searchable — the control still renders, but no query can find it, with no
 * type error and no failing render test. These cases pin the resolution
 * behaviour so a future refactor of the regexes can't quietly break it.
 *
 * `DisplayPanel.tsx` is used as the filename because the extractor only emits
 * entries for files in its panel→tab map.
 */

const FILE = 'DisplayPanel.tsx'

describe('settingsExtract — i18n-aware label resolution', () => {
  it('resolves a t() label to its English catalog value', () => {
    const { entries } = extractFromSource(
      `<SettingsSelect label={t('settings.display.language.label')} value={x} onChange={f} />`,
      FILE,
    )
    expect(entries).toHaveLength(1)
    expect(entries[0]).toMatchObject({
      label: 'Language',
      labelKey: 'settings.display.language.label',
    })
  })

  it('resolves a t() description alongside a t() label', () => {
    const { entries } = extractFromSource(
      `<SettingsSelect
         label={t('settings.display.language.label')}
         description={t('settings.display.language.description')}
         value={x} onChange={f} />`,
      FILE,
    )
    expect(entries[0].description).toBe('Language for the dashboard interface.')
  })

  it('accepts double quotes and inner whitespace in the t() call', () => {
    const { entries } = extractFromSource(
      `<SettingsSelect label={ t( "settings.display.language.label" ) } value={x} onChange={f} />`,
      FILE,
    )
    expect(entries[0].label).toBe('Language')
  })

  it('retains the exact key from an i18nT() label', () => {
    const { entries } = extractFromSource(
      `<SettingsSelect label={i18nT('settings.display.language.label')} value={x} onChange={f} />`,
      FILE,
    )
    expect(entries[0]).toMatchObject({
      label: 'Language',
      labelKey: 'settings.display.language.label',
    })
  })

  it('still extracts plain string literals', () => {
    // Un-converted panels must keep working — the extractor has to handle a
    // partially-converted tree, which is the normal state mid-migration.
    const { entries } = extractFromSource(
      `<SettingsToggle label="Zoom Level" description="Native window zoom" value={x} onChange={f} />`,
      FILE,
    )
    expect(entries[0].label).toBe('Zoom Level')
    expect(entries[0].description).toBe('Native window zoom')
  })

  it('skips a t() label whose key is not in the catalog', () => {
    // Emitting the raw key would put `settings.nope.missing` in the search
    // index as if it were a label — worse than not indexing the setting.
    const { entries, skipped } = extractFromSource(
      `<SettingsSelect label={t('settings.nope.missing')} value={x} onChange={f} />`,
      FILE,
    )
    expect(entries).toHaveLength(0)
    expect(skipped).toBe(1)
  })

  it('still skips a genuinely dynamic label', () => {
    const { entries, skipped } = extractFromSource(
      `<SettingsSelect label={someVariable} value={x} onChange={f} />`,
      FILE,
    )
    expect(entries).toHaveLength(0)
    expect(skipped).toBe(1)
  })
})

describe('settingsExtract — quote-aware JSX prop parsing', () => {
  it('does not terminate tag early on > inside a double-quoted prop value', () => {
    const { entries } = extractFromSource(
      `<SettingsSelect label="Threshold" description="lower = more, > 50% is aggressive" value={x} onChange={f} />`,
      FILE,
    )
    expect(entries).toHaveLength(1)
    expect(entries[0].label).toBe('Threshold')
    expect(entries[0].description).toBe('lower = more, > 50% is aggressive')
  })

  it('does not terminate tag early on > inside a single-quoted expression prop', () => {
    // Single-quoted strings inside JSX expression braces
    const { entries } = extractFromSource(
      `<SettingsToggle label="Check" description={'if count > limit'} value={x} onChange={f} />`,
      FILE,
    )
    expect(entries).toHaveLength(1)
    expect(entries[0].label).toBe('Check')
    expect(entries[0].description).toBe('if count > limit')
  })

  it('handles } inside a quoted string within a JSX expression', () => {
    const { entries } = extractFromSource(
      `<SettingsSelect label={t('settings.display.language.label')} description={"use } carefully"} value={x} onChange={f} />`,
      FILE,
    )
    expect(entries).toHaveLength(1)
    expect(entries[0].label).toBe('Language')
    expect(entries[0].description).toBe('use } carefully')
  })

  it('handles > inside a JSX expression string', () => {
    const { entries } = extractFromSource(
      `<SettingsInput label="Limit" description={"values > 100 are capped"} value={x} onChange={f} />`,
      FILE,
    )
    expect(entries).toHaveLength(1)
    expect(entries[0].label).toBe('Limit')
    expect(entries[0].description).toBe('values > 100 are capped')
  })
})
