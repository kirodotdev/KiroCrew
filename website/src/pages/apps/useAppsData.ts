/**
 * useAppsData — the SINGLE data contract shared by the Discover and Library
 * pages (the PR1 App Store split).
 *
 * Both pages render projections of the same two server sources — the registry
 * feed (`GET /api/apps/registry`) and the installed-app list (`GET /api/apps`)
 * — and every derived row (browse shelf, featured blocks, update map, Library
 * list) is computed HERE, once. Splitting the old AppsPage into two files must
 * not split this logic into two copies that can drift, for the same reason
 * `mergeBuiltinRow` exists: two inline derivations in two files can contradict
 * each other; one shared module cannot.
 *
 * View-local state (search query, category pick, sort, action loading) stays
 * in each page — this hook owns data identity, not presentation.
 */
import { useMemo, useRef } from 'react'
import { useQuery } from '@tanstack/react-query'
import { api } from '../../api/client'
import { i18nT } from '../../i18n/t'
import { compareText } from '../../i18n/format'
import type { EditorialArtwork } from '../../components/appstore/useEditorialArt'
import type { SourceRow } from '../../components/appstore/CategoryRail'
import { categoryCounts, mergeCategoryOrder, type Category } from '../../components/appstore/categories'
import { hasHeroArt } from '../../components/appstore/useHeroArt'
import {
  isVerified, normalizeInstalledApp, normalizeRegistryApp,
  type InstalledApp, type RegistryApp,
} from '../../components/appstore/types'
import { isBuiltinServerRow } from '../../components/appstore/mergeBuiltinRow'

/**
 * One published featured section, as it arrives from the registry endpoint.
 *
 * `type` is the discriminator the card branches on. An `app` section always
 * carries exactly one ref; a `collection` carries two or more plus the title
 * that explains why they share a card. Both spell the refs as a list so
 * resolution is one code path regardless of type.
 */
export type EditorialItem = {
  type: 'app' | 'collection'
  appRefs: string[]
  title?: string
  blurb?: string
  artwork?: EditorialArtwork
}

/**
 * One BLOCK of the Discover page: a `form` saying how its items are arranged,
 * and the items it arranges. The grouping is the document's, not inferred from
 * array position -- `full` renders one card across the width, `row` renders its
 * items side by side. An unrecognised form skips the WHOLE block (the
 * arrangement cannot be drawn at all); an unrecognised item type skips just
 * that card. `carousel` is a published form with no renderer here yet, so it
 * takes the unknown-form path on purpose.
 */
export type EditorialBlock = {
  form: 'full' | 'row'
  items: EditorialItem[]
  /**
   * Whether this block's placement was written by a curator (published
   * document) or synthesized from the registry (`pickFeatured`). This is a
   * DATA field, not a UI branch: both kinds render through the same path and
   * components, and the only thing that reads it is FeaturedSpotlight's
   * artwork sourcing (a curated card draws editorial art or nothing; a derived
   * card may fall back to the app's own hero, since no curator chose its art).
   */
  curated: boolean
}

/** An editorial item with its refs resolved to renderable registry rows. */
export type FeaturedItem = EditorialItem & { apps: RegistryApp[] }

/** A featured block after resolution — what Discover actually renders. */
export type FeaturedBlock = {
  form: 'full' | 'row'
  items: FeaturedItem[]
  curated: boolean
}

/** An installed app projected into the Library list (update state attached). */
export type LibraryApp = InstalledApp & {
  updateAvailable: boolean
  _newVersion?: string
}

/** A collection below this has lost members; see the drop in `featuredSections`. */
const MIN_COLLECTION_APPS = 2
/** The schema's collection ceiling, re-applied at the fetch boundary. */
const MAX_SECTION_APPS = 6

