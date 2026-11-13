/**
 * The whole document of a desktop window the Electron shell hosts for an app.
 *
 * `main.tsx` renders this instead of the dashboard when the document path is
 * `/popout/desktop/<app>/<window>` (see `desktopWindowTarget.ts`). It mounts the
 * one ESM `entry` the app's manifest declares for that window through `AppHost`,
 * on a transparent page, with no dashboard chrome around it. Without the shell's
 * bridge, or for a window the app does not declare, or for an app that is not
 * enabled, it renders nothing and asks the shell to close the window. An app
 * list that fails to load, or a bundle that fails or throws, closes it too: the
 * shell reveals a window only once the app calls `ready()`, so a failure is
 * never painted over the desktop, and the shell's next reconcile reopens an
 * `openOnEnable` window it finds closed.
 *
 * A mounted window runs the dashboard's live socket (`useWebSocket`), as an app
 * pop-out window does through the shell it mounts. The SDK's `ChatPanel` sends
 * only while that socket is connected, and its streamed replies arrive on it.
 *
 * The router is a `MemoryRouter`. The shell answers a page message only while
 * the document URL is still this window's own path, so an app's SDK
 * `navigate()` must never move that URL: it moves the in-memory location, and
 * the page keeps rendering the one declared module.
 */
import { StrictMode, useCallback, useEffect } from 'react'
import { MemoryRouter } from 'react-router-dom'
import { Provider } from 'react-redux'
import { QueryClientProvider, useQuery } from '@tanstack/react-query'
import { api } from '../api/client'
import { queryClient } from '../api/queryClient'
import AppHost from '../components/AppHost'
import { useWebSocket } from '../hooks/useWebSocket'
import { BrandingProvider } from '../hooks/useBranding'
import { ThemeProvider } from '../hooks/useTheme'
import { LanguageProvider } from '../i18n/LanguageProvider'
import { routerBasename } from '../lib/basePath'
import { ProviderProvider } from '../providers'
import { store } from '../store'
import {
  declaredDesktopWindow,
  desktopWindowBridge,
  type DesktopWindowBridge,
  type DesktopWindowTarget,
} from './desktopWindowTarget'

/** A hosted window draws only what the app paints; the rest of it is the desktop. */
function useTransparentDocument(): void {
  useEffect(() => {
    for (const el of [document.documentElement, document.body, document.getElementById('root')]) {
      if (el) el.style.background = 'transparent'
    }
  }, [])
}

/** The live socket alone, with none of the dashboard chrome that mounts it there. */
function DesktopWindowSocket(): null {
  useWebSocket()
  return null
}

export function DesktopWindowPage({ target, bridge }: { target: DesktopWindowTarget; bridge: DesktopWindowBridge }) {
  useTransparentDocument()
  const { data: apps, isSuccess, isError, error } = useQuery({
    queryKey: ['apps'],
    queryFn: () => api.listApps(),
    staleTime: Infinity,
  })
  const app = isSuccess ? apps.find(a => a.name === target.app) : undefined
  const declared = app ? declaredDesktopWindow(app.manifest, target.window) : null
  const usable = !!app && !!declared && app.enabled !== false && !app.unavailable
  useEffect(() => {
    if (isError) {
      // eslint-disable-next-line no-console -- the window is never revealed, so the log is the only trace
      console.error(`[DesktopWindow] ${target.app}/${target.window}: could not read the app list:`, error)
    }
    if (isError || (isSuccess && !usable)) bridge.close()
  }, [isError, error, isSuccess, usable, bridge, target.app, target.window])
  const onFailure = useCallback(() => bridge.close(), [bridge])
  if (!usable || !app || !declared) return null
  return (
    <>
      <DesktopWindowSocket />
      <AppHost app={app} entry={declared.entry} onFailure={onFailure} />
    </>
  )
}

export default function DesktopWindowRoot({ target }: { target: DesktopWindowTarget }) {
  const bridge = desktopWindowBridge(target)
  if (!bridge) return null
  return (
    <StrictMode>
      <QueryClientProvider client={queryClient}>
        <Provider store={store}>
          <LanguageProvider>
            <ThemeProvider>
              <MemoryRouter basename={routerBasename()} initialEntries={[window.location.pathname]}>
                <BrandingProvider>
                  <ProviderProvider>
                    <DesktopWindowPage target={target} bridge={bridge} />
                  </ProviderProvider>
                </BrandingProvider>
              </MemoryRouter>
            </ThemeProvider>
          </LanguageProvider>
        </Provider>
      </QueryClientProvider>
    </StrictMode>
  )
}
