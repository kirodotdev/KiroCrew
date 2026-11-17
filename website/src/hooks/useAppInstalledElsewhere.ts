import { useEffect } from 'react'

/**
 * Lets a "not installed" view hear that its app was installed from somewhere else.
 *
 * The gateway sends the `app_reload` frame, which the dashboard socket
 * re-dispatches as `mc:app-reload`, after every route that put an app's files in
 * place: an install from the CLI, another tab or the App Store. A mounted AppHost
 * answers it by reloading the window. A view that says the app is absent has no
 * host, so without this it keeps saying so until a reload or a focus change.
 *
 * For an announcement that names `name`, this announces `mc:apps-changed`, so
 * the shell re-reads the installed-apps list (the rail and the legacy-route
 * lookup), then calls `onInstalled`, so the view re-reads its own record. A null
 * `name` listens to nothing. Pass a stable `onInstalled`: a new one re-subscribes.
 */
export function useAppInstalledElsewhere(name: string | null, onInstalled?: () => void): void {
  useEffect(() => {
    if (name === null) return
    const handler = (e: Event) => {
      const app = (e as CustomEvent<{ app?: unknown } | undefined>).detail?.app
      if (app !== name) return
      window.dispatchEvent(new Event('mc:apps-changed'))
      onInstalled?.()
    }
    window.addEventListener('mc:app-reload', handler)
    return () => window.removeEventListener('mc:app-reload', handler)
  }, [name, onInstalled])
}
