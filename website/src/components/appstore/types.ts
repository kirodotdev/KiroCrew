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
  useCases?: string[]
  configuration?: string[]
  screenshots?: string[]
  heroImage?: string
  heroImageDark?: string
  heroImageDetail?: string
  heroImageDetailDark?: string
  license?: string
  repo?: string
  /** Server-resolved clone target shown and echoed by the trust consent flow. */
  trustRepository?: string
  branch?: string
  featured?: boolean | number
  /**
   * GitHub star count baked into git-type third-party rows by the publisher.
   * Display-only; the server sanitizes it to a non-negative int
   * (``_apply_trust_fields``) and built-ins never carry it.
   */
  stargazersCount?: number
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
  /**
   * The git URL this app was installed from, recorded at install time. It is
   * the only repo identifier that survives independently of the store's
   * registry caches, so art resolution falls back to it when neither the row
   * nor the manifest names a repo. Empty on a built-in, a local-directory
   * install, and on records written before provenance was captured.
   */
  sourceUrl?: string
  /** Server-normalized source URL used as the trust-consent scope. */
  trustRepository?: string
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
    ui?: {
      entry?: string
      pages?: { route: string; label: string; icon: string }[]
      /**
       * Host surfaces this app replaces while enabled. Serialized by the manifest
       * but previously undeclared here, so a reader outside `overlaySlots.ts` (which
       * carries its own record type) could not see it.
       */
      overlays?: { id: string; replaces: string }[]
    }
    /**
     * Rows this app adds to host-owned surfaces. Declared here for the same reason
     * `ui.overlays` above is: the manifest serializes it, and a reader outside
     * `contributedCommands.ts` (which carries its own record type, and validates the
     * shape because this data is third-party) could not otherwise see the field
     * exists. Left as `unknown` on purpose — the only code allowed to decide what a
     * contribution IS is the module that checks it.
     */
    contributes?: {
      commands?: unknown
    }
    permissions?: { api?: string[]; events?: string[]; mcpTools?: string[]; storage?: boolean; cron?: boolean; network?: boolean }
    setup?: { onInstall?: string; onUpdate?: string; onUninstall?: string; onEnable?: string; onDisable?: string }
    minKiroCrewVersion?: string
    iconPath?: string
    repo?: string
    screenshots?: string[]
    /** Dark-appearance screenshots, when the manifest ships a second set. */
    screenshotsDark?: string[]
    heroImage?: string
    heroImageDark?: string
    // The wide detail-page banners. Ten of the twelve builtins ship them, but
    // they were absent from this shared type, so `AppsPage` could not forward
    // them to the Discover catalog even though `AppDetailPage` renders them.
    heroImageDetail?: string
    heroImageDetailDark?: string
    highlights?: string[]
    useCases?: string[]
    configuration?: string[]
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

/** The ``source`` prefix ``install_from_registry`` records on a cloned app. */
const REGISTRY_SOURCE_PREFIX = 'registry:'

/**
 * Whether an installed app's bytes came from a registry clone rather than from a
 * directory on this machine.
 *
 * This is the discriminator ``handle_update_app`` itself branches on, and the two
 * refresh paths are not interchangeable: a registry-sourced app is re-cloned
 * through ``/api/apps/registry/install``, while an app installed from a path is
 * re-copied from that path by ``POST /api/apps/{name}/update``. A local-source app
 * has no registry row at all, so sending it down the registry path fails with
 * "not found in registry" however it was installed.
 *
 * ``origin`` is the fallback for a record written before ``source`` was stored —
 * the same secondary signal ``manager.py`` accepts for the same question. It is
 * also the fallback when ``source`` is present but not a string: the detail page
 * spreads a CATALOG row into its app object when the installed-record fetch
 * fails, and ``registry.py`` copies index keys verbatim for a row it has not
 * installed, so an external index can publish ``source: {type: "git"}``. The
 * declared type says ``string``, but the payload is untrusted and this runs
 * inside the ``autoAction`` effect — an unguarded ``startsWith`` throws there and
 * Sync never dispatches at all.
 */
export function isRegistrySourced(app: Pick<InstalledApp, 'source' | 'origin'>): boolean {
  const source = app.source
  if (typeof source === 'string' && source) return source.startsWith(REGISTRY_SOURCE_PREFIX)
  return app.origin === 'registry'
}

/**
 * Sanitize a self-reported GitHub star count for display.
 *
 * Shared by every path that turns a registry payload into a rendered row:
 * `normalizeRegistryApp` (the Discover query boundary) AND `AppDetailPage`'s
 * own row builds, which spread the raw `listRegistry()` payload without going
 * through normalize. An older gateway does not sanitize this field
 * server-side and external indexes are user-supplied JSON, so the client must
 * hold the line alone: only a safe non-negative integer renders (`1e308` is
 * finite but compact-formats into hundreds of digits; `NaN`/`-1`/`3.5` are
 * `typeof number` and would pass a bare typeof gate).
 */
export function sanitizeStargazersCount(v: unknown): number | undefined {
  return typeof v === 'number' && Number.isSafeInteger(v) && v >= 0 ? v : undefined
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
    stargazersCount: sanitizeStargazersCount(raw?.stargazersCount),
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
      highlights: strings(manifest.highlights),
      useCases: strings(manifest.useCases),
      configuration: strings(manifest.configuration),
      // Art fields, coerced here for the reason in this function's docstring: the
      // payload's entry point is where a wrong TYPE stops being every consumer's
      // problem. `screenshots` was coerced and its dark sibling was not, which is
      // how `"screenshotsDark": {}` reached a bare `.map`, and `"iconPath": {}` a
      // bare `startsWith` — each throwing on the surface that read it rather than
      // degrading. `repo` rides along because it is the base the others resolve
      // against, so a non-string there produces a nonsense request instead of none.
      iconUrl: str(manifest.iconUrl),
      iconUrlDark: str(manifest.iconUrlDark),
      iconPath: str(manifest.iconPath),
      iconPathDark: str(manifest.iconPathDark),
      heroImage: str(manifest.heroImage),
      heroImageDark: str(manifest.heroImageDark),
      heroImageDetail: str(manifest.heroImageDetail),
      heroImageDetailDark: str(manifest.heroImageDetailDark),
      repo: str(manifest.repo),
      screenshots: strings(manifest.screenshots),
      screenshotsDark: strings(manifest.screenshotsDark),
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
