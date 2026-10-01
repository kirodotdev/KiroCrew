/**
 * Shared loopback-hostname helper for Remote Crew pane addressing.
 *
 * ## Why only this remains here
 *
 * This module once owned the whole "how is a pane addressed?" decision
 * (`EmbedMode`, `resolveEmbedMode`, `paneAddress`, `paneOrigin`,
 * `resolvePaneOrigin`). That authority moved to `lib/paneChannel.ts` when the
 * same-origin capability relay landed: `paneChannel` resolves BOTH transports
 * (direct-loopback and same-origin-relay) through one discriminated
 * `PaneMode`/`PaneEndpoint`, so a second address module would be a place for the
 * two to drift. The superseded exports are gone; `paneChannel` is the single
 * addressing authority.
 *
 * What stays is the one fact both transports still share: whether a hostname is
 * a loopback name the SSH forward's loopback bind is reachable on. `paneChannel`
 * imports `isLoopbackHostname` to decide direct-loopback vs same-origin-relay,
 * and keeping the host allowlist in one place means it cannot disagree with the
 * origin regex baked into `tunnelOrigin`.
 */

/**
 * Loopback hostnames the SSH forward's loopback bind is reachable on:
 * `127.0.0.1`, `localhost`, and single-label `*.localhost` names. This mirrors
 * the host allowlist baked into `tunnelOrigin`'s origin regex so the two cannot
 * disagree on what "loopback" means.
 */
const LOOPBACK_HOSTNAME_RE = /^(?:127\.0\.0\.1|localhost|[a-z0-9-]+\.localhost)$/

/** True when *hostname* is a loopback name the forward's bind is reachable on. */
export function isLoopbackHostname(hostname: string): boolean {
  return typeof hostname === 'string' && LOOPBACK_HOSTNAME_RE.test(hostname)
}
