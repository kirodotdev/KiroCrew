/**
 * Localized display name for a settings tab key.
 *
 * `SettingEntry.tab` is a machine key (`browser`, `computer-use`), so rendering it
 * raw puts an untranslated internal id in front of the user. The settings page
 * already localizes every tab name; this resolves the same catalog key so a tab
 * name reads the same wherever it appears.
 *
 * Most tabs follow `settings.tabs.<key>.label`. Two do not, and a mechanical
 * derivation would silently render the key itself for exactly those two, so they
 * are listed explicitly. `settingsTabLabel.test.ts` pins every tab in
 * SETTINGS_REGISTRY to a key that exists in the catalog, so adding a tab or moving
 * its key fails the test instead of shipping a raw id.
 */
import { i18nT } from '../../i18n/t'

/** Tabs whose catalog key does not follow `settings.tabs.<key>.label`. */
const IRREGULAR_TAB_LABEL_KEYS: Record<string, string> = {
  // The tab key is kebab-case; the catalog segment is camelCase.
  'computer-use': 'settings.tabs.computerUse.label',
  // Owned by the privacy disclosure copy, not the settings tab block.
  privacy: 'privacyDisclosure.settingsLabel',
}

/** Catalog key holding a tab's display name. */
export function settingsTabLabelKey(tab: string): string {
  return IRREGULAR_TAB_LABEL_KEYS[tab] ?? `settings.tabs.${tab}.label`
}

/** Localized display name for a settings tab key. */
export function settingsTabLabel(tab: string): string {
  return i18nT(settingsTabLabelKey(tab))
}

/**
 * Subtitle for a settings row: what tells two rows apart.
 *
 * The tab name alone is not enough. The registry holds entries with the SAME label
 * in the SAME tab -- two `Speed` selects under Voice -- distinguished only by their
 * description, so a tab-only subtitle leaves those rows identical. The composition
 * is a catalog entry, so a locale can reorder the parts or change the separator.
 * `description` itself is not in the catalog, so that detail stays English until the
 * registry carries a key for it -- still better than two rows a user cannot choose
 * between.
 */
export function settingsSubtitle(entry: { tab: string; description?: string }): string {
  const tab = settingsTabLabel(entry.tab)
  if (!entry.description) return tab
  return i18nT('components.commandPalette.settings_subtitle', {
    tab,
    detail: entry.description,
  })
}
