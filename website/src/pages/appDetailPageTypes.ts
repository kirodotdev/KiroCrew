/**
 * The App Store detail page's view model.
 *
 * `AppInfo` is one app as `AppDetailPage.tsx` renders it: the installed record
 * and its manifest (`AppManifest`, with its `McpServerConfig` servers and
 * `AppPermissions`) joined with the app's registry row. `RegistryEntry` is a
 * row of /api/apps/registry as the page reads it, and `NormalizedRegistryEntry`
 * is that row after `normalizeRegistryApp`. Types only: this module holds no
 * runtime code and never imports the page.
 */
import type { RegistryApp } from '../components/appstore/types'

type AppInfo = Pick<RegistryApp, '_registry' | 'provenance'> & {
  catalogListed?: boolean
  name: string
  displayName: string
  description: string
  version: string
  author: string
  icon?: string
  iconUrl?: string
  iconUrlDark?: string
  // Second-chance icon art: an INSTALLED app's own local route, consulted by
  // AppIcon only when the primary (usually registry) URL fails to load.
  iconUrlFallback?: string
  iconUrlFallbackDark?: string
  tags?: string[]
  highlights?: string[]
  useCases?: string[]
  configuration?: string[]
  screenshots?: string[]
  screenshotsDark?: string[]
  heroImage?: string
  heroImageDark?: string
  heroImageDetail?: string
  heroImageDetailDark?: string
  // Second-chance hero/screenshot art (#6864): an INSTALLED app's own local
  // routes, consulted only when the primary (usually registry) URL fails to
  // load. Optional because only the installed branch sets them — a
  // not-installed app has no local bytes, so hide-on-error stays its terminal
  // state. The screenshot lists are index-aligned with their primaries.
  heroImageFallback?: string
  heroImageDarkFallback?: string
  heroImageDetailFallback?: string
  heroImageDetailDarkFallback?: string
  screenshotsFallback?: string[]
  screenshotsDarkFallback?: string[]
  repo?: string
  trustRepository?: string
  branch?: string
  /**
   * GitHub star count baked into git-type third-party rows by the publisher.
   * Display-only and server-sanitized; built-ins never carry it, so presence
   * is the display gate.
   */
  stargazersCount?: number
  // Installed state
  installed: boolean
  installedVersion?: string
  enabled?: boolean
  sessionApprovalConsentPending?: boolean
  approvedGrants?: { api?: string[]; events?: string[] }
  managed?: string
  source?: string
  installedAt?: string
  updateAvailable?: boolean
  // Three-axis classification
  origin?: string     // "builtin" | "registry" | "local" | "external"
  resources?: string  // "gateway" | "app"
  lifecycle?: string  // "gateway" | "app" | "locked"
  // Platform
  platform?: { os?: string[]; installMode?: string; clientInstall?: { shell?: string; postInstall?: string }
    // Set when the app's UI needs the Electron shell (native windows,
    // global shortcuts, tray). A UX gate only — the marker is client-side.
    requiresDesktopApp?: boolean }
  // Manifest (from installed app)
  manifest?: AppManifest
}

interface McpServerConfig {
  url?: string
  command?: string
  autoApprove?: string[]
  [key: string]: unknown
}

interface AppPermissions {
  api?: string[]
  events?: string[]
  mcpTools?: string[]
  storage?: boolean
  cron?: boolean
  network?: boolean
  memory?: boolean | string
  sessionApproval?: boolean
  [key: string]: unknown
}

/** A registry app entry from /api/apps/registry — a superset of the fields the
 *  page reads, spread into AppInfo when there's no installed app. */
interface RegistryEntry extends Partial<AppInfo> {
  name: string
  updateAvailable?: boolean
}

/** A registry entry after `normalizeRegistryApp`: its display fields are always
 *  strings (fallbacks filled) and its star count is sanitized. */
type NormalizedRegistryEntry = RegistryEntry & Pick<AppInfo, 'displayName' | 'description' | 'version' | 'author'>

interface AppManifest {
  displayName?: string
  description?: string
  version?: string
  author?: string
  tags?: string[]
  highlights?: string[]
  useCases?: string[]
  configuration?: string[]
  screenshots?: string[]
  screenshotsDark?: string[]
  // Store-listing metadata. For built-in apps these live on the manifest
  // (preserved through AppManifest.extra) rather than on a registry entry —
  // built-ins are not part of the /api/apps/registry feed.
  iconUrl?: string
  iconUrlDark?: string
  // Repo-relative icon paths. An external app declares these (the backend
  // rewrites them into blob-proxy URLs on a registry row); `iconUrl` is the
  // built-in spelling.
  iconPath?: string
  iconPathDark?: string
  // The repo an external app's art paths are relative to, when the manifest
  // declares it.
  repo?: string
  heroImage?: string
  heroImageDark?: string
  heroImageDetail?: string
  heroImageDetailDark?: string
  ui?: { pages?: { route?: string; label?: string; icon?: string; iconUrl?: string }[] }
  // Installed apps carry the platform config here (a registry/catalog entry
  // exposes it top-level instead); `needsDesktopApp` reads both. Without this the
  // desktop requirement was invisible on surfaces that pass the manifest shape.
  platform?: { requiresDesktopApp?: boolean; os?: string[] }
  agents?: string[]
  skills?: string[]
  crons?: { name: string }[]
  mcpServers?: Record<string, McpServerConfig>
  permissions?: AppPermissions
  minKiroCrewVersion?: string
}

// `AppDetailPage.tsx` builds and renders these.
export type { AppInfo, NormalizedRegistryEntry, RegistryEntry }
