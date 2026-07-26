/**
 * AppsPage — App Store with Browse and Installed tabs.
 *
 * Browse: shows available apps from registry with search/filter.
 * Installed: manage installed apps (enable, disable, uninstall).
 * Install: install from local path via input field.
 */
import { useEffect, useState, useRef } from 'react'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import { useNavigate } from 'react-router-dom'
import {
  Package, Power, PowerOff, Trash2, RefreshCw, FolderOpen,
  Download, Bot, Tag, Users, Zap, ChevronRight,
  ExternalLink, Clock, ShoppingBag, Lock, X, ArrowUp,
} from 'lucide-react'
import { api } from '../api/client'
import {
  PageHeader, Card, CardTitle, Badge, Btn, StatCard,
  SearchInput, EmptyState, Input,
} from '../components/ui'
import InfoTip from '../components/InfoTip'
import { recordEvent } from '../rum'
import SegmentedControl from '../components/SegmentedControl'
import AppIcon from '../components/AppIcon'
import RegistryManager from '../components/RegistryManager'
import { useTheme } from '../hooks/useTheme'

type InstalledApp = {
  name: string
  version: string
  displayName: string
  enabled: boolean
  installedAt: string
  source?: string
  // Three-axis classification (replaces old managed field)
  origin?: string     // "builtin" | "registry" | "local" | "external"
  resources?: string  // "gateway" | "app"
  lifecycle?: string  // "gateway" | "app" | "locked"
  // Migration fields
  migratedTo?: string  // "registry:{name}" or "standalone:{name}"
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
    // Store-listing metadata (also present on RegistryApp; optional here)
    screenshots?: string[]
    heroImage?: string
    heroImageDark?: string
    iconUrl?: string
    openCommand?: string
  }
}

/** Uninstall preview payload (mirrors ``api.uninstallPreview`` return shape). */
type UninstallPreview = Awaited<ReturnType<typeof api.uninstallPreview>>
type RemovableDep = UninstallPreview['dependencies']['removable'][number]
type SharedDep = UninstallPreview['dependencies']['shared'][number]
type UserInstalledDep = UninstallPreview['dependencies']['userInstalled'][number]

/**
 * Registry app entry — mirrors backend ``app-registry.json`` schema
 * enriched with install status by ``registry.py:_enrich_with_install_status()``.
 */
type RegistryApp = {
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
  repo?: string
  branch?: string
  installed: boolean
  installedVersion?: string
  enabled?: boolean
  updateAvailable?: boolean
  origin?: string     // "builtin" | "registry" | "local" | "external"
  resources?: string  // "gateway" | "app"
  lifecycle?: string  // "gateway" | "app" | "locked"
  platform?: { os?: string[]; installMode?: string; clientInstall?: { shell?: string; postInstall?: string } }
}

