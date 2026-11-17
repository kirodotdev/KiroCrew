/**
 * AppPage — loads an installed app via AppHost (dynamic ESM import).
 */
import { useCallback, useEffect, useRef, useState } from 'react'
import { useParams, useNavigate } from 'react-router-dom'
import { ArrowLeft, Loader2 } from 'lucide-react'
import { api } from '../api/client'
import { isNotFoundError } from '../api/apiError'
import type { AppHostProps } from '../components/AppHost'
import ErrorNotice from '../components/ErrorNotice'
import InstalledAppSurface from '../components/InstalledAppSurface'
import { Btn } from '../components/ui'
import { useAppInstalledElsewhere } from '../hooks/useAppInstalledElsewhere'
import { SURFACE_FORMER_APPS } from '../surfaces/surfaceData'

import { i18nT } from '../i18n/t'
/** App metadata from /api/apps; null before load / when the app does not exist.
 *  The response is a superset of AppHost's prop shape — it also carries `origin`,
 *  which the builtin-redirect check below reads. */
type AppData = AppHostProps['app'] & { origin?: string }

export default function AppPage() {
  const { name } = useParams<{ name: string }>()
  const navigate = useNavigate()
  const [app, setApp] = useState<AppData | null>(null)
  const [loading, setLoading] = useState(true)
  /** A rejected fetch that was NOT a 404. Kept apart from `app === null` so a
   *  transport failure is not reported as "not found". */
  const [error, setError] = useState<string | null>(null)
  // Bumped by Retry; the load effect re-runs on it.
  const [attempt, setAttempt] = useState(0)
  // A retry swaps this view for the loading line, which unmounts the focused
  // Retry button. When that retry fails too, focus goes back to the new Retry
  // instead of staying on <body>. The press is claimed by the one load run it
  // starts, so a load a route change started never refocuses; and focus the
  // user moved elsewhere meanwhile is left alone.
  const retryRef = useRef<HTMLButtonElement>(null)
  const retryPressed = useRef(false)
  const [refocusRetry, setRefocusRetry] = useState(false)
  useEffect(() => {
    if (!refocusRetry) return
    setRefocusRetry(false)
    const focusLost = !document.activeElement || document.activeElement === document.body
    if (focusLost) retryRef.current?.focus()
  }, [refocusRetry])

  useEffect(() => {
    if (!name) return
    // Every /apps/:name shares this instance, so a switch re-runs the effect while
    // the previous app's request is still in flight. Its answer is for a page the
    // user has left: it must neither render nor redirect.
    let cancelled = false
    let redirecting = false
    const fromRetry = retryPressed.current
    retryPressed.current = false
    let failed = false
    setError(null)
    setLoading(true)
    api.getApp(name)
      .then((data: AppData) => {
        if (cancelled) return
        // Native builtin apps have a registered surface at their bare route —
        // redirect there. Builtins that ship a dynamic UI bundle (manifest.ui.entry)
        // have no native surface and render via AppHost below, like installed apps.
        // A builtin skipped at boot has no routes behind that surface, so it stays
        // here and AppHost shows why it is unavailable.
        // A built-in that became a core surface opens that surface.
        const surfaceRoute = data?.origin === 'builtin' ? SURFACE_FORMER_APPS.get(name) : undefined
        if (surfaceRoute) {
          redirecting = true
          navigate(surfaceRoute, { replace: true })
          return
        }
        if (data?.origin === 'builtin' && !data?.unavailable && !data?.manifest?.ui?.entry && data?.manifest?.ui?.pages?.[0]?.route) {
          redirecting = true
          navigate(data.manifest.ui.pages[0].route, { replace: true })
          return
        }
        setApp(data)
      })
      .catch((e: unknown) => {
        if (cancelled) return
        // The old launch URL of a built-in that became a core surface (its record
        // retired, nothing installed under the name) opens that surface.
        const surfaceRoute = isNotFoundError(e) ? SURFACE_FORMER_APPS.get(name) : undefined
        if (surfaceRoute) {
          redirecting = true
          navigate(surfaceRoute, { replace: true })
          return
        }
        setApp(null)
        // Only a 404 means the app is absent; anything else is a failure to say so.
        if (!isNotFoundError(e)) {
          setError(e instanceof Error ? e.message : String(e))
          failed = true
        }
      })
      .finally(() => {
        if (cancelled || redirecting) return
        setLoading(false)
        // Same batch as the loading flip, so the effect sees the Retry mounted.
        if (fromRetry && failed) setRefocusRetry(true)
      })
    return () => { cancelled = true }
  }, [name, navigate, attempt])

  // The "not installed" view re-reads the record when the gateway announces that
  // this app's files were put in place, so an install made elsewhere shows here.
  const rereadApp = useCallback(() => setAttempt(a => a + 1), [])
  useAppInstalledElsewhere(!loading && !error && app === null && name ? name : null, rereadApp)

  if (loading) {
    // A status region, so a screen reader hears that the app is loading: the
    // page it replaces is gone, and nothing else on screen says what is next.
    return (
      <div role="status" className="flex-1 flex items-center justify-center text-muted text-sm">
        <Loader2 size={16} className="animate-spin mr-2" aria-hidden="true" /> {i18nT('pages.appPage.loading_app')}
      </div>
    )
  }

  if (error) {
    // Nothing on this shell is editable, so the hand-off loses nothing. Retry
    // and a way back sit beside it so the failure is not a dead end.
    return (
      <div className="flex-1 p-4 flex flex-col gap-3">
        <ErrorNotice title={i18nT('pages.appPage.failed_to_load_app')} message={error} askAgent />
        <div className="flex items-center gap-2">
          <Btn ref={retryRef} onClick={() => { retryPressed.current = true; setAttempt(a => a + 1) }}>{i18nT('pages.appPage.retry')}</Btn>
          <Btn onClick={() => navigate('/apps')}><ArrowLeft size={14} /> {i18nT('pages.appPage.back_to_apps')}</Btn>
        </div>
      </div>
    )
  }

  return <InstalledAppSurface app={app} requestedName={name} />
}
