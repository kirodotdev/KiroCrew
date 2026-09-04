/**
 * AppPage — loads an installed app via AppHost (dynamic ESM import).
 */
import { useEffect, useState } from 'react'
import { useParams, useNavigate } from 'react-router-dom'
import { ArrowLeft, Loader2 } from 'lucide-react'
import { api } from '../api/client'
import { isNotFoundError } from '../api/apiError'
import AppHost from '../components/AppHost'
import type { AppHostProps } from '../components/AppHost'
import ErrorNotice from '../components/ErrorNotice'
import { Btn } from '../components/ui'

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

  useEffect(() => {
    if (!name) return
    let redirecting = false
    setError(null)
    setLoading(true)
    api.getApp(name)
      .then((data: AppData) => {
        // Native builtin apps have a registered surface at their bare route —
        // redirect there. Builtins that ship a dynamic UI bundle (manifest.ui.entry)
        // have no native surface and render via AppHost below, like installed apps.
        if (data?.origin === 'builtin' && !data?.manifest?.ui?.entry && data?.manifest?.ui?.pages?.[0]?.route) {
          redirecting = true
          navigate(data.manifest.ui.pages[0].route, { replace: true })
          return
        }
        setApp(data)
      })
      .catch((e: unknown) => {
        setApp(null)
        // Only a 404 means the app is absent; anything else is a failure to say so.
        if (!isNotFoundError(e)) setError(e instanceof Error ? e.message : String(e))
      })
      .finally(() => {
        if (!redirecting) setLoading(false)
      })
  }, [name, navigate, attempt])

  if (loading) {
    return (
      <div className="flex-1 flex items-center justify-center text-muted text-sm">
        <Loader2 size={16} className="animate-spin mr-2" /> {i18nT('pages.appPage.loading_app')}
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
          <Btn onClick={() => setAttempt(a => a + 1)}>{i18nT('pages.appPage.retry')}</Btn>
          <Btn onClick={() => navigate('/apps')}><ArrowLeft size={14} /> {i18nT('pages.appPage.back_to_apps')}</Btn>
        </div>
      </div>
    )
  }

  // AppHost internally guards a null `app` (renders "not found"); its prop type
  // is non-null, so cast the nullable state through — behavior is unchanged.
  return <AppHost app={app as AppData} />
}
