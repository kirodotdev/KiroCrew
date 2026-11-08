import { createContext, useContext, type ReactNode } from 'react'
import { useQuery } from '@tanstack/react-query'
import { api } from '../api/client'

/**
 * `directLocal` and `desktopLauncher` are the two halves of the gate on the
 * OS-desktop file actions (see `useCanReveal` in FilePathMenu): the browser is on
 * the gateway host, and that host can open a file manager right now (the gateway
 * decides that from its own launcher and desktop session). Both default to
 * false, so a gateway that could not be read offers neither action.
 */
interface Branding { botName: string; avatar: string; directLocal: boolean; desktopLauncher: boolean }

const defaults: Branding = { botName: 'Kiro Crew', avatar: '/logo.png', directLocal: false, desktopLauncher: false }
const BrandingContext = createContext<Branding>(defaults)

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
  const b: Branding = data
    ? {
        botName: data.bot_name || defaults.botName,
        avatar: data.avatar || defaults.avatar,
        directLocal: !!data.direct_local,
        desktopLauncher: !!data.desktop_launcher,
      }
    : defaults
  return <BrandingContext.Provider value={b}>{children}</BrandingContext.Provider>
}

export const useBranding = () => useContext(BrandingContext)
