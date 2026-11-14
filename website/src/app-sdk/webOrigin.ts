/**
 * An app's web origin: `http://<app>.apps.localhost:<dashboard port>`, which the
 * gateway serves for an enabled app whose manifest sets `backend.webOrigin: true`
 * (`apps/app_web_origin.py`). The browser resolves every `*.localhost` name to its
 * own loopback, so the origin rides whatever connection carries the dashboard,
 * including one `ssh -L` forward from another machine.
 *
 * Only a dashboard opened on one of its own loopback names over plain http has
 * one: the gateway serves app web origins only to direct local peers, and a
 * tunnel or published HTTPS host has no wildcard name to offer. Everywhere else
 * this answers `null`, and the app keeps whatever it did before. App Kit seam
 * level 8.
 */

/** One DNS label, as the gateway requires of the app name. */
const LABEL_RE = /^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$/

/** The dashboard's own loopback names, as `location.hostname` spells them. */
// CSP frame-ancestors cannot express a bracketed IPv6 host source.
const DASHBOARD_LOOPBACK_HOSTS = new Set(['localhost', '127.0.0.1', 'kirocrew.localhost'])

export function appWebOrigin(appName: string, location: Pick<Location, 'protocol' | 'hostname' | 'port'> = window.location): string | null {
  if (!LABEL_RE.test(appName)) return null
  if (location.protocol !== 'http:') return null
  if (!DASHBOARD_LOOPBACK_HOSTS.has(location.hostname.toLowerCase())) return null
  const port = location.port || '80'
  return `http://${appName}.apps.localhost:${port}`
}
