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
import { useEffect, useMemo, useState } from 'react'
import { Link, Navigate } from 'react-router-dom'
import { AlertTriangle, ShoppingBag, X } from 'lucide-react'
import { EmptyState, PageHeader, SearchInput } from '../../components/ui'
import SimpleSelect from '../../components/SimpleSelect'
import FeaturedSpotlight from '../../components/appstore/FeaturedSpotlight'
import CategoryRail from '../../components/appstore/CategoryRail'
import AppListRow from '../../components/appstore/AppListRow'
import TrustAppModal, { isTrustDeniedError } from '../../components/appstore/TrustAppModal'
import SourcesPopover from '../../components/appstore/SourcesPopover'
import { categoryFor, type Category } from '../../components/appstore/categories'
import type { RegistryApp } from '../../components/appstore/types'
import { i18nT } from '../../i18n/t'
import { compareText } from '../../i18n/format'
import ErrorNotice from '../../components/ErrorNotice'
import ErrorBoundary from '../../components/ErrorBoundary'
import useAppsData from './useAppsData'
import { useAppActions } from './useAppActions'
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
 */
export default function DiscoverPage() {
  const stored = sessionStorage.getItem('appstore-tab')
  const legacyLibrary = stored === 'library' || stored === 'installed'
  useEffect(() => { sessionStorage.removeItem('appstore-tab') }, [])
  if (legacyLibrary) return <Navigate to="/apps/library" replace />
  return <DiscoverPageBody />
}

function DiscoverPageBody() {
  const [query, setQuery] = useState('')
  const [category, setCategory] = useState<Category | 'all'>('all')
  const [sort, setSort] = useState<'name' | 'category'>('name')
  const [sourcesOpen, setSourcesOpen] = useState(false)
  const [successMsg, setSuccessMsg] = useState('')
  const [actionLoading, setActionLoading] = useState<string | null>(null)

  const {
    apps, appsError, registryError, loading,
    browseApps, featuredSections, categories, sources,
    announceAppsChanged,
  } = useAppsData()

  const {
    setError, displayError, dismissError,
    openDetail, getApp, updateApp, trustTarget, runEnable, trust,
  } = useAppActions({ apps, browseApps, appsError, registryError, announceAppsChanged })

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
          <SearchInput
            placeholder={i18nT('pages.appsPage.search_apps')}
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
              // sidebar. Library is its own page now, so instead of switching a
              // tab we confirm here and point at it — with a direct action,
              // because the required next step (enable) lives on another page.
              // No auto-dismiss: the notice carries a pending task, so it stays
              // until the user acts on it or dismisses it.
              setSuccessMsg(i18nT('pages.appsPage.installed_app_find_in_library_and_enable', { name }))
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
            onDismiss={dismissError}
            className="mb-4 animate-rise"
          />
        )}
        {successMsg && (
          <div className="mb-4 bg-bg-elevated border rounded-lg p-3 flex items-center gap-3 animate-rise" style={{ borderColor: 'color-mix(in srgb, var(--ok) 45%, transparent)' }}>
            <span className="text-text text-sm flex-1">{successMsg}</span>
            {/* The install flow now finishes on another page, so the notice
                carries the navigation instead of asking the user to hunt. */}
            <Link to="/apps/library" className="text-accent text-sm font-medium hover:underline shrink-0">
              {i18nT('nav.library')}
            </Link>
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

        {loading ? (
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
        )}
        </div>
      </div>
    </>
  )
}
