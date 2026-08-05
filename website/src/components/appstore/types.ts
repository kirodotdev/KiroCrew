import { i18nT } from '../../i18n/t'

/**
 * Shared types for the Apps page (Discover + Library) surfaces.
 *
 * ``RegistryApp`` mirrors the backend ``app-registry.json`` schema (core file
 * or federated external registry index) after ``registry.py`` enrichment:
 *  - ``_registry``: source registry name tagged by ``_load_external_registries``
 *    (absent for core-file entries and for built-ins merged client-side).
 *  - ``featured``: curator flag carried on registry INDEX entries (not
 *    app.json) — ``true`` or a number for explicit ordering (lower first).
 */
export type RegistryApp = {
  name: string
  displayName: string
  description: string
  version: string
  author: string
  icon?: string
  iconUrl?: string
  tags?: string[]
  highlights?: string[]
  screenshots?: string[]
  heroImage?: string
  heroImageDark?: string
  heroImageDetail?: string
  heroImageDetailDark?: string
  license?: string
  repo?: string
  branch?: string
  featured?: boolean | number
  _registry?: string
  installed: boolean
  installedVersion?: string
  enabled?: boolean
  updateAvailable?: boolean
  origin?: string     // "builtin" | "registry" | "local" | "external"
  resources?: string  // "gateway" | "app"
  lifecycle?: string  // "gateway" | "app" | "locked"
  platform?: { os?: string[]; installMode?: string; clientInstall?: { shell?: string; postInstall?: string }
    // Set when the app's UI needs the Electron shell (native windows,
    // global shortcuts, tray). A UX gate only — the marker is client-side.
    requiresDesktopApp?: boolean }
}

/** Installed app shape from ``GET /api/apps`` (mirrors app manager records). */
export type InstalledApp = {
  name: string
  version: string
  displayName: string
  enabled: boolean
  installedAt: string
  source?: string
  origin?: string     // "builtin" | "registry" | "local" | "external"
  resources?: string  // "gateway" | "app"
  lifecycle?: string  // "gateway" | "app" | "locked"
  migratedTo?: string
  orphaned?: boolean
  updateAvailable?: boolean
  manifest: {
    name: string
    version: string
    displayName: string
    description: string
    author: string
    agents?: string[]
    skills?: string[]
    sops?: string[]
    crons?: { name: string }[]
    tags?: string[]
    jobFamilies?: string[]
    ui?: { entry?: string; pages?: { route: string; label: string; icon: string }[] }
    permissions?: { api?: string[]; events?: string[]; mcpTools?: string[]; storage?: boolean; cron?: boolean; network?: boolean }
    setup?: { onInstall?: string; onUpdate?: string; onUninstall?: string; onEnable?: string; onDisable?: string }
    minKiroCrewVersion?: string
    iconPath?: string
    repo?: string
    screenshots?: string[]
    heroImage?: string
    heroImageDark?: string
    // The wide detail-page banners. Ten of the twelve builtins ship them, but
    // they were absent from this shared type, so `AppsPage` could not forward
    // them to the Discover catalog even though `AppDetailPage` renders them.
    heroImageDetail?: string
    heroImageDetailDark?: string
    highlights?: string[]
    license?: string
    iconUrl?: string
    openCommand?: string
    hidden?: boolean
  }
}

/**
 * Human label for the registry an app came from (trust provenance).
 *
 * The ``_registry`` tag is checked FIRST: it is applied server-side by
 * ``_load_external_registries`` and cannot be set by index content, whereas
 * ``origin`` is copied verbatim from an index entry for apps that are not yet
 * installed. Testing ``origin`` first would let an external registry publish
 * ``"origin": "builtin"`` and render a "Built-in" provenance label.
 */
export function sourceLabel(app: Pick<RegistryApp, '_registry' | 'origin'>): string {
  if (app._registry) return app._registry
  if (app.origin === 'builtin') return i18nT('components.appstore.types.built_in')
  return i18nT('components.appstore.types.kirocrew_registry')
}

/**
 * The verified mark asserts FIRST-PARTY provenance, so it must never be
 * awardable from manifest or index content: the badge sits next to an Install
 * button that runs setup code with gateway privileges.
 *
 * ``_registry`` is rejected BEFORE any other signal. That tag is attached
 * server-side per configured registry and cannot be forged by index content,
 * while both ``origin`` and ``author`` are copied verbatim from an index entry
 * for a not-yet-installed app — so checking ``origin === 'builtin'`` first
 * would let any added registry self-award the first-party mark. Genuine
 * built-ins are merged client-side from the installed-apps list and never
 * carry ``_registry``.
 */
export function isVerified(app: Pick<RegistryApp, 'origin' | 'author' | '_registry'>): boolean {
  if (app._registry) return false
  if (app.origin === 'builtin') return true
  return (app.author || '').toLowerCase() === 'kirocrew'
}

/**
 * Normalize a registry row for rendering.
 *
 * ``registry.py`` intentionally yields a MINIMAL index row when an app's
 * ``app.json`` fetch fails (name/repo only, no display fields), and external
 * registries are user-supplied JSON — so display fields can be missing or the
 * wrong type. Every consumer sorts, lowercases, and renders these, so coerce
 * once at the query boundary instead of defending at each call site.
 */
export function normalizeRegistryApp(raw: RegistryApp): RegistryApp {
  const str = (v: unknown, fallback = '') => (typeof v === 'string' ? v : fallback)
  const name = str(raw?.name)
  return {
    ...raw,
    name,
    displayName: str(raw?.displayName, name),
    description: str(raw?.description),
    version: str(raw?.version, '0.0.0'),
    author: str(raw?.author),
    tags: Array.isArray(raw?.tags) ? raw.tags.filter((t): t is string => typeof t === 'string') : [],
  }
}
