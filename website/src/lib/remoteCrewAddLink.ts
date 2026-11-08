/**
 * The dashboard half of the desktop app's `kirocrew://remote-crew/add` link.
 *
 * The Electron resolver (`website/electron/deep-link.js`) checks the link and
 * sends this window the route `/settings/instances?host=<ssh host>[&port=<n>][&name=<label>]`
 * over the same `navigate` channel the native menu uses. `remoteCrewAddFromPath`
 * tells that route apart from an ordinary visit to Settings → Remote Crew; the
 * shell then hands the values to the add form (`prefillCrewAddForm`) and opens
 * the bare page. The values only FILL IN the form: nothing is saved or connected
 * until the user's Add click, after which the crew is one they added by hand.
 *
 * The query is read here, from the native channel, and nowhere else. Remote
 * Crew never reads its own URL, so a browser opening
 * `/settings/instances?host=…` fills in nothing.
 */

/** Settings → Remote Crew, the page the add form lives on. */
export const REMOTE_CREW_SETTINGS_PATH = '/settings/instances'

/** What an add link carries, already in the add form's own terms. */
export interface RemoteCrewAddLink {
  /** The SSH host field: `host` or `user@host`. */
  host: string
  /** The remote gateway's port, or `''` to keep the form's default. */
  port: string
  /** The crew's label, or `''` to leave the field to the user. */
  name: string
}

const LINK_KEYS = new Set(['host', 'port', 'name'])
// The resolver's own caps (SSH_HOST_MAX_LENGTH, CREW_NAME_MAX_LENGTH): a route
// over them did not come from it.
const HOST_MAX = 255
const NAME_MAX = 200
const PORT_RE = /^[1-9][0-9]{0,4}$/

/**
 * The add link a dashboard path carries, or `null` for any other path,
 * including a plain `/settings/instances` with no `host`.
 */
export function remoteCrewAddFromPath(path: string): RemoteCrewAddLink | null {
  let url: URL
  try {
    // A relative path needs some base to parse against; only the path and query
    // are read back off it.
    url = new URL(path, 'http://dashboard.invalid')
  } catch {
    return null
  }
  if (url.pathname !== REMOTE_CREW_SETTINGS_PATH) return null
  const keys = [...url.searchParams.keys()]
  if (new Set(keys).size !== keys.length || keys.some(key => !LINK_KEYS.has(key))) return null
  const host = url.searchParams.get('host') ?? ''
  const port = url.searchParams.get('port') ?? ''
  const name = url.searchParams.get('name') ?? ''
  if (!host || host.length > HOST_MAX) return null
  if (port && !(PORT_RE.test(port) && Number(port) <= 65535)) return null
  if (Array.from(name).length > NAME_MAX) return null
  return { host, port, name }
}
