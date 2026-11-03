/**
 * Page keys of the Settings > Developer SubNav (`DeveloperPanel.tsx`).
 * Constants only, so a link builder can name a page without pulling the
 * panel and its internals views into its chunk.
 */

/** The first page: the per-device opt-in switches. */
export const DEVELOPER_FEATURE_PREVIEWS_SUB = 'feature-previews'

/** The page key MCP Management had here. It lives in Customize → Connections →
 *  MCP Servers now; DeveloperPanel and the `/developer` redirect send this key
 *  to `MCP_MANAGEMENT_PATH`. */
export const LEGACY_DEVELOPER_MCP_POOL_SUB = 'mcp-pool'

/** Every page key the panel renders, in rail order. */
export const DEVELOPER_SUBS: readonly string[] = [
  DEVELOPER_FEATURE_PREVIEWS_SUB,
  'logs',
  'system',
  'telemetry',
  'storage',
  'memory',
  'config',
  'debug-tools',
  'archive',
]
