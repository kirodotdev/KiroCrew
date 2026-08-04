/**
 * AppsPage — the Apps page, per the locked hybrid design (editorial front,
 * marketplace engine).
 *
 * Discover (landing tab): featured spotlight + two secondary feature cards
 * (editorial layer, curator-driven via the registry-index ``featured`` flag
 * with a deterministic fallback), then an "All apps" section with a category
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
import { useQuery, useQueryClient } from '@tanstack/react-query'
import { useNavigate } from 'react-router-dom'
import {
  Package, Bot, Zap, Clock, ShoppingBag, Lock, Trash2, X, ArrowUp, Boxes,
} from 'lucide-react'
import { api } from '../api/client'
import { Btn, EmptyState, PageHeader, SearchInput } from '../components/ui'
import SimpleSelect from '../components/SimpleSelect'
import { recordEvent } from '../rum'
import SegmentedControl from '../components/SegmentedControl'
import FeaturedSpotlight from '../components/appstore/FeaturedSpotlight'
import FeatureCard from '../components/appstore/FeatureCard'
import CategoryRail, { type SourceRow } from '../components/appstore/CategoryRail'
import AppListRow from '../components/appstore/AppListRow'
import InstalledAppCard from '../components/appstore/InstalledAppCard'
import TrustAppModal, { isTrustDeniedError, useTrustGate, type TrustAppTarget } from '../components/appstore/TrustAppModal'
import SourcesPopover from '../components/appstore/SourcesPopover'
import { categoryFor, categoryCounts, type Category } from '../components/appstore/categories'
import { hasHeroArt } from '../components/appstore/useHeroArt'
import { isVerified, normalizeRegistryApp, type InstalledApp, type RegistryApp } from '../components/appstore/types'

import { i18nT } from '../i18n/t'
import ErrorNotice from '../components/ErrorNotice'
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
export function pickFeatured(apps: RegistryApp[]): RegistryApp[] {
  const rank = (f: RegistryApp['featured']) => (typeof f === 'number' ? f : 1e9)
  const flagged = apps
    .filter(a => !a._registry && a.featured !== undefined && a.featured !== false)
    .sort((a, b) => rank(a.featured) - rank(b.featured) || a.displayName.localeCompare(b.displayName))
  const rest = apps
    .filter(a => !flagged.includes(a))
    .sort((a, b) =>
      (Number(hasHeroArt(b)) - Number(hasHeroArt(a)))
      || (Number(isVerified(b)) - Number(isVerified(a)))
      || a.displayName.localeCompare(b.displayName))
  return [...flagged, ...rest].slice(0, 3)
}

export default function AppsPage() {
  const navigate = useNavigate()
  const queryClient = useQueryClient()
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

  const { data: apps = [], isLoading: appsLoading, error: appsError } = useQuery<InstalledApp[]>({
    queryKey: ['apps'],
    queryFn: () => api.listApps(),
  })

  const { data: registryData, isLoading: registryLoading, error: registryError } = useQuery<{ apps: RegistryApp[] }>({
    queryKey: ['registry'],
    // api.listRegistry() types `apps` as unknown[]; the backend payload matches
    // RegistryApp, so narrow it here at the single fetch boundary.
    queryFn: async () => {
      const res = await api.listRegistry()
      // Normalize at the single fetch boundary: registry.py yields minimal
      // rows when an app.json fetch fails, and external registries are
      // user-supplied JSON, so display fields may be missing or mistyped.
      return { apps: (res.apps as RegistryApp[]).map(normalizeRegistryApp) }
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

  // Browse catalog: all non-hidden builtins (each carrying its live enabled
  // state, so Discover shows Enabled/Disabled rather than dropping enabled
  // ones) merged with registry entries; installed apps enrich matching
  // registry entries with local hero/screenshot metadata.
  const browseApps: RegistryApp[] = useMemo(() => {
    const builtinEntries: RegistryApp[] = apps
      .filter(a => a.origin === 'builtin' && !a.manifest?.hidden)
      .map(a => ({
        name: a.name,
        displayName: a.displayName || a.name,
        description: a.manifest?.description || '',
        version: a.version,
        author: a.manifest?.author || 'kirocrew',
        tags: a.manifest?.tags,
        screenshots: a.manifest?.screenshots,
        heroImage: a.manifest?.heroImage,
        heroImageDark: a.manifest?.heroImageDark,
        // Forwarded too: a builtin has no `registryEntry` (the core
        // `app-registry.json` is empty), so anything omitted here is simply
        // absent from the Discover catalog for every built-in app. The detail
        // page reads these off the installed manifest and so happened to keep
        // working, which is why the omission stayed invisible.
        heroImageDetail: a.manifest?.heroImageDetail,
        heroImageDetailDark: a.manifest?.heroImageDetailDark,
        highlights: a.manifest?.highlights,
        license: a.manifest?.license,
        icon: a.manifest?.ui?.pages?.[0]?.icon || '',
        iconUrl: a.manifest?.iconUrl || '',
        installed: true,
        enabled: a.enabled,
        origin: 'builtin',
        lifecycle: 'locked',
      }))
    const builtinNames = new Set(builtinEntries.map(a => a.name))
    const enriched = registry.filter(r => !builtinNames.has(r.name)).map(r => {
      const installed = apps.find(a => a.name === r.name)
      return installed
        ? { ...r, heroImage: r.heroImage || installed.manifest?.heroImage, heroImageDark: r.heroImageDark || installed.manifest?.heroImageDark, screenshots: r.screenshots || installed.manifest?.screenshots }
        : r
    })
    return [...builtinEntries, ...enriched]
  }, [apps, registry])

  const featured = useMemo(() => pickFeatured(browseApps), [browseApps])
  const [spotlight, ...secondary] = featured

  const categories = useMemo(() => categoryCounts(browseApps), [browseApps])

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

  const showEditorial = category === 'All' && !query.trim() && featured.length > 0

  // ---- Library data --------------------------------------------------------

  const updateMap = useMemo(
    () => new Map(registry.filter(r => r.updateAvailable).map(r => [r.name, r.version])),
    [registry],
  )
  const installedApps = useMemo(
    () => apps.filter(a => !(a.origin === 'builtin' && !a.enabled)),
    [apps],
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

  const invalidate = () => {
    queryClient.invalidateQueries({ queryKey: ['apps'] })
    queryClient.invalidateQueries({ queryKey: ['registry'] })
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

  // Provenance the consent modal shows. The browse-catalog row is preferred:
  // registry rows carry `repo`/`_registry`, which the installed record does not.
  const trustTarget = (name: string): TrustAppTarget => {
    const row = browseApps.find(a => a.name === name)
    if (row) return { name: row.name, displayName: row.displayName, repo: row.repo, origin: row.origin, _registry: row._registry }
    const installed = apps.find(a => a.name === name)
    return { name, displayName: installed?.displayName, repo: installed?.manifest?.repo, origin: installed?.origin }
  }

  /** The single enable path — shared by Discover, Library, and the trust retry. */
  const runEnable = async (name: string) => {
    await api.enableApp(name)
    recordEvent('app_enable', { app: name })
    invalidate()
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
    // Update navigates to detail page (streaming install UI). Blocked while
    // Update All is running so the same update can't run twice concurrently.
    if (action === 'update') {
      if (updatingAll) return
      updateApp(name)
      return
    }
    setActionLoading(`${name}:${action}`)
    setError('')
    try {
      if (action === 'enable') await runEnable(name)
      else if (action === 'disable') await api.disableApp(name)
      invalidate()
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
      invalidate()
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
    invalidate()
    if (failed.length) setError(i18nT('pages.appsPage.failed_to_update', { names: failed.join(', ') }))
    else {
      setSuccessMsg(`Updated ${targets.length} app${targets.length === 1 ? '' : 's'}.`)
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
          <SourcesPopover open={sourcesOpen} onOpenChange={setSourcesOpen} onError={setError} />
        </>}
      />

      <div className="px-6 pb-8 overflow-y-auto flex-1 min-h-0">
        {/* Notifications */}
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
                {(uninstallTarget.manifest?.agents?.length || 0) > 0 && (
                  <div className="flex items-center gap-2"><Bot size={12} className="text-muted" /> {i18nT('pages.appsPage.agent', { count: uninstallTarget.manifest.agents!.length })}</div>
                )}
                {(uninstallTarget.manifest?.skills?.length || 0) > 0 && (
                  <div className="flex items-center gap-2"><Zap size={12} className="text-muted" /> {i18nT('pages.appsPage.skill', { count: uninstallTarget.manifest.skills!.length })}</div>
                )}
                {(uninstallTarget.manifest?.crons?.length || 0) > 0 && (
                  <div className="flex items-center gap-2"><Clock size={12} className="text-muted" /> {i18nT('pages.appsPage.cron_job', { count: uninstallTarget.manifest.crons!.length })}</div>
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
              {showEditorial && spotlight && (
                <>
                  <FeaturedSpotlight
                    app={spotlight}
                    busy={actionLoading === `${spotlight.name}:enable`}
                    onOpen={e => openDetail(spotlight.name, e)}
                    onGet={() => getApp(spotlight.name)}
                    onEnable={() => enableApp(spotlight.name)}
                  />
                  {secondary.length > 0 && (
                    <div className="grid grid-cols-1 md:grid-cols-2 gap-3.5 mb-6">
                      {secondary.map(app => (
                        <FeatureCard
                          key={app.name}
                          app={app}
                          busy={actionLoading === `${app.name}:enable`}
                          onOpen={e => openDetail(app.name, e)}
                          onGet={() => getApp(app.name)}
                          onEnable={() => enableApp(app.name)}
                        />
                      ))}
                    </div>
                  )}
                </>
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
                    filteredBrowse.map(app => (
                      <AppListRow
                        key={app.name}
                        app={app}
                        busy={actionLoading === `${app.name}:enable` || !!updatingAll}
                        onOpen={e => openDetail(app.name, e)}
                        onGet={() => getApp(app.name)}
                        onUpdate={() => updateApp(app.name)}
                        onEnable={() => enableApp(app.name)}
                      />
                    ))
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
                  <InstalledAppCard
                    key={app.name}
                    app={{ ...app, updateAvailable: updateMap.has(app.name), _newVersion: updateMap.get(app.name) }}
                    actionLoading={updatingAll ? `${app.name}:update` : actionLoading}
                    onAction={handleAction}
                    onOpen={() => navigate(app.manifest?.ui?.pages?.[0]?.route || `/apps/${app.name}`)}
                    onDetail={() => openDetail(app.name)}
                  />
                ))}
              </div>
            </>
          )
        )}
      </div>
    </>
  )
}
