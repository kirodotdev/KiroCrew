/**
 * AppsPage — the Apps page, per the locked hybrid design (editorial front,
 * marketplace engine).
 *
 * Discover (landing tab): featured editorial blocks (published layout when the
 * catalog carries one, otherwise the same block shape synthesized from the
 * ``featured`` flag -- one render path either way), then an "All apps" section
 * with a category
 * rail (canonical categories + registry sources with counts) and a sortable
 * dense list. The editorial layer shows only for the unfiltered All view.
 *
 * Library: installed-app management — pending-updates banner with Update All,
 * plus the existing management cards (InstalledAppCard).
 *
 * Supply-side controls (external registries, Install from Path) live behind
 * the Sources gear in the header (SourcesPopover).
 */
import { useEffect, useMemo, useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { useNavigate } from 'react-router-dom'
import {
  Package, Bot, Zap, Clock, ShoppingBag, Lock, Trash2, X, ArrowUp, Boxes,
  AlertTriangle, PowerOff,
} from 'lucide-react'
import { api } from '../api/client'
import { appNavTarget } from '../appNav'
import { Btn, EmptyState, PageHeader, SearchInput } from '../components/ui'
import SimpleSelect from '../components/SimpleSelect'
import { recordEvent } from '../rum'
import SegmentedControl from '../components/SegmentedControl'
import FeaturedSpotlight from '../components/appstore/FeaturedSpotlight'
import type { EditorialArtwork } from '../components/appstore/useEditorialArt'
import CategoryRail, { type SourceRow } from '../components/appstore/CategoryRail'
import AppListRow from '../components/appstore/AppListRow'
import InstalledAppCard from '../components/appstore/InstalledAppCard'
import TrustAppModal, { isTrustDeniedError, useTrustGate, type TrustAppTarget } from '../components/appstore/TrustAppModal'
import SourcesPopover from '../components/appstore/SourcesPopover'
import { categoryFor, categoryCounts, mergeCategoryOrder, type Category } from '../components/appstore/categories'
import { hasHeroArt } from '../components/appstore/useHeroArt'
import { isRegistrySourced, isVerified, normalizeInstalledApp, normalizeRegistryApp, type InstalledApp, type RegistryApp } from '../components/appstore/types'
import { isBuiltinServerRow } from '../components/appstore/mergeBuiltinRow'

import { i18nT } from '../i18n/t'
import ErrorNotice from '../components/ErrorNotice'
import ErrorBoundary from '../components/ErrorBoundary'
/** Uninstall preview payload (mirrors ``api.uninstallPreview`` return shape). */
type UninstallPreview = Awaited<ReturnType<typeof api.uninstallPreview>>
type RemovableDep = UninstallPreview['dependencies']['removable'][number]
type SharedDep = UninstallPreview['dependencies']['shared'][number]
type UserInstalledDep = UninstallPreview['dependencies']['userInstalled'][number]

type Tab = 'discover' | 'library'

/** Read the persisted tab, mapping legacy stored values (installed/browse) onto current tabs. */
function initialTab(): Tab {
  const stored = sessionStorage.getItem('appstore-tab')
  if (stored === 'library' || stored === 'installed') return 'library'
  return 'discover'
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
/**
 * One published featured section, as it arrives from the registry endpoint.
 *
 * `type` is the discriminator the card branches on. An `app` section always
 * carries exactly one ref; a `collection` carries two or more plus the title
 * that explains why they share a card. Both spell the refs as a list so
 * resolution is one code path regardless of type.
 */
type EditorialItem = {
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
type EditorialBlock = {
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
 * Which app in a featured card has an action in flight, or null.
 *
 * `actionLoading` is a single `"<name>:<action>"` slot, so only one app can be
 * busy at a time. Resolving it to a name lets each row disable its OWN control
 * instead of the card disabling all of them — pressing Get on one member of a
 * collection must not freeze the others.
 *
 * A value with no colon is treated as the whole name rather than silently losing
 * its last character, so a future caller that sets a bare name still disables the
 * right row instead of no row.
 */
export function featuredBusyName(actionLoading: string | null, apps: RegistryApp[]): string | null {
  if (!actionLoading) return null
  const sep = actionLoading.indexOf(':')
  const name = sep === -1 ? actionLoading : actionLoading.slice(0, sep)
  return apps.some(a => a.name === name) ? name : null
}

/**
 * Boundary key carrying FULL data identity (#3702).
 *
 * `ErrorBoundary` latches its error state, so the key must change whenever the
 * row's data changes — that is what remounts a boundary whose card threw once
 * the registry payload is corrected. Keying on selected fields (name, version,
 * icon, …) is whack-a-mole: the fix re-latches whenever the crashing field is
 * one the key does not carry (e.g. a same-version icon correction). Serializing
 * the whole row makes "any field changed" the remount condition, and an
 * identical refetch produces the identical string, so nothing remounts
 * spuriously. Rows come from React Query's cache via `useMemo`, so references
 * are stable across unrelated re-renders — the WeakMap makes the serialization
 * once per distinct row object.
 */
const cardKeyCache = new WeakMap<object, string>()
function cardDataKey(row: object): string {
  let key = cardKeyCache.get(row)
  if (key === undefined) {
    key = JSON.stringify(row)
    cardKeyCache.set(row, key)
  }
  return key
}

/**
 * Compact degraded placeholder for a Browse-tab card whose render threw
 * (#3702). Mirrors the Library-card boundary fallback (#3689): the broken
 * card degrades in place while its siblings and the page chrome keep
 * rendering. Browse cards describe registry entries rather than installed
 * apps, so unlike the Library fallback there is no management action to
 * preserve — the card is notice-only.
 */
function BrowseCardFallback({ label, message, className }: { label?: string; message: string; className?: string }) {
  return (
    <div className={`border border-border rounded-lg p-4 flex items-center gap-3${className ? ` ${className}` : ''}`}>
      <AlertTriangle aria-hidden className="lucide-inline text-[var(--warn)] shrink-0" />
      <div className="min-w-0 text-sm">
        {label && <span className="font-medium text-text">{label}</span>}
        <span className={label ? 'text-muted ml-2' : 'text-muted'}>{message}</span>
      </div>
    </div>
  )
}

export function pickFeatured(apps: RegistryApp[]): RegistryApp[] {
  const rank = (f: RegistryApp['featured']) => (typeof f === 'number' ? f : 1e9)
  // "Not external" via the server-computed field, falling back to the
  // server-attached ``_registry`` tag for rows from older gateways. Either
  // signal marks the row external — belt-and-braces with the server, which
  // also strips ``featured`` from external rows entirely.
  const external = (a: RegistryApp) => a.provenance === 'external' || !!a._registry
  const flagged = apps
    .filter(a => !external(a) && a.featured !== undefined && a.featured !== false)
    .sort((a, b) => rank(a.featured) - rank(b.featured) || a.displayName.localeCompare(b.displayName))
  const rest = apps
    .filter(a => !flagged.includes(a))
    .sort((a, b) =>
      (Number(hasHeroArt(b)) - Number(hasHeroArt(a)))
      || (Number(isVerified(b)) - Number(isVerified(a)))
      || a.displayName.localeCompare(b.displayName))
  return [...flagged, ...rest].slice(0, 3)
}

/**
 * Whether an installed app belongs in the Library list.
 *
 * A disabled builtin is normally hidden: the wheel ships ~20 of them default-off
 * and listing every one would bury the apps a reader actually uses. An app that
 * REPLACES a host surface is the exception, because it is the only class a reader
 * can turn off and then need to find again -- its own copy tells them to disable
 * it to get the old surface back, and with the row gone from Library and no
 * catalog row in Discover that would be a one-way switch. Keyed on `ui.overlays`
 * rather than on the app id so the rule belongs to the capability, not to a name.
 *
 * Exported so its test exercises this predicate rather than a copy of it.
 */
export function keepInLibrary(
  app: Pick<InstalledApp, 'origin' | 'enabled' | 'manifest'>,
): boolean {
  return !(app.origin === 'builtin' && !app.enabled && !app.manifest?.ui?.overlays?.length)
}

export default function AppsPage() {
  const navigate = useNavigate()
  const [tab, setTab] = useState<Tab>(initialTab)
  useEffect(() => { sessionStorage.setItem('appstore-tab', tab) }, [tab])
  const [query, setQuery] = useState('')
  const [category, setCategory] = useState<Category | 'All'>('All')
  const [sort, setSort] = useState<'name' | 'category'>('name')
  const [sourcesOpen, setSourcesOpen] = useState(false)
  const [error, setError] = useState('')
  const [successMsg, setSuccessMsg] = useState('')
  const [actionLoading, setActionLoading] = useState<string | null>(null)
  const [updatingAll, setUpdatingAll] = useState<{ done: number; total: number } | null>(null)
  const [dismissedQueryError, setDismissedQueryError] = useState(false)

  // Uninstall confirmation state (Library)
  const [uninstallTarget, setUninstallTarget] = useState<InstalledApp | null>(null)
  const [keepData, setKeepData] = useState(true)
  const [uninstallPreview, setUninstallPreview] = useState<UninstallPreview | null>(null)
  const [keepSpecific, setKeepSpecific] = useState<Set<string>>(new Set())
  // Resource lists the uninstall dialog itemises, derived once so the count it
  // prints and the check that decides to print it read the same value. Reading
  // the list twice — a `?.` gate here, a `!` assertion there — is the shape that
  // produced #3689; `normalizeInstalledApp` means the fallback is now only for a
  // record that never reached the fetch boundary.
  const uninstallAgents = uninstallTarget?.manifest?.agents || []
  const uninstallSkills = uninstallTarget?.manifest?.skills || []
  const uninstallCrons = uninstallTarget?.manifest?.crons || []

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
    // api.listRegistry() types `apps` as unknown[]; the backend payload matches
    // RegistryApp, so narrow it here at the single fetch boundary.
    queryFn: async () => {
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
    },
    staleTime: 5 * 60_000, // cache for 5min to avoid re-fetching on tab switch
  })
  const registry: RegistryApp[] = useMemo(() => registryData?.apps || [], [registryData])

  // Configured external registries (shared cache key with RegistryManager)
  const { data: registriesData } = useQuery({
    queryKey: ['registries'],
    queryFn: () => api.listRegistries(),
  })

  useEffect(() => { if (appsError || registryError) setDismissedQueryError(false) }, [appsError, registryError])
  const displayError = error
    || (!dismissedQueryError && appsError ? (appsError as Error)?.message || i18nT('pages.appsPage.failed_to_load_apps') : '')
    || (!dismissedQueryError && registryError ? (registryError as Error)?.message || i18nT('pages.appsPage.failed_to_load_registry') : '')

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
  // from local manifests: nothing is installable offline anyway, and installed
  // built-ins remain fully visible and manageable under Library, which reads
  // `GET /api/apps` locally.
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
  const featuredSections = useMemo(() => {
    const byName = new Map(browseApps.map(a => [a.name, a]))
    const published = (registryData?.editorialSections || []).flatMap(block => {
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
    // derived pick, so the fallback happens in DATA and the render path below
    // never learns which source fed it. The lead takes the `full` slot the
    // curator would have written; the remaining picks sit beside each other as
    // a `row`. `curated: false` is what lets these cards draw the app's own
    // hero art (no curator supplied editorial artwork to prefer).
    const [lead, ...rest] = pickFeatured(browseApps)
    if (!lead) return []
    // Explicitly EditorialItem-shaped (plus the resolved apps), so a derived
    // card and a published card are the same type to the render path -- the
    // optional fields a curator could have written simply hold nothing here.
    const derive = (app: RegistryApp): EditorialItem & { apps: RegistryApp[] } => ({
      type: 'app',
      appRefs: [app.name],
      apps: [app],
    })
    const blocks: typeof published = [{ form: 'full', items: [derive(lead)], curated: false }]
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

  const filteredBrowse = useMemo(() => {
    const q = query.trim().toLowerCase()
    const list = browseApps.filter(a => {
      if (category !== 'All' && categoryFor(a.tags) !== category) return false
      if (!q) return true
      return a.displayName.toLowerCase().includes(q)
        || a.description.toLowerCase().includes(q)
        || (a.tags || []).some(t => t.toLowerCase().includes(q))
    })
    return list.sort((a, b) => sort === 'category'
      ? categoryFor(a.tags).localeCompare(categoryFor(b.tags)) || a.displayName.localeCompare(b.displayName)
      : a.displayName.localeCompare(b.displayName))
  }, [browseApps, category, query, sort])

  /* The editorial layer survives a CATEGORY pick -- curated placements are
     content, not list rows, so the rail only filters the All-apps list below.
     A SEARCH still hides it: a typed query is a stated intent to find one
     thing, and the spotlight would push the results below the fold. */
  const showEditorial = !query.trim() && featuredSections.length > 0

  // ---- Library data --------------------------------------------------------

  const updateMap = useMemo(
    () => new Map(registry.filter(r => r.updateAvailable).map(r => [r.name, r.version])),
    [registry],
  )
  const installedApps = useMemo(
    () =>
      apps
        .filter(keepInLibrary)
        .map(a => ({
          ...a,
          updateAvailable: updateMap.has(a.name),
          _newVersion: updateMap.get(a.name),
        })),
    [apps, updateMap],
  )
  const filteredInstalled = useMemo(() => {
    const q = query.trim().toLowerCase()
    if (!q) return installedApps
    return installedApps.filter(a =>
      a.name.toLowerCase().includes(q)
      || (a.displayName || '').toLowerCase().includes(q)
      || (a.manifest?.description || '').toLowerCase().includes(q)
      || (a.manifest?.tags || []).some(t => t.toLowerCase().includes(q)))
  }, [installedApps, query])

  const updatables = useMemo(
    () => installedApps.filter(a => updateMap.has(a.name) && a.lifecycle === 'gateway'),
    [installedApps, updateMap],
  )

  // ---- Actions --------------------------------------------------------------

  const announceAppsChanged = () => {
    // Neither ['apps'] nor ['registry'] is invalidated here: the
    // mc:apps-changed listener in App.tsx owns both caches, for every
    // dispatch site at once.
    window.dispatchEvent(new Event('mc:apps-changed'))
  }

  // Cmd/Ctrl-click opens the detail page in a new tab.
  const openDetail = (name: string, e?: React.MouseEvent | React.KeyboardEvent) => {
    if (e && (e.metaKey || e.ctrlKey)) { window.open(`/apps/detail/${name}`, '_blank', 'noopener,noreferrer'); return }
    navigate(`/apps/detail/${name}`)
  }
  // autoAction travels as router STATE, never a query param — a URL-reachable
  // trigger would let a cross-site navigation start a privileged install.
  //
  // Get / Update on this page NAVIGATE and never call an install endpoint
  // themselves (FeaturedSpotlight, the Browse cards and AppListRow all route
  // their `onGet` here), so the registry-install trust refusal — which the
  // gateway now raises before cloning — surfaces on the detail page, where
  // `handleInstall` owns the consent modal. Nothing to gate here.
  const getApp = (name: string) => navigate(`/apps/detail/${name}`, { state: { autoAction: 'install' } })
  const updateApp = (name: string) => navigate(`/apps/detail/${name}`, { state: { autoAction: 'update' } })

  // Provenance the consent modal shows. For an installed app, the server-bound
  // source wins over today's registry row; for an install prompt, the registry's
  // server-resolved clone target is the authority.
  const trustTarget = (name: string): TrustAppTarget => {
    const row = browseApps.find(a => a.name === name)
    const installed = apps.find(a => a.name === name)
    if (installed) return {
      name,
      displayName: installed.displayName,
      trustRepository: installed.trustRepository,
      origin: installed.origin,
      _registry: row?._registry,
    }
    if (row) return {
      name: row.name,
      displayName: row.displayName,
      trustRepository: row.trustRepository,
      origin: row.origin,
      _registry: row._registry,
    }
    return { name }
  }

  /** The single enable path — shared by Discover, Library, and the trust retry. */
  const runEnable = async (name: string) => {
    await api.enableApp(name)
    recordEvent('app_enable', { app: name })
    announceAppsChanged()
  }

  const trust = useTrustGate(runEnable)

  const enableApp = async (name: string) => {
    setActionLoading(`${name}:enable`)
    setError('')
    try {
      await runEnable(name)
    } catch (e) {
      // A third-party app that has not been granted execution trust yet is a
      // consent prompt, not an error — branch on the machine-readable code.
      if (isTrustDeniedError(e)) trust.open(trustTarget(name))
      else setError((e as Error)?.message || i18nT('pages.appsPage.failed_to_enable', { name }))
    } finally {
      setActionLoading(null)
    }
  }

  const handleAction = async (name: string, action: 'enable' | 'disable' | 'uninstall' | 'update') => {
    // Intercept uninstall to show confirmation modal with preview
    if (action === 'uninstall') {
      const app = apps.find(a => a.name === name)
      if (app) {
        setUninstallTarget(app)
        setKeepData(true)
        setKeepSpecific(new Set())
        // Fetch uninstall preview (best-effort — dialog works without it)
        try {
          setUninstallPreview(await api.uninstallPreview(name))
        } catch {
          setUninstallPreview(null)
        }
      }
      return
    }
    // Update dispatches on the RECORDED SOURCE, mirroring ``handle_update_app``'s
    // own branch. A registry-sourced app is re-cloned from its registry and the
    // detail page owns that flow (streaming log plus the trust consent modal), so
    // it navigates there. An app installed from a path has no registry row: it is
    // refreshed in place from the directory recorded at install — the same call
    // Update All makes — and routing it at the registry instead failed every sync
    // with "not found in registry". A row absent from this list carries no source
    // to read, so it navigates and the detail page re-dispatches on the record it
    // loads. Blocked while Update All is running so the same update can't run
    // twice concurrently.
    if (action === 'update') {
      if (updatingAll) return
      const target = apps.find(a => a.name === name)
      if (!target || isRegistrySourced(target)) {
        updateApp(name)
        return
      }
    }
    setActionLoading(`${name}:${action}`)
    setError('')
    try {
      if (action === 'enable') await runEnable(name)
      else if (action === 'disable') await api.disableApp(name)
      else if (action === 'update') await api.updateApp(name)
      announceAppsChanged()
      // An in-place sync is the one action here whose success is otherwise
      // INVISIBLE: re-copying a source directory usually carries the same
      // version, so the card re-renders byte-identical and the dev cannot tell
      // whether new bytes landed. Reflect it the way `disable` already does.
      if (action === 'update') {
        const app = apps.find(a => a.name === name)
        setSuccessMsg(i18nT('pages.appsPage.synced_from_its_source_directory', {
          name: app?.displayName || name,
        }))
        setTimeout(() => setSuccessMsg(''), 4000)
      }
      // Show toast when hiding a builtin app
      if (action === 'disable') {
        const app = apps.find(a => a.name === name)
        if (app?.origin === 'builtin') {
          setSuccessMsg(i18nT('pages.appsPage.hidden_you_can_re_enable_it_from_the_discover_ta'))
          setTimeout(() => setSuccessMsg(''), 4000)
        }
      }
    } catch (e) {
      if (action === 'enable' && isTrustDeniedError(e)) trust.open(trustTarget(name))
      else setError((e as Error)?.message || i18nT('pages.appsPage.action_failed', { action, name }))
    } finally {
      setActionLoading(null)
    }
  }

  const confirmUninstall = async () => {
    if (!uninstallTarget) return
    const name = uninstallTarget.name
    setActionLoading(`${name}:uninstall`)
    setError('')
    try {
      await api.uninstallApp(name, keepData, false, Array.from(keepSpecific))
      recordEvent('app_uninstall', { app: name, version: uninstallTarget.version })
      announceAppsChanged()
    } catch (e) {
      setError((e as Error)?.message || i18nT('pages.appsPage.failed_to_uninstall', { name }))
    } finally {
      setActionLoading(null)
      setUninstallTarget(null)
      setUninstallPreview(null)
    }
  }

  const updateAll = async () => {
    if (updatingAll) return
    const targets = updatables.map(a => a.name)
    setUpdatingAll({ done: 0, total: targets.length })
    setError('')
    const failed: string[] = []
    for (let i = 0; i < targets.length; i++) {
      try {
        await api.updateApp(targets[i])
      } catch {
        failed.push(targets[i])
      }
      setUpdatingAll({ done: i + 1, total: targets.length })
    }
    setUpdatingAll(null)
    announceAppsChanged()
    if (failed.length) setError(i18nT('pages.appsPage.failed_to_update', { names: failed.join(', ') }))
    else {
      setSuccessMsg(i18nT('pages.appsPage.updated_app', { count: targets.length }))
      setTimeout(() => setSuccessMsg(''), 4000)
    }
  }

  const loading = appsLoading || registryLoading

  return (
    <>
      {/* Standard page header with a right-side actions slot: tabs, search,
          and the Sources gear (page-layout-pattern). */}
      <PageHeader
        title={i18nT('pages.appsPage.apps')}
        subtitle={i18nT('pages.appsPage.discover_install_and_manage_agentic_apps')}
        actions={<>
          <SegmentedControl
            segments={[
              { key: 'discover' as const, label: i18nT('pages.appsPage.discover'), icon: <Boxes size={13} /> },
              { key: 'library' as const, label: i18nT('pages.appsPage.library'), icon: <Package size={13} />, count: installedApps.length },
            ]}
            value={tab}
            onChange={setTab}
            layoutId="app-store-tabs"
          />
          <SearchInput
            placeholder={tab === 'discover' ? i18nT('pages.appsPage.search_apps') : i18nT('pages.appsPage.search_library')}
            value={query}
            onChange={(e: React.ChangeEvent<HTMLInputElement>) => setQuery(e.target.value)}
            className="w-[220px]"
            aria-label={i18nT('pages.appsPage.search_apps')}
          />
          <SourcesPopover
            open={sourcesOpen}
            onOpenChange={setSourcesOpen}
            onError={setError}
            onInstalled={(name) => {
              // A path-installed app lands DISABLED, so it never shows in the
              // sidebar — steer to Library (where disabled apps live) and
              // confirm, instead of the popover silently closing. Clear any
              // Discover search first: the Library list filters on the same
              // `query`, so a stale non-matching term would hide the new app.
              setQuery('')
              setTab('library')
              setSuccessMsg(i18nT('pages.appsPage.installed_app_find_in_library_and_enable', { name }))
              setTimeout(() => setSuccessMsg(''), 6000)
            }}
          />
        </>}
      />

      <div className="px-4 md:px-6 pb-8 overflow-y-auto flex-1 min-h-0">
        {/* Width cap on the content column only (the scrollbar stays at the
            viewport edge). Discover is the one storefront surface: uncapped,
            an ultrawide monitor stretches the lead card's 16:9 art and the
            copy's line length past comfortable reading. Utility pages stay
            full-width; a content shelf follows store convention instead. */}
        <div className="max-w-[1200px] mx-auto">
        {/* Notifications. No hand-off on the error notice: the SourcesPopover's
            install-path input shares this page — navigating away would discard
            what the user typed. */}
        {displayError && (
          <ErrorNotice
            message={displayError}
            onDismiss={() => { setError(''); setDismissedQueryError(true) }}
            className="mb-4 animate-rise"
          />
        )}
        {successMsg && (
          <div className="mb-4 bg-bg-elevated border rounded-lg p-3 flex items-center gap-3 animate-rise" style={{ borderColor: 'color-mix(in srgb, var(--ok) 45%, transparent)' }}>
            <span className="text-text text-sm flex-1">{successMsg}</span>
            <button aria-label={i18nT('pages.appsPage.dismiss_message')} className="text-muted hover:text-text text-sm" onClick={() => setSuccessMsg('')}><X className="lucide-inline" /></button>
          </div>
        )}

        {/* Third-party execution-trust consent. Opened when an enable is
            refused with code `app_execution_denied`, instead of surfacing the
            raw backend string in the error card above. */}
        <TrustAppModal
          app={trust.target}
          pending={trust.pending}
          failed={trust.failed}
          granted={trust.granted}
          onCancel={trust.cancel}
          onConfirm={trust.confirm}
        />

        {/* Uninstall confirmation modal. The backdrop closes on click (mouse
            convenience); keyboard users press Escape (handled) or the Cancel
            button inside. The inner card's onClick only stops propagation so a
            click inside doesn't bubble to the backdrop-close — it is not a user
            interaction, hence the scoped disables. */}
        {uninstallTarget && (
          // eslint-disable-next-line jsx-a11y/no-noninteractive-element-interactions
          <div className="fixed inset-0 z-[100] flex items-center justify-center bg-bg/60 backdrop-blur-sm animate-rise"
            onClick={() => { setUninstallTarget(null); setUninstallPreview(null) }}
            onKeyDown={e => { if (e.key === 'Escape') { setUninstallTarget(null); setUninstallPreview(null) } }}
            tabIndex={-1} ref={el => el?.focus()} role="dialog" aria-modal="true" aria-label={i18nT('pages.appsPage.confirm_uninstall')}
          >
            {/* eslint-disable-next-line jsx-a11y/no-static-element-interactions, jsx-a11y/click-events-have-key-events */}
            <div className="bg-card border border-border rounded-xl p-6 max-w-md w-full mx-4 shadow-xl" onClick={e => e.stopPropagation()}>
              <div className="flex items-center gap-3 mb-4">
                <div className="w-10 h-10 rounded-xl bg-danger/10 flex items-center justify-center">
                  <Trash2 size={20} className="text-danger" />
                </div>
                <div>
                  <div className="font-medium text-text">{i18nT('pages.appsPage.uninstall')} {uninstallTarget.displayName || uninstallTarget.name}?</div>
                  <div className="text-[12px] text-muted">{i18nT('pages.appsPage.v')}{uninstallTarget.version}</div>
                </div>
              </div>

              <p className="text-[13px] text-muted mb-3">{i18nT('pages.appsPage.this_will_remove_all_resources_provided_by_this')}</p>
              <div className="text-[13px] text-text mb-4 space-y-1">
                {uninstallTarget.resources === 'app' && !uninstallTarget.manifest?.setup?.onUninstall && uninstallTarget.origin !== 'registry' && (
                  <div className="bg-bg-elevated border border-border rounded-md px-2.5 py-2 text-[12px] text-muted mb-2">
                    {i18nT('pages.appsPage.this_is_a_self_managed_app_only_kirocrew_metadat')}
                  </div>
                )}
                {uninstallTarget.manifest?.setup?.onUninstall && (
                  <div className="bg-danger/5 border border-danger/20 rounded-md px-2.5 py-2 text-[12px] text-muted mb-2">
                    {i18nT('pages.appsPage.this_app_has_an_uninstall_script_that_will_run_b')}
                  </div>
                )}
                {uninstallTarget.origin === 'registry' && (
                  <div className="bg-bg-elevated border border-border rounded-md px-2.5 py-2 text-[12px] text-muted mb-2">
                    {uninstallTarget.resources === 'app' ? i18nT('pages.appsPage.uninstall_removes_metadata_secret_and_source') : i18nT('pages.appsPage.uninstall_removes_metadata_and_source')}{uninstallTarget.resources === 'app' && !uninstallTarget.manifest?.setup?.onUninstall ? ' ' + i18nT('pages.appsPage.the_app_itself_is_managed_externally') : ''}
                  </div>
                )}
                {uninstallTarget.origin !== 'registry' && uninstallTarget.resources === 'app' && uninstallTarget.manifest?.setup?.onUninstall && (
                  <div className="bg-bg-elevated border border-border rounded-md px-2.5 py-2 text-[12px] text-muted mb-2">
                    {i18nT('pages.appsPage.not_installed_from_apps_your_local_source_code_w')}
                  </div>
                )}
                {uninstallAgents.length > 0 && (
                  <div className="flex items-center gap-2"><Bot size={12} className="text-muted" /> {i18nT('pages.appsPage.agent', { count: uninstallAgents.length })}</div>
                )}
                {uninstallSkills.length > 0 && (
                  <div className="flex items-center gap-2"><Zap size={12} className="text-muted" /> {i18nT('pages.appsPage.skill', { count: uninstallSkills.length })}</div>
                )}
                {uninstallCrons.length > 0 && (
                  <div className="flex items-center gap-2"><Clock size={12} className="text-muted" /> {i18nT('pages.appsPage.cron_job', { count: uninstallCrons.length })}</div>
                )}
              </div>

              {/* Dependency preview */}
              {uninstallPreview?.dependencies && (
                (() => {
                  const deps = uninstallPreview.dependencies
                  const hasAny = (deps.removable?.length || 0) + (deps.shared?.length || 0) + (deps.userInstalled?.length || 0) > 0
                  if (!hasAny) return null
                  return (
                    <div className="mb-4">
                      <p className="text-[13px] text-muted mb-2">{i18nT('pages.appsPage.dependencies')}</p>
                      <div className="space-y-2 text-[13px]">
                        {(deps.removable || []).map((d: RemovableDep) => (
                          <div key={d.id} className="flex items-start gap-2">
                            <Trash2 size={12} className="text-danger mt-0.5 shrink-0" />
                            <div className="flex-1">
                              <div className="text-text">{d.id.split('/').pop()}</div>
                              <div className="text-[11px] text-muted">{d.reason}</div>
                              <label htmlFor={`keep-dep-${d.id}`} className="flex items-center gap-1.5 mt-1 text-[11px] text-muted cursor-pointer">
                                <input
                                  id={`keep-dep-${d.id}`}
                                  type="checkbox"
                                  aria-label={i18nT('pages.appsPage.keep_dependency', { name: d.id.split('/').pop() })}
                                  checked={keepSpecific.has(d.id)}
                                  onChange={e => {
                                    const next = new Set(keepSpecific)
                                    if (e.target.checked) next.add(d.id); else next.delete(d.id)
                                    setKeepSpecific(next)
                                  }}
                                  className="rounded"
                                />
                                {i18nT('pages.appsPage.keep_this_dependency')}
                              </label>
                            </div>
                          </div>
                        ))}
                        {(deps.shared || []).map((d: SharedDep) => (
                          <div key={d.id} className="flex items-start gap-2">
                            <Lock size={12} className="text-muted mt-0.5 shrink-0" />
                            <div>
                              <div className="text-text">{d.id.split('/').pop()}</div>
                              <div className="text-[11px] text-muted">{i18nT('pages.appsPage.kept')} {d.reason}</div>
                            </div>
                          </div>
                        ))}
                        {(deps.userInstalled || []).map((d: UserInstalledDep) => (
                          <div key={d.id} className="flex items-start gap-2">
                            <Lock size={12} className="text-muted mt-0.5 shrink-0" />
                            <div>
                              <div className="text-text">{d.id.split('/').pop()}</div>
                              <div className="text-[11px] text-muted">{i18nT('pages.appsPage.kept_installed_by_you')}</div>
                            </div>
                          </div>
                        ))}
                      </div>
                    </div>
                  )
                })()
              )}

              <label htmlFor="uninstall-keep-data" className="flex items-center gap-2 text-[13px] text-muted mb-5 cursor-pointer select-none">
                <input id="uninstall-keep-data" type="checkbox" aria-label={i18nT('pages.appsPage.keep_app_data')} checked={keepData} onChange={e => setKeepData(e.target.checked)} className="rounded" />
                {i18nT('pages.appsPage.keep_app_data')}
              </label>

              <div className="flex items-center gap-2 justify-end">
                <Btn onClick={() => { setUninstallTarget(null); setUninstallPreview(null) }}>{i18nT('pages.appsPage.cancel')}</Btn>
                <Btn danger onClick={confirmUninstall} disabled={actionLoading === `${uninstallTarget.name}:uninstall`}>
                  {actionLoading === `${uninstallTarget.name}:uninstall` ? i18nT('pages.appsPage.removing') : i18nT('pages.appsPage.uninstall')}
                </Btn>
              </div>
            </div>
          </div>
        )}

        {/* ---- Discover tab ---- */}
        {tab === 'discover' && (
          loading ? (
            <div className="text-center py-12 text-muted text-sm">{i18nT('pages.appsPage.loading_apps')}</div>
          ) : browseApps.length === 0 ? (
            <EmptyState
              icon={<ShoppingBag size={36} />}
              title={i18nT('pages.appsPage.no_apps_available')}
              subtitle={i18nT('pages.appsPage.add_an_app_source_gear_icon_above_or_install_fro')}
            />
          ) : (
            <>
              {/* One render path, whatever fed it. `featuredSections` already
                  resolved the choice between a published layout and the derived
                  pick (a published layout replaces the derived one entirely:
                  mixing a curator's cards with `featured`-flag picks would show
                  the same app twice and give the curator no way to say "only
                  these"). By here the source is invisible: each block renders
                  the arrangement its FORM names -- `full` runs one card across
                  the width with its art beside the copy; `row` lays its cards
                  side by side, one column on a narrow viewport. */}
              {showEditorial && (
                <div className="flex flex-col gap-3.5 mb-6">
                {featuredSections.map((block, position) => (
                  <div
                    key={`block:${position}`}
                    className={
                      block.form === 'row'
                        ? 'grid grid-cols-1 md:grid-cols-2 gap-3.5 items-start'
                        : ''
                    }
                  >
                  {block.items.map((section, idx) => (
                  <ErrorBoundary
                    /* Keyed by block+item POSITION plus the item's FULL data
                       identity (cardDataKey: members, title, blurb, artwork).
                       The position prefix keeps two content-identical cards from
                       colliding -- the publish gate checks duplicate refs within
                       an item, not across them, and a colliding key lets React
                       reconcile one card against the other's fiber. The
                       cardDataKey suffix gives this boundary the same "any field
                       changed" remount contract as the other three sites, so a
                       corrected payload clears a latched fallback. */
                    key={`${position}:${idx}:${cardDataKey(section)}`}
                    scope={`apps:featured-section:${position}:${idx}:${section.type}`}
                    fallback={
                      <BrowseCardFallback
                        /* A collection is labeled by its theme; an `app` item
                           has no title by design, so its label is the app's
                           own name -- same line the old dedicated fallback
                           cards printed. */
                        label={section.title || section.apps[0]?.displayName || section.apps[0]?.name}
                        message={i18nT('pages.appsPage.this_section_could_not_be_displayed')}
                        className="mb-6"
                      />
                    }
                  >
                    <FeaturedSpotlight
                      type={section.type}
                      apps={section.apps}
                      title={section.title}
                      blurb={section.blurb}
                      artwork={section.artwork}
                      /* Data-driven, not a render branch: a curated placement
                         draws editorial art or nothing (the lead app's own hero
                         may not fill the art band -- see FeaturedSpotlight's
                         `curated`); a derived placement may use the app's own
                         hero, since no curator chose art for it. */
                      curated={block.curated}
                      layout={block.form === 'full' ? 'side' : 'stacked'}
                      /* A row's collections fold their rows into a dialog: three
                         inline install rows per card made the row taller than
                         the lead above it, inverting the hierarchy. */
                      compact={block.form === 'row'}
                      busyName={
                        featuredBusyName(actionLoading, section.apps)
                      }
                      onGet={name => getApp(name)}
                      onEnable={name => enableApp(name)}
                      onOpenApp={(name, e) => openDetail(name, e)}
                    />
                  </ErrorBoundary>
                  ))}
                  </div>
                ))}
                </div>
              )}

              <div className="flex items-baseline justify-between mt-2 mb-3">
                <h3 className="text-[17px] font-semibold text-text-strong">
                  {category === 'All' ? i18nT('pages.appsPage.all_apps') : category}
                </h3>
              </div>
              <div className="grid grid-cols-1 md:grid-cols-[224px_minmax(0,1fr)] gap-6 items-start">
                <div className="md:sticky md:top-2">
                  <CategoryRail
                    categories={categories}
                    total={browseApps.length}
                    selected={category}
                    onSelect={setCategory}
                    sources={sources}
                    onAddSource={() => setSourcesOpen(true)}
                  />
                </div>
                <div className="min-w-0">
                  <div className="flex items-center justify-between mb-3 text-[12.5px] text-muted">
                    <span>{i18nT('pages.appsPage.app', { count: filteredBrowse.length })}</span>
                    {/* A `<label>` cannot wrap this any more: `SimpleSelect`
                        renders a button, and a button takes its accessible name
                        from its own content, not from an enclosing label. The
                        name is on `aria-label` instead. */}
                    <span className="flex items-center gap-1.5">
                      <span>{i18nT('pages.appsPage.sort')}</span>
                      <SimpleSelect
                        options={['name', 'category']}
                        optionLabels={[i18nT('pages.appsPage.name'), i18nT('pages.appsPage.category')]}
                        value={sort}
                        onChange={v => setSort(v as 'name' | 'category')}
                        aria-label={i18nT('pages.appsPage.sort_apps')}
                        style={{ flexShrink: 0 }}
                      />
                    </span>
                  </div>
                  {filteredBrowse.length === 0 ? (
                    <EmptyState icon={<ShoppingBag size={32} />} title={i18nT('pages.appsPage.no_matching_apps')} subtitle={i18nT('pages.appsPage.try_a_different_search_or_category')} />
                  ) : (
                    /* Two rows to a line on a desktop dashboard. A row is a
                       name, a provenance line and one control -- it never needed
                       1100px, and at one per line the list spent a whole screen
                       on four apps. `items-start` is not needed: every row is
                       the same height. */
                    <div className="grid grid-cols-1 lg:grid-cols-2 gap-x-3.5">
                    {filteredBrowse.map(app => (
                      <ErrorBoundary
                        /* Full-data key (cardDataKey): the boundary latches
                           its error state, so ANY corrected registry payload —
                           including a same-version metadata fix — must remount
                           it; a partial key would reuse the errored fiber and
                           leave the placeholder up after the data is fixed. */
                        key={cardDataKey(app)}
                        scope={`apps:app-list-row:${app.name}`}
                        fallback={
                          <BrowseCardFallback
                            label={app.displayName || app.name}
                            message={i18nT('pages.appsPage.this_app_could_not_be_displayed')}
                            className="mb-2"
                          />
                        }
                      >
                        <AppListRow
                          app={app}
                          busy={actionLoading === `${app.name}:enable` || !!updatingAll}
                          onOpen={e => openDetail(app.name, e)}
                          onGet={() => getApp(app.name)}
                          onUpdate={() => updateApp(app.name)}
                          onEnable={() => enableApp(app.name)}
                        />
                      </ErrorBoundary>
                    ))}
                    </div>
                  )}
                </div>
              </div>
            </>
          )
        )}

        {/* ---- Library tab ---- */}
        {tab === 'library' && (
          appsLoading ? (
            <div className="text-center py-12 text-muted text-sm">{i18nT('pages.appsPage.loading_apps')}</div>
          ) : filteredInstalled.length === 0 ? (
            <EmptyState
              icon={<Package size={36} />}
              title={installedApps.length === 0 ? i18nT('pages.appsPage.no_apps_installed_yet') : i18nT('pages.appsPage.no_matching_apps')}
              subtitle={installedApps.length === 0
                ? i18nT('pages.appsPage.find_apps_in_the_discover_tab_or_install_from_a')
                : i18nT('pages.appsPage.try_a_different_search_term')}
            />
          ) : (
            <>
              {updatables.length > 0 && (
                <div className="mb-4 border border-[color-mix(in_srgb,var(--info)_45%,transparent)] bg-bg-elevated rounded-lg p-3 flex items-center gap-3 animate-rise">
                  <ArrowUp size={15} className="text-[var(--info)] shrink-0" />
                  <span className="text-text text-sm flex-1">
                    {i18nT('pages.appsPage.update', { count: updatables.length })} {i18nT('pages.appsPage.available')}
                  </span>
                  <Btn
                    className="!bg-[var(--info)] !text-white hover:!opacity-80"
                    onClick={updateAll}
                    disabled={!!updatingAll}
                  >
                    {updatingAll ? i18nT('pages.appsPage.updating_progress', { done: updatingAll.done, total: updatingAll.total }) : i18nT('pages.appsPage.update_all')}
                  </Btn>
                </div>
              )}
              <div className="space-y-3">
                {filteredInstalled.map(app => (
                  <ErrorBoundary
                    /* Full-data key (cardDataKey): the boundary latches
                       its error state, so remount when the installed app or its
                       update availability changes — e.g. when an updated payload
                       fixes a broken card (#3719). */
                    key={cardDataKey(app)}
                    scope="apps:installed-card"
                    fallback={
                      <div className="border border-border rounded-lg p-4 flex items-center gap-3">
                        <AlertTriangle aria-hidden className="lucide-inline text-[var(--warn)] shrink-0" />
                        <div className="min-w-0 text-sm flex-1">
                          <span className="font-medium text-text">{app.manifest?.displayName || app.name}</span>
                          <span className="text-muted ml-2">{i18nT('pages.appsPage.this_app_could_not_be_displayed')}</span>
                        </div>
                        {/* The crashed card removed the app's management surface, so the
                            fallback must keep one recovery path: quiet a broken enabled
                            app, or remove a disabled one entirely (locked apps cannot
                            be uninstalled). Same handlers as the healthy card. */}
                        <div className="shrink-0">
                          {app.enabled ? (
                            <Btn
                              onClick={() => handleAction(app.name, 'disable')}
                              disabled={actionLoading === `${app.name}:disable`}
                            >
                              <PowerOff size={14} /> {i18nT('components.appstore.installedAppCard.disable')}
                            </Btn>
                          ) : app.lifecycle !== 'locked' && (
                            <Btn danger onClick={() => handleAction(app.name, 'uninstall')}>
                              <Trash2 size={14} /> {i18nT('components.appstore.installedAppCard.uninstall')}
                            </Btn>
                          )}
                        </div>
                      </div>
                    }
                  >
                    <InstalledAppCard
                      app={app}
                      actionLoading={updatingAll ? `${app.name}:update` : actionLoading}
                      onAction={handleAction}
                      onOpen={() => navigate(appNavTarget(app)?.route || `/apps/${app.name}`)}
                      onDetail={() => openDetail(app.name)}
                    />
                  </ErrorBoundary>
                ))}
              </div>
            </>
          )
        )}
        </div>
      </div>
    </>
  )
}
