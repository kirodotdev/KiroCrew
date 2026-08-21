/**
 * Settings registry types (Search Everywhere — Settings provider).
 *
 * Shared between:
 *  - The codegen script (`scripts/settingsExtract.ts`)
 *  - The checked-in registry (`settingsRegistry.gen.ts`)
 *  - The settings provider at runtime
 */

/** Primitive types we recognize. */
export type SettingPrimitiveType = 'toggle' | 'select' | 'input' | 'stepper' | 'buttonGroup'

/** A single extracted setting entry. */
export interface SettingEntry {
  /** Unique id: `<tab>.<kebab-label>` (with `-N` suffix for duplicates within a tab). */
  id: string
  /** Human-readable label from the JSX prop. */
  label: string
  /**
   * Catalog key for a translated label. The registry keeps the English label
   * for search, while deep-link highlighting resolves this key at runtime so
   * it can match the label rendered in the active locale.
   */
  labelKey?: string
  /**
   * Fan-out disambiguator (e.g. 'Discord') for entries a multi-target panel
   * emits once per channel. `label` already carries it in English as
   * `<label> (<labelSuffix>)`; a localized display re-appends it to the
   * resolved `labelKey` string, which is un-suffixed by design so highlight
   * DOM lookup still matches the rendered text.
   */
  labelSuffix?: string
  /** Optional description from the JSX prop. */
  description?: string
  /** Which settings tab this belongs to (matches SettingsPage TABS key). */
  tab: string
  /** Which primitive renders this setting. */
  type: SettingPrimitiveType
  /**
   * 1-based occurrence index for duplicate labels within the same tab.
   * Defaults to 1. When >1 the highlight hook uses querySelectorAll to pick
   * the Nth matching element in DOM order.
   */
  occurrence: number
  /**
   * Extra query params required for the setting's panel to mount — e.g.
   * `{ channel: 'slack' }` for entries living inside the Channels tab's
   * list-detail sub-selection. Appended to the deep-link route by the
   * settings provider; without them the highlight would silently no-op on
   * a panel that never mounts.
   */
  params?: Record<string, string>
  /**
   * Backend config key this setting writes (e.g. 'telemetry.beacon_enabled').
   * Used by SettingRef to resolve a config key to its UI deep-link.
   * Undefined for settings that don't map to a single config path.
   */
  configKey?: string
}

/**
 * A hand-curated entry in settingsManual.ts. Key-only by construction: the
 * i18n gate forbids English prose literals in hand-written source, so manual
 * entries carry catalog KEYS and generation (`mergeManualEntries`) resolves
 * `label`/`description` from the English catalogs — the same strings the
 * panel renders, which keeps `data-setting-label` highlighting exact.
 */
export type ManualSettingEntry = Omit<SettingEntry, 'label' | 'labelKey' | 'description'> & {
  labelKey: string
  descriptionKey?: string
}
