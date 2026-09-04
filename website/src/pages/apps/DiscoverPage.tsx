/**
 * DiscoverPage — the App Store storefront (`/apps`), one half of the PR1
 * Discover/Library split.
 *
 * Featured editorial blocks (published layout when the catalog carries one,
 * otherwise the same block shape synthesized from the ``featured`` flag — one
 * render path either way), then an "All apps" section with a category rail
 * (canonical categories + registry sources with counts) and a sortable dense
 * list. The editorial layer shows only for the unfiltered view.
 *
 * Supply-side controls (external registries, Install from Path) live behind
 * the Sources gear in the header (SourcesPopover).
 *
 * All data identity comes from `useAppsData` — the single contract shared with
 * LibraryPage, so the two pages cannot drift. Only view-local state (search
 * query, category pick, sort, action loading) lives here.
 */
import { useEffect, useMemo, useRef, useState } from 'react'
import { useQueryClient } from '@tanstack/react-query'
import { Link, Navigate, useLocation, useNavigate } from 'react-router-dom'
import { AlertTriangle, CheckCircle2, RefreshCw, ShoppingBag, X } from 'lucide-react'
import { EmptyState, IconButton, PageHeader, SearchInput } from '../../components/ui'
import SimpleSelect from '../../components/SimpleSelect'
import { Tabs, TabsContent, TabsCount, TabsList, TabsTrigger } from '../../components/ui/tabs'
import { TABS_RAIL_ROW_CLASS } from '../../components/ui/tabsPill'
import FeaturedSpotlight from '../../components/appstore/FeaturedSpotlight'
import CategoryRail from '../../components/appstore/CategoryRail'
import AppListRow from '../../components/appstore/AppListRow'
import TrustAppModal, { isTrustDeniedError } from '../../components/appstore/TrustAppModal'
import SourcesPopover from '../../components/appstore/SourcesPopover'
import { categoryFor, type Category } from '../../components/appstore/categories'
import type { RegistryApp } from '../../components/appstore/types'
import { api } from '../../api/client'
import { i18nT } from '../../i18n/t'
import { compareText } from '../../i18n/format'
import ErrorNotice from '../../components/ErrorNotice'
import ErrorBoundary from '../../components/ErrorBoundary'
import useAppsData from './useAppsData'
import { useAppActions } from './useAppActions'
import { useAppUpdates } from './useAppUpdates'
import UpdatesList from './UpdatesList'
import { cardDataKey } from './cardDataKey'

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
 * Compact degraded placeholder for a Discover card whose render threw
 * (#3702). Mirrors the Library-card boundary fallback (#3689): the broken
 * card degrades in place while its siblings and the page chrome keep
 * rendering. Discover cards describe registry entries rather than installed
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

/**
 * Legacy tab migration wrapper — the default `/apps` mount.
 *
 * The pre-split AppsPage persisted its active tab in
 * `sessionStorage['appstore-tab']` (values: discover/library/installed/browse,
 * see the retired `initialTab()`). A stored library/installed value means the
 * user's last view was the Library, so redirect there once via a declarative
 * `<Navigate replace>`.
 *
 * The read is SYNCHRONOUS during render (not in a passive effect) so a
 * Library-bound visit never paints a Discover frame — the same read-side
 * fallback pattern as other legacy query→path migrations. The key is cleared
 * in an idempotent effect in ALL cases (library, discover, or garbage) so the
 * redirect can never fire twice and the legacy key dies here.
 *
 * The redirect only applies to the bare `/apps` mount: a `/apps/-/updates`
 * deep-link is an explicit destination, and a stale legacy key must not
 * hijack it. (The key is still cleared either way.)
 */
export default function DiscoverPage() {
  const { pathname } = useLocation()
  const stored = sessionStorage.getItem('appstore-tab')
  const legacyLibrary = stored === 'library' || stored === 'installed'
  useEffect(() => { sessionStorage.removeItem('appstore-tab') }, [])
  if (legacyLibrary && pathname !== UPDATES_PATH) return <Navigate to="/apps/library" replace />
  return <DiscoverPageBody />
}

/** The Updates sub-tab's canonical URL. The `-/` prefix can never collide with
 *  an installed app: `-` is not a valid app name, so unlike `library` it needs
 *  no server-side reservation (see the route comment in App.tsx). */
const UPDATES_PATH = '/apps/-/updates'

type DiscoverTab = 'featured' | 'updates'

/** URL → sub-tab. Anything that is not the Updates path is Featured — `/apps`
 *  is the default mount and stays the canonical Featured URL. */
function tabForPath(pathname: string): DiscoverTab {
  return pathname === UPDATES_PATH ? 'updates' : 'featured'
}

function DiscoverPageBody() {
  const location = useLocation()
  const navigate = useNavigate()
  const [query, setQuery] = useState('')
  const [category, setCategory] = useState<Category | 'all'>('all')
  const [sort, setSort] = useState<'name' | 'category'>('name')
  const [sourcesOpen, setSourcesOpen] = useState(false)
  const [successMsg, setSuccessMsg] = useState('')
  /* Whether the success notice carries the Library link. Only the
     install-from-path flow sets it — that notice hands off to another page
     (enable lives in Library) and persists until acted on; an update success
     resolves HERE, so it links nowhere and auto-dismisses instead. */
  const [successLink, setSuccessLink] = useState(false)
  const [actionLoading, setActionLoading] = useState<string | null>(null)

  /* Sub-tab state (V1 mockup: Featured | Updates). Component-internal state
     initialized SYNCHRONOUSLY from the URL so a `/apps/-/updates` deep-link or
     refresh renders the Updates tab on the first frame (no Featured flash —
     the same read-side rule as the legacy-key migration above). The effect
     re-syncs on history navigation (back/forward), where the path changes
     without going through `switchTab`. */
  const [tab, setTab] = useState<DiscoverTab>(() => tabForPath(location.pathname))
  useEffect(() => { setTab(tabForPath(location.pathname)) }, [location.pathname])
  const switchTab = (next: DiscoverTab) => {
    setTab(next)
    // Push (not replace): the tabs are navigation between two addressable
    // screens, so Back must return to the one the user came from.
    navigate(next === 'updates' ? UPDATES_PATH : '/apps')
  }

  const {
    apps, appsError, registryError, loading,
    browseApps, featuredSections, categories, sources, updatables,
    announceAppsChanged,
  } = useAppsData()

  const {
    setError, displayError, dismissError,
    openDetail, getApp, updateApp, trustTarget, runEnable, trust,
  } = useAppActions({ apps, browseApps, appsError, registryError, announceAppsChanged })

  // Transient success surface for the shared update hook: the hook reports
  // outcomes, this page owns display + auto-dismiss (the Library's 4s).
  const showSuccess = (msg: string) => {
    setSuccessMsg(msg)
    setSuccessLink(false)
    setTimeout(() => setSuccessMsg(''), 4000)
  }

  // Per-row update and Update All for the Updates sub-tab — the SAME
  // useAppUpdates instance shape the Library runs, so the two surfaces cannot
  // drift on pending state and the sequential batch. `rowUpdatesInPlace`
  // keeps every row on this worklist updating where it stands: the header
  // and Update All promise in-place updating, so a row's button must not
  // navigate away mid-triage (the hook's comment carries the full contract).
  const { updatingAll, updatePending, runUpdate, updateAll } = useAppUpdates({
    apps, updatables, announceAppsChanged, updateApp,
    setError, setSuccess: showSuccess,
    // A lapsed execution trust on an in-place update is a consent prompt:
    // reuse the enable path's modal, but hand the gate the UPDATE retry —
    // its default retry is the enable action, which would start the app
    // instead of updating it after the user consents.
    onTrustDenied: (name, retryUpdate) => trust.open(trustTarget(name), retryUpdate),
    rowUpdatesInPlace: true,
  })

  // Manual store refresh. This exists because the two cache layers degrade
  // silently -- a failed catalog fetch leaves the store on the seed listing
  // for up to an hour with nothing the user can do about it from the UI.
  // Order matters: the POSTs drop the server's document caches (official
  // documents AND the user's external registries -- both sources the store
  // renders), then the invalidations rebuild both lists past their staleTime
  // (same mechanism as TrustAppModal / RegistryManager; awaiting them holds
  // the spinner until the refetches settle). allSettled, because one source
  // being unreachable must not stop the refetch from repairing the other.
  //
  // The settled results are READ, not discarded: a failed refresh keeps
  // serving the prior (stale or seed) listing, which looks identical to a
  // successful one, so the outcome must reach the error banner or the user
  // cannot tell them apart. TWO outcomes are observable here and the code
  // claims only those: a REJECTED POST reports its own message, and a
  // fulfilled registries response reporting per-source failures names them
  // (the same branch RegistryManager runs on this response shape).
  //
  // A degraded OFFICIAL catalog is deliberately NOT claimed: the store POST
  // only drops caches and never fetches (see handle_registry_refresh), so it
  // resolves ok whatever the catalog's health, and the fetch it defers to
  // serves the seed listing with a 200. Reporting that needs a signal on the
  // follow-up GET, which is a server change and not this one.
  //
  // Reporting never skips the invalidations below -- the healthy source still
  // gets its refetch.
  const queryClient = useQueryClient()
  const [refreshing, setRefreshing] = useState(false)
  // The last banner text THIS handler wrote, so a later success clears its own
  // stale error without erasing an unrelated one. `setError` is shared page
  // state (useAppActions): SourcesPopover, runEnable and useAppUpdates all
  // write it, and Update All's failure notice has no auto-dismiss -- so an
  // unconditional clear here would silently drop the only report of a failed
  // update the moment the user clicked the adjacent refresh.
  const refreshErrorRef = useRef('')
  const reportRefreshOutcome = (message: string) => {
    const previous = refreshErrorRef.current
    refreshErrorRef.current = message
    if (message) setError(message)
    // Clear only while the banner still shows what this handler put there.
    else setError(prev => (prev === previous ? '' : prev))
  }
  const handleRefresh = async () => {
    if (refreshing) return
    setRefreshing(true)
    try {
      const [storeResult, registriesResult] = await Promise.allSettled([
        api.refreshAppStore(),
        api.refreshRegistries(),
      ])
      const rejection = [storeResult, registriesResult].find(
        (r): r is PromiseRejectedResult => r.status === 'rejected',
      )
      if (rejection) {
        reportRefreshOutcome(rejection.reason instanceof Error && rejection.reason.message
          ? rejection.reason.message
          : i18nT('components.registryManager.failed_to_refresh_registries'))
      } else if (
        registriesResult.status === 'fulfilled'
        && registriesResult.value.ok === false
        && registriesResult.value.failed && registriesResult.value.failed.length > 0
      ) {
        reportRefreshOutcome(
          i18nT('components.registryManager.could_not_refresh_still_showing_last_synced',
            { names: registriesResult.value.failed.join(', ') }))
      } else {
        reportRefreshOutcome('')
      }
      await Promise.all([
        queryClient.invalidateQueries({ queryKey: ['registry'] }),
        queryClient.invalidateQueries({ queryKey: ['apps'] }),
      ])
    } finally {
      setRefreshing(false)
    }
  }

  const filteredBrowse = useMemo(() => {
    const q = query.trim().toLowerCase()
    const list = browseApps.filter(a => {
      if (category !== 'all' && categoryFor(a.tags) !== category) return false
      if (!q) return true
      return a.displayName.toLowerCase().includes(q)
        || a.description.toLowerCase().includes(q)
        || (a.tags || []).some(t => t.toLowerCase().includes(q))
    })
    return list.sort((a, b) => sort === 'category'
      ? compareText(categoryFor(a.tags), categoryFor(b.tags)) || compareText(a.displayName, b.displayName)
      : compareText(a.displayName, b.displayName))
  }, [browseApps, category, query, sort])

  /* The editorial layer survives a CATEGORY pick -- curated placements are
     content, not list rows, so the rail only filters the All-apps list below.
     A SEARCH still hides it: a typed query is a stated intent to find one
     thing, and the spotlight would push the results below the fold. */
  const showEditorial = !query.trim() && featuredSections.length > 0

  // ---- Actions --------------------------------------------------------------
  // Detail navigation, install/update routing, the trust-consent target, and
  // the single enable path come from useAppActions — shared with LibraryPage
  // so the two pages cannot drift on how an action behaves. Get / Update on
  // this page NAVIGATE and never call an install endpoint themselves
  // (FeaturedSpotlight, the Browse cards and AppListRow all route their
  // `onGet` there), so the registry-install trust refusal — which the gateway
  // raises before cloning — surfaces on the detail page, where
  // `handleInstall` owns the consent modal.

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

  return (
    <>
      {/* Standard page header with a right-side actions slot: search and the
          Sources gear (page-layout-pattern). No tab switch any more — Library
          is its own page at /apps/library. */}
      <PageHeader
        title={i18nT('pages.discoverPage.title')}
        subtitle={i18nT('pages.discoverPage.subtitle')}
        actions={<>
          {/* Search filters the Featured browse grid only, so the field hides
              on the Updates worklist — a live input that visibly does nothing
              there reads as broken. */}
          {tab === 'featured' && (
            <SearchInput
              placeholder={i18nT('pages.appsPage.search_apps')}
              value={query}
              onChange={(e: React.ChangeEvent<HTMLInputElement>) => setQuery(e.target.value)}
              className="w-[220px]"
              aria-label={i18nT('pages.appsPage.search_apps')}
            />
          )}
          {/* Manual store refresh — always visible (unlike search): the
              degraded-catalog state it repairs also starves the Updates
              worklist, whose update map derives from the same registry rows. */}
          <IconButton
            aria-label={i18nT('pages.appsPage.refresh_store')}
            title={i18nT('pages.appsPage.refresh_store')}
            onClick={handleRefresh}
            disabled={refreshing}
          >
            <RefreshCw size={15} className={refreshing ? 'animate-spin' : undefined} />
          </IconButton>
          <SourcesPopover
            open={sourcesOpen}
            onOpenChange={setSourcesOpen}
            onError={setError}
            onInstalled={(name) => {
              // A path-installed app lands DISABLED, so it never shows in the
              // sidebar. Library is its own page now, so instead of switching a
              // tab we confirm here and point at it — with a direct action,
              // because the required next step (enable) lives on another page.
              // No auto-dismiss: the notice carries a pending task, so it stays
              // until the user acts on it or dismisses it.
              setSuccessMsg(i18nT('pages.appsPage.installed_app_find_in_library_and_enable', { name }))
              setSuccessLink(true)
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
        {/* The Tabs root spans the rail and both panels, so they are one
            tablist. The notices and the trust modal sitting between them are
            ordinary children, not panel content. */}
        <Tabs
          value={tab}
          onValueChange={v => switchTab(v as DiscoverTab)}
          layoutId="discover-subtab"
        >
        {/* Sub-tabs: Featured is the storefront, Updates the pending-updates
            worklist. Radix carries the WAI-ARIA tabs contract (roving tabindex,
            arrow keys, aria-selected, and the trigger ⇄ panel linkage);
            TabsCount hides the count at zero, so no badge noise when
            everything is current. */}
        <div className={TABS_RAIL_ROW_CLASS}>
          <TabsList aria-label={i18nT('pages.discoverPage.title')}>
            <TabsTrigger value="featured">
              <span>{i18nT('pages.discoverPage.tab_featured')}</span>
            </TabsTrigger>
            <TabsTrigger
              value="updates"
              title={updatables.length > 0
                ? i18nT('pages.discoverPage.updates_badge_label', { count: updatables.length })
                : undefined}
            >
              <span>{i18nT('pages.discoverPage.tab_updates')}</span>
              <TabsCount value={updatables.length} />
            </TabsTrigger>
          </TabsList>
        </div>
        {/* Notifications. No hand-off on the error notice: the SourcesPopover's
            install-path input shares this page — navigating away would discard
            what the user typed. */}
        {displayError && (
          <ErrorNotice
            message={displayError}
            onDismiss={dismissError}
            className="mb-4 animate-rise"
          />
        )}
        {successMsg && (
          <div className="mb-4 bg-bg-elevated border rounded-lg p-3 flex items-center gap-3 animate-rise" style={{ borderColor: 'color-mix(in srgb, var(--ok) 45%, transparent)' }}>
            <span className="text-text text-sm flex-1">{successMsg}</span>
            {/* Set only by the install-from-path notice: that flow finishes on
                another page, so the notice carries the navigation instead of
                asking the user to hunt. Update successes resolve here and
                carry no link. */}
            {successLink && (
              <Link to="/apps/library" className="text-accent text-sm font-medium hover:underline shrink-0">
                {i18nT('nav.library')}
              </Link>
            )}
            <button aria-label={i18nT('pages.appsPage.dismiss_message')} className="text-muted hover:text-text text-sm" onClick={() => { setSuccessMsg(''); setSuccessLink(false) }}><X className="lucide-inline" /></button>
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

        <TabsContent value="updates">{
          /* Updates sub-page: the pending-updates worklist. This panel owns
             the tab's frame (loading, the all-current empty state); the row
             list and its Update All header render through UpdatesList, driven
             by the shared useAppUpdates instance above. */
          loading ? (
            <div className="text-center py-12 text-muted text-sm">{i18nT('pages.appsPage.loading_apps')}</div>
          ) : updatables.length === 0 ? (
            /* An empty list is only an all-clear when the data actually
               loaded: on a failed registry/apps fetch the count is UNKNOWN —
               a success checkmark there would visually assert "up to date"
               over a dismissed error notice, so the failed state wears the
               warning glyph and offers a refetch instead of an exit. */
            registryError || appsError ? (
              <EmptyState
                icon={<AlertTriangle size={36} />}
                title={i18nT('pages.discoverPage.updates_check_failed')}
                action={
                  <button
                    type="button"
                    className="text-accent text-sm font-medium hover:underline bg-transparent border-none cursor-pointer"
                    onClick={announceAppsChanged}
                  >
                    {i18nT('pages.discoverPage.updates_retry')}
                  </button>
                }
              />
            ) : (
            <EmptyState
              icon={<CheckCircle2 size={36} />}
              title={i18nT('pages.discoverPage.updates_empty')}
              action={
                <button
                  type="button"
                  className="text-accent text-sm font-medium hover:underline bg-transparent border-none cursor-pointer"
                  onClick={() => switchTab('featured')}
                >
                  {i18nT('pages.discoverPage.updates_empty_back_to_featured')}
                </button>
              }
            />
            )
          ) : (
            <UpdatesList
              rows={updatables}
              updatingAll={updatingAll}
              updatePending={updatePending}
              onUpdate={runUpdate}
              onUpdateAll={updateAll}
            />
          )
        }</TabsContent>
        <TabsContent value="featured">{
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
                {category === 'all' ? i18nT('pages.appsPage.all_apps') : category}
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
                        /* Update All lives on the Library page, so the old
                           `|| !!updatingAll` freeze no longer applies here. */
                        busy={actionLoading === `${app.name}:enable`}
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
        )}</TabsContent>
        </Tabs>
        </div>
      </div>
    </>
  )
}
