/**
 * Page keys of the Settings > Developer SubNav (`DeveloperPanel.tsx`).
 * Constants only, so a link builder can name a page without pulling the
 * panel and its internals views into its chunk.
 */

/** The first page: the per-device opt-in switches. */
export const DEVELOPER_FEATURE_PREVIEWS_SUB = 'feature-previews'

/** Every page key the panel renders, in rail order. */
export const DEVELOPER_SUBS: readonly string[] = [
  DEVELOPER_FEATURE_PREVIEWS_SUB,
  'logs',
  'system',
  'telemetry',
  'storage',
  'mcp-pool',
  'memory',
  'config',
  'debug-tools',
  'archive',
]
