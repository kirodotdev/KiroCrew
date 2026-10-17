/**
 * Pane lifecycle journal.
 *
 * A remote crew pane that never becomes a live document shows only "loading
 * pane", and the failure is intermittent — it can survive an app restart or
 * vanish on one. So these lines are ALWAYS emitted rather than gated behind a
 * debug flag: the next occurrence has to leave evidence without anyone having
 * turned anything on beforehand, because by the time someone would think to,
 * the state is gone.
 *
 * Routed through `console.info` with a fixed prefix. The desktop app's
 * `frame-load-log` forwarder allowlists that prefix, so these land in
 * gateway-launch.log interleaved with the Chromium frame events they explain;
 * in a plain browser they are ordinary console lines.
 *
 * The prefix is a marker, not a capability: the forwarder honours it only for a
 * document that is BOTH the top-level frame and on the origin the window was loaded
 * with, because a crew pane is a cross-origin iframe of the same webContents and
 * could otherwise print these same characters to forge pane records — and being the
 * top frame is a position a pane can reach by navigating the top-level window. So
 * this module must keep being called from the dashboard document itself — move a
 * caller into an iframe or a worker and its lines stop reaching the log file (they
 * remain ordinary console output).
 */

export const PANE_LOG_PREFIX = '[pane]'

/**
 * Field names whose values are credentials and must never be journaled.
 *
 * Matched as a SUBSTRING on purpose: `authToken`, `session_secret` and
 * `tokenValue` all have to be caught, and a caller that passes a whole connection
 * object must not be able to leak one through a key this list did not anticipate.
 * The cost is that a presence FLAG whose name contains one of these words —
 * `hasToken` is the one in this codebase — matches too. See `isSecretValue`.
 */
const SECRET_KEYS = /token|secret|password|cookie/i

/**
 * Does this field actually carry a secret, or only say whether one was there?
 *
 * A boolean cannot hold a credential: `hasToken=false` is the whole finding in
 * `remint-empty` and `warm-declined`, and redacting it to `<redacted>` throws away
 * the one bit the line exists to record while protecting nothing. Every other type
 * is treated as the secret itself, so widening this exemption past `boolean` would
 * trade a real credential for a nicer log.
 */
function isSecretValue(key: string, value: unknown): boolean {
  return SECRET_KEYS.test(key) && typeof value !== 'boolean'
}

function formatValue(value: unknown): string {
  if (typeof value === 'string') {
    // Quote only when needed, so the common case stays greppable as `key=value`.
    return /[\s"]/.test(value) ? JSON.stringify(value) : value
  }
  if (value === null) return 'null'
  if (typeof value === 'object') {
    try {
      return JSON.stringify(value)
    } catch {
      return '<unserializable>'
    }
  }
  return String(value)
}

/**
 * Emit one structured line: `[pane] <event> key=value key=value`.
 *
 * `undefined` fields are dropped rather than printed as "undefined" — an absent
 * field reads as "not applicable here", which is what the caller means.
 * Anything whose key looks like a credential is replaced, so a caller that
 * passes a whole connection object cannot leak a token into a log file — except a
 * boolean, which is a presence flag and not a secret (see `isSecretValue`).
 */
export function paneLog(event: string, fields: Record<string, unknown> = {}): void {
  const parts = [PANE_LOG_PREFIX, event]
  for (const [key, value] of Object.entries(fields)) {
    if (value === undefined) continue
    parts.push(`${key}=${isSecretValue(key, value) ? '<redacted>' : formatValue(value)}`)
  }
  // eslint-disable-next-line no-console -- this IS the diagnostic channel: a packaged app has no devtools console, so these lines are the only record of a pane that never loaded.
  console.info(parts.join(' '))
}

/**
 * A pane URL safe to journal: the `?token=` value is dropped while the fact a
 * token was present is kept, because a token the remote refused is the failure
 * worth diagnosing and its value never is.
 */
export function safePaneUrl(url: string | null | undefined): string {
  const text = String(url || '')
  if (!text) return '<empty>'
  const cut = text.indexOf('?')
  if (cut < 0) return text
  const query = text.slice(cut + 1)
  return text.slice(0, cut) + (/(^|&)token=/.test(query) ? '?token=<redacted>' : '?<query>')
}

/**
 * Where an iframe's document actually is — the one question that decides this
 * diagnosis, answerable from the parent realm alone.
 *
 * Reading `contentWindow.location.href` THROWS once the frame has navigated to
 * the crew's own origin, and succeeds while the frame is still on the initial
 * `about:blank` (which inherits the parent's origin). So the exception is the
 * good outcome: `cross-origin` means the pane really loaded the tunnel URL,
 * while a readable `about:blank` means the navigation never happened and no
 * crew bundle can ever run there.
 */
export function frameDocumentState(el: HTMLIFrameElement | null | undefined): string {
  if (!el) return 'no-element'
  const win = el.contentWindow
  if (!win) return 'no-contentwindow'
  try {
    return safePaneUrl(win.location.href) || 'same-origin'
  } catch {
    return 'cross-origin'
  }
}
