/**
 * An app's web origin: `http://<app>.apps.localhost:<dashboard port>`, which the
 * gateway serves for an enabled app whose manifest sets `backend.webOrigin: true`
 * (`apps/app_web_origin.py`). The browser resolves every `*.localhost` name to its
 * own loopback, so the origin rides whatever connection carries the dashboard,
 * including one `ssh -L` forward from another machine.
 *
 * Only a dashboard opened on one of its own loopback names over plain http has
 * one: the gateway serves app web origins only to direct local peers, and a
 * tunnel or published HTTPS host has no wildcard name to offer. A Remote Crew
 * pane is on its hub's own loopback name, so it has one too: the crew's gateway
 * serves the pane's forward port while the pane's signed token has recorded it.
 * Everywhere else this answers `null`, and the app keeps whatever it did before;
 * `appWebOriginStatus` says why. The answer reads the dashboard's host name only,
 * so an app probes the origin (a `no-cors` fetch) before it switches its frame.
 * App Kit seam level 8.
 */

/** The characters of one DNS label, as the gateway requires of the app name. */
const LABEL_CHARS = new Set('abcdefghijklmnopqrstuvwxyz0123456789-')
const LABEL_MAX_LENGTH = 63

/** One DNS label: 1 to 63 of `a-z0-9-`, not starting or ending with `-`. */
function isDnsLabel(name: string): boolean {
  return (
    name.length >= 1
    && name.length <= LABEL_MAX_LENGTH
    && [...name].every((char) => LABEL_CHARS.has(char))
    && !name.startsWith('-')
    && !name.endsWith('-')
  )
}

/** The dashboard's own loopback names, as `location.hostname` spells them. */
// CSP frame-ancestors cannot express a bracketed IPv6 host source.
const DASHBOARD_LOOPBACK_HOSTS = new Set(['localhost', '127.0.0.1', 'kirocrew.localhost'])

type DashboardLocation = Pick<Location, 'protocol' | 'hostname' | 'port'>

/**
 * Why this dashboard does or does not offer an app web origin.
 *
 * - `loopback`: it does; the dashboard is on one of its own loopback names over
 *   plain http, which includes a Remote Crew pane, loaded on the hub's own name.
 * - `remote-front-door`: the dashboard came through a front door with no name to
 *   give an app (https, a tunnel, the relay, a published host, a phone link).
 *   Open the app's upstream page in a new tab instead of framing a loopback port,
 *   which on this viewer's device is the viewer's own machine.
 * - `ipv6`: the dashboard is on IPv6 loopback, which CSP `frame-ancestors` cannot
 *   name, so no app page could be framed there.
 * - `invalid-name`: the app name is not one DNS label.
 */
export type AppWebOriginReason = 'loopback' | 'remote-front-door' | 'ipv6' | 'invalid-name'

/** `origin` is what {@link appWebOrigin} answers; `reason` says why. */
export interface AppWebOriginStatus {
  origin: string | null
  reason: AppWebOriginReason
}

/**
 * {@link appWebOrigin} with the reason for its answer, so an app can tell "not
 * here, open upstream" from "a bad name" without parsing `location` itself.
 * It has no seam level on this release line: feature-detect it by name.
 */
export function appWebOriginStatus(appName: string, location: DashboardLocation = window.location): AppWebOriginStatus {
  if (!isDnsLabel(appName)) return { origin: null, reason: 'invalid-name' }
  const hostname = location.hostname.toLowerCase()
  // The URL parser always spells IPv6 loopback this way.
  if (hostname === '[::1]') return { origin: null, reason: 'ipv6' }
  if (location.protocol !== 'http:' || !DASHBOARD_LOOPBACK_HOSTS.has(hostname)) {
    return { origin: null, reason: 'remote-front-door' }
  }
  const port = location.port || '80'
  return { origin: `http://${appName}.apps.localhost:${port}`, reason: 'loopback' }
}

export function appWebOrigin(appName: string, location: DashboardLocation = window.location): string | null {
  return appWebOriginStatus(appName, location).origin
}
