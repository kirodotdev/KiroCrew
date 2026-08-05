/**
 * AppDetailPage — unified detail view for both installed and registry apps.
 *
 * Route: /apps/detail/:name
 * Fetches from both /api/apps/{name} (installed) and /api/apps/registry (browse).
 * Shows full description, features, screenshots, tags, and action buttons.
 */
import { useEffect, useState, useCallback, useRef } from 'react'
import { useParams, useNavigate, useLocation } from 'react-router-dom'
import {
  ArrowLeft, Download, Check, Loader2, Power, PowerOff,
  Trash2, RefreshCw, Bot, Zap, ArrowUp,
  Clock, ChevronLeft, ChevronRight, X, Monitor, Copy, Terminal,
  Sparkles,
} from 'lucide-react'
import { needsDesktopApp } from '../lib/electron'
import { api } from '../api/client'
import { PageHeader, Card, CardTitle, Badge, Btn } from '../components/ui'
import AppIcon from '../components/AppIcon'
import { recordEvent } from '../rum'
import { useTheme } from '../hooks/useTheme'
import AskAgentButton from '../components/AskAgentButton'

import { i18nT } from '../i18n/t'
import { appDisplayName, appDescription, appHighlights } from '../components/appstore/appManifest'
import { fmtDateNumeric } from '../i18n/format'
type AppInfo = {
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
  screenshotsDark?: string[]
  heroImage?: string
  heroImageDark?: string
  heroImageDetail?: string
  heroImageDetailDark?: string
  repo?: string
  branch?: string
  // Installed state
  installed: boolean
  installedVersion?: string
  enabled?: boolean
  managed?: string
  source?: string
  installedAt?: string
  updateAvailable?: boolean
  // Three-axis classification
  origin?: string     // "builtin" | "registry" | "local" | "external"
  resources?: string  // "gateway" | "app"
  lifecycle?: string  // "gateway" | "app" | "locked"
  // Platform
  platform?: { os?: string[]; installMode?: string; clientInstall?: { shell?: string; postInstall?: string }
    // Set when the app's UI needs the Electron shell (native windows,
    // global shortcuts, tray). A UX gate only — the marker is client-side.
    requiresDesktopApp?: boolean }
  // Manifest (from installed app)
  manifest?: AppManifest
}

interface McpServerConfig {
  url?: string
  command?: string
  autoApprove?: string[]
  [key: string]: unknown
}

interface AppPermissions {
  api?: string[]
  events?: string[]
  mcpTools?: string[]
  storage?: boolean
  cron?: boolean
  network?: boolean
  memory?: boolean | string
  [key: string]: unknown
}

/** True when a failure is the third-party-app execution gate refusing.
 *
 *  The repo's wire contract (test_error_code_contract.py) is that `code` is
 *  machine-readable and `error` is advisory prose, so this keys off `code` only
 *  — never off the sentence, which is English, unlocalizable, and free to be
 *  reworded by the backend at any time.
 *
 *  Two shapes reach us, because the two call paths fail differently:
 *   - the registry install resolves a payload object carrying `code`;
 *   - `enableApp` REJECTS with an `ApiError`, which keeps the payload as a raw
 *     JSON *string* on `.body` rather than as own properties — reading
 *     `err.code` finds nothing, so `.body` has to be parsed first (same
 *     approach as `embedModelErrorMessage`).
 */
function isExecutionDenied(source: unknown): boolean {
  if (source == null || typeof source !== 'object') return false
  let obj = source as Record<string, unknown>
  const raw = obj.body
  if (typeof raw === 'string' && raw.trim()) {
    try {
      const parsed = JSON.parse(raw)
      if (parsed && typeof parsed === 'object') obj = { ...obj, ...parsed }
    } catch { /* not JSON — fall through to whatever fields are already there */ }
  }
  return obj.code === 'app_execution_denied'
}

/** A registry app entry from /api/apps/registry — a superset of the fields we
 *  read here, spread into AppInfo when there's no installed app. */
interface RegistryEntry extends Partial<AppInfo> {
  name: string
  updateAvailable?: boolean
}

interface AppManifest {
  displayName?: string
  description?: string
  version?: string
  author?: string
  tags?: string[]
  highlights?: string[]
  screenshots?: string[]
  screenshotsDark?: string[]
  // Store-listing metadata. For built-in apps these live on the manifest
  // (preserved through AppManifest.extra) rather than on a registry entry —
  // built-ins are not part of the /api/apps/registry feed.
  iconUrl?: string
  heroImage?: string
  heroImageDark?: string
  heroImageDetail?: string
  heroImageDetailDark?: string
  ui?: { pages?: { route?: string; label?: string; icon?: string; iconUrl?: string }[] }
  // Installed apps carry the platform config here (a registry/catalog entry
  // exposes it top-level instead); `needsDesktopApp` reads both. Without this the
  // desktop requirement was invisible on surfaces that pass the manifest shape.
  platform?: { requiresDesktopApp?: boolean; os?: string[] }
  agents?: string[]
  skills?: string[]
  crons?: { name: string }[]
  mcpServers?: Record<string, McpServerConfig>
  permissions?: AppPermissions
  minKiroCrewVersion?: string
}

