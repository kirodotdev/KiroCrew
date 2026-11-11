import { createContext, useContext, useEffect, useState, type ReactNode } from 'react'
import { useQuery } from '@tanstack/react-query'
import { api } from '../api/client'
import { safeGetItem, safeRemoveItem, safeSetItem } from '../utils/safeStorage'

/**
 * `directLocal` and `desktopLauncher` are the two halves of the gate on the
 * OS-desktop file actions (see `useCanReveal` in FilePathMenu): the browser is on
 * the gateway host, and that host can open a file manager right now (the gateway
 * decides that from its own launcher and desktop session). Both default to
 * false, so a gateway that could not be read offers neither action.
 *
 * `instanceLabel` is `dashboard.instance_label`, empty when unset.
 */
interface Branding {
  botName: string
  instanceLabel: string
  avatar: string
  directLocal: boolean
  desktopLauncher: boolean
  /** `dashboard.remote_editor.editor` and `.host` for the "Open in <editor>"
   *  row in the Markdown panel's ⋯ menu (`MarkdownPanelHeader.tsx`); "" = not set. */
  remoteEditor: string
  remoteEditorHost: string
}

const defaults: Branding = {
  botName: 'Kiro Crew',
  instanceLabel: '',
  avatar: '/logo.png',
  directLocal: false,
  desktopLauncher: false,
  remoteEditor: '',
  remoteEditorHost: '',
}
const BrandingContext = createContext<Branding>(defaults)

/** The last instance label this origin served, read by index.html to title the
 *  tab before React mounts and used here until the branding fetch lands. */
const INSTANCE_LABEL_KEY = 'mc-instance-label'

function cachedInstanceLabel(): string {
  return safeGetItem(INSTANCE_LABEL_KEY) ?? ''
}

export function BrandingProvider({ children }: { children: ReactNode }) {
  // Runs on the app-wide QueryClient (both production mounts sit inside it).
  // That client's ambient retry policy only retries ONCE for a non-429 error
  // (api/queryClient.ts retryPolicy = failureCount < 1), and with
  // staleTime: Infinity nothing refetches later — so a couple of transient
  // startup failures would strand directLocal false until a page reload. We
  // therefore pin retry: 3 on this query itself, and keep polling while there
  // is still no data, so a gateway that was down at load recovers the real
  // branding without a reload. Once data lands the interval switches off.
  const { data } = useQuery({
    queryKey: ['branding'],
    queryFn: () => api.branding(),
    retry: 3,
    staleTime: Infinity,
    refetchInterval: q => (q.state.data ? false : 30_000),
  })
  const [cachedLabel] = useState(cachedInstanceLabel)
  const instanceLabel = data?.instance_label ?? ''
  useEffect(() => {
    if (!data) return
    if (instanceLabel) safeSetItem(INSTANCE_LABEL_KEY, instanceLabel)
    else safeRemoveItem(INSTANCE_LABEL_KEY)
  }, [data, instanceLabel])
  const b: Branding = data
    ? {
        botName: data.bot_name || defaults.botName,
        instanceLabel,
        avatar: data.avatar || defaults.avatar,
        directLocal: !!data.direct_local,
        desktopLauncher: !!data.desktop_launcher,
        remoteEditor: data.remote_editor?.editor ?? '',
        remoteEditorHost: data.remote_editor?.host ?? '',
      }
    : { ...defaults, instanceLabel: cachedLabel }
  return <BrandingContext.Provider value={b}>{children}</BrandingContext.Provider>
}

export const useBranding = () => useContext(BrandingContext)
