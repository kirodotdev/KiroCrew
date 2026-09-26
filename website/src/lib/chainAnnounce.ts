/**
 * chainAnnounce — the pane's half of Remote Crew chaining.
 *
 * When this dashboard runs inside an InstancesViewport <iframe>, the gateway it
 * belongs to is itself a crew of the gateway showing the page. A crew connected
 * from in here is therefore reachable from the host only by riding the host's
 * existing hop to us, and the host cannot learn about it on its own: it never
 * sees our registry.
 *
 * So the pane says so. One postMessage upward, carrying WHICH crew came up and
 * on WHICH of our loopback ports — and nothing else. In particular it carries no
 * token and no credential: the host mints its own by asking our gateway over the
 * credential it already holds for us (`/api/instances/{id}/embed-token`), which
 * is what makes it safe for this notice to travel through frame code at all.
 *
 * The host decides what to do with it. It validates the sender's origin against
 * its own warm-pane map, then asks ITS gateway to add the crew, where the depth
 * cap and the cycle guard live. Nothing here is trusted, and nothing here is a
 * decision.
 *
 * A no-op at top level: a gateway nobody is embedding has no host to tell.
 */
import { isEmbeddedPane } from './embedded'

/** What the pane tells its host about a crew it just connected. */
export interface ChainedCrewNotice {
  /** The crew's id in OUR registry. The host derives its own id; this only
   *  labels the notice and lets the host recognise a re-announce. */
  id: string
  /** The crew's display name, as the user typed it here. */
  name: string
  /** The host/target string that names the machine on a switcher row. */
  sshHost: string
  /** The port the crew's OWN gateway listens on, for the host's record. */
  remotePort: number
  /** The loopback port on THIS machine where our forward to the crew listens.
   *  This is the far end of the forward the host will open. */
  port: number
}

export const CHAINED_CREW_MESSAGE = 'mc-instance-ready'

/** The host's answer when it could NOT adopt an announced crew.
 *
 * The connect SUCCEEDED here -- the crew is up and reachable on this gateway --
 * and only the host's attempt to show it as a tab of its own failed, so the two
 * facts have to be reported together or the user reads a working crew as broken.
 * The host is the only side that knows the reason: the depth cap and the cycle
 * guard are its gateway's decisions, taken against a registry this pane never
 * sees.
 */
export const CHAINED_CREW_REFUSED_MESSAGE = 'mc-instance-refused'

/** What the host tells the pane about a crew it declined to adopt. */
export interface ChainedCrewRefusal {
  /** The crew's id in OUR registry, so the panel can name which crew it was. */
  id: string
  /** The gateway's own reason, already user-facing. */
  reason: string
}

/** Cap on the relayed reason. It crosses a postMessage boundary, so it is
 *  untrusted LENGTH as well as untrusted content, and it is rendered into a
 *  notice sized for a sentence. Generous past any honest gateway reason. */
export const CHAIN_REFUSAL_MAX = 400

// The refusal the host last sent this realm, and the listener that catches it.
//
// It lives HERE, not in the panel that draws it, because the two events do not
// coincide: the host answers while the user is still watching the crew connect,
// and the panel may well be closed by the time the answer lands. A listener
// mounted with the panel would drop it, and the user would see a crew that
// connected and then silently failed to appear as a tab -- the exact outcome the
// relay exists to explain. Registered at module load for the same reason: there
// is no later moment that is still early enough.
//
// One value, not a queue: it answers "why did the crew I just connected not show
// up", and only the newest answer can be about the crew the user is looking at.
let lastRefusal: ChainedCrewRefusal | null = null
const refusalListeners = new Set<() => void>()

function onHostMessage(e: MessageEvent) {
  if (e.source !== window.parent) return
  const d = e.data
  if (!d || typeof d !== 'object' || d.type !== CHAINED_CREW_REFUSED_MESSAGE) return
  lastRefusal = {
    id: typeof d.id === 'string' ? d.id.slice(0, CHAINED_NAME_MAX) : '',
    reason: typeof d.reason === 'string' ? d.reason.slice(0, CHAIN_REFUSAL_MAX) : '',
  }
  refusalListeners.forEach(l => l())
}

if (typeof window !== 'undefined') window.addEventListener('message', onHostMessage)

/** Subscribe to the relayed refusal, for `useSyncExternalStore`. */
export function subscribeChainRefusal(cb: () => void): () => void {
  refusalListeners.add(cb)
  return () => {
    refusalListeners.delete(cb)
  }
}

/** The refusal the host last sent, or null. Identity-stable between changes, so
 *  `useSyncExternalStore` does not see a new object on every render. */
export function readChainRefusal(): ChainedCrewRefusal | null {
  return lastRefusal
}

/** Dismiss it. The user has read the reason; a stale one must not reappear on the
 *  next time the panel opens. */
export function clearChainRefusal(): void {
  if (lastRefusal === null) return
  lastRefusal = null
  refusalListeners.forEach(l => l())
}

/** Caps on the notice's two free-text fields, for the same reason: the payload is
 *  untrusted length as well as untrusted content. Sized past any honest value --
 *  a crew name is a label a person typed, and a host string is an ssh alias or an
 *  FQDN, which DNS itself caps at 253. */
