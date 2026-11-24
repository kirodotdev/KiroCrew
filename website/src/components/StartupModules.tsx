/**
 * StartupModules — mounts every admitted app's `ui.startup` component, on every route.
 *
 * The contract is `ui.startup` in `docs/app-kit/manifest-reference.md`. Each component is
 * an ordinary AppHost mount of the app's startup entry, so it gets the same SDK
 * context, error boundary and Suspense as the app's page. A failed import or throw
 * is logged and shown once in a dismissible notice; healthy mounts add no chrome.
 *
 * The gateway decides admission on every `/api/apps` read (owner, enabled, trusted,
 * browser-only, approved at this version), so a component unmounts, running its
 * cleanups, as soon as a refreshed list stops admitting it. Keyed by name, version,
 * entry and approval digest: re-approval at a new entry or a changed tree mounts a
 * fresh relative module graph, even when the other parts are unchanged. Older
 * gateways omit the digest; their rows keep the legacy entry URL.
 *
 * Nothing mounts on a `?safe=1` load or inside an embedded instance pane.
 */
import { useRef, useState } from 'react'
import type { MouseEvent } from 'react'
import { Link, useNavigate } from 'react-router-dom'
import { useInstalledApps } from '../hooks/panelTabRegistry'
import { i18nT } from '../i18n/t'
import { isSafeReload } from '../lib/safeReload'
import { isEmbeddedPane } from '../lib/embedded'
import AppHost, { type AppHostProps } from './AppHost'
import ErrorNotice from './ErrorNotice'
import { useGuardedLeave } from './NavigationLeaveGuard'

function isPlainPrimaryClick(e: MouseEvent<HTMLAnchorElement>): boolean {
  return e.button === 0 && !e.metaKey && !e.ctrlKey && !e.shiftKey && !e.altKey
}

export default function StartupModules() {
  const { apps } = useInstalledApps()
  const navigate = useNavigate()
  const guardedLeave = useGuardedLeave()
  const [failedApps, setFailedApps] = useState(() => new Map<string, true>())
  // Dismissing a notice must not let a later failure show it again before reload.
  const reportedApps = useRef(new Set<string>())

  function logFailure(name: string) {
    return (error: unknown) => {
      // eslint-disable-next-line no-console -- retain the diagnostic when the notice is dismissed
      console.error(`[StartupModules] ${name} startup module failed:`, error)
      if (reportedApps.current.has(name)) return
      reportedApps.current.add(name)
      setFailedApps(previous => new Map(previous).set(name, true))
    }
  }

  if (isSafeReload() || isEmbeddedPane()) return null
  return (
    <>
      {apps
        // An unavailable built-in would render AppHost's visible notice, not nothing.
        .filter(app => app.enabled !== false && !app.unavailable
          && app.startup?.state === 'admitted' && app.startup.entry)
        .map(app => (
          <AppHost
            key={`${app.name}@${app.version}:${app.startup!.entry}:${app.startup!.digest}`}
            app={app as AppHostProps['app']}
            entry={app.startup!.digest
              ? `startup:${app.startup!.digest}/${app.startup!.entry}`
              : app.startup!.entry}
            sessionKey="dashboard:ui"
            onFailure={logFailure(app.name)}
            suppressReload
          />
        ))}
      {failedApps.size > 0 && (
        <div
          className="fixed bottom-safe-offset-4 right-safe-offset-4 z-50 flex max-h-[50vh] w-[min(20rem,calc(100vw-2rem-var(--safe-area-x)-var(--safe-area-x)))] flex-col gap-2 overflow-y-auto rounded-lg bg-card p-3 shadow-lg"
          data-testid="startup-module-errors"
        >
          {/* No hand-off: the current page may hold an unsaved Settings or app form draft. */}
          {[...failedApps.keys()].map(name => (
            <ErrorNotice
              key={name}
              testId={`startup-module-error-${name}`}
              impact="minor"
              message={i18nT('components.startupModules.failed', { name })}
              dismissLabel={i18nT('components.startupModules.dismiss', { name })}
              onDismiss={() => setFailedApps(previous => {
                const remaining = new Map(previous)
                remaining.delete(name)
                return remaining
              })}
              actions={(
                <Link
                  to={`/apps/detail/${name}`}
                  className="text-minor-link underline"
                  onClick={(e: MouseEvent<HTMLAnchorElement>) => {
                    // Opening the app page unmounts the current page, which may
                    // hold an unsaved Settings or app-form draft, so ask the
                    // leave guard — the same opt-in ask SettingsLink makes. A
                    // modified or middle click opens a new tab and unmounts
                    // nothing, so it is left to the browser.
                    if (!isPlainPrimaryClick(e)) return
                    e.preventDefault()
                    const to = `/apps/detail/${name}`
                    guardedLeave(() => navigate(to), to)
                  }}
                >
                  {i18nT('components.startupModules.open_app', { name })}
                </Link>
              )}
            />
          ))}
        </div>
      )}
    </>
  )
}
