/**
 * StartupModules — mounts every admitted app's `ui.startup` component, on every route.
 *
 * The contract is `ui.startup` in `docs/app-kit/manifest-reference.md`. Each component is
 * an ordinary AppHost mount of the app's startup entry, so it gets the same SDK
 * context, error boundary and Suspense as the app's page, and renders nothing of the
 * host's own: a failed import or a throw is logged and the shell keeps running.
 *
 * The gateway decides admission on every `/api/apps` read (owner, enabled, trusted,
 * browser-only, approved at this version), so a component unmounts, running its
 * cleanups, as soon as a refreshed list stops admitting it. Keyed by name and
 * version: an update mounts the new version only after the owner approves it again.
 *
 * Nothing mounts on a `?safe=1` load or inside an embedded instance pane.
 */
import { useInstalledApps } from '../hooks/panelTabRegistry'
import { isSafeReload } from '../lib/safeReload'
import { isEmbeddedPane } from '../lib/embedded'
import AppHost, { type AppHostProps } from './AppHost'

function logFailure(name: string) {
  return (error: unknown) => {
    // eslint-disable-next-line no-console -- the only report a headless mount has
    console.error(`[StartupModules] ${name} startup module failed:`, error)
  }
}

export default function StartupModules() {
  const { apps } = useInstalledApps()
  if (isSafeReload() || isEmbeddedPane()) return null
  return (
    <>
      {apps
        // An unavailable built-in would render AppHost's visible notice, not nothing.
        .filter(app => app.enabled !== false && !app.unavailable
          && app.startup?.state === 'admitted' && app.startup.entry)
        .map(app => (
          <AppHost
            key={`${app.name}@${app.version}`}
            app={app as AppHostProps['app']}
            entry={app.startup!.entry}
            sessionKey="dashboard:ui"
            onFailure={logFailure(app.name)}
          />
        ))}
    </>
  )
}