function ScreenshotGallery({ screenshots }: { screenshots: string[] }) {
  const [selected, setSelected] = useState<number | null>(null)

  if (screenshots.length === 0) return null

  return (
    <>
      <div className="mb-6">
        <div className="text-[12px] text-muted uppercase tracking-wider mb-3">{i18nT('pages.appDetailPage.screenshots')}</div>
        <div className="flex gap-3 overflow-x-auto pb-2 scrollbar-none">
          {screenshots.map((url, i) => (
            <button
              key={i}
              type="button"
              aria-label={`Open screenshot ${i + 1}`}
              className="p-0 border-none bg-transparent shrink-0 cursor-pointer"
              onClick={() => setSelected(i)}
            >
              {/* onError is an image-load lifecycle handler (hide broken images), */}
              {/* not a user interaction; the rule flags onError regardless. */}
              {/* eslint-disable-next-line jsx-a11y/no-noninteractive-element-interactions */}
              <img
                src={url}
                alt={`Screenshot ${i + 1}`}
                className="h-40 rounded-lg border border-border hover:border-accent/40 hover:shadow-md transition-all object-cover"
                onError={e => { (e.target as HTMLImageElement).style.display = 'none' }}
              />
            </button>
          ))}
        </div>
      </div>

      {/* Lightbox */}
      {selected !== null && (
        // Modal backdrop: click-to-dismiss is a mouse affordance; keyboard users
        // dismiss/navigate via the onKeyDown handler (Escape / arrows) below.
        // eslint-disable-next-line jsx-a11y/no-noninteractive-element-interactions
        <div
          className="fixed inset-0 z-[9999] flex items-center justify-center bg-bg/80 backdrop-blur-sm"
          onClick={() => setSelected(null)}
          onKeyDown={e => {
            if (e.key === 'Escape') setSelected(null)
            if (e.key === 'ArrowRight' && selected < screenshots.length - 1) setSelected(selected + 1)
            if (e.key === 'ArrowLeft' && selected > 0) setSelected(selected - 1)
          }}
          tabIndex={-1}
          ref={el => el?.focus()}
          role="dialog"
          aria-modal="true"
        >
          {/* Presentational wrapper: stops backdrop-dismiss when clicking the image. */}
          {/* eslint-disable-next-line jsx-a11y/no-static-element-interactions, jsx-a11y/click-events-have-key-events */}
          <div className="relative max-w-4xl max-h-[80vh] mx-4" onClick={e => e.stopPropagation()}>
            <img src={screenshots[selected]} alt="" className="max-w-full max-h-[80vh] rounded-xl shadow-2xl" />
            <button className="absolute top-2 right-2 bg-bg/80 rounded-full p-1.5 text-muted hover:text-text" onClick={() => setSelected(null)} aria-label={i18nT('pages.appDetailPage.close')}><X size={18} /></button>
            {selected > 0 && (
              <button className="absolute left-2 top-1/2 -translate-y-1/2 bg-bg/80 rounded-full p-2 text-muted hover:text-text" onClick={e => { e.stopPropagation(); setSelected(selected - 1) }} aria-label={i18nT('pages.appDetailPage.previous')}><ChevronLeft size={20} /></button>
            )}
            {selected < screenshots.length - 1 && (
              <button className="absolute right-2 top-1/2 -translate-y-1/2 bg-bg/80 rounded-full p-2 text-muted hover:text-text" onClick={e => { e.stopPropagation(); setSelected(selected + 1) }} aria-label={i18nT('pages.appDetailPage.next')}><ChevronRight size={20} /></button>
            )}
            <div className="absolute bottom-3 left-1/2 -translate-x-1/2 text-[12px] text-muted bg-bg/80 px-3 py-1 rounded-full">{selected + 1} / {screenshots.length}</div>
          </div>
        </div>
      )}
    </>
  )
}