export default function AppsPage() {
  const navigate = useNavigate()
  const queryClient = useQueryClient()
  const [filter, setFilter] = useState('')
  const [error, setError] = useState('')
  const [actionLoading, setActionLoading] = useState<string | null>(null)
  const [tab, setTab] = useState<'installed' | 'browse'>((sessionStorage.getItem('appstore-tab') as 'installed' | 'browse') || 'installed')
  useEffect(() => { sessionStorage.setItem('appstore-tab', tab) }, [tab])
  const tabInitializedRef = useRef(false)
  const { theme: resolvedMode } = useTheme()
  const [installPath, setInstallPath] = useState('')
  const [showInstall, setShowInstall] = useState(false)
  const [successMsg, setSuccessMsg] = useState('')
  const [dismissedQueryError, setDismissedQueryError] = useState(false)
  const [dismissedRegistryError, setDismissedRegistryError] = useState(false)

  // Registry state
  const [registryFilter, setRegistryFilter] = useState('')

  // Uninstall confirmation state
  const [uninstallTarget, setUninstallTarget] = useState<InstalledApp | null>(null)
  const [keepData, setKeepData] = useState(false)
  const [uninstallPreview, setUninstallPreview] = useState<UninstallPreview | null>(null)
  const [keepSpecific, setKeepSpecific] = useState<Set<string>>(new Set())

  // React Query: fetch installed apps
  const { data: apps = [], isLoading: loading, error: appsError } = useQuery<InstalledApp[]>({
    queryKey: ['apps'],
    queryFn: () => api.listApps(),
  })

  // Auto-switch to browse tab when no visible installed apps on first load
  useEffect(() => {
    if (!tabInitializedRef.current && !loading) {
      const visibleInstalled = apps.filter((a) => !(a.origin === 'builtin' && !a.enabled))
      if (visibleInstalled.length === 0) { setTab('browse') }
      tabInitializedRef.current = true
    }
  }, [apps, loading])

  // React Query: fetch registry (cached so the Installed tab can show update availability)
  const { data: registryData, isLoading: registryLoading, error: registryError } = useQuery<{ apps: RegistryApp[] }>({
    queryKey: ['registry'],
    // api.listRegistry() types `apps` as unknown[]; the backend payload matches
    // RegistryApp, so narrow it here at the single fetch boundary.
    queryFn: async () => {
      const res = await api.listRegistry()
      return { apps: res.apps as RegistryApp[] }
    },
    staleTime: 5 * 60_000, // cache for 5min to avoid re-fetching on tab switch
  })
  const registry: RegistryApp[] = registryData?.apps || []

  // Build a map of updateAvailable from registry for installed apps
  const updateMap = new Map(registry.filter(r => r.updateAvailable).map(r => [r.name, r.version]))

  // Display error: action errors take priority, then query errors
  useEffect(() => { if (appsError) setDismissedQueryError(false) }, [appsError])
  useEffect(() => { if (registryError) setDismissedRegistryError(false) }, [registryError])
  const displayError = error
    || (!dismissedQueryError && appsError ? (appsError as Error)?.message || 'Failed to load apps' : '')
    || (tab === 'browse' && !dismissedRegistryError && registryError ? (registryError as Error)?.message || 'Failed to load registry' : '')

  const handleAction = async (name: string, action: 'enable' | 'disable' | 'uninstall' | 'update') => {
    // Intercept uninstall to show confirmation modal with preview
    if (action === 'uninstall') {
      const app = apps.find(a => a.name === name)
      if (app) {
        setUninstallTarget(app)
        setKeepData(false)
        setKeepSpecific(new Set())
        // Fetch uninstall preview (best-effort — dialog works without it)
        try {
          const preview = await api.uninstallPreview(name)
          setUninstallPreview(preview)
        } catch {
          setUninstallPreview(null)
        }
      }
      return
    }
    // Update navigates to detail page (streaming install UI)
    if (action === 'update') {
      navigate(`/apps/detail/${name}?action=update`)
      return
    }
    setActionLoading(`${name}:${action}`)
    setError('')
    try {
      if (action === 'enable') await api.enableApp(name)
      else if (action === 'disable') await api.disableApp(name)
      queryClient.invalidateQueries({ queryKey: ['apps'] })
      window.dispatchEvent(new Event('mc:apps-changed'))
      // Show toast when hiding a builtin app
      if (action === 'disable') {
        const app = apps.find(a => a.name === name)
        if (app?.origin === 'builtin') {
          setSuccessMsg('Hidden. You can re-enable it from the Browse tab.')
          setTimeout(() => setSuccessMsg(''), 4000)
        }
      }
    } catch (e) {
      setError((e as Error)?.message || `Failed to ${action} ${name}`)
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
      queryClient.invalidateQueries({ queryKey: ['apps'] })
      window.dispatchEvent(new Event('mc:apps-changed'))
    } catch (e) {
      setError((e as Error)?.message || `Failed to uninstall ${name}`)
    } finally {
      setActionLoading(null)
      setUninstallTarget(null)
      setUninstallPreview(null)
    }
  }

  const handleInstall = async () => {
    if (!installPath.trim()) return
    setActionLoading('install')
    setError('')
    try {
      const result = await api.installApp(installPath.trim())
      recordEvent('app_install', { app: result.name || installPath.trim(), source: 'local' })
      setInstallPath('')
      setShowInstall(false)
      queryClient.invalidateQueries({ queryKey: ['apps'] })
      window.dispatchEvent(new Event('mc:apps-changed'))
    } catch (e) {
      setError((e as Error)?.message || 'Install failed')
    } finally {
      setActionLoading(null)
    }
  }

  // Exclude disabled builtins from Installed tab — they only appear in Browse tab
  const installedApps = apps.filter(a => !(a.origin === 'builtin' && !a.enabled))

  const filtered = installedApps.filter(a => {
    if (!filter) return true
    const q = filter.toLowerCase()
    return (
      a.name.toLowerCase().includes(q) ||
      (a.displayName || '').toLowerCase().includes(q) ||
      (a.manifest?.description || '').toLowerCase().includes(q) ||
      (a.manifest?.tags || []).some(t => t.toLowerCase().includes(q))
    )
  })

  const enabledCount = apps.filter(a => a.enabled).length
  const totalAgents = installedApps.reduce((n, a) => n + (a.manifest?.agents?.length || 0), 0)
  const totalSkills = installedApps.reduce((n, a) => n + (a.manifest?.skills?.length || 0), 0)
  const totalCrons = installedApps.reduce((n, a) => n + (a.manifest?.crons?.length || 0), 0)

  return (
    <>
      <PageHeader title="Apps" subtitle="Discover, install, and manage agentic apps" />
      <div className="px-6 pb-8 overflow-y-auto flex-1 min-h-0">

        {/* Stats */}
        <div className="grid gap-3.5 grid-cols-[repeat(auto-fit,minmax(130px,1fr))] mb-6">
          <StatCard label="Installed" value={installedApps.length} accent />
          <StatCard label="Enabled" value={enabledCount} />
          <StatCard label="Agents" value={totalAgents} />
          <StatCard label="Skills" value={totalSkills} />
          <StatCard label="Cron Jobs" value={totalCrons} />
        </div>

        {/* Notifications */}
        {displayError && (
          <div className="mb-4 bg-danger/10 border border-danger/20 rounded-lg p-3 flex items-center gap-3 animate-rise">
            <span className="text-danger text-sm flex-1">{displayError}</span>
            <button aria-label="Dismiss error" className="text-danger/60 hover:text-danger text-sm" onClick={() => { setError(''); setDismissedQueryError(true); setDismissedRegistryError(true) }}><X className="lucide-inline" /></button>
          </div>
        )}

        {successMsg && (
          <div className="mb-4 bg-bg-elevated border rounded-lg p-3 flex items-center gap-3 animate-rise" style={{ borderColor: 'color-mix(in srgb, var(--ok) 45%, transparent)' }}>
            <span className="text-text text-sm flex-1">{successMsg}</span>
            <button aria-label="Dismiss message" className="text-muted hover:text-text text-sm" onClick={() => setSuccessMsg('')}><X className="lucide-inline" /></button>
          </div>
        )}

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
            tabIndex={-1} ref={el => el?.focus()} role="dialog" aria-modal="true" aria-label="Confirm uninstall"
          >
            {/* eslint-disable-next-line jsx-a11y/no-static-element-interactions, jsx-a11y/click-events-have-key-events */}
            <div className="bg-card border border-border rounded-xl p-6 max-w-md w-full mx-4 shadow-xl" onClick={e => e.stopPropagation()}>
              <div className="flex items-center gap-3 mb-4">
                <div className="w-10 h-10 rounded-xl bg-danger/10 flex items-center justify-center">
                  <Trash2 size={20} className="text-danger" />
                </div>
                <div>
                  <div className="font-medium text-text">Uninstall {uninstallTarget.displayName || uninstallTarget.name}?</div>
                  <div className="text-[12px] text-muted">v{uninstallTarget.version}</div>
                </div>
              </div>

              <p className="text-[13px] text-muted mb-3">This will remove all resources provided by this app:</p>
              <div className="text-[13px] text-text mb-4 space-y-1">
                {uninstallTarget.resources === 'app' && !uninstallTarget.manifest?.setup?.onUninstall && uninstallTarget.origin !== 'registry' && (
                  <div className="bg-bg-elevated border border-border rounded-md px-2.5 py-2 text-[12px] text-muted mb-2">
                    This is a self-managed app — only KiroCrew metadata and the app secret will be removed.
                    The app itself and its agent/skill registrations are managed externally and will not be affected.
                    If the app is still running, it may re-register on next launch.
                  </div>
                )}
                {uninstallTarget.manifest?.setup?.onUninstall && (
                  <div className="bg-danger/5 border border-danger/20 rounded-md px-2.5 py-2 text-[12px] text-muted mb-2">
                    This app has an uninstall script that will run before removal. It may delete the app binary, agent configs, skills, and other resources it created during installation.
                  </div>
                )}
                {uninstallTarget.origin === 'registry' && (
                  <div className="bg-bg-elevated border border-border rounded-md px-2.5 py-2 text-[12px] text-muted mb-2">
                    Installed from Apps — KiroCrew metadata{uninstallTarget.resources === 'app' ? ', the app secret, and' : ' and'} the downloaded source code will be removed.{uninstallTarget.resources === 'app' && !uninstallTarget.manifest?.setup?.onUninstall ? ' The app itself is managed externally.' : ''}
                  </div>
                )}
                {uninstallTarget.origin !== 'registry' && uninstallTarget.resources === 'app' && uninstallTarget.manifest?.setup?.onUninstall && (
                  <div className="bg-bg-elevated border border-border rounded-md px-2.5 py-2 text-[12px] text-muted mb-2">
                    Not installed from Apps — your local source code will not be affected.
                  </div>
                )}
                {(uninstallTarget.manifest?.agents?.length || 0) > 0 && (
                  <div className="flex items-center gap-2"><Bot size={12} className="text-muted" /> {uninstallTarget.manifest.agents!.length} agent{uninstallTarget.manifest.agents!.length > 1 ? 's' : ''}</div>
                )}
                {(uninstallTarget.manifest?.skills?.length || 0) > 0 && (
                  <div className="flex items-center gap-2"><Zap size={12} className="text-muted" /> {uninstallTarget.manifest.skills!.length} skill{uninstallTarget.manifest.skills!.length > 1 ? 's' : ''}</div>
                )}
                {(uninstallTarget.manifest?.crons?.length || 0) > 0 && (
                  <div className="flex items-center gap-2"><Clock size={12} className="text-muted" /> {uninstallTarget.manifest.crons!.length} cron job{uninstallTarget.manifest.crons!.length > 1 ? 's' : ''}</div>
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
                      <p className="text-[13px] text-muted mb-2">Dependencies:</p>
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
                                  aria-label={`Keep dependency ${d.id.split('/').pop()}`}
                                  checked={keepSpecific.has(d.id)}
                                  onChange={e => {
                                    const next = new Set(keepSpecific)
                                    if (e.target.checked) next.add(d.id); else next.delete(d.id)
                                    setKeepSpecific(next)
                                  }}
                                  className="rounded"
                                />
                                Keep this dependency
                              </label>
                            </div>
                          </div>
                        ))}
                        {(deps.shared || []).map((d: SharedDep) => (
                          <div key={d.id} className="flex items-start gap-2">
                            <Lock size={12} className="text-muted mt-0.5 shrink-0" />
                            <div>
                              <div className="text-text">{d.id.split('/').pop()}</div>
                              <div className="text-[11px] text-muted">Kept — {d.reason}</div>
                            </div>
                          </div>
                        ))}
                        {(deps.userInstalled || []).map((d: UserInstalledDep) => (
                          <div key={d.id} className="flex items-start gap-2">
                            <Lock size={12} className="text-muted mt-0.5 shrink-0" />
                            <div>
                              <div className="text-text">{d.id.split('/').pop()}</div>
                              <div className="text-[11px] text-muted">Kept — installed by you</div>
                            </div>
                          </div>
                        ))}
                      </div>
                    </div>
                  )
                })()
              )}

              <label htmlFor="uninstall-keep-data" className="flex items-center gap-2 text-[13px] text-muted mb-5 cursor-pointer select-none">
                <input id="uninstall-keep-data" type="checkbox" aria-label="Keep app data" checked={keepData} onChange={e => setKeepData(e.target.checked)} className="rounded" />
                Keep app data
              </label>

              <div className="flex items-center gap-2 justify-end">
                <Btn onClick={() => { setUninstallTarget(null); setUninstallPreview(null) }}>Cancel</Btn>
                <Btn danger onClick={confirmUninstall} disabled={actionLoading === `${uninstallTarget.name}:uninstall`}>
                  {actionLoading === `${uninstallTarget.name}:uninstall` ? 'Removing…' : 'Uninstall'}
                </Btn>
              </div>
            </div>
          </div>
        )}

        {/* Tabs + Actions */}
        <div className="flex items-center justify-between mb-4">
          <SegmentedControl
            segments={[
              { key: 'installed' as const, label: 'Installed', icon: <Package size={13} />, count: installedApps.length },
              { key: 'browse' as const, label: 'Browse', icon: <ShoppingBag size={13} /> },
            ]}
            value={tab}
            onChange={setTab}
            layoutId="app-store-tabs"
          />
          <div className="flex items-center gap-2">
            <Btn onClick={() => setShowInstall(!showInstall)}>
              <Download size={14} /> Install from Path
            </Btn>
            <Btn aria-label="Refresh apps" onClick={() => queryClient.invalidateQueries({ queryKey: ['apps'] })}><RefreshCw size={14} /> Refresh</Btn>
          </div>
        </div>

        {/* Install from path */}
        {showInstall && (
          <Card>
            <div className="flex items-center gap-3">
              <FolderOpen size={16} className="text-muted shrink-0" />
              <Input
                placeholder="Local path to app directory (e.g. /path/to/oncall-watchtower)"
                value={installPath}
                onChange={(e: React.ChangeEvent<HTMLInputElement>) => setInstallPath(e.target.value)}
                onKeyDown={(e: React.KeyboardEvent<HTMLInputElement>) => e.key === 'Enter' && handleInstall()}
                className="flex-1"
              />
              <Btn
                onClick={handleInstall}
                disabled={actionLoading === 'install' || !installPath.trim()}
              >
                {actionLoading === 'install' ? 'Installing…' : 'Install'}
              </Btn>
            </div>
          </Card>
        )}

        {/* Installed Tab */}
        {tab === 'installed' && (
          <Card>
            <CardTitle>
              Installed Apps
              <InfoTip text="Apps contribute agents, skills, and cron jobs to KiroCrew. Enable an app to activate its resources." />
            </CardTitle>
            <SearchInput placeholder="Filter apps…" value={filter} onChange={(e: React.ChangeEvent<HTMLInputElement>) => setFilter(e.target.value)} />

            {loading ? (
              <div className="text-center py-12 text-muted text-sm">Loading apps…</div>
            ) : filtered.length === 0 ? (
              <EmptyState
                icon={<Package size={36} />}
                title={installedApps.length === 0 ? 'No apps installed yet' : 'No matching apps'}
                subtitle={installedApps.length === 0
                  ? 'Install your first app with the "Install from Path" button above, or run: kirocrew app install <path>'
                  : 'Try a different search term'}
              />
            ) : (
              <div className="space-y-3 mt-4">
                {filtered.map(app => (
                  <AppCard
                    key={app.name}
                    app={{...app, updateAvailable: updateMap.has(app.name), _newVersion: updateMap.get(app.name)}}
                    actionLoading={actionLoading}
                    onAction={handleAction}
                    onOpen={() => navigate(app.manifest?.ui?.pages?.[0]?.route || `/apps/${app.name}`)}
                    onDetail={() => navigate(`/apps/detail/${app.name}`)}
                  />
                ))}
              </div>
            )}
          </Card>
        )}

        {/* Browse Tab */}
        {tab === 'browse' && (<>
          <Card>
            <CardTitle>
              Browse Apps
              <InfoTip text="Discover and install apps from the KiroCrew registry." />
            </CardTitle>
            <SearchInput placeholder="Search apps…" value={registryFilter} onChange={(e: React.ChangeEvent<HTMLInputElement>) => setRegistryFilter(e.target.value)} />

            {registryLoading ? (
              <div className="text-center py-12 text-muted text-sm">Loading registry…</div>
            ) : (() => {
              // Merge disabled builtins into browse list for discovery
              // (hidden builtins are excluded — opt-in via `kirocrew app enable <name>`)
              const disabledBuiltins: RegistryApp[] = apps
                .filter(a => a.origin === 'builtin' && !a.enabled && !(a.manifest as any)?.hidden)
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
                  icon: a.manifest?.ui?.pages?.[0]?.icon || '',
                  iconUrl: a.manifest?.iconUrl || '',
                  installed: true,
                  enabled: false,
                  origin: 'builtin',
                  lifecycle: 'locked',
                }))
              const disabledBuiltinNames = new Set(disabledBuiltins.map(a => a.name))
              // Enrich registry entries with heroImage from locally installed app manifests
              const enrichedRegistry = registry.filter(r => !disabledBuiltinNames.has(r.name)).map(r => {
                const installed = apps.find(a => a.name === r.name)
                if (installed) {
                  return { ...r, heroImage: r.heroImage || installed.manifest?.heroImage, heroImageDark: r.heroImageDark || installed.manifest?.heroImageDark, screenshots: r.screenshots || installed.manifest?.screenshots }
                }
                return r
              })
              const browseApps = [...disabledBuiltins, ...enrichedRegistry]

              return browseApps.length === 0 ? (
                <EmptyState icon={<ShoppingBag size={36} />} title="No apps available" subtitle="Check back later or install from a local path." />
              ) : (
              <div className="grid grid-cols-[repeat(auto-fill,minmax(240px,1fr))] gap-4 mt-4">
                {browseApps
                  .filter(a => {
                    if (!registryFilter) return true
                    const q = registryFilter.toLowerCase()
                    return a.displayName.toLowerCase().includes(q)
                      || a.description.toLowerCase().includes(q)
                      || (a.tags || []).some(t => t.toLowerCase().includes(q))
                  })
                  .map(app => (
                    <div
                      key={app.name}
                      role="button"
                      tabIndex={0}
                      aria-label={`View details for ${app.displayName}`}
                      className="border border-border rounded-xl overflow-hidden hover:border-accent/40 hover:shadow-md transition-all cursor-pointer group"
                      onClick={(e) => {
                        if (e.metaKey || e.ctrlKey) {
                          window.open(`/apps/detail/${app.name}`, '_blank')
                        } else {
                          navigate(`/apps/detail/${app.name}`)
                        }
                      }}
                      onKeyDown={(e) => {
                        if (e.target !== e.currentTarget) return
                        if (e.key !== 'Enter' && e.key !== ' ') return
                        e.preventDefault()
                        if (e.metaKey || e.ctrlKey) {
                          window.open(`/apps/detail/${app.name}`, '_blank')
                        } else {
                          navigate(`/apps/detail/${app.name}`)
                        }
                      }}
                    >
                      {/* Hero image */}
                      {(() => {
                        const hero = resolvedMode === 'dark'
                          ? (app.heroImageDark || app.heroImage || app.screenshots?.[0])
                          : (app.heroImage || app.heroImageDark || app.screenshots?.[0])
                        return (
                          <div className="w-full aspect-video bg-[var(--card)] overflow-hidden relative">
                            {hero ? (
                              <img
                                src={hero}
                                alt=""
                                className="w-full h-full object-cover group-hover:scale-[1.02] transition-transform duration-300"
                                onError={(e) => { const img = e.currentTarget; img.style.display = 'none'; img.parentElement!.querySelector('.hero-fallback')?.classList.remove('hidden') }}
                              />
                            ) : null}
                            <div className={`absolute inset-0 flex items-center justify-center bg-[var(--bg-elevated)] ${hero ? 'hidden' : ''} hero-fallback`}>
                              <span className="text-2xl font-bold text-[var(--text)] opacity-10 tracking-widest">KIRO CREW</span>
                            </div>
                          </div>
                        )
                      })()}
                      <div className="p-4">
                        <div className="flex items-center gap-3 mb-3">
                          <div className="w-10 h-10 rounded-xl bg-accent/10 flex items-center justify-center group-hover:bg-accent/20 transition-colors overflow-hidden shrink-0">
                            <AppIcon icon={app.icon} iconUrl={app.iconUrl} size={28} />
                          </div>
                          <div className="flex-1 min-w-0">
                            <div className="font-bold text-text text-[14px] truncate">{app.displayName}</div>
                            <div className="text-[11px] text-muted">{app.author}</div>
                          </div>
                        </div>
                        <p className="text-[12px] text-muted line-clamp-2 mb-3 leading-relaxed">{app.description}</p>
                        <div className="flex items-center justify-between">
                          <div className="flex items-center gap-1.5">
                            <span className="text-[11px] text-muted">v{app.installedVersion || app.version}</span>
                            {app.origin === 'builtin' && (
                              <span className="text-[11px] text-aim bg-aim/10 border border-aim/20 px-1.5 py-0.5 rounded">Built-in</span>
                            )}
                            {app.platform?.os?.length === 1 && app.platform.os[0] === 'macos' && (
                              <span className="text-[11px] text-muted bg-bg-elevated border border-border px-1.5 py-0.5 rounded">macOS</span>
                            )}
                            {app.platform?.os?.length === 1 && app.platform.os[0] === 'linux' && (
                              <span className="text-[11px] text-muted bg-bg-elevated border border-border px-1.5 py-0.5 rounded">Linux</span>
                            )}
                          </div>
                          {app.origin === 'builtin' && !app.enabled ? (
                            <Btn onClick={(e: React.MouseEvent) => { e.stopPropagation(); handleAction(app.name, 'enable') }}>
                              <Power size={14} /> Enable
                            </Btn>
                          ) : app.installed ? (
                            <Badge variant="ok">Installed</Badge>
                          ) : (
                            <span className="text-[13px] text-accent font-medium">Get</span>
                          )}
                        </div>
                      </div>
                    </div>
                  ))}
              </div>
              )
            })()}
          </Card>

          {/* External Registries Management */}
          <div className="mt-6">
            <RegistryManager />
          </div>
        </>)}
      </div>
    </>
  )
}

