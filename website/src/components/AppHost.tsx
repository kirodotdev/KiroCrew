/**
 * AppHost — dynamically loads and renders a KiroCrew app via ESM import.
 *
 * A single component that:
 * 1. Reads the app manifest to get permissions
 * 2. Creates a permission-scoped API context via AppApiProvider
 * 3. Dynamically imports the app's ESM bundle
 * 4. Renders it inside an ErrorBoundary + Suspense
 *
 * The app is a normal React component in the same tree — no iframes,
 * no Web Components, no Shadow DOM.
 */
import { Suspense, lazy, useMemo, useState, useCallback, useEffect } from 'react'
import React from 'react'
import { useNavigate } from 'react-router-dom'
import { ArrowLeft, RefreshCw, PowerOff, AlertTriangle, Package, Bot } from 'lucide-react'
import { AppApiProvider } from '../app-sdk'
import { ContentSkeleton, Btn, PageHeader } from './ui'

import { i18nT } from '../i18n/t'
import { appDisplayName, appDescription } from './appstore/appManifest'
// ---------------------------------------------------------------------------
// Types
// ---------------------------------------------------------------------------

export interface AppHostProps {
  /** Installed app metadata (from /api/apps). */
  app: {
    name: string
    version?: string
    displayName?: string
    enabled?: boolean
    manifest?: {
      name?: string
      version?: string
      displayName?: string
      description?: string
      ui?: {
        entry?: string
        pages?: { route: string; label: string; icon?: string }[]
      }
      permissions?: {
        api?: string[]
        events?: string[]
      }
    }
  }
}

// ---------------------------------------------------------------------------
// Error boundary (class component — React requirement)
// ---------------------------------------------------------------------------

interface EBProps { appName: string; onReset: () => void; children: React.ReactNode }
interface EBState { error: Error | null }

class AppErrorBoundary extends React.Component<EBProps, EBState> {
  state: EBState = { error: null }

  static getDerivedStateFromError(error: Error): EBState {
    return { error }
  }

  componentDidCatch(error: Error, info: React.ErrorInfo) {
    // eslint-disable-next-line no-console -- surface app crashes for debugging
    console.error(`[AppHost] ${this.props.appName} crashed:`, error, info.componentStack)
  }

  render() {
    if (this.state.error) {
      return (
        <AppCrashFallback
          appName={this.props.appName}
          error={this.state.error}
          onRetry={() => { this.setState({ error: null }); this.props.onReset() }}
        />
      )
    }
    return this.props.children
  }
}

// ---------------------------------------------------------------------------
// Crash fallback
// ---------------------------------------------------------------------------

function AppCrashFallback({
  appName,
  error,
  onRetry,
}: {
  appName: string
  error: Error
  onRetry: () => void
}) {
  const navigate = useNavigate()
  return (
    <>
      <PageHeader title={appName} subtitle={i18nT('components.appHost.app_crashed')} />
      <div className="flex-1 flex items-center justify-center p-8">
        <div className="text-center max-w-md">
          <AlertTriangle size={48} className="text-danger mx-auto mb-4" />
          <h3 className="text-text font-medium mb-2">{appName} {i18nT('components.appHost.encountered_an_error')}</h3>
          <p className="text-sm text-muted mb-2">{error.message}</p>
          <p className="text-[12px] text-muted/60 mb-6 font-mono break-all max-h-24 overflow-y-auto">
            {error.stack?.split('\n').slice(0, 4).join('\n')}
          </p>
          <div className="flex gap-2 justify-center">
            <Btn onClick={onRetry}><RefreshCw size={14} /> {i18nT('components.appHost.retry')}</Btn>
            <Btn onClick={() => navigate('/apps')}><ArrowLeft size={14} /> {i18nT('components.appHost.apps')}</Btn>
          </div>
        </div>
      </div>
    </>
  )
}

// ---------------------------------------------------------------------------
// Loading skeleton
// ---------------------------------------------------------------------------

function AppLoadingSkeleton({ appName }: { appName: string }) {
  return (
    <div className="px-6 pt-6 pb-8">
      <div className="skeleton h-7 w-48 rounded mb-2" />
      <div className="skeleton h-4 w-72 rounded mb-6" />
      <ContentSkeleton rows={6} />
      <p className="text-[12px] text-muted mt-4">{i18nT('components.appHost.loading')} {appName}…</p>
    </div>
  )
}

// ---------------------------------------------------------------------------
// Not found / not enabled
// ---------------------------------------------------------------------------

function AppNotFound({ name }: { name: string }) {
  const navigate = useNavigate()
  return (
    <>
      <PageHeader title={i18nT('components.appHost.app_not_found')} subtitle={i18nT('components.appHost.not_installed', { name })} />
      <div className="flex-1 flex items-center justify-center p-8">
        <div className="text-center">
          <div className="text-[48px] mb-4 opacity-20"><Package size={48} /></div>
          <p className="text-muted mb-4">{i18nT('components.appHost.install_it_from_apps_or_via_cli')}</p>
          <Btn onClick={() => navigate('/apps')}><ArrowLeft size={14} /> {i18nT('components.appHost.apps')}</Btn>
        </div>
      </div>
    </>
  )
}

function AppDisabled({ app }: { app: AppHostProps['app'] }) {
  const navigate = useNavigate()
  return (
    <>
      <PageHeader
        title={appDisplayName(app)}
        subtitle={i18nT('components.appHost.this_app_is_disabled')}
      />
      <div className="flex-1 flex items-center justify-center p-8">
        <div className="text-center max-w-md">
          <PowerOff size={48} className="text-muted mx-auto mb-4 opacity-30" />
          <p className="text-sm text-muted mb-4">{i18nT('components.appHost.enable_this_app_from_apps_to_use_it')}</p>
          <Btn onClick={() => navigate('/apps')}><ArrowLeft size={14} /> {i18nT('components.appHost.apps')}</Btn>
        </div>
      </div>
    </>
  )
}

