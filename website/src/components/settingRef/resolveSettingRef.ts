/**
 * Pure resolver: given a config key, determine how to render a SettingRef.
 *
 * - mode 'ui': found in SETTINGS_REGISTRY via configKey — link to settings page.
 * - mode 'file': found in backend schema index — file/CLI-only setting.
 * - mode 'unknown': not found anywhere — render as plain <code>, no link.
 */
import type { SettingEntry } from '../commandPalette/settingsTypes'
import { SETTINGS_REGISTRY } from '../commandPalette/settingsRegistry.gen'

/** A single entry from the backend config schema (GET /api/config/schema). */
export interface SchemaEntry {
  path: string
  type: string
  label?: string
  help?: string
  tags?: string[]
  enum?: string[]
  default?: unknown
}

export type SettingRefResolution =
  | { mode: 'ui'; entry: SettingEntry }
  | { mode: 'file'; schemaEntry: SchemaEntry }
  | { mode: 'unknown' }

/**
 * Resolve a config key to its display mode.
 *
 * @param configKey  The dotted config path (e.g. 'telemetry.beacon_enabled')
 * @param schemaIndex  A Map<path, SchemaEntry> built from GET /api/config/schema,
 *                     or `undefined` if the schema is not yet loaded.
 *                     When undefined and key is not in the static registry,
 *                     resolves optimistically to 'file' mode with a synthetic entry.
 */
export function resolveSettingRef(
  configKey: string,
  schemaIndex?: Map<string, SchemaEntry> | undefined,
): SettingRefResolution {
  // Validate input: must be a non-empty string with only safe characters
  if (!configKey || typeof configKey !== 'string') {
    return { mode: 'unknown' }
  }

  // Safety: reject anything that looks like a URL, script, or path traversal
  if (
    configKey.includes('://') ||
    configKey.startsWith('//') ||
    configKey.startsWith('javascript:') ||
    configKey.includes('..') ||
    configKey.includes('<') ||
    configKey.includes('>')
  ) {
    return { mode: 'unknown' }
  }

  // 1. Check SETTINGS_REGISTRY for an entry with matching configKey
  const registryEntry = SETTINGS_REGISTRY.find(e => e.configKey === configKey)
  if (registryEntry) {
    return { mode: 'ui', entry: registryEntry }
  }

  // 2. Schema not yet loaded (undefined): resolve optimistically to file mode
  //    with a synthetic entry so the user always gets the CLI popover.
  if (schemaIndex === undefined) {
    return {
      mode: 'file',
      schemaEntry: { path: configKey, type: 'unknown' },
    }
  }

  // 3. Schema loaded — check backend schema index
  const schemaEntry = schemaIndex.get(configKey)
  if (schemaEntry) {
    return { mode: 'file', schemaEntry }
  }

  // 4. Schema loaded and key positively absent
  return { mode: 'unknown' }
}