function AppCard({
  app,
  actionLoading,
  onAction,
  onOpen,
  onDetail,
}: {
  app: InstalledApp & { _newVersion?: string }
  actionLoading: string | null
  onAction: (name: string, action: 'enable' | 'disable' | 'uninstall' | 'update') => void
  onOpen: () => void
  onDetail: () => void
}) {
  const [expanded, setExpanded] = useState(false)
  const [remoteCmd, setRemoteCmd] = useState('')
  const m = app.manifest
  const agentCount = m?.agents?.length || 0
  const skillCount = m?.skills?.length || 0
  const cronCount = m?.crons?.length || 0
  const sopCount = m?.sops?.length || 0
  const hasUI = !!(m?.ui?.entry) || (m?.ui?.pages?.length || 0) > 0
  const pageIcon = m?.ui?.pages?.[0]?.icon || ''
  const isSelfManaged = app.resources === 'app'
  const isBuiltin = app.origin === 'builtin'
  const canUpdate = app.lifecycle === 'gateway'
  const canUninstall = app.lifecycle !== 'locked'
  const hasOpenCommand = !!m?.openCommand
  // Derive icon URL: prefer manifest iconUrl (builtins), fallback to blob proxy (registry)
  const iconUrl = m?.iconUrl || (m?.iconPath && m?.repo
    ? `/api/apps/blob?repo=${encodeURIComponent(m.repo)}&path=${encodeURIComponent(m.iconPath)}`
    : undefined)

  return (
    <div className="border border-border rounded-lg hover:border-accent/30 transition-colors overflow-hidden">
      {remoteCmd && (
        <div className="px-4 pt-3 pb-2">
          <div className="bg-accent/10 border border-accent/20 rounded-lg p-3 text-[13px]">
            <div className="flex items-start justify-between gap-2">
              <div>
                <span className="text-text font-medium">Remote environment detected</span>
                <p className="text-muted mt-1">Run this on your local machine:</p>
                <code className="block mt-1.5 bg-bg-elevated px-2 py-1 rounded text-[12px] font-mono select-all">{remoteCmd}</code>
              </div>
              <button aria-label="Dismiss" className="text-muted hover:text-text text-sm shrink-0" onClick={() => setRemoteCmd('')}><X className="lucide-inline" /></button>
            </div>
          </div>
        </div>
      )}
      <div className="p-4">
        <div className="flex items-start justify-between gap-4">
          <div className="flex items-start gap-3 flex-1 min-w-0">
            <div className="w-12 h-12 rounded-xl bg-accent/10 flex items-center justify-center shrink-0 mt-0.5 overflow-hidden">
              <AppIcon icon={pageIcon} iconUrl={iconUrl} size={36} />
            </div>
            <div className="flex-1 min-w-0">
              <div className="flex items-center gap-2 mb-1 flex-wrap">
                <button type="button" className="font-medium text-text cursor-pointer hover:text-accent transition-colors bg-transparent border-0 p-0 text-left" onClick={onDetail}>{app.displayName || app.name}</button>
                <span className="text-[11px] text-muted bg-bg-elevated px-1.5 py-0.5 rounded">v{app.version}{app.updateAvailable && ` (v${app._newVersion} available)`}</span>
                {isBuiltin ? (
                  <Badge variant="aim">Built-in</Badge>
                ) : isSelfManaged ? (
                  <Badge variant="ok">Self-managed</Badge>
                ) : (
                  <Badge variant={app.enabled ? 'ok' : 'warn'}>
                    {app.enabled ? 'Enabled' : 'Disabled'}
                  </Badge>
                )}
                {app.migratedTo && (
                  <Badge variant="warn">Migrating</Badge>
                )}
                {!isBuiltin && app.origin === 'registry' && (
                  <Badge variant="aim">Registry</Badge>
                )}
                {app.origin === 'local' && (
                  <Badge variant="warn">Local</Badge>
                )}
                {app.origin === 'external' && !isSelfManaged && (
                  <Badge variant="ok">External</Badge>
                )}
              </div>
              <p className="text-sm text-muted mb-2 line-clamp-2">{m?.description}</p>
              <div className="flex items-center gap-3 text-[12px] text-muted flex-wrap">
                {m?.author && <span className="flex items-center gap-1"><Users size={11} /> {m.author}</span>}
                {agentCount > 0 && <span className="flex items-center gap-1"><Bot size={11} /> {agentCount} agent{agentCount > 1 ? 's' : ''}</span>}
                {skillCount > 0 && <span className="flex items-center gap-1"><Zap size={11} /> {skillCount} skill{skillCount > 1 ? 's' : ''}</span>}
                {cronCount > 0 && <span className="flex items-center gap-1"><Clock size={11} /> {cronCount} cron{cronCount > 1 ? 's' : ''}</span>}
                {hasUI && <span className="flex items-center gap-1"><Package size={11} /> {m.ui!.pages!.length} page{m.ui!.pages!.length > 1 ? 's' : ''}</span>}
              </div>
            </div>
          </div>
          <div className="flex items-center gap-2 shrink-0">
            {/* Open button — all app types */}
            {hasOpenCommand && (
              <Btn primary onClick={() => api.openApp(app.name).then((res: { remote?: boolean; command?: string; message?: string } | null) => {
                if (res?.remote) setRemoteCmd(res.command || res.message || 'App cannot be opened — KiroCrew is running in a headless environment.')
              }).catch(() => {})}>
                <ExternalLink size={14} /> Open
              </Btn>
            )}
            {app.enabled && hasUI && !hasOpenCommand && (
              <Btn primary onClick={onOpen}>
                <ExternalLink size={14} /> Open
              </Btn>
            )}

            {/* Enable/Disable */}
            {app.enabled ? (
              <Btn
                onClick={() => onAction(app.name, 'disable')}
                disabled={actionLoading === `${app.name}:disable`}
              >
                <PowerOff size={14} /> {isBuiltin ? 'Hide' : 'Disable'}
              </Btn>
            ) : (
              <Btn
                onClick={() => onAction(app.name, 'enable')}
                disabled={actionLoading === `${app.name}:enable`}
              >
                <Power size={14} /> {isBuiltin ? 'Show' : 'Enable'}
              </Btn>
            )}

            {/* Update — show accent button when new version available (any installed app) */}
            {app.updateAvailable && (
              <Btn
                onClick={() => onAction(app.name, 'update')}
                disabled={actionLoading === `${app.name}:update`}
                title={`Update to v${app.version}`}
                className="!bg-[var(--info)] !text-white hover:!opacity-80"
              >
                <ArrowUp size={14} /> Update
              </Btn>
            )}
            {/* Sync — always available for gateway apps */}
            {canUpdate && !app.updateAvailable && (
              <Btn
                onClick={() => onAction(app.name, 'update')}
                disabled={actionLoading === `${app.name}:update`}
                title="Sync app from its source directory"
              >
                <RefreshCw size={14} /> Sync
              </Btn>
            )}

            {/* Uninstall — only for lifecycle != locked */}
            {canUninstall && (
              <Btn
                danger
                onClick={() => onAction(app.name, 'uninstall')}
                disabled={actionLoading === `${app.name}:uninstall`}
              >
                <Trash2 size={14} /> Uninstall
              </Btn>
            )}

            <button
              className="text-muted hover:text-text transition-colors p-1"
              onClick={() => setExpanded(!expanded)}
            >
              <ChevronRight size={16} className={`transition-transform ${expanded ? 'rotate-90' : ''}`} />
            </button>
          </div>
        </div>
      </div>

      {/* Expanded details */}
      {expanded && (
        <div className="border-t border-border bg-bg-elevated/50 p-4 space-y-3 text-[13px]">
          {(m?.tags || []).length > 0 && (
            <div className="flex items-center gap-2 flex-wrap">
              <Tag size={12} className="text-muted" />
              {m!.tags!.map(t => (
                <span key={t} className="bg-bg-elevated border border-border px-2 py-0.5 rounded text-[11px] text-muted">{t}</span>
              ))}
            </div>
          )}
          {(m?.permissions?.mcpTools || []).length > 0 && (
            <div>
              <span className="text-muted">MCP Tools: </span>
              <span className="text-text">{m!.permissions!.mcpTools!.join(', ')}</span>
            </div>
          )}
          {hasUI && m?.ui?.pages && (
            <div>
              <span className="text-muted">UI Pages: </span>
              {m.ui.pages.map(p => (
                <span key={p.route} className="text-text mr-3">{p.label} ({p.route})</span>
              ))}
            </div>
          )}
          {sopCount > 0 && (
            <div>
              <span className="text-muted">SOPs: </span>
              <span className="text-text">{sopCount} standard operating procedure{sopCount > 1 ? 's' : ''}</span>
            </div>
          )}
          <div className="text-[11px] text-muted">
            Installed: {new Date(app.installedAt).toLocaleDateString()}
            {m?.minKiroCrewVersion && <span className="ml-3">Min version: {m.minKiroCrewVersion}</span>}
            {isSelfManaged && <div className="mt-1">Management: App handles its own agent/skill/MCP registration</div>}
            {isBuiltin && <div className="mt-1">Built-in: This feature is part of the KiroCrew dashboard</div>}
            {app.source && !isBuiltin && <div className="mt-1 truncate" title={app.source}>Source: {app.source}</div>}
            {app.origin && <div className="mt-1">Origin: {app.origin} | Resources: {app.resources || 'gateway'} | Lifecycle: {app.lifecycle || 'gateway'}</div>}
          </div>
        </div>
      )}
    </div>
  )
}