function AppNoUI({ app }: { app: AppHostProps['app'] }) {
  const navigate = useNavigate()
  return (
    <>
      <PageHeader
        title={appDisplayName(app)}
        subtitle={appDescription({ name: app.name, description: app.manifest?.description })}
      />
      <div className="flex-1 flex items-center justify-center p-8">
        <div className="text-center max-w-md">
          <div className="text-[48px] mb-4 opacity-20"><Bot size={48} /></div>
          <h3 className="text-text font-medium mb-2">{i18nT('components.appHost.agent_only_app')}</h3>
          <p className="text-sm text-muted mb-4">
            {i18nT('components.appHost.this_app_provides_agents_and_skills_but_no_visua')}
          </p>
          <Btn onClick={() => navigate('/apps')}><ArrowLeft size={14} /> {i18nT('components.appHost.apps')}</Btn>
        </div>
      </div>
    </>
  )
}

// ---------------------------------------------------------------------------
// AppHost
// ---------------------------------------------------------------------------

function AppHostInner({ app }: AppHostProps) {
  const navigate = useNavigate()
  const [resetKey, setResetKey] = useState(0)

  // App dev-mode live reload: the gateway broadcasts app_reload when a
  // dev-flagged app's ui/ files change (useWebSocket re-dispatches it as a
  // window CustomEvent). We do a FULL PAGE RELOAD rather than only bumping the
  // entry-module import URL: cache-busting just the entry leaves its statically
  // imported dependency chunks pinned to their original URLs, and the browser's
  // ESM module map is keyed by URL — so edits confined to a chunk would stay
  // stale. A page reload re-fetches every module (the app's assets are served
  // no-store in dev mode), guaranteeing the latest bytes across the whole graph.
  useEffect(() => {
    const handler = (e: Event) => {
      const detail = (e as CustomEvent).detail as { app?: string } | undefined
      if (detail?.app === app.name) window.location.reload()
    }
    window.addEventListener('mc:app-reload', handler)
    return () => window.removeEventListener('mc:app-reload', handler)
  }, [app.name])

  const entry = app.manifest?.ui?.entry
  const permissions = app.manifest?.permissions || {}
  const allowedApi = permissions.api || []
  const allowedEvents = permissions.events || []
  const bundlePath = `/apps/${app.name}/ui/${entry}`

  // Dynamically import the app bundle. Keyed on resetKey so retry works.
  const LazyApp = useMemo(
    () => lazy(() =>
      import(/* @vite-ignore */ bundlePath).catch(err => {
        // eslint-disable-next-line no-console -- surface bundle load failures for debugging
        console.error(`[AppHost] Failed to load ${app.name} from ${bundlePath}:`, err)
        // Return a module with a default export that shows the error
        return {
          default: () => (
            <div className="flex-1 flex items-center justify-center p-8">
              <div className="text-center max-w-md">
                <AlertTriangle size={48} className="text-danger mx-auto mb-4" />
                <h3 className="text-text font-medium mb-2">{i18nT('components.appHost.failed_to_load')} {appDisplayName(app)}</h3>
                <p className="text-sm text-muted mb-4">{err.message}</p>
                <Btn onClick={() => navigate('/apps')}><ArrowLeft size={14} /> {i18nT('components.appHost.apps')}</Btn>
              </div>
            </div>
          ),
        }
      })
    ),
    [bundlePath, resetKey] // eslint-disable-line react-hooks/exhaustive-deps
  )

  // WebSocket event subscription bridge
  // TODO: Apps currently only receive CustomEvents; integrate WebSocket subscription when app event forwarding is implemented.
  const subscribeFn = useCallback((event: string, cb: (data: unknown) => void) => {
    // For now, use a simple CustomEvent bridge.
    // Apps subscribe to events, and the host's WebSocket handler dispatches them.
    const handler = (e: Event) => cb((e as CustomEvent).detail)
    window.addEventListener(`mc:app:${event}`, handler)
    return () => window.removeEventListener(`mc:app:${event}`, handler)
  }, [])

  const navigateFn = useCallback((path: string) => navigate(path), [navigate])

  const notifyFn = useCallback((message: string, opts?: { type?: 'info' | 'success' | 'error' }) => {
    // Dispatch to host notification system
    window.dispatchEvent(new CustomEvent('mc:notify', { detail: { message, ...opts } }))
  }, [])

  return (
    <AppErrorBoundary appName={app.name} onReset={() => setResetKey(k => k + 1)}>
      <AppApiProvider
        appName={app.name}
        appVersion={app.manifest?.version || app.version}
        allowedApiPaths={allowedApi}
        allowedEvents={allowedEvents}
        subscribeFn={subscribeFn}
        navigateFn={navigateFn}
        notifyFn={notifyFn}
      >
        <Suspense fallback={<AppLoadingSkeleton appName={appDisplayName(app)} />}>
          <LazyApp />
        </Suspense>
      </AppApiProvider>
    </AppErrorBoundary>
  )
}

export default function AppHost({ app }: AppHostProps) {
  // Guard: not installed
  if (!app) return <AppNotFound name="unknown" />
  // Guard: disabled
  if (app.enabled === false) return <AppDisabled app={app} />
  // Guard: no UI bundle
  if (!app.manifest?.ui?.entry) return <AppNoUI app={app} />

  return <AppHostInner app={app} />
}