/**
 * An artwork URL safe to hand an `<img src>`, or undefined.
 *
 * The server already screens these refs and this is the SECOND check, not the
 * first. It exists because one guard for a property the contract states
 * absolutely -- no scheme other than the catalog's own may reach the DOM -- is
 * one regression away from none.
 *
 * What it blocks: every scheme except https, which covers `javascript:` and
 * `data:` (neither has a slash after the colon, so a naive `"://"` test admits
 * both), and the scheme-relative `//host` form that inherits the page scheme
 * while looking like a path.
 *
 * What it deliberately does NOT block: an https URL on a host other than the
 * catalog. Rejecting that needs the catalog origin, and the only copy of it lives
 * server-side (`official_catalog.OFFICIAL_CATALOG_BASE`); a second copy here
 * would silently blank all artwork the day the catalog moves. That case stays
 * the server's job, where the origin is already known.
 */
function editorialArtUrl(value: unknown): string | undefined {
  if (typeof value !== 'string' || !value || value.startsWith('//')) return undefined
  const local = value.startsWith('/')
  return local || value.startsWith('https://') ? value : undefined
}

/** Project a section's artwork, dropping anything whose light variant is unusable. */
function normalizeEditorialArtwork(value: unknown): EditorialArtwork | undefined {
  if (!value || typeof value !== 'object') return undefined
  const raw = value as Record<string, unknown>
  const url = editorialArtUrl(raw.url)
  if (!url) return undefined
  const urlDark = editorialArtUrl(raw.urlDark)
  const alt = typeof raw.alt === 'string' ? raw.alt : undefined
  return { url, ...(urlDark ? { urlDark } : {}), ...(alt ? { alt } : {}) }
}

/**
 * Pick up to three featured apps for the editorial layer.
 *
 * Curator flags win, but only from TRUSTED sources — a ``featured`` flag on an
 * external registry entry is ignored. The spotlight is the store's most
 * persuasive install surface and its Get button runs third-party setup code
 * with gateway privileges, so letting any added registry flag itself into that
 * slot would reintroduce the self-promotion hole that ``isVerified`` closes.
 * Numbers order the slots (lower first); remaining slots fill
 * deterministically — apps shipping hero art first, then verified publishers,
 * then name.
 */
export function pickFeatured(apps: RegistryApp[]): RegistryApp[] {
  const rank = (f: RegistryApp['featured']) => (typeof f === 'number' ? f : 1e9)
  // "Not external" via the server-computed field, falling back to the
  // server-attached ``_registry`` tag for rows from older gateways. Either
  // signal marks the row external — belt-and-braces with the server, which
  // also strips ``featured`` from external rows entirely.
  const external = (a: RegistryApp) => a.provenance === 'external' || !!a._registry
  const flagged = apps
    .filter(a => !external(a) && a.featured !== undefined && a.featured !== false)
    .sort((a, b) => rank(a.featured) - rank(b.featured) || compareText(a.displayName, b.displayName))
  const rest = apps
    .filter(a => !flagged.includes(a))
    .sort((a, b) =>
      (Number(hasHeroArt(b)) - Number(hasHeroArt(a)))
      || (Number(isVerified(b)) - Number(isVerified(a)))
      || compareText(a.displayName, b.displayName))
  return [...flagged, ...rest].slice(0, 3)
}

/**
 * Announce an install-state change to the app shell.
 *
 * Neither ['apps'] nor ['registry'] is invalidated here: the mc:apps-changed
 * listener in App.tsx owns both caches, for every dispatch site at once.
 */
export function announceAppsChanged(): void {
  window.dispatchEvent(new Event('mc:apps-changed'))
}

/** The typed contract both pages consume. */
export type AppsData = {
  /** Normalized installed apps, raw (unfiltered — Library applies `keepInLibrary` via `installedApps`). */
  apps: InstalledApp[]
  appsLoading: boolean
  appsError: Error | null
  registryError: Error | null
  /** Either source still fetching (first paint). */
  loading: boolean
  /** The Discover shelf: registry rows after suppression/demotion rules. */
  browseApps: RegistryApp[]
  /** The featured blocks Discover renders (published, or derived fallback). */
  featuredSections: FeaturedBlock[]
  /** Per-category counts over the browse shelf, empty categories omitted. */
  categories: { category: Category; count: number }[]
  /** Source rows for the rail footer / Sources popover counts. */
  sources: SourceRow[]
  /** The Library list: installed apps with update state attached. */
  installedApps: LibraryApp[]
  /** Library apps Update All would touch (gateway-lifecycle with a pending update). */
  updatables: LibraryApp[]
  /** Dispatch mc:apps-changed (module-level function, re-exported for convenience). */
  announceAppsChanged: () => void
}

