import { describe, it, expect } from 'vitest'

import { extractFromSource, mergeManualEntries } from '../../scripts/settingsExtract'
import type { ManualSettingEntry, SettingEntry } from '../components/commandPalette/settingsTypes'

/**
 * Guards the two coverage mechanisms of the settings extractor beyond plain
 * single-panel extraction:
 *
 *  - multi-target panels (one source file whose labels render for several
 *    channels) fan each primitive out into one entry per target, suffixing
 *    labelKey entries so the four otherwise-identical results stay
 *    distinguishable while highlight-time DOM lookup (which re-resolves the
 *    un-suffixed catalog string) keeps working;
 *  - the manual supplement (settingsManual.ts) merges by id — replacing a
 *    generated entry to attach params the file-level map cannot scope, or
 *    appending — and its validation throws instead of shipping a registry
 *    that would silently shadow entries or highlight labels no locale renders.
 *    Manual entries are KEY-ONLY (the i18n gate forbids English literals in
 *    hand-written source), so the merge materializes label/description from
 *    the English catalogs.
 */

const BOT_FILE = 'BotChannelPanel.tsx'

/** A labelKey that resolves in the English catalogs (used by SecurityPanel). */
const REAL_KEY = 'pages.settings.securityPanel.denied_commands'

function entry(overrides: Partial<SettingEntry>): SettingEntry {
  return {
    id: 'x',
    label: 'X',
    tab: 'security',
    type: 'toggle',
    occurrence: 1,
    ...overrides,
  }
}

function manualEntry(overrides: Partial<ManualSettingEntry>): ManualSettingEntry {
  return {
    id: 'x',
    labelKey: REAL_KEY,
    tab: 'security',
    type: 'toggle',
    occurrence: 1,
    ...overrides,
  }
}

describe('settingsExtract — multi-target panels', () => {
  it('emits one entry per channel target with the channel param attached', () => {
    const { entries } = extractFromSource(
      `<SettingsToggle label={t('pages.settings.botChannelPanel.file_sessions_in_folder')} checked={x} onChange={f} />`,
      BOT_FILE,
    )
    // Webex is deliberately absent: channel=webex mounts the standalone
    // WebexPanel, so a fan-out entry would deep-link to controls it lacks.
    expect(entries).toHaveLength(3)
    expect(entries.map(e => e.params?.channel)).toEqual(['discord', 'telegram', 'wecom'])
    expect(new Set(entries.map(e => e.tab))).toEqual(new Set(['channels']))
  })

  it('suffixes labelKey entries per target so ids stay unique', () => {
    const { entries } = extractFromSource(
      `<SettingsToggle label={t('pages.settings.botChannelPanel.file_sessions_in_folder')} checked={x} onChange={f} />`,
      BOT_FILE,
    )
    const labels = entries.map(e => e.label)
    expect(new Set(labels).size).toBe(3)
    expect(labels[0]).toMatch(/\(Discord\)$/)
    // labelKey stays the un-suffixed catalog key: highlight-time DOM lookup
    // resolves it to the rendered label, which carries no suffix. The raw
    // suffix rides along separately so a localized display can re-append it.
    expect(new Set(entries.map(e => e.labelKey)).size).toBe(1)
    expect(entries.map(e => e.labelSuffix)).toEqual(['Discord', 'Telegram', 'WeCom'])
  })

  it('keeps a literal label byte-identical (no suffix) across targets', () => {
    const { entries } = extractFromSource(
      `<SettingsInput label="Bot token" value={x} onChange={f} />`,
      BOT_FILE,
    )
    expect(entries).toHaveLength(3)
    expect(new Set(entries.map(e => e.label))).toEqual(new Set(['Bot token']))
    expect(entries.every(e => e.labelSuffix === undefined)).toBe(true)
  })
})

describe('settingsExtract — manual entry merge', () => {
  it('replaces a generated entry when ids collide, keeping list length', () => {
    const generated = [entry({ id: 'security.a', label: 'A' }), entry({ id: 'security.b', label: 'B' })]
    const manual = [manualEntry({ id: 'security.a', params: { section: 'apps' } })]
    const merged = mergeManualEntries(generated, manual)
    expect(merged).toHaveLength(2)
    expect(merged.find(e => e.id === 'security.a')?.params).toEqual({ section: 'apps' })
  })

  it('appends manual entries with new ids, resolving the English label from the catalog', () => {
    const generated = [entry({ id: 'security.a', label: 'A' })]
    const manual = [manualEntry({ id: 'security.new' })]
    const merged = mergeManualEntries(generated, manual)
    expect(merged.map(e => e.id).sort()).toEqual(['security.a', 'security.new'])
    const appended = merged.find(e => e.id === 'security.new')
    // Key-only manual entry → label materialized from the English catalog,
    // and the descriptionKey field never leaks into the registry shape.
    expect(appended?.label).toBe('Denied Commands')
    expect(appended && 'descriptionKey' in appended).toBe(false)
  })

  it('throws on duplicate manual ids', () => {
    const manual = [manualEntry({ id: 'dup' }), manualEntry({ id: 'dup' })]
    expect(() => mergeManualEntries([], manual)).toThrow(/duplicate manual entry id 'dup'/)
  })

  it('throws on a labelKey that does not resolve in the English catalogs', () => {
    const manual = [manualEntry({ id: 'security.x', labelKey: 'no.such.key.anywhere' })]
    expect(() => mergeManualEntries([], manual)).toThrow(/does not resolve/)
  })

  it('throws on a descriptionKey that does not resolve in the English catalogs', () => {
    const manual = [manualEntry({ id: 'security.x', descriptionKey: 'no.such.key.anywhere' })]
    expect(() => mergeManualEntries([], manual)).toThrow(/descriptionKey 'no\.such\.key\.anywhere'/)
  })
})
