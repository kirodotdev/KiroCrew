/**
 * The credential a Remote Crew pane loads with, for the port THIS page is on.
 *
 * A pane's token carries an `embed_parent_port` claim, and the crew's CSP admits
 * only that port's loopback origins as the pane's frame ancestor. The gateway
 * mints the token it keeps for a crew with its own port, which is the page's
 * port whenever the page is served by that gateway directly. It is not when the
 * page reaches the gateway through a port forward listening on another number:
 * the desktop app's managed tunnel (`--local-port 5477 --remote-port 5476`)
 * serves this page on 5477, and a pane framed with the gateway's token names
 * 5476, so the crew's CSP refuses the frame however well the forward works.
 *
 * So every warm-writer passes the connection through `paneCredentialFor` before
 * it is stored. A token naming this page's port, or naming none, is used as it
 * is, with no further request. Otherwise the gateway is asked for one naming
 * this page's port (`POST /api/instances/{id}/embed-token`), which stores
 * nothing: the credential the gateway keeps for the crew stays minted for its
 * own port, and this one belongs to this page's pane alone.
 *
 * The claim is read off the token's payload unverified. Only the crew can verify
 * it, and does, before the port reaches its CSP; a misreading here costs one
 * extra mint or a pane that does not load, never a wider frame-ancestors set.
 */
import { api } from '../api/client'
import type { WarmConn } from '../store/instancesSlice'
import { paneLog } from './paneLog'
import { isEmbeddableLoopbackOrigin } from './tunnelOrigin'

function asPort(value: unknown): number | null {
  const n = typeof value === 'string' && value !== '' ? Number(value) : value
  if (typeof n !== 'number' || !Number.isInteger(n) || n < 1 || n > 65535) return null
  // A string claim must be the plain decimal spelling, as the crew reads it.
  return typeof value === 'string' && String(n) !== value ? null : n
}

/** The port a page on `protocol` with `port` (a `Location` pair) is served on. */
export function pagePort(protocol: string, port: string): number | null {
  if (port) return asPort(port)
  if (protocol === 'http:') return 80
  if (protocol === 'https:') return 443
  return null
}

/** The `embed_parent_port` a dashboard token names, or null when it names none. */
export function tokenParentPort(token: string): number | null {
  const payload = token.split('.')[0]
  if (!payload) return null
  try {
    const json = JSON.parse(atob(payload.replaceAll('-', '+').replaceAll('_', '/'))) as unknown
    if (!json || typeof json !== 'object') return null
    return asPort((json as Record<string, unknown>).embed_parent_port)
  } catch {
    return null
  }
}

/**
 * `conn` when its token can be framed by this page, otherwise the gateway's
 * token for crew `id` naming this page's port, paired with the forward the
 * gateway names for it. Rejects when the gateway cannot mint one, so a caller
 * never stores a credential the crew's CSP would refuse.
 */
export async function paneCredentialFor(id: string, conn: WarmConn): Promise<WarmConn> {
  const { protocol, hostname, port } = window.location
  // A page that cannot embed a pane at all mounts none (InstancesViewport's
  // nonLoopbackOrigin), so a credential for it would only be a wasted mint.
  if (!isEmbeddableLoopbackOrigin(protocol, hostname)) return conn
  const page = pagePort(protocol, port)
  const named = tokenParentPort(conn.token)
  if (page === null || named === null || named === page) return conn
  const res = await api.embedInstanceToken(id, page)
  const hop = asPort(res?.port)
  if (typeof res?.token !== 'string' || !res.token || hop === null) {
    throw new Error('the gateway answered without a pane credential for this page')
  }
  paneLog('pane-credential', { id, port: hop, gatewayPort: named, pagePort: page })
  return { port: hop, token: res.token }
}