// ---- Pending-update derivation ------------------------------------------
// Shared by this hook's `updatables` and App.tsx's sidebar Discover badge —
// the sidebar count must equal the Updates sub-tab count, and one shared
// derivation is the only arrangement two surfaces cannot drift under.

/** The registry-row fields the update derivation reads. */
type UpdatableRegistryRow = Pick<RegistryApp, 'name' | 'version' | 'updateAvailable'>

/**
 * The installed-row fields the update derivation reads.
 *
 * Only server-emitted fields appear here: the `['apps']` cache can hold RAW
 * rows (some observers fetch it without normalizing — see the queryFn note in
 * `useAppsData`), so this derivation must not lean on anything normalization
 * adds.
 */
export type UpdatableInstalledRow = Pick<
  InstalledApp,
  'name' | 'lifecycle' | 'origin' | 'enabled' | 'manifest'
>

/**
 * Whether an installed app belongs in the Library list.
 *
 * Every installed app belongs here except a disabled hidden builtin. Discover is
 * built from published catalog rows and deliberately does not synthesize rows
 * from local manifests, so hiding an ordinary default-off builtin here can make a
 * shipped app unreachable from both surfaces. `hidden` is still honoured for a
 * builtin because concealment is the wheel's product decision; it is not honoured
 * for a third-party manifest, which must not be able to hide itself from the only
 * surface that can enable or uninstall it.
 *
 * Exported so its test exercises this predicate rather than a copy of it.
 */
export function keepInLibrary(
  app: Pick<InstalledApp, 'origin' | 'enabled' | 'manifest'>,
): boolean {
  return app.enabled || app.origin !== 'builtin' || !app.manifest?.hidden
}

/** One app's Library placement, decided for the visit and then held. */
export type LibrarySlot = { listed: boolean; wasEnabled: boolean }

/**
 * Build the Library rows without moving or removing the control just clicked.
 *
 * Listing default-off builtins adds roughly 20 rows, so a fresh visit puts
 * enabled apps first. Both that group and the hidden-builtin admission decision
 * are held across refetches: enabling or disabling a row updates its controls in
 * place instead of moving it off-screen or deleting it. A previously concealed
 * row is promoted if an out-of-band action enables it, so the per-visit cache
 * cannot keep an enabled app unreachable. Uninstalled rows are forgotten.
 */
export function libraryView<
  T extends Pick<InstalledApp, 'origin' | 'enabled' | 'manifest'> & { name: string },
>(apps: T[], view: Map<string, LibrarySlot>): T[] {
  const live = new Set(apps.map(app => app.name))
  for (const name of view.keys()) {
    if (!live.has(name)) view.delete(name)
  }
  for (const app of apps) {
    const slot = view.get(app.name)
    if (!slot) {
      view.set(app.name, { listed: keepInLibrary(app), wasEnabled: !!app.enabled })
    } else if (!slot.listed && keepInLibrary(app)) {
      slot.listed = true
      // The row was not previously visible, so this is its first placement in
      // the visit. It appears enabled and belongs with the enabled group.
      slot.wasEnabled = !!app.enabled
    }
  }
  const rows = apps.filter(app => view.get(app.name)?.listed)
  return [
    ...rows.filter(app => view.get(app.name)?.wasEnabled),
    ...rows.filter(app => !view.get(app.name)?.wasEnabled),
  ]
}

/** name → new version for every registry row with an update available. */
function buildUpdateMap(
  registry: readonly UpdatableRegistryRow[],
): Map<string, string> {
  return new Map(registry.filter(r => r.updateAvailable).map(r => [r.name, r.version]))
}

