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
  // Dark-appearance variant of iconUrl. Raster icons have fixed bytes, so an
  // app that must read well on both backgrounds ships two files; first-party
  // /app-assets/ SVGs are inlined and repaint from theme tokens instead.
  iconUrlDark?: string
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
  // 'core' is the pre-migration spelling of 'official'; both mean "an app WE
  // list", the bundled index being the offline seed of that list.
  provenance?: 'official' | 'core' | 'external' | 'builtin'
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
    iconUrlDark?: string
    iconPathDark?: string
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
 * row as built-in or official. The ``origin`` fallback exists only for rows
 * from older gateways that emit neither field.
 *
 * ``'core'`` is the previous spelling of ``'official'`` and is accepted for as
 * long as a client can meet an older gateway. Both mean "an app WE list": the
 * bundled ``app-registry.json`` is the offline seed of that list, not a
 * separate kind of app.
 */
export function sourceLabel(app: Pick<RegistryApp, '_registry' | 'origin' | 'provenance'>): string {
  if (app._registry) return app._registry
  if (app.provenance === 'builtin') return i18nT('components.appstore.types.built_in')
  if (app.provenance === 'official' || app.provenance === 'core') {
    return i18nT('components.appstore.types.kirocrew_registry')
  }
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

/**
 * Normalize an installed-app record for rendering — the ``InstalledApp``
 * counterpart of ``normalizeRegistryApp``.
 *
 * ``GET /api/apps`` mirrors on-disk app records, so a manifest field exists only
 * if the installed ``app.json`` published it: a hand-written or older app can
 * arrive with no ``manifest`` object at all, and every list-valued field is
 * independently optional. Defended per render site, that shape produces the
 * failure mode of #3689 — a ``!`` assertion whose guard lives in another
 * expression and drifts out of step with it. Coerce once where the payload
 * enters the client instead, so the manifest object and its lists are always
 * there to read.
 *
 * Generic in the record type because normalization only fills gaps: fields a
 * caller carries beyond ``InstalledApp`` (``managed``, ``_newVersion``) survive,
 * and the call site keeps the type it already had.
 */
export function normalizeInstalledApp<T extends InstalledApp>(raw: T): T {
  // A non-object payload is passed through untouched: filling a manifest into it
  // would invent a record the server never sent, and every caller already has to
  // handle the request having failed.
  if (!raw || typeof raw !== 'object') return raw
  const str = (v: unknown, fallback = '') => (typeof v === 'string' ? v : fallback)
  const strings = (v: unknown): string[] =>
    Array.isArray(v) ? v.filter((s): s is string => typeof s === 'string') : []
  const manifest = (raw.manifest ?? {}) as InstalledApp['manifest']
  const ui = (manifest.ui && typeof manifest.ui === 'object' ? manifest.ui : {}) as
    NonNullable<InstalledApp['manifest']['ui']>
  const name = str(raw?.name)
  const version = str(raw?.version, '0.0.0')
  const displayName = str(raw?.displayName, name)
  return {
    ...raw,
    name,
    version,
    displayName,
    manifest: {
      ...manifest,
      name: str(manifest.name, name),
      version: str(manifest.version, version),
      displayName: str(manifest.displayName, displayName),
      description: str(manifest.description),
      author: str(manifest.author),
      agents: strings(manifest.agents),
      skills: strings(manifest.skills),
      sops: strings(manifest.sops),
      tags: strings(manifest.tags),
      jobFamilies: strings(manifest.jobFamilies),
      screenshots: strings(manifest.screenshots),
      highlights: strings(manifest.highlights),
      // A cron entry is only useful for its name, which is also the only field
      // the dashboard reads, so an entry without one is dropped rather than
      // rendered as a blank row.
      crons: Array.isArray(manifest.crons)
        ? manifest.crons.filter(
            (c): c is { name: string } => !!c && typeof (c as { name?: unknown }).name === 'string',
          )
        : [],
      // Preserve ``ui.entry`` (and any extra keys) untouched: ``hasUI`` and
      // AppHost routing read entry truthiness, so injecting one would change
      // eligibility. Only ``pages`` is coerced; rows without a string route
      // are dropped because every consumer routes through ``pages[0].route``.
      ui: {
        ...ui,
        pages: Array.isArray(ui.pages)
          ? ui.pages.filter((p): p is NonNullable<typeof p> => !!p && typeof (p as { route?: unknown }).route === 'string')
          : [],
      },
    },
  } as T
}

/** ``normalizeInstalledApp`` over a ``GET /api/apps`` list payload. */
export function normalizeInstalledApps<T extends InstalledApp>(raw: T[]): T[] {
  return Array.isArray(raw) ? raw.map(normalizeInstalledApp) : raw
}
