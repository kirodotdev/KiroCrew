/**
 * Which remote Crews stay warm: the `warm_set_cap` most recently opened ones,
 * SSH and relay together, by recency.
 *
 * "Opened" means brought to the front of this dashboard (a switcher click, a
 * shortcut, a deep link, a reload restoring the pane). A background connect or a
 * prewarm never counts, so a Crew is never warmed only because it was warmed.
 * The order is kept in localStorage so the next app start warms the same Crews.
 * Only Crew ids are stored (an SSH crew's name slug, a relay endpoint ID), never
 * a port or token. Sign out and Switch account forget the relay Crews in it, so
 * the next account never warms a Crew only the last one opened, in any window.
 */

const KEY = 'mc-crew-recent'
/** How many opened Crews are remembered; far more than any cap needs. */
const MAX_REMEMBERED = 50
// Ids are name slugs or endpoint IDs; anything longer was not written by us.
const MAX_KEY_LEN = 300
const RELAY = 'relay:'
const SSH = 'ssh:'

export const sshCrewKey = (id: string) => `${SSH}${id}`
export const relayCrewKey = (endpointId: string) => `${RELAY}${endpointId}`

function parse(raw: string | null): string[] {
  try {
    const list: unknown = JSON.parse(raw ?? '[]')
    if (!Array.isArray(list)) return []
    return list.filter((k): k is string => typeof k === 'string' && k.length <= MAX_KEY_LEN).slice(0, MAX_REMEMBERED)
  } catch {
    return []
  }
}

function stored(): string | null {
  try {
    return localStorage.getItem(KEY)
  } catch {
    return null
  }
}

// Storage is the one copy: every read and every write starts from it, so a write in
// another window (a Sign out included) is never overwritten by a stale list held here.
// `last` only keeps the answer stable while the stored text is unchanged.
let last: { raw: string | null; list: string[] } | null = null
const listeners = new Set<() => void>()

/** The opened Crews, most recent first. The same array until storage changes (useSyncExternalStore). */
export function recentCrews(): string[] {
  const raw = stored()
  if (last?.raw !== raw) last = { raw, list: parse(raw) }
  return last.list
}

function write(next: string[]) {
  try {
    localStorage.setItem(KEY, JSON.stringify(next))
  } catch {
    // Storage disabled or full: this page keeps the order until storage changes, so an open
    // it could not save is not ranked as never opened (and its pane dropped) here.
    last = { raw: stored(), list: next }
  }
  for (const cb of listeners) cb()
}

/** Calls `cb` on a write in this window or another one. */
export function subscribeRecentCrews(cb: () => void): () => void {
  const onStorage = (e: StorageEvent) => { if (e.key === KEY || e.key === null) cb() }
  listeners.add(cb)
  window.addEventListener('storage', onStorage)
  return () => {
    listeners.delete(cb)
    window.removeEventListener('storage', onStorage)
  }
}

/** Record that the Crew `key` came to the front. */
export function noteCrewOpened(key: string): void {
  const now = recentCrews()
  if (now[0] === key) return
  write([key, ...now.filter(k => k !== key)].slice(0, MAX_REMEMBERED))
}

/** Forget every relay Crew opened (the person signed out or switched account). */
export function forgetRelayCrewsOpened(): void {
  const now = recentCrews()
  if (now.some(k => k.startsWith(RELAY))) write(now.filter(k => !k.startsWith(RELAY)))
}

export interface WarmCrews {
  ssh: string[]
  relay: string[]
}

/**
 * The Crews to keep warm, most recent first. `ssh` and `relay` are the Crews that
 * could hold a pane now (SSH in registry order). Opened Crews come first, by
 * recency; free slots then go to SSH crews never opened, in registry order, so a
 * person with fewer opened Crews than the cap sees what they saw before. A relay
 * Crew never opened is never picked: warming it would mint an access token for a
 * Crew nobody asked to see.
 */
export function rankWarmCrews(
  order: string[],
  ssh: string[],
  relay: string[],
  cap: number,
): WarmCrews {
  const sshIds = new Set(ssh)
  const relayIds = new Set(relay)
  const out: WarmCrews = { ssh: [], relay: [] }
  const full = () => out.ssh.length + out.relay.length >= cap
  for (const key of order) {
    if (full()) return out
    if (key.startsWith(SSH)) {
      const id = key.slice(SSH.length)
      if (sshIds.has(id) && !out.ssh.includes(id)) out.ssh.push(id)
    } else if (key.startsWith(RELAY)) {
      const id = key.slice(RELAY.length)
      if (relayIds.has(id) && !out.relay.includes(id)) out.relay.push(id)
    }
  }
  for (const id of ssh) {
    if (full()) break
    if (!out.ssh.includes(id)) out.ssh.push(id)
  }
  return out
}