export default function AppDetailPage() {
  const { name } = useParams<{ name: string }>()
  const navigate = useNavigate()
  const location = useLocation()
  const { theme: resolvedMode } = useTheme()
  const [app, setApp] = useState<AppInfo | null>(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState('')
  // Tracked separately from `error` because it changes what the banner OFFERS,
  // not just what it says: this is the one failure the user can act on from
  // here, and until now they were handed an English sentence naming a config
  // key with nothing to click.
  const [deniedByPolicy, setDeniedByPolicy] = useState(false)
  /** Clear BOTH error fields together.
   *
   *  Resetting only `error` would leave `deniedByPolicy` set, so the next
   *  unrelated failure would render the third-party-gate copy and an
   *  "open security settings" button for something that has nothing to do with
   *  it. Routing every reset through here is what keeps the two in step. */
  const clearError = useCallback(() => {
    setError('')
    setDeniedByPolicy(false)
  }, [])
  const [actionLoading, setActionLoading] = useState<string | null>(null)
  const [installLog, setInstallLog] = useState('')
  const [showInstallLog, setShowInstallLog] = useState(false)
  const [installDone, setInstallDone] = useState(false)
  const installLogRef = useRef<HTMLPreElement>(null)
  const installAbortRef = useRef<AbortController | null>(null)
  const [clientInstall, setClientInstall] = useState<{ shell?: string; postInstall?: string } | null>(null)
  const [copied, setCopied] = useState(false)
  const [serverHostname, setServerHostname] = useState('')
  const [showUninstallConfirm, setShowUninstallConfirm] = useState(false)
  const [keepData, setKeepData] = useState(true)

  // Helper: open chat with a pre-filled message (same mechanism as useChatLauncher from app-sdk)
  const openChatWithMessage = useCallback((message: string) => {
    ;(window as Window & { __mc_chat_launch?: { message: string; ts: number } }).__mc_chat_launch = { message, ts: Date.now() }
    navigate('/chat')
  }, [navigate])

  // Abort in-flight streaming install on unmount
  useEffect(() => () => { installAbortRef.current?.abort() }, [])

  const load = useCallback(async () => {
    if (!name) return
    setLoading(true)
    clearError()
    try {
      // Try installed app first
      const installed = await api.getApp(name).catch(() => null)
      // Also check registry for richer metadata (screenshots, highlights)
      const registryData = await api.listRegistry().catch(() => ({ apps: [], serverPlatform: { os: '', arch: '' } }))
      const registryList = (registryData.apps || []) as RegistryEntry[]

      // Fetch server hostname for client install template variables
      const sysInfo = await api.system().catch(() => ({ hostname: '' }))
      if (sysInfo.hostname) setServerHostname(sysInfo.hostname)
      const registryEntry = registryList.find((r) => r.name === name)

      if (installed) {
        const m = installed.manifest || {}
        setApp({
          name: installed.name,
          displayName: installed.displayName || m.displayName || installed.name,
          description: m.description || '',
          version: registryEntry?.version || m.version || installed.version || '0.0.0',
          author: m.author || registryEntry?.author || '',
          // Built-in apps aren't in the registry feed, so registryEntry is
          // undefined for them — their icon/hero metadata lives on the
          // manifest. Fall back to it so built-in detail pages render the real
          // icon and hero instead of the generic Package box.
          icon: registryEntry?.icon || m.ui?.pages?.[0]?.icon || '',
          iconUrl: registryEntry?.iconUrl || m.iconUrl || m.ui?.pages?.[0]?.iconUrl || '',
          tags: m.tags || registryEntry?.tags || [],
          highlights: m.highlights || registryEntry?.highlights || [],
          screenshots: registryEntry?.screenshots || m.screenshots || [],
          screenshotsDark: registryEntry?.screenshotsDark || m.screenshotsDark || [],
          heroImage: registryEntry?.heroImage || m.heroImage || '',
          heroImageDark: registryEntry?.heroImageDark || m.heroImageDark || '',
          heroImageDetail: registryEntry?.heroImageDetail || m.heroImageDetail || '',
          heroImageDetailDark: registryEntry?.heroImageDetailDark || m.heroImageDetailDark || '',
          repo: registryEntry?.repo || '',
          installed: true,
          installedVersion: installed.version,
          enabled: installed.enabled,
          managed: installed.managed,
          source: installed.source,
          installedAt: installed.installedAt,
          origin: installed.origin,
          resources: installed.resources,
          lifecycle: installed.lifecycle,
          updateAvailable: registryEntry?.updateAvailable || false,
          manifest: m,
        })
      } else if (registryEntry) {
        setApp({
          ...registryEntry,
          // Required AppInfo fields — registry entries normally carry these, but
          // fall back so the object always satisfies AppInfo.
          name: registryEntry.name,
          displayName: registryEntry.displayName || registryEntry.name,
          description: registryEntry.description || '',
          version: registryEntry.version || '0.0.0',
          author: registryEntry.author || '',
          // Preserve install status from registry (set by detectInstalled)
          installed: registryEntry.installed ?? false,
          platform: registryEntry.platform,
        })
      } else {
        setError(`App "${name}" not found`)
      }
    } catch (e: unknown) {
      setError(e instanceof Error ? e.message : i18nT('pages.appDetailPage.failed_to_load_app'))
    } finally {
      setLoading(false)
    }
  }, [name, clearError])

  useEffect(() => { load() }, [load])

  // Auto-trigger install/update after an IN-APP action navigated here.
  //
  // The trigger is router state (``location.state.autoAction``) and ONLY that:
  // a query param is attacker-reachable — a cross-site page can navigate an
  // authenticated browser to a detail URL and the Lax session cookie rides
  // along, so any URL-driven trigger (install OR update, since update installs
  // an absent app) would run third-party setup code with gateway privileges
  // without user intent. Router state can only be produced by an in-app
  // navigate() call, so it cannot be forged from outside the app.
  const autoActionTriggered = useRef(false)
  useEffect(() => {
    if (!app || autoActionTriggered.current) return
    const stateAction = (location.state as { autoAction?: string } | null)?.autoAction
    if (stateAction !== 'install' && stateAction !== 'update') return
    if (stateAction === 'install' && app.installed) return
    autoActionTriggered.current = true
    // Clear the state so a refresh or Back/Forward doesn't re-fire it.
    navigate(`${location.pathname}${location.search}`, { replace: true, state: null })
    handleInstall()
  }, [app, location]) // eslint-disable-line react-hooks/exhaustive-deps

  const handleInstall = async () => {
    if (!app) return
    setActionLoading('install')
    setInstallLog('')
    setInstallDone(false)
    setShowInstallLog(true)
    clearError()
    setClientInstall(null)
    installAbortRef.current?.abort()
    const controller = new AbortController()
    installAbortRef.current = controller
    try {
      const result = await api.installFromRegistryStream(
        app.name,
        (line) => {
          setInstallLog(prev => prev + (prev ? '\n' : '') + line)
          // Auto-scroll to bottom
          requestAnimationFrame(() => {
            if (installLogRef.current) {
              installLogRef.current.scrollTop = installLogRef.current.scrollHeight
            }
          })
        },
        controller.signal,
      )
      // Server says this app needs client-side installation
      if (result.needsClientInstall) {
        setClientInstall(result.clientInstall || app.platform?.clientInstall || {})
        setShowInstallLog(false)
        setActionLoading(null)
        return
      }
      setInstallDone(true)
      if (result.ok) {
        recordEvent('app_install', { app: app.name, source: 'registry', version: app.version })
        await load()
        window.dispatchEvent(new Event('mc:apps-changed'))
      } else {
        setDeniedByPolicy(isExecutionDenied(result))
        setError(result.error || i18nT('pages.appDetailPage.install_failed'))
      }
    } catch (e: unknown) {
      if (e instanceof Error && e.name === 'AbortError') return
      setInstallDone(true)
      setDeniedByPolicy(isExecutionDenied(e))
      setError(e instanceof Error ? e.message : i18nT('pages.appDetailPage.install_failed'))
    } finally {
      // Only clear loading if this is still the active install —
      // compare by identity to avoid the race where a second invocation
      // replaces the ref before this finally runs.
      if (installAbortRef.current === controller) {
        setActionLoading(null)
      }
    }
  }

  const handleAction = async (action: 'enable' | 'disable' | 'uninstall' | 'update') => {
    if (!app) return
    // Intercept uninstall to show confirmation modal
    if (action === 'uninstall') {
      setShowUninstallConfirm(true)
      setKeepData(true)
      return
    }
    setActionLoading(action)
    clearError()
    try {
      if (action === 'enable') await api.enableApp(app.name)
      else if (action === 'disable') await api.disableApp(app.name)
      else if (action === 'update') await api.updateApp(app.name)
      if (action === 'enable' || action === 'disable') {
        recordEvent(`app_${action}`, { app: app.name, version: app.installedVersion || app.version })
      }
      await load()
      window.dispatchEvent(new Event('mc:apps-changed'))
    } catch (e: unknown) {
      setDeniedByPolicy(isExecutionDenied(e))
      setError(e instanceof Error ? e.message : `Failed to ${action}`)
    } finally {
      setActionLoading(null)
    }
  }

  const confirmUninstall = async () => {
    if (!app) return
    setActionLoading('uninstall')
    clearError()
    try {
      const res = await api.uninstallApp(app.name, keepData)
      if (res.uninstall_log) setInstallLog(res.uninstall_log)
      recordEvent('app_uninstall', { app: app.name, version: app.installedVersion || app.version })
      window.dispatchEvent(new Event('mc:apps-changed'))
      navigate('/apps')
    } catch (e: unknown) {
      setError(e instanceof Error ? e.message : i18nT('pages.appDetailPage.failed_to_uninstall'))
    } finally {
      setActionLoading(null)
      setShowUninstallConfirm(false)
    }
  }

  if (loading) {
    return (
      <>
        <PageHeader title={i18nT('pages.appDetailPage.apps')} subtitle={i18nT('pages.appDetailPage.loading')} />
        <div className="flex-1 flex items-center justify-center text-muted text-sm">
          <Loader2 size={16} className="animate-spin mr-2" /> {i18nT('pages.appDetailPage.loading_app_details')}
        </div>
      </>
    )
  }

  if (!app) {
    return (
      <>
        <PageHeader title={i18nT('pages.appDetailPage.app_not_found')} subtitle={error || `"${name}" doesn't exist`} />
        <div className="flex-1 flex items-center justify-center p-8">
          <Btn onClick={() => navigate('/apps')}><ArrowLeft size={14} /> {i18nT('pages.appDetailPage.back_to_apps')}</Btn>
        </div>
      </>
    )
  }

  const isSelfManaged = app.resources === 'app'
  const isBuiltin = app.origin === 'builtin'
  // An app can declare that its UI only works inside the Electron shell
  // (native always-on-top windows, global shortcuts, tray). Browser sessions
  // are told to use the desktop app instead of being handed a broken UI.
  // UX gate only: the marker is client-side, so nothing security-relevant may
  // rest on it (see PlatformConfig.requiresDesktopApp in manifest.py).
  const desktopOnly = needsDesktopApp(app)
  const canUpdate = app.lifecycle === 'gateway'
  const canUninstall = app.lifecycle !== 'locked'
  const agentCount = app.manifest?.agents?.length || 0
  const skillCount = app.manifest?.skills?.length || 0
  const cronCount = app.manifest?.crons?.length || 0
  // Theme-aware hero banner source (mirrors the Browse card resolution).
  // Prefer the wide detail-ratio banner (heroImageDetail*); fall back to the
  // Browse hero, then the opposite theme.
  const heroDetailSrc = resolvedMode === 'dark'
    ? (app.heroImageDetailDark || app.heroImageDetail || '')
    : (app.heroImageDetail || app.heroImageDetailDark || '')
  const heroBrowseSrc = resolvedMode === 'dark'
    ? (app.heroImageDark || app.heroImage || '')
    : (app.heroImage || app.heroImageDark || '')
  const heroSrc = heroDetailSrc || heroBrowseSrc
  // When a dedicated detail banner is shown, size the container to its
  // 1200x288 (25:6) ratio so object-cover doesn't horizontally crop the art
  // on viewports narrower than 1200px. Fall back to 16:9 for the Browse hero.
  const heroIsDetail = Boolean(heroDetailSrc)

  return (
    <>
      <PageHeader title={i18nT('pages.appDetailPage.apps')} subtitle={appDisplayName(app)} />
      <div className="px-6 pb-8 overflow-y-auto flex-1 min-h-0">
        {/* Back link */}
        <button className="flex items-center gap-1.5 text-[13px] text-muted hover:text-text mb-5 bg-transparent border-none cursor-pointer p-0 font-body transition-colors" onClick={() => navigate('/apps')}>
          <ArrowLeft size={14} /> {i18nT('pages.appDetailPage.back_to_apps')}
        </button>

        {/* Error */}
        {error && (
          <div className="mb-4 bg-danger/10 border border-danger/20 rounded-lg p-3 flex items-start gap-3 animate-rise">
            <div className="flex-1 min-w-0">
              {/* An execution-policy denial is the one failure here the user can
                  actually resolve, so it gets localized copy plus the switch —
                  not the backend's English sentence naming a config key. Every
                  other failure still renders the prose, which is better than
                  swallowing an unrecognized backend error. */}
              <span className="text-danger text-sm block">
                {deniedByPolicy
                  ? i18nT('pages.appDetailPage.third_party_blocked')
                  : error}
              </span>
              {deniedByPolicy ? (
                <div className="mt-2">
                  <Btn danger onClick={() => navigate('/settings?tab=security')}>
                    {i18nT('pages.appDetailPage.open_security_settings')}
                  </Btn>
                </div>
              ) : (
                // The unrecognized-failure branch above renders raw backend prose
                // and is otherwise a dead end — hand it to the agent with the
                // status/endpoint/code the journal captured at the transport.
                <div className="mt-2">
                  <AskAgentButton message={error} />
                </div>
              )}
            </div>
            <button aria-label={i18nT('pages.appDetailPage.dismiss_error')} className="text-danger/60 hover:text-danger text-sm shrink-0" onClick={clearError}><X className="lucide-inline" /></button>
          </div>
        )}

        {/* Uninstall confirmation modal */}
        {showUninstallConfirm && app && (
          // Modal backdrop: click-to-dismiss is a mouse affordance; keyboard
          // users dismiss via the Escape handler in onKeyDown below.
          // eslint-disable-next-line jsx-a11y/no-noninteractive-element-interactions
          <div className="fixed inset-0 z-[100] flex items-center justify-center bg-bg/60 backdrop-blur-sm animate-rise"
            onClick={() => setShowUninstallConfirm(false)}
            onKeyDown={e => { if (e.key === 'Escape') setShowUninstallConfirm(false) }}
            tabIndex={-1} ref={el => el?.focus()} role="dialog" aria-modal="true" aria-label={i18nT('pages.appDetailPage.confirm_uninstall')}
          >
            {/* Presentational wrapper: stops backdrop-dismiss on inner clicks. */}
            {/* eslint-disable-next-line jsx-a11y/no-static-element-interactions, jsx-a11y/click-events-have-key-events */}
            <div className="bg-card border border-border rounded-xl p-6 max-w-sm w-full mx-4 shadow-xl" onClick={e => e.stopPropagation()}>
              <div className="flex items-center gap-3 mb-4">
                <div className="w-10 h-10 rounded-xl bg-danger/10 flex items-center justify-center">
                  <Trash2 size={20} className="text-danger" />
                </div>
                <div>
                  <div className="font-medium text-text">{i18nT('pages.appDetailPage.uninstall')} {appDisplayName(app)}?</div>
                  <div className="text-[12px] text-muted">{i18nT('pages.appDetailPage.v')}{app.installedVersion || app.version}</div>
                </div>
              </div>

              <p className="text-[13px] text-muted mb-4">{i18nT('pages.appDetailPage.this_will_remove_the_app_and_all_its_registered')}</p>

              <label htmlFor="keep-app-data" className="flex items-center gap-2 text-[13px] text-muted mb-5 cursor-pointer select-none">
                <input id="keep-app-data" type="checkbox" checked={keepData} onChange={e => setKeepData(e.target.checked)} className="rounded" aria-label={i18nT('pages.appDetailPage.keep_app_data')} />
                {i18nT('pages.appDetailPage.keep_app_data')}
              </label>

              <div className="flex items-center gap-2 justify-end">
                <Btn onClick={() => setShowUninstallConfirm(false)}>{i18nT('pages.appDetailPage.cancel')}</Btn>
                <Btn danger onClick={confirmUninstall} disabled={actionLoading === 'uninstall'}>
                  {actionLoading === 'uninstall' ? i18nT('pages.appDetailPage.removing') : i18nT('pages.appDetailPage.uninstall')}
                </Btn>
              </div>
            </div>
          </div>
        )}

        {/* Hero banner (only when the app ships one) */}
        {heroSrc && (
          <div className={`w-full ${heroIsDetail ? 'aspect-[25/6]' : 'aspect-video'} max-h-72 rounded-2xl border border-border overflow-hidden mb-6 bg-[var(--card)]`}>
            {/* onError is an image-load lifecycle handler (hide broken images). */}
            {/* eslint-disable-next-line jsx-a11y/no-noninteractive-element-interactions */}
            <img
              src={heroSrc}
              alt=""
              className="w-full h-full object-cover"
              onError={e => { (e.currentTarget as HTMLImageElement).style.display = 'none' }}
            />
          </div>
        )}

        {/* Hero */}
        <div className="flex items-start gap-5 mb-6">
          <div className="w-24 h-24 rounded-2xl bg-accent/10 flex items-center justify-center shrink-0 overflow-hidden">
            <AppIcon icon={app.icon} iconUrl={app.iconUrl} size={64} />
          </div>
          <div className="flex-1 min-w-0">
            <div className="flex items-center gap-3 mb-1 flex-wrap">
              <span className="text-xl font-medium text-text">{appDisplayName(app)}</span>
              {app.installed && isBuiltin && <Badge variant="aim">{i18nT('pages.appDetailPage.built_in')}</Badge>}
              {app.installed && isBuiltin && <Badge variant={app.enabled ? 'ok' : 'warn'}>{app.enabled ? i18nT('pages.appDetailPage.enabled') : i18nT('pages.appDetailPage.disabled')}</Badge>}
              {app.installed && isSelfManaged && !isBuiltin && <Badge variant="ok">{i18nT('pages.appDetailPage.self_managed')}</Badge>}
              {app.installed && !isSelfManaged && !isBuiltin && <Badge variant={app.enabled ? 'ok' : 'warn'}>{app.enabled ? i18nT('pages.appDetailPage.enabled') : i18nT('pages.appDetailPage.disabled')}</Badge>}
            </div>
            <div className="text-[13px] text-muted mb-3">{app.author} {i18nT('pages.appDetailPage.v_2')}{app.version}</div>

            {/* Actions */}
            <div className="flex items-center gap-2 flex-wrap">
              {!app.installed && !clientInstall && (
                <Btn primary onClick={handleInstall} disabled={actionLoading === 'install'}>
                  {actionLoading === 'install' ? <><Loader2 size={14} className="animate-spin" /> {i18nT('pages.appDetailPage.installing')}</> : <><Download size={14} /> {i18nT('pages.appDetailPage.install')}</>}
                </Btn>
              )}
              {!app.installed && clientInstall && (
                <div className="text-[13px] text-muted flex items-center gap-1.5"><Monitor size={14} /> {i18nT('pages.appDetailPage.requires_local_install')}</div>
              )}
              {app.installed && isBuiltin && (
                <>
                  {app.enabled ? (
                    <Btn onClick={() => handleAction('disable')} disabled={actionLoading === 'disable'}><PowerOff size={14} /> {i18nT('pages.appDetailPage.disable')}</Btn>
                  ) : (
                    /* Enable stays available in a browser: enabling is a
                       server-side state change (backend, agents and crons run
                       in the gateway) and only the app's own window needs the
                       desktop shell. The requirement is shown in TEXT here (not
                       only a hover title): a tooltip is unreachable by touch or
                       keyboard, and the bare "Desktop app" label reads as a
                       category tag rather than a requirement — and this detail
                       page is where a user decides to enable the app. The compact
                       store row / feature card keep the badge alone (the decision
                       does not happen there). */
                    <>
                    <Btn onClick={() => handleAction('enable')} disabled={actionLoading === 'enable'}><Power size={14} /> {i18nT('pages.appDetailPage.enable')}</Btn>
                    {desktopOnly && (
                    <span className="text-[13px] text-muted flex items-center gap-1.5">
                      <Monitor size={14} /> {i18nT('pages.appDetailPage.desktop_app_hint')}
                    </span>
                  )}
                    </>
                  )}
                </>
              )}
              {app.installed && isSelfManaged && !isBuiltin && (
                <>
                  <div className="text-[13px] text-ok flex items-center gap-1.5"><Check size={14} /> {i18nT('pages.appDetailPage.installed_version', { version: app.installedVersion })}</div>
                  {app.updateAvailable && <Btn onClick={handleInstall} disabled={actionLoading === 'install'} className="!bg-[var(--info)] !text-white hover:!opacity-80">{actionLoading === 'install' ? <><Loader2 size={14} className="animate-spin" /> {i18nT('pages.appDetailPage.updating')}</> : <><ArrowUp size={14} /> {i18nT('pages.appDetailPage.update')}</>}</Btn>}
                  {canUninstall && <Btn danger onClick={() => handleAction('uninstall')} disabled={actionLoading === 'uninstall'} title={i18nT('pages.appDetailPage.removes_kirocrew_metadata_only_the_app_itself_is')}><Trash2 size={14} /> {i18nT('pages.appDetailPage.uninstall')}</Btn>}
                </>
              )}
              {app.installed && !isSelfManaged && !isBuiltin && (
                <>
                  {app.enabled ? (
                    <Btn onClick={() => handleAction('disable')} disabled={actionLoading === 'disable'}><PowerOff size={14} /> {i18nT('pages.appDetailPage.disable')}</Btn>
                  ) : (
                    /* Enable stays available in a browser: enabling is a
                       server-side state change (backend, agents and crons run
                       in the gateway) and only the app's own window needs the
                       desktop shell. Replacing the button with a static claim
                       left every browser user at a dead end — and this page is
                       where store rows land, so it was the common path. Same
                       pattern as AppListRow / FeatureCard. */
                    <>
                    <Btn onClick={() => handleAction('enable')} disabled={actionLoading === 'enable'}><Power size={14} /> {i18nT('pages.appDetailPage.enable')}</Btn>
                    {desktopOnly && (
                    /* The consequence is VISIBLE here, not only in `title`. A
                       hover tooltip does not exist on touch and is not reachable
                       by keyboard, and on its own the bare "Desktop app" label
                       reads as a category tag rather than a requirement — so the
                       one place a user decides whether to enable the app was the
                       one place the requirement could go unread. The compact
                       store row and feature card keep the badge alone: every
                       other badge in those components works that way, and the
                       decision does not happen there. */
                    <span className="text-[13px] text-muted flex items-center gap-1.5">
                      <Monitor size={14} /> {i18nT('pages.appDetailPage.desktop_app_hint')}
                    </span>
                  )}
                    </>
                  )}
                  {canUpdate && app.updateAvailable && <Btn onClick={handleInstall} disabled={actionLoading === 'install'} className="!bg-[var(--info)] !text-white hover:!opacity-80">{actionLoading === 'install' ? <><Loader2 size={14} className="animate-spin" /> {i18nT('pages.appDetailPage.updating')}</> : <><ArrowUp size={14} /> {i18nT('pages.appDetailPage.update')}</>}</Btn>}
                  {canUpdate && !app.updateAvailable && <Btn onClick={() => handleAction('update')} disabled={actionLoading === 'update'} title={i18nT('pages.appDetailPage.sync_app_from_its_source_directory')}><RefreshCw size={14} /> {i18nT('pages.appDetailPage.sync')}</Btn>}
                  {canUninstall && <Btn danger onClick={() => handleAction('uninstall')} disabled={actionLoading === 'uninstall'}><Trash2 size={14} /> {i18nT('pages.appDetailPage.uninstall')}</Btn>}
                </>
              )}
            </div>
          </div>
        </div>

        {/* Install log (inline, between hero and description) */}
        {showInstallLog && (
          <Card>
            <div className="flex items-center justify-between mb-2">
              <div className="flex items-center gap-2">
                {!installDone && <Loader2 size={14} className="animate-spin text-accent" />}
                {installDone && !error && <Check size={14} className="text-ok" />}
                {installDone && error && <X size={14} className="text-danger" />}
                <CardTitle>
                  {!installDone ? i18nT('pages.appDetailPage.installing') : error ? i18nT('pages.appDetailPage.install_failed') : i18nT('pages.appDetailPage.install_complete')}
                </CardTitle>
              </div>
              <div className="flex items-center gap-2">
                {installDone && error && (
                  <Btn onClick={() => {
                    const appSourcePath = `~/.kiro/crew/app-sources/${app?.name || name}/`
                    const msg = [
                      `App "${app?.displayName || name}" installation failed. Error log:`,
                      '',
                      '```',
                      installLog.slice(-2000),
                      '```',
                      '',
                      `The app source is at: ${appSourcePath}`,
                      `Read the README.md and any setup instructions in that directory, then fix the environment and complete the installation.`,
                    ].join('\n')
                    openChatWithMessage(msg)
                  }}>
                    <Sparkles size={14} /> {i18nT('pages.appDetailPage.fix_with_ai')}
                  </Btn>
                )}
                {installDone && (
                  <button className="text-muted hover:text-text transition-colors p-1" onClick={() => setShowInstallLog(false)} aria-label={i18nT('pages.appDetailPage.close')}>
                    <X size={14} />
                  </button>
                )}
              </div>
            </div>
            <pre
              ref={installLogRef}
              className="bg-bg border border-border rounded-lg p-3 text-[12px] text-muted whitespace-pre-wrap font-mono max-h-64 overflow-y-auto"
            >{installLog || i18nT('pages.appDetailPage.starting_install')}</pre>
          </Card>
        )}

        {/* Client install instructions */}
        {clientInstall && clientInstall.shell && (() => {
          // Replace template variables with actual values
          const gatewayUrl = window.location.origin
          const gatewayHost = serverHostname || '<your-cloud-desktop-host>'
          const replaceVars = (s: string) => s
            .replace(/\{\{gateway_url\}\}/g, gatewayUrl)
            .replace(/\{\{gateway_host\}\}/g, gatewayHost)
          const resolvedShell = replaceVars(clientInstall.shell!)
          const resolvedPostInstall = replaceVars(clientInstall.postInstall || '')
          return (
          <Card>
            <div className="flex items-start gap-3">
              <div className="w-10 h-10 rounded-xl bg-accent/10 flex items-center justify-center shrink-0 mt-0.5">
                <Terminal size={20} className="text-accent" />
              </div>
              <div className="flex-1 min-w-0">
                <div className="font-medium text-text mb-1">{i18nT('pages.appDetailPage.install_on_your_mac')}</div>
                <p className="text-[13px] text-muted mb-3">
                  {i18nT('pages.appDetailPage.this_app_requires_macos_and_needs_to_be_installe')}
                </p>
                <div className="relative group/cmd">
                  <pre className="bg-bg border border-border rounded-lg p-3 pr-10 text-[13px] font-mono text-text overflow-x-auto whitespace-pre-wrap break-all">{resolvedShell}</pre>
                  <button
                    className="absolute top-2 right-2 p-1.5 rounded-md bg-bg-elevated border border-border text-muted hover:text-text hover:border-accent/40 transition-all opacity-0 group-hover/cmd:opacity-100"
                    aria-label={i18nT('pages.appDetailPage.copy_command')}
                    onClick={() => {
                      navigator.clipboard.writeText(resolvedShell)
                      setCopied(true)
                      setTimeout(() => setCopied(false), 2000)
                    }}
                  >
                    {copied ? <Check size={14} className="text-ok" /> : <Copy size={14} />}
                  </button>
                </div>
                {resolvedPostInstall && (
                  <p className="text-[12px] text-muted mt-2">
                    {i18nT('pages.appDetailPage.after_installation_run')} <code className="bg-bg-elevated px-1.5 py-0.5 rounded text-[12px]">{resolvedPostInstall}</code>
                  </p>
                )}
                <p className="text-[12px] text-muted mt-2">
                  {i18nT('pages.appDetailPage.once_launched_the_app_will_automatically_connect')}
                </p>
              </div>
            </div>
          </Card>
          )
        })()}

        {/* Description */}
        <Card>
          <p className="text-sm text-muted leading-relaxed">{appDescription(app)}</p>
        </Card>

        {/* Screenshots */}
        <ScreenshotGallery screenshots={(() => {
          const dark = app.screenshotsDark || []
          const light = app.screenshots || []
          return resolvedMode === 'dark' && dark.length ? dark : light
        })()} />

        {/* Features */}
        {(app.highlights || []).length > 0 && (
          <Card>
            <CardTitle>{i18nT('pages.appDetailPage.features')}</CardTitle>
            <div className="grid gap-2 mt-2">
              {appHighlights(app).map((h, i) => (
                <div key={i} className="flex items-start gap-2.5 text-[13px] text-text">
                  <Check size={13} className="text-ok mt-0.5 shrink-0" />
                  <span>{h}</span>
                </div>
              ))}
            </div>
          </Card>
        )}

        {/* Info grid */}
        <div className="grid grid-cols-[repeat(auto-fit,minmax(280px,1fr))] gap-4 mt-4">
          {/* Permissions (transparency) */}
          {app.manifest?.permissions && (
            <Card>
              <CardTitle>{i18nT('pages.appDetailPage.permissions')}</CardTitle>
              <div className="grid gap-2 mt-2 text-[13px]">
                {(app.manifest.permissions.api || []).length > 0 && (
                  <div>
                    <div className="text-muted text-[11px] uppercase tracking-wider mb-1">{i18nT('pages.appDetailPage.api_access')}</div>
                    <div className="flex flex-wrap gap-1">
                      {(app.manifest.permissions.api || []).map((p: string) => (
                        <code key={p} className="bg-bg-elevated border border-border px-1.5 py-0.5 rounded text-[11px] text-text">{p}</code>
                      ))}
                    </div>
                  </div>
                )}
                {(app.manifest.permissions.events || []).length > 0 && (
                  <div>
                    <div className="text-muted text-[11px] uppercase tracking-wider mb-1">{i18nT('pages.appDetailPage.websocket_events')}</div>
                    <div className="flex flex-wrap gap-1">
                      {(app.manifest.permissions.events || []).map((e: string) => (
                        <code key={e} className="bg-bg-elevated border border-border px-1.5 py-0.5 rounded text-[11px] text-text">{e}</code>
                      ))}
                    </div>
                  </div>
                )}
                {(app.manifest.permissions.mcpTools || []).length > 0 && (
                  <div>
                    <div className="text-muted text-[11px] uppercase tracking-wider mb-1">{i18nT('pages.appDetailPage.mcp_tools')}</div>
                    <div className="flex flex-wrap gap-1">
                      {(app.manifest.permissions.mcpTools || []).map((t: string) => (
                        <code key={t} className="bg-ok-subtle border border-ok/20 px-1.5 py-0.5 rounded text-[11px] text-ok">{t}</code>
                      ))}
                    </div>
                  </div>
                )}
                <div className="flex flex-wrap gap-3 text-[12px] text-muted mt-1">
                  {app.manifest.permissions.storage && <span className="flex items-center gap-1">{i18nT('pages.appDetailPage.storage_yes')}</span>}
                  {app.manifest.permissions.cron && <span className="flex items-center gap-1">{i18nT('pages.appDetailPage.cron_yes')}</span>}
                  {app.manifest.permissions.network && <span className="flex items-center gap-1">{i18nT('pages.appDetailPage.network_yes')}</span>}
                  {app.manifest.permissions.memory && <span className="flex items-center gap-1">{i18nT('pages.appDetailPage.memory')} {String(app.manifest.permissions.memory)}</span>}
                </div>
              </div>
            </Card>
          )}

          {/* MCP Servers */}
          {app.manifest?.mcpServers && Object.keys(app.manifest.mcpServers).length > 0 && (
            <Card>
              <CardTitle>{i18nT('pages.appDetailPage.mcp_servers')}</CardTitle>
              <div className="grid gap-2 mt-2 text-[13px]">
                {Object.entries(app.manifest.mcpServers).map(([sName, sConfig]) => (
                  <div key={sName} className="bg-bg-elevated border border-border rounded-md px-2.5 py-2">
                    <div className="font-mono font-medium text-text text-[12px]">{sName}</div>
                    {sConfig.url && <div className="text-muted text-[11px] mt-0.5">{sConfig.url}</div>}
                    {sConfig.command && <div className="text-muted text-[11px] mt-0.5">{sConfig.command}</div>}
                    {(sConfig.autoApprove || []).length > 0 && (
                      <div className="flex flex-wrap gap-1 mt-1.5">
                        {(sConfig.autoApprove || []).map((t: string) => (
                          <span key={t} className="bg-ok-subtle border border-ok/20 px-1 py-0 rounded text-[10px] text-ok">{t}</span>
                        ))}
                      </div>
                    )}
                  </div>
                ))}
              </div>
            </Card>
          )}

          {/* Tags */}
          {(app.tags || []).length > 0 && (
            <Card>
              <CardTitle>{i18nT('pages.appDetailPage.tags')}</CardTitle>
              <div className="flex items-center gap-1.5 flex-wrap mt-2">
                {app.tags!.map(t => (
                  <span key={t} className="bg-bg-elevated border border-border px-2 py-0.5 rounded text-[11px] text-muted">{t}</span>
                ))}
              </div>
            </Card>
          )}

          {/* Resources (installed only) */}
          {app.installed && (agentCount > 0 || skillCount > 0 || cronCount > 0) && (
            <Card>
              <CardTitle>{i18nT('pages.appDetailPage.resources')}</CardTitle>
              <div className="grid gap-1.5 mt-2 text-[13px]">
                {(app.manifest?.agents || []).length > 0 && (
                  <div className="flex items-start gap-2 text-muted">
                    <Bot size={13} className="mt-0.5 shrink-0" />
                    <div>{app.manifest!.agents!.map((a: string) => a.split('/').pop()?.replace('.json', '')).join(', ')}</div>
                  </div>
                )}
                {(app.manifest?.skills || []).length > 0 && (
                  <div className="flex items-start gap-2 text-muted">
                    <Zap size={13} className="mt-0.5 shrink-0" />
                    <div>{app.manifest!.skills!.map((s: string) => s.split('/').pop()).join(', ')}</div>
                  </div>
                )}
                {(app.manifest?.crons || []).length > 0 && (
                  <div className="flex items-start gap-2 text-muted">
                    <Clock size={13} className="mt-0.5 shrink-0" />
                    <div>{app.manifest!.crons!.map((c) => c.name).join(', ')}</div>
                  </div>
                )}
              </div>
            </Card>
          )}

          {/* Metadata */}
          <Card>
            <CardTitle>{i18nT('pages.appDetailPage.details')}</CardTitle>
            <div className="grid gap-1.5 mt-2 text-[13px] text-muted">
              {app.repo && <div>{i18nT('pages.appDetailPage.repository')} {app.repo}</div>}
              {app.author && <div>{i18nT('pages.appDetailPage.author')} {app.author}</div>}
              {app.installedAt && <div>{i18nT('pages.appDetailPage.installed')} {fmtDateNumeric(app.installedAt)}</div>}
              {app.origin && <div>{i18nT('pages.appDetailPage.origin')} {app.origin} {i18nT('pages.appDetailPage.resources_2')} {app.resources || 'gateway'} {i18nT('pages.appDetailPage.lifecycle')} {app.lifecycle || 'gateway'}</div>}
              {app.manifest?.minKiroCrewVersion && <div>{i18nT('pages.appDetailPage.min_kirocrew_v')}{app.manifest.minKiroCrewVersion}</div>}
              {app.platform?.os && <div>{i18nT('pages.appDetailPage.platform')} {app.platform.os.join(', ')}</div>}
            </div>
          </Card>
        </div>
      </div>
    </>
  )
}
