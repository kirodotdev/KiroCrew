/**
 * Can THIS browser reach a remote-crew pane's loopback port at all?
 *
 * The gateway forwards each crew to `127.0.0.1:<port>` on the GATEWAY host, and
 * the pane iframe loads `http://<window.location.hostname>:<port>/`. When the
 * dashboard is reached through a forward of its own port only (the gateway on a
 * remote host, the browser on a laptop), `localhost` resolves on the browser's
 * machine, where nothing listens on `<port>`. The tunnel still reads connected,
 * because the backend probes the port on its own host, so the load watchdog's
 * timeout is the only symptom. A probe from the browser is the one check that
 * can see that.
 *
 * The fetch is `no-cors`: the response is opaque and nothing in it is read.
 * Chromium resolves such a fetch even for the gateway's `nosniff` HTML shell
 * (Opaque Response Blocking hands back an empty opaque response, not an error),
 * so any answer resolves. A refused or reset connection rejects at once. That
 * rejection is the ONLY `unreachable`: a same-host ssh forward whose far end is
 * down also accepts and then drops the connection, so callers must word the
 * verdict as a possible cause, not a certainty. A probe that times out proves
 * nothing either way and is `unknown`.
 *
 * The dashboard CSP admits this only for the IPv4 loopback hosts in its
 * `connect-src` (`_LOOPBACK_FRAME_SRC` in src/kiro_crew/dashboard/server.py). A
 * `*.localhost` host is framable but not connectable, so a probe there would be
 * refused by CSP and read as unreachable; `canProbePanePort` says no for it.
 */

export const PANE_PORT_PROBE_TIMEOUT_MS = 3_000

export type PanePortProbe = 'reachable' | 'unreachable' | 'unknown'

const PROBEABLE_HOST_RE = /^(?:127\.0\.0\.1|localhost|0\.0\.0\.0)$/

/** True when the dashboard CSP lets this page fetch `http://<host>:<port>/`. */
export function canProbePanePort(host: string): boolean {
  return PROBEABLE_HOST_RE.test(host)
}

/** The pane's base URL as the iframe loads it, without the token. */
export function panePortUrl(host: string, port: number): string {
  return `http://${host}:${port}/`
}

/** Probe the URL once. See the module note for what each verdict can prove. */
export async function probePanePort(url: string, timeoutMs = PANE_PORT_PROBE_TIMEOUT_MS): Promise<PanePortProbe> {
  const ctrl = new AbortController()
  let timedOut = false
  const t = setTimeout(() => { timedOut = true; ctrl.abort() }, timeoutMs)
  try {
    await fetch(url, { mode: 'no-cors', cache: 'no-store', credentials: 'omit', signal: ctrl.signal })
    return 'reachable'
  } catch {
    return timedOut ? 'unknown' : 'unreachable'
  } finally {
    clearTimeout(t)
  }
}
