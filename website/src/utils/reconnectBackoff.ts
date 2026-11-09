/**
 * The reconnect rule the two app sockets (`apps/mochi/panel/panelBridge`,
 * `apps/crew-companion/sessionWatch`) share with the dashboard socket.
 *
 * After a gateway stall every open copy loses its socket at the same moment. A
 * fixed doubling delay then has every copy retry at the same moments, and a delay
 * reset on every open retries a gateway that accepts and then drops sockets at the
 * shortest delay. So each delay loses a random part, and the backoff starts over
 * only after a socket stayed open long enough to prove the gateway healthy.
 */

/** A socket open at least this long has proved the gateway healthy, so its close
 *  starts the backoff over; one that drops sooner keeps it growing. */
export const RECONNECT_HEALTHY_MS = 10_000

/** `delay` less a random part of up to half of it. */
export function jittered(delay: number): number {
  return delay - Math.floor(Math.random() * (delay / 2))
}

/** True when a socket opened at `openedAt` (null: it never opened) and closing
 *  at `now` stayed open long enough to start the backoff over. */
export function provedHealthy(openedAt: number | null, now: number): boolean {
  return openedAt !== null && now - openedAt >= RECONNECT_HEALTHY_MS
}
