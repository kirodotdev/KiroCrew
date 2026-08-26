/**
 * AppPage — loads an installed app via AppHost (dynamic ESM import).
 */
import { useEffect, useState } from 'react'
import { useParams, useNavigate } from 'react-router-dom'
import { Loader2 } from 'lucide-react'
import { api } from '../api/client'
import AppHost from '../components/AppHost'
import type { AppHostProps } from '../components/AppHost'

import { i18nT } from '../i18n/t'
/** App metadata from /api/apps; null before load / on fetch failure. The
 *  response is a superset of AppHost's prop shape — it also carries `origin`,
 *  which the builtin-redirect check below reads. */
type AppData = AppHostProps['app'] & { origin?: string }

export default function AppPage() {
  const { name } = useParams<{ name: string }>()
  const navigate = useNavigate()
  const [app, setApp] = useState<AppData | null>(null)
  const [loading, setLoading] = useState(true)

  useEffect(() => {
    if (!name) return
    let redirecting = false
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
      .catch(() => setApp(null))
      .finally(() => {
        if (!redirecting) setLoading(false)
      })
  }, [name, navigate])

  if (loading) {
    return (
      <div className="flex-1 flex items-center justify-center text-muted text-sm">
        <Loader2 size={16} className="animate-spin mr-2" /> {i18nT('pages.appPage.loading_app')}
      </div>
    )
  }

  // AppHost internally guards a null `app` (renders "not found"); its prop type
  // is non-null, so cast the nullable state through — behavior is unchanged.
  return <AppHost app={app as AppData} />
}