/**
 * Whether Update All / the Updates sub-page would touch this Library row: a
 * pending update AND a gateway-managed lifecycle (`app`- and `locked`-
 * lifecycle apps manage their own updates, so the store cannot update them).
 */
function isUpdatable(
  app: Pick<InstalledApp, 'name' | 'lifecycle'>,
  updateMap: ReadonlyMap<string, string>,
): boolean {
  return updateMap.has(app.name) && app.lifecycle === 'gateway'
}

/**
 * The updates count every badge surface shows (sidebar Discover row, Discover
 * Updates sub-tab) — the length of the same list `useAppsData.updatables`
 * builds, computed from the same two payloads. Tolerates absent inputs so a
 * cold cache (store pages never visited yet) reads as "no known updates".
 */
export function countUpdatables(
  registry: readonly UpdatableRegistryRow[] | undefined,
  installed: readonly UpdatableInstalledRow[] | undefined,
): number {
  if (!registry?.length || !installed?.length) return 0
  const updateMap = buildUpdateMap(registry)
  if (updateMap.size === 0) return 0
  return installed.filter(a => keepInLibrary(a) && isUpdatable(a, updateMap)).length
}

/**
 * The single fetch boundary for the ['registry'] cache, shared by every
 * observer of that key. React Query keeps one queryFn per key — whichever
 * observer registered last fetches — so the app shell's badge query and this
 * hook MUST reference the same function: a second, raw fetcher would win the
 * registration race and hand normalized-shape consumers an unnormalized
 * payload.
 */
export async function registryQueryFn(): Promise<{
  apps: RegistryApp[]
  categoryOrder: string[]
  editorialSections: EditorialBlock[]
}> {
    const res = await api.listRegistry()
    // Normalize at the single fetch boundary: registry.py yields minimal
    // rows when an app.json fetch fails, and external registries are
    // user-supplied JSON, so display fields may be missing or mistyped.
    //
    // `categoryOrder` is published presentation, so it gets the same
    // treatment: a non-array, or a member that is not a string, collapses to
    // an empty list, which `mergeCategoryOrder` reads as "use the canonical
    // order".
    const publishedOrder = Array.isArray(res.categoryOrder)
      ? res.categoryOrder.filter((id): id is string => typeof id === 'string')
      : []
    // Published layout gets the same treatment as the order: the server
    // already screened each artwork URL, but the SHAPE arrives over HTTP like
    // any other payload, so a malformed block is dropped here rather than
    // reaching a component that would throw mid-render.
    const publishedSections: EditorialBlock[] = Array.isArray(res.editorialSections)
      ? res.editorialSections.flatMap((rawBlock: unknown) => {
          if (!rawBlock || typeof rawBlock !== 'object') return []
          const b = rawBlock as Record<string, unknown>
          // An unrecognised FORM skips the whole block: the arrangement is
          // what a form names, and a block whose arrangement this client
          // cannot draw has no partial rendering that is not a guess.
          // `carousel` lands here deliberately until a renderer ships.
          if (b.form !== 'full' && b.form !== 'row') return []
          const items: EditorialItem[] = Array.isArray(b.items)
            ? b.items.flatMap((raw: unknown) => {
                if (!raw || typeof raw !== 'object') return []
                const s = raw as Record<string, unknown>
                // An unrecognised item TYPE skips just this card -- a narrower
                // failure than the form's, because the arrangement can still
                // be drawn around a card it does not know.
                if (s.type !== 'app' && s.type !== 'collection') return []
                const refs = Array.isArray(s.appRefs)
                  ? s.appRefs.filter((n): n is string => typeof n === 'string' && !!n.trim()).map(n => n.trim())
                  : []
                // Dedupe and cap HERE as well as server-side. This boundary exists
                // to not trust the payload, and every bound it skipped was one the
                // component would have rendered: duplicate refs collide row keys,
                // and an `app` item carrying several refs would render a multi-row
                // card headed by one member's name.
                const unique = [...new Set(refs)].slice(0, MAX_SECTION_APPS)
                if (s.type === 'app' ? unique.length !== 1 : unique.length < MIN_COLLECTION_APPS) return []
                const title = typeof s.title === 'string' && s.title.trim() ? s.title.trim() : undefined
                // A collection is nothing without its theme, so one that arrives
                // without a title is dropped rather than rendered anonymously. A
                // whitespace-only title is absent, not present-and-blank -- otherwise
                // the card renders an empty heading over the rows.
                if (s.type === 'collection' && !title) return []
                return [{
                  type: s.type,
                  appRefs: unique,
                  // An `app` item is headed by the app's own name; a published
                  // title there means the document meant `collection`.
                  title: s.type === 'collection' ? title : undefined,
                  blurb: typeof s.blurb === 'string' ? s.blurb : undefined,
                  artwork: normalizeEditorialArtwork(s.artwork),
                }]
              })
            : []
          // The form's own floor, re-applied at the boundary: a `full` block
          // holds exactly one card, a `row` needs two to have anything to sit
          // beside. A block that lost cards to the item filter above can fall
          // through its floor here, and dropping it whole beats rendering a
          // half-width card against empty space.
          if (b.form === 'full' ? items.length !== 1 : items.length < 2) return []
          return [{ form: b.form, items, curated: true }]
        })
      : []
    return {
      apps: (res.apps as RegistryApp[]).map(normalizeRegistryApp),
      categoryOrder: publishedOrder,
      editorialSections: publishedSections,
    }
}