export const CHAINED_NAME_MAX = 200
export const CHAINED_HOST_MAX = 255

/** The instance-id grammar, mirroring the registry's own. The announced id ends
 *  up in a request path on the announcing gateway, so its shape is checked here
 *  as well as there. */
export const CHAINED_ID_RE = /^[a-z0-9][a-z0-9-]{0,62}$/

/**
 * Read an inbound notice, or ``null`` if it is not one this host can act on.
 *
 * Pure and exported so the rules are testable without a host: every field came
 * from frame code, and the SENDER being trusted (its origin resolved to a warm
 * pane) says nothing about the PAYLOAD. A missing `id` is fatal rather than
 * cosmetic -- it is the id the parent knows the crew by, and without it the mint
 * would be aimed at an id derived here that the parent does not hold.
 */
export function readChainedCrewNotice(raw: unknown): ChainedCrewNotice | null {
  const d = (raw ?? {}) as Record<string, unknown>
  const port = Number(d.port)
  if (!Number.isInteger(port) || port < 1 || port > 65535) return null
  const name = typeof d.name === 'string' ? d.name.slice(0, CHAINED_NAME_MAX) : ''
  const sshHost = typeof d.sshHost === 'string' ? d.sshHost.slice(0, CHAINED_HOST_MAX) : ''
  const id = typeof d.id === 'string' && CHAINED_ID_RE.test(d.id) ? d.id : ''
  if (!name || !sshHost || !id) return null
  const remote = Number(d.remotePort)
  // A crew's own gateway port is a record here, not a dial target, so an
  // out-of-range value is dropped rather than refusing the whole notice.
  const remotePort = Number.isInteger(remote) && remote >= 1 && remote <= 65535 ? remote : 0
  return { id, name, sshHost, remotePort, port }
}

/**
 * Tell the host a crew is connected here and reachable through us.
 *
 * Silent on every failure. A pane with no host, a host that predates the
 * message, and a browser that refuses the post all mean the same thing to the
 * user: the crew is connected HERE and simply does not appear as a tab up there.
 * Throwing would instead break the connect that just succeeded.
 */
export function announceChainedCrew(notice: ChainedCrewNotice): boolean {
  if (!isEmbeddedPane()) return false
  if (!notice.id || !Number.isInteger(notice.port) || notice.port < 1 || notice.port > 65535) {
    return false
  }
  try {
    // nosemgrep: javascript.browser.security.wildcard-postmessage-configuration.wildcard-postmessage-configuration
    window.parent?.postMessage(
      {
        type: CHAINED_CREW_MESSAGE,
        v: 1,
        id: notice.id,
        name: notice.name,
        sshHost: notice.sshHost,
        remotePort: notice.remotePort,
        port: notice.port,
      },
      // The host validates our ORIGIN against the loopback port it forwarded to
      // us, which is the check that matters; we cannot name its origin from here
      // (a cross-origin iframe cannot read `parent.location`), and the payload
      // carries nothing secret precisely so this is safe.
      '*',
    )
    return true
  } catch {
    return false
  }
}

/** What the host should do about a crew it already has a row for. */
export interface ChainAdoptionPlan {
  /** Write the announced hop port onto the row -- only when it actually moved. */
  repoint: boolean
  /** Connect the row. */
  connect: boolean
}

/**
 * What to do when an announcement names a crew already on the record.
 *
 * `connect` is unconditional, and that is the whole point of this function
 * existing: an announcement means the pane's user just connected this crew on
 * the parent, while the tab HERE can be down for reasons the row cannot show --
 * this gateway restarted, or the hop dropped and took the child with it.
 * Deciding from the port alone left that tab dead beside a running crew. The
 * backend's connect is idempotent, so connecting an already-live row costs
 * nothing.
 *
 * `repoint` is conditional because it is a write: the row only needs changing
 * when the parent is serving the crew somewhere new.
 */
export function chainAdoptionPlan(
  existingRemotePort: number | undefined,
  announcedPort: number,
): ChainAdoptionPlan {
  return { repoint: existingRemotePort !== announcedPort, connect: true }
}

/**
 * The gateway's machine-readable refusal code for a failed chained add, or ''.
 *
 * Read off the error's raw body rather than its message: the message is prose
 * written for a person and is the wrong thing to branch on. Duck-typed on `body`
 * so this stays a pure function with no import of the api client.
 *
 * `chain_duplicate` is the one a caller must treat as benign. It means the crew
 * is already on the record here, which is not a failure -- it is this pane's own
 * list having been stale when it decided to add. Showing it as a refusal would
 * tell the user something went wrong about a crew that is present and connected.
 */
export function chainRefusalCode(err: unknown): string {
  const body = (err as { body?: unknown } | null)?.body
  if (typeof body !== 'string' || !body) return ''
  try {
    const parsed: unknown = JSON.parse(body)
    const code = (parsed as { code?: unknown } | null)?.code
    return typeof code === 'string' ? code : ''
  } catch {
    return ''
  }
}
