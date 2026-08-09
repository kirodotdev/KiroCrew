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
  /**
   * Server-computed trust fields — the API trust boundary of
   * ``/api/apps/registry`` (``_apply_trust_fields`` in ``registry.py``).
   * Optional only because rows from an older gateway lack them; when
   * present they are authoritative and the client must not re-derive
   * trust from ``_registry`` absence.
   */
  provenance?: 'core' | 'external' | 'builtin'
  verified?: boolean
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
 * The server-computed ``provenance`` field is authoritative
 * (``_apply_trust_fields`` in ``registry.py`` computes it where the
 * ``_registry`` tag is applied and overwrites anything an index publishes).
 * The ``_registry`` tag is still checked FIRST: it is equally
 * server-attached, and a row carrying it is external by construction — so
 * a ``provenance`` value smuggled through an OLDER gateway (which copies
 * index keys verbatim and computes nothing) can never relabel an external
 * row as built-in or core. The ``origin`` fallback exists only for rows
 * from older gateways that emit neither field.
 */
export function sourceLabel(app: Pick<RegistryApp, '_registry' | 'origin' | 'provenance'>): string {
  if (app._registry) return app._registry
  if (app.provenance === 'builtin') return i18nT('components.appstore.types.built_in')
  if (app.provenance === 'core') return i18nT('components.appstore.types.kirocrew_registry')
  // Legacy fallback (older gateway: no ``provenance`` field).
  if (app.origin === 'builtin') return i18nT('components.appstore.types.built_in')
  return i18nT('components.appstore.types.kirocrew_registry')
}

/**
 * The verified mark asserts FIRST-PARTY provenance, so it must never be
 * awardable from manifest or index content: the badge sits next to an Install
 * button that runs setup code with gateway privileges.
 *
 * The server-computed ``verified`` field is authoritative when present
 * (``_apply_trust_fields`` in ``registry.py`` overwrites anything an index
 * publishes). ``_registry`` is still rejected BEFORE it: the tag is equally
 * server-attached and the server never emits ``verified: true`` on a tagged
 * row, so this order only differs for a ``verified`` smuggled through an
 * OLDER gateway that copies index keys verbatim — exactly the case that must
 * lose. The ``origin``/``author`` derivation below is the legacy fallback
 * for rows from older gateways that emit neither field; genuine built-ins
 * merged client-side set ``verified: true`` directly and never carry
 * ``_registry``.
 */
export function isVerified(app: Pick<RegistryApp, 'origin' | 'author' | '_registry' | 'verified'>): boolean {
  if (app._registry) return false
  if (typeof app.verified === 'boolean') return app.verified
  // Legacy fallback (older gateway: no ``verified`` field).
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