export default function useAppsData(): AppsData {
  const libraryViewRef = useRef(new Map<string, LibrarySlot>())
  const { data: apps = [], isLoading: appsLoading, error: appsError } = useQuery<InstalledApp[]>({
    queryKey: ['apps'],
    queryFn: () => api.listApps(),
    // The ['apps'] cache is shared with observers that fetch it raw
    // (MigrationCheck, the command palette's apps provider), and React Query
    // keeps one queryFn per key — whichever observer registered last fetches.
    // Normalization therefore lives in `select`, which runs on every read of
    // THIS observer no matter which caller populated the cache. Manifests
    // come from user-authored app.json files, so optional collections may be
    // missing or mistyped (mirrors the registry normalization below).
    select: rows => rows.map(normalizeInstalledApp),
  })

  const { data: registryData, isLoading: registryLoading, error: registryError } = useQuery<{
    apps: RegistryApp[]
    categoryOrder: string[]
    editorialSections: EditorialBlock[]
  }>({
    queryKey: ['registry'],
    // Shared fetch boundary — see registryQueryFn's contract comment.
    queryFn: registryQueryFn,
    staleTime: 5 * 60_000, // cache for 5min to avoid re-fetching on page switch
  })
  const registry: RegistryApp[] = useMemo(() => registryData?.apps || [], [registryData])

  // Configured external registries (shared cache key with RegistryManager)
  const { data: registriesData } = useQuery({
    queryKey: ['registries'],
    queryFn: () => api.listRegistries(),
  })

  // ---- Discover data -------------------------------------------------------

  // EXPLORE'S SOURCES ARE ALL REGISTRIES: the official registry, the user's own
  // added registries, and any this build pins. All arrive as rows on
  // `GET /api/apps/registry`, already carrying display copy, artwork, version and
  // server-stamped trust/state, so this list is those rows — nothing is
  // synthesized here, and a new kind of registry needs no change on this side.
  //
  // In particular a BUILT-IN appears on the shelf because the published catalog
  // lists it, NOT because this client read the wheel's own manifests. Rendering
  // built-ins from local manifests made the shelf a third source that only the
  // client knew about, and every defect it caused followed from that: an author
  // line the catalog had corrected but the client re-derived, a `version` taken
  // from the wrong side, and a name-collision classification that existed purely
  // to decide which local field to trust. Deleting the source deletes the class.
  //
  // The one local input that remains is a SUPPRESSION, not a source: a built-in
  // its manifest marks `hidden` stays off the shelf even when the catalog lists
  // it, because concealment is the wheel's call and a republished document must
  // not be able to reveal an app this build deliberately hides.
  //
  // Offline, the shelf is whatever the server can still answer with — the
  // catalog's cache, then the bundled seed. It is deliberately NOT topped up
  // from local manifests: nothing is installable offline anyway, and Library
  // reads `GET /api/apps` locally and lists every installed app except a disabled
  // hidden builtin (see `keepInLibrary`).
  const browseApps: RegistryApp[] = useMemo(() => {
    const hiddenBuiltins = new Set(
      apps.filter(a => a.origin === 'builtin' && a.manifest?.hidden).map(a => a.name),
    )
    const installedNames = new Set(apps.map(a => a.name))
    return registry
      .filter(r => {
        if (hiddenBuiltins.has(r.name)) return false
        // A catalog-only BUILT-IN row — `source.type === 'builtin'` with nothing
        // installed under that name — names an app this wheel does not ship. A
        // built-in has no install coordinates, so the generic Install card would
        // render a control that cannot work. Dropped until a
        // `minClientVersion`-aware "needs a newer Kiro Crew" state exists to say
        // so honestly. This reads `apps` purely as INSTALL STATE, never as a
        // source of display copy.
        if (
          !installedNames.has(r.name) &&
          (r as { source?: { type?: string } }).source?.type === 'builtin'
        ) {
          return false
        }
        return true
      })
      // `origin` is stamped from the INSTALLED app of the same name, so an
      // EXTERNAL registry row named after an installed built-in arrives carrying
      // `origin: "builtin"` while `_registry` / `provenance` still say external.
      // The row's own copy is all that renders now, but the FIRST-PARTY LABEL and
      // the Sources count still read `origin`, so a row that fails the trust test
      // is demoted here rather than allowed to wear a badge it did not earn.
      .map(r => (r.origin === 'builtin' && !isBuiltinServerRow(r) ? { ...r, origin: 'registry' } : r))
  }, [apps, registry])

  /**
   * The featured blocks Discover renders, whatever their source. Published
   * editorial sections are resolved against the apps this client can actually
   * show. A reference that resolves to nothing is dropped — the registry is
   * the source of truth for what exists, so editorial can never conjure an
   * app by naming one.
   *
   * A collection that falls below two resolvable apps is dropped whole rather
   * than demoted to a single-app card: the title states why several apps belong
   * together, and showing one survivor under that theme would claim something
   * the curator did not write.
   *
   * When no published block survives (today's live state: `sections` is
   * published empty), the memo synthesizes blocks of the SAME shape from
   * `pickFeatured`. The fallback is a data-level substitution — the render
   * path consumes one list and cannot tell a curated block from a derived
   * one except through the `curated` field it forwards.
   */
  const featuredSections: FeaturedBlock[] = useMemo(() => {
    const byName = new Map(browseApps.map(a => [a.name, a]))
    const published: FeaturedBlock[] = (registryData?.editorialSections || []).flatMap(block => {
      const items = block.items.flatMap(item => {
        const resolved = item.appRefs.map(n => byName.get(n)).filter((a): a is RegistryApp => !!a)
        const floor = item.type === 'collection' ? MIN_COLLECTION_APPS : 1
        if (resolved.length < floor) return []
        return [{ ...item, apps: resolved }]
      })
      // Re-apply the form's floor AFTER resolution: a row whose second card
      // dissolved (its apps left the registry) is a full-width slot holding a
      // half-width card, which is an arrangement the curator did not write.
      if (block.form === 'full' ? items.length !== 1 : items.length < 2) return []
      return [{ form: block.form, items, curated: block.curated }]
    })
    if (published.length > 0) return published
    // No usable published layout: synthesize the SAME block shape from the
    // derived pick, so the fallback happens in DATA and the render path
    // never learns which source fed it. The lead takes the `full` slot the
    // curator would have written; the remaining picks sit beside each other as
    // a `row`. `curated: false` is what lets these cards draw the app's own
    // hero art (no curator supplied editorial artwork to prefer).
    const [lead, ...rest] = pickFeatured(browseApps)
    if (!lead) return []
    // Explicitly EditorialItem-shaped (plus the resolved apps), so a derived
    // card and a published card are the same type to the render path -- the
    // optional fields a curator could have written simply hold nothing here.
    const derive = (app: RegistryApp): FeaturedItem => ({
      type: 'app',
      appRefs: [app.name],
      apps: [app],
    })
    const blocks: FeaturedBlock[] = [{ form: 'full', items: [derive(lead)], curated: false }]
    // A row needs two cards to have anything to sit beside -- the same floor
    // the published boundary applies. With one leftover pick, the lead stands
    // alone rather than a half-width card against empty space.
    if (rest.length >= 2) {
      blocks.push({ form: 'row', items: rest.map(derive), curated: false })
    }
    return blocks
  }, [registryData, browseApps])

  // The published rail order decides the sequence of the categories it names;
  // anything it omits keeps its canonical position. An absent or unusable
  // document leaves the order exactly as it was before the editorial document
  // existed.
  const categoryOrder = useMemo(
    () => mergeCategoryOrder(registryData?.categoryOrder || []),
    [registryData],
  )
  const categories = useMemo(
    () => categoryCounts(browseApps, categoryOrder),
    [browseApps, categoryOrder],
  )

  const sources: SourceRow[] = useMemo(() => {
    // Count built-ins from browseApps so the SOURCES totals describe the same
    // population as the "All apps" count (built-ins are always browsable,
    // enabled or not).
    const builtinCount = browseApps.filter(a => a.origin === 'builtin').length
    const counts = new Map<string, number>()
    let coreCount = 0
    for (const a of browseApps) {
      if (a.origin === 'builtin') continue
      if (a._registry) counts.set(a._registry, (counts.get(a._registry) || 0) + 1)
      else coreCount++
    }
    const rows: SourceRow[] = []
    if (builtinCount > 0) rows.push({ name: '__builtin__', label: i18nT('pages.appsPage.built_in_kirocrew'), count: builtinCount, builtin: true })
    for (const reg of registriesData?.registries || []) {
      rows.push({ name: reg.repo, label: reg.name || reg.repo, count: counts.get(reg.name || reg.repo) || 0, builtin: false })
      counts.delete(reg.name || reg.repo)
    }
    // Registries present in entries but no longer configured (stale cache)
    for (const [name, count] of counts) rows.push({ name, label: name, count, builtin: false })
    if (coreCount > 0) rows.push({ name: '__core__', label: i18nT('pages.appsPage.kirocrew_registry'), count: coreCount, builtin: true })
    return rows
  }, [browseApps, registriesData])

  // ---- Library data --------------------------------------------------------

  const updateMap = useMemo(() => buildUpdateMap(registry), [registry])
  const installedApps: LibraryApp[] = useMemo(
    () =>
      libraryView(apps, libraryViewRef.current)
        .map(a => ({
          ...a,
          updateAvailable: updateMap.has(a.name),
          _newVersion: updateMap.get(a.name),
        })),
    [apps, updateMap],
  )
  const updatables = useMemo(
    // Keep this live-filtered rather than visit-held: `countUpdatables` powers
    // the shell badge from the same raw cache and must equal the Updates list.
    () => apps
      .filter(keepInLibrary)
      .map(a => ({ ...a, updateAvailable: updateMap.has(a.name), _newVersion: updateMap.get(a.name) }))
      .filter(a => isUpdatable(a, updateMap)),
    [apps, updateMap],
  )

  return {
    apps,
    appsLoading,
    appsError,
    registryError,
    loading: appsLoading || registryLoading,

    browseApps,
    featuredSections,
    categories,
    sources,
    installedApps,
    updatables,
    announceAppsChanged,
  }
}
