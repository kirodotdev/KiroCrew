import type { RegistryApp } from './types'

/**
 * Fields a built-in's installed manifest can contribute, as `GET /api/apps`
 * shapes them. Deliberately loose: `manifest` mirrors on-disk JSON, so every
 * field is independently optional and an older app can arrive with none.
 */
export type BuiltinManifestFields = {
  /**
   * The version actually installed on this machine, from the app record rather
   * than the manifest body. It is a fact about the LOCAL install, so it wins
   * over the catalog's `version`: the document is fetched from the network and
   * can advertise a release newer than the wheel the user is running, and a row
   * claiming a version the user does not have is a lie about their own machine.
   */
  version?: string
  displayName?: string
  description?: string
  author?: string
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
  license?: string
  iconUrl?: string
  iconUrlDark?: string
  ui?: { pages?: { icon?: string; iconUrl?: string }[] }
}

/**
 * Merge a server row for an installed built-in with its local manifest.
 *
 * ONE function because the precedence IS the contract, and it was previously
 * spelled twice with OPPOSITE answers: the browse list preferred the row while
 * the detail page preferred the manifest, so the same app could read
 * "Kiro Crew · Developer Tools" in the list and "kirocrew · Productivity" one
 * click later. Two truthiness chains in two files cannot disagree if there is
 * only one of them.
 *
 * **The published catalog wins on what it publishes.** That is the whole point
 * of the store rendering catalog rows: a republished document must be able to
 * correct a built-in's artwork, taxonomy or author without shipping a wheel.
 * The manifest is the FALLBACK, filling the fields the catalog does not carry
 * (detail hero art, highlights, license, the lucide page icon) and standing in
 * wholesale when the catalog is unreachable.
 *
 * **One exception, and it runs the other way: `version`.** That is a fact about
 * the local install rather than catalog metadata, so the installed value wins
 * even when the document publishes one — see `BuiltinManifestFields.version`.
 *
 * Display copy is NOT merged here. `appDisplayName` / `appDescription` resolve a
 * first-party built-in through the i18n catalog, which overrides both sources —
 * that copy ships translated in every locale while the catalog document is
 * English-only.
 */
/**
 * The server row, as loosely as the two call sites type it. `AppsPage` has a
 * fully-normalised `RegistryApp`; `AppDetailPage` has its own `RegistryEntry`
 * whose display fields are optional. Accepting the loose shape is correct rather
 * than convenient: the row arrives over the network, so every field is
 * independently absent-able and the merge exists precisely to fill gaps.
 */
export type BuiltinServerRow = Partial<RegistryApp> & { name: string }

/**
 * Is this server row genuinely a built-in this wheel ships — safe to let it
 * supply display metadata for a first-party surface?
 *
 * `origin` alone is NOT sufficient, and that is the trap. The server stamps
 * `origin` from the INSTALLED app of the same name
 * (`_enrich_with_install_status`), so an EXTERNAL registry row named `meetings`
 * inherits `origin: "builtin"` from the installed built-in it collides with.
 * Preferring that row's fields would render attacker-controlled copy, artwork and
 * author on a built-in's browse row and detail page.
 *
 * So a row must also not be external. `_registry` is the server-attached tag
 * (index content cannot forge it) and `provenance` is the documented trust
 * field; either one marking the row external disqualifies it, and the local
 * built-in fallback wins instead. This mirrors what `official_catalog.annotate`
 * already does server-side for the same name-squatting class — refusing on the
 * PRESENCE of an external marker, never granting trust from its absence.
 */
export function isBuiltinServerRow(row: BuiltinServerRow): boolean {
  return row.origin === 'builtin' && !row._registry && row.provenance !== 'external'
}

/**
 * Merge ONE server row for an installed built-in with its local manifest, for
 * the detail page.
 *
 * The generic parameter this function used to carry existed so the browse list
 * could keep its fully-normalised row type through the merge. Explore no longer
 * merges anything — it renders the rows the gateway sends — so the generic had
 * exactly one caller left and bought nothing.
 */
export function mergeBuiltinRow(
  row: BuiltinServerRow,
  manifest?: BuiltinManifestFields,
): BuiltinServerRow & RegistryApp {
  const m = manifest
  return {
    ...row,
    displayName: row.displayName || m?.displayName || row.name,
    description: row.description || m?.description || '',
    // Install-state facts describe the machine, not the document: the local
    // value wins even though the catalog publishes one.
    version: m?.version || row.version || '0.0.0',
    author: row.author || m?.author || '',
    installed: row.installed ?? true,
    tags: row.tags?.length ? row.tags : m?.tags,
    highlights: row.highlights?.length ? row.highlights : m?.highlights,
    useCases: row.useCases?.length ? row.useCases : m?.useCases,
    configuration: row.configuration?.length ? row.configuration : m?.configuration,
    screenshots: row.screenshots?.length ? row.screenshots : m?.screenshots,
    heroImage: row.heroImage || m?.heroImage,
    heroImageDark: row.heroImageDark || m?.heroImageDark,
    heroImageDetail: row.heroImageDetail || m?.heroImageDetail,
    heroImageDetailDark: row.heroImageDetailDark || m?.heroImageDetailDark,
    license: row.license || m?.license,
    icon: row.icon || m?.ui?.pages?.[0]?.icon || '',
    iconUrl: row.iconUrl || m?.iconUrl || m?.ui?.pages?.[0]?.iconUrl || '',
    iconUrlDark: row.iconUrlDark || m?.iconUrlDark || '',
  }
}
