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
}
