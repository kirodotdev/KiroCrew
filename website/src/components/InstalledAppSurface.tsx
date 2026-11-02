/**
 * InstalledAppSurface — an installed app's routed page: its own `ui.entry` bundle
 * mounted through AppHost, under the record's OWN origin.
 *
 * One mount for the two URLs an installed app's page can sit at: `/apps/<name>`
 * (AppPage, after it has fetched the record) and an adopted former built-in's
 * legacy route (BuiltinAppRoute, from the installed-apps list). AppHost derives
 * the SDK identity's origin from `app.origin`, so a registry copy never runs with
 * the `builtin` origin the compiled page at that route publishes.
 */
import AppHost from './AppHost'
import type { AppHostProps } from './AppHost'

export default function InstalledAppSurface({
  app,
  requestedName,
}: {
  /** The record to mount; `null` renders AppHost's not-installed state. */
  app: AppHostProps['app'] | null
  /** The name the route asked for, shown when `app` is absent. */
  requestedName?: string
}) {
  // AppHost guards a null `app` itself (its "not installed" state); its prop type
  // is non-null, so the nullable value is cast through. Routed pages act under the
  // dashboard-page identity the core API client uses, not whichever chat happens
  // to be selected elsewhere.
  return <AppHost app={app as AppHostProps['app']} sessionKey="dashboard:ui" requestedName={requestedName} />
}
