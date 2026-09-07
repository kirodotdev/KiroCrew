/**
 * Host-side backup for the dashboard settings that live in `localStorage`.
 *
 * The problem this solves: most settings the user sets in the UI are stored
 * only in the renderer's `localStorage`, and `localStorage` is keyed by ORIGIN
 * and (in the desktop app) kept inside Electron's `userData` directory. Both
 * can change without the user doing anything: the dashboard port moves, the
 * `userData` directory is relocated by a package rename, the user switches
 * between the stable and nightly builds, or the browser evicts the origin. Each
 * of those looks identical from the user's chair — "I upgraded and had to set
 * everything up again" — even though nothing on the host was lost, because
 * nothing on the host was ever written.
 *
 * So: mirror the durable subset to the gateway (`/api/ui-prefs`, stored in the
 * data home), and read it back when a profile shows up with none of those keys.
 *
 * Design decisions worth knowing before changing this file:
 *
 * 1. **Read on a COLD profile only.** The server copy is a backup, not a live
 *    cross-tab sync channel. If any durable key exists locally, that copy wins
 *    and the server is only written to. Reading on every boot would let a stale
 *    backup fight a tab the user just changed, and would make two browsers on
 *    one host tug settings back and forth.
 * 2. **Snapshot-diff, not write interception.** ~300 call sites write
 *    `localStorage` directly. Rather than route them all through one writer (a
 *    migration that would go stale the next time someone adds a raw call), this
 *    module periodically reads the allowlisted keys and PUTs what changed. Any
 *    writer, present or future, is covered.
 * 3. **Explicit allowlist, no prefix wildcards for session-scoped keys.**
 *    Ephemeral and per-session state (height caches, panel tabs, drafts,
 *    touched files, web-preview URLs) is deliberately excluded: it is worthless
 *    on another origin, it is what the quota reclaimer already evicts, and
 *    mirroring it would grow without bound.
 * 4. **Keys already mirrored server-side are excluded** — theme mode, theme
 *    colour, language and the onboarding flags reconcile through
 *    `/api/config/theme` and `dashboard.*` config. Backing them up here too
 *    would create a second source of truth for the same setting.
 */

import { safeGetItem, safeSetItem } from '../utils/safeStorage'

/**
 * Durable, user-chosen preferences. Exact keys only.
 *
 * A key belongs here when a user would be annoyed to set it twice, and would
 * NOT be surprised to see it follow them to a new window. When in doubt leave
 * it out: a missing backup degrades to today's behaviour, while backing up
 * session-scoped state resurrects stale UI on an unrelated profile.
 */
export const DURABLE_PREF_KEYS: readonly string[] = [
  // Chat preferences — one JSON blob holding ~16 settings (send-key mode,
  // timestamps, turn stats, content width, collapse-steps, stream mode, ...).
  // This single key is the most-reported loss; see issue #8705.
  'mc-chat-config',
  'mc-busy-send-mode',
  // Typography and zoom.
  //
  // NOT here: 'mc-zoom' and 'mc-font-scale'. hooks/useZoom.ts runs a one-time
  // migration that folds both legacy page-scaling keys into the native zoom
  // factor and then DELETES them, so backing them up would restore them on the
  // next cold profile and re-run the migration forever.
  'mc-font-family',
  // Navigation and shell layout the user arranged by hand.
  'mc-nav',
  'mc-app-nav-order',
  'mc-apps-expanded',
  'mc-bottom-terminal',
  'mc-files-rail-open',
  'mc-crews-view',
  'mc-crew-switcher-pinned',
  'mc-crew-switcher-stable-order',
  'mc-filter-folders-shelved',
  'mc-flat-hidden-folders',
  'mc-input-height',
  // Diff and file viewer toggles.
  'mc-diff-plain',
  'mc-diff-split',
  'mc-file-linenums',
  'mc-file-wordwrap',
  'mc-file-collapse-unchanged',
  // Artifacts browser.
  'mc-artifacts-view',
  'mc-artifacts-sort',
  'mc-artifacts-pinned-only',
  'mc-artifacts-session-docs-collapsed',
  'mc-artifact-folders-expanded',
  // Misc opt-ins and acknowledgements the user should not have to repeat.
  //
  // NOT here: 'mc-yolo-ack'. Its mere PRESENCE makes ApprovalModePicker skip the
  // confirmation dialog and activate full auto-approval directly, and this backup
  // lives in the agent-writable data home — so restoring it would let an agent
  // that can write ~/.kiro/crew/ui-prefs.json pre-satisfy a human safety gate on
  // the user's next fresh origin. The rule this key is an instance of: a value
  // that GATES a safety confirmation must not be restorable from a file the agent
  // can write, however convenient re-acknowledging is.
  'mc-dev-mode',
  'mc-agent-scene',
  'mc-kb-graph-physics',
  'mc:notif:activeKinds:v2',
  'kirocrew:account-email-hidden',
  'kirocrew:comment-hint-dismissed',
  // Cloud launch defaults.
  'mc-cloud-profile',
  'mc-cloud-region',
  'mc-cloud-size',
  // Per-app preferences.
  'kc-cron-folders-collapsed',
  'kc:file-explorer:state:v2',
  'kc:issue-radar:ui-state',
  'telemetry:tab',
  'telemetry:spend-group',
  'mdnb-view',
  'mdnb-sort',
  'mdnb-list-view',
  'mdnb-full-width',
  'mdnb-panel-width',
  'mdnb-panel-open',
  'mdnb-auto-commit',
  'mdnb-auto-sync',
  'mdnb-auto-sync-mins',
  'mdnb-sync-shortcut',
  'ste_rail_w',
]

const ENDPOINT = '/api/ui-prefs'
/**
 * Fingerprints of the durable keys this profile last successfully synced with
 * the host, as `{key: hash}`.
 *
 * Three jobs, all of which need to outlive a reload:
 *  1. Change detection after a reload. `lastSent` is in-memory, so the first
 *     flush of a new page had no baseline and re-PUT every local value — which
 *     on a profile holding STALE values overwrote newer preferences another
 *     origin had already backed up. Fingerprints make that first flush send only
 *     what this profile actually changed.
 *  2. Deletion detection. Without a persisted key set, a key this profile once
 *     uploaded and has since deleted was never reported, and a later cold
 *     profile restored the value the user had deleted.
 *  3. "Have we ever reached the host?" Its absence is what makes the boot-time
 *     hydrate retry. Without it, one failed fetch on a fresh profile burned the
 *     only restore attempt the backup exists for, because the first setting
 *     written afterwards made the profile look warm forever.
 *
 * Hashes rather than values: one of these keys can hold a large blob, and storing
 * the values would double their footprint in the very storage this feature exists
 * to protect. The hash only has to detect CHANGE, so a non-cryptographic string
 * hash is the right tool — nothing here is a security decision.
 *
 * Bookkeeping, so deliberately NOT in DURABLE_PREF_KEYS: it describes this
 * profile's relationship to the host, not a user preference.
 */
const SYNCED_KEYS_KEY = 'mc-ui-prefs-synced'
/**
 * Written when a never-synced profile's restore FAILED (network, 401/403, 5xx).
 * Its value is the JSON list of durable keys the profile ALREADY HELD at that
 * moment. It changes what the NEXT successful restore does with a key that is
 * present both locally and on the host:
 *
 *   * a key in the list was the user's before anything went wrong, and any
 *     change they made since is theirs too — local wins, as always;
 *   * a key NOT in the list was written after the failure by a page that
 *     rendered without its settings (a login screen counts): hooks that persist
 *     on mount wrote defaults into localStorage. Treating those as the user's
 *     choice would make the first flush upload defaults over the real backup —
 *     the very thing the restore failed to read. For those keys the host wins.
 *
 * Letting the host win for EVERY key instead (an earlier version did) had the
 * mirror-image defect: a returning user whose GET failed once, then changed a
 * preference, saw the stale host value overwrite the change at next launch.
 *
 * A repeat failure never widens the list — the keys added since the first
 * failure are exactly the untrusted ones. Cleared by the first successful
 * restore. A warm profile upgrading into this feature cannot be caught by it:
 * its first GET finds an empty host, succeeds, and there is nothing to override.
 */
const HYDRATE_PENDING_KEY = 'mc-ui-prefs-hydrate-pending'

/** Keys the profile held when its first restore failed, or null if it never failed. */
function ownedAtFailure(): Set<string> | null {
  const raw = safeGetItem(HYDRATE_PENDING_KEY)
  if (raw === null) return null
  try {
    return new Set((JSON.parse(raw) as string[]).filter((k) => typeof k === 'string'))
  } catch {
    return new Set()
  }
}

/** Record a failed restore. Never widens an existing snapshot (see the key's doc). */
function markHydrateFailed(): void {
  if (safeGetItem(HYDRATE_PENDING_KEY) !== null) return
  const owned = DURABLE_PREF_KEYS.filter((k) => safeGetItem(k) !== null)
  safeSetItem(HYDRATE_PENDING_KEY, JSON.stringify(owned))
}
/** Debounce for a change-triggered flush. Long enough that dragging a splitter
 *  produces one PUT, short enough that closing the window right after a click
 *  still catches it (the visibility flush is the backstop). */
const FLUSH_DEBOUNCE_MS = 1500
/** Safety-net scan for writers that fire no event we listen to. */
const POLL_INTERVAL_MS = 30_000
/** The boot-time hydrate must not hold the first paint hostage on a slow or
 *  unreachable gateway. On timeout we render with defaults, exactly as today. */
const HYDRATE_TIMEOUT_MS = 3000

/** Last state we successfully sent, so a flush PUTs only what changed. */
let lastSent: Map<string, string> | null = null
let flushTimer: ReturnType<typeof setTimeout> | undefined
let pollTimer: ReturnType<typeof setInterval> | undefined
let started = false
let inFlight: Promise<void> | null = null
/** A change arrived while a PUT was in flight: chain one more flush after it. */
let dirtyDuringFlush = false

function readLocalSnapshot(): Map<string, string> {
  const snapshot = new Map<string, string>()
  for (const key of DURABLE_PREF_KEYS) {
    const value = safeGetItem(key)
    if (value !== null) snapshot.set(key, value)
  }
  return snapshot
}

/**
 * Non-cryptographic change detector: two independent 32-bit FNV-1a lanes plus
 * the length, ~64 bits of separation. A collision here is not harmless — the
 * changed value is read as "already synced", never uploaded, and an origin
 * reset then restores the stale host copy over it — so one 32-bit lane
 * (2^-32 per change) was too thin. Two lanes with different offset bases and a
 * different multiplier walk unrelated orbits, and the length forecloses the
 * cheapest constructed collisions. Storing the prior values themselves would
 * be exact but doubles localStorage use for the largest keys; this is not a
 * digest and does not claim adversarial resistance.
 */
function fingerprint(value: string): string {
  let a = 0x811c9dc5
  let b = 0x9747b28c
  for (let i = 0; i < value.length; i++) {
    const c = value.charCodeAt(i)
    a ^= c
    a = Math.imul(a, 0x01000193)
    b ^= c
    b = Math.imul(b, 0x5bd1e995)
    b ^= b >>> 15
  }
  return `${value.length.toString(36)}.${(a >>> 0).toString(36)}.${(b >>> 0).toString(36)}`
}

function readSyncedPrints(): Map<string, string> {
  const raw = safeGetItem(SYNCED_KEYS_KEY)
  if (!raw) return new Map()
  try {
    const parsed: unknown = JSON.parse(raw)
    if (!parsed || typeof parsed !== 'object' || Array.isArray(parsed)) return new Map()
    const out = new Map<string, string>()
    const durable = new Set(DURABLE_PREF_KEYS)
    for (const [k, v] of Object.entries(parsed as Record<string, unknown>)) {
      // Ignore a fingerprint for a key THIS build does not know. On a downgrade
      // the marker still lists keys a newer build synced; keeping them would put
      // them in `previousKeys` while `current` can never hold them (they are not
      // read at all), so buildPatch would emit `null` and the older build would
      // delete a preference the newer one owns.
      if (typeof v === 'string' && durable.has(k)) out.set(k, v)
    }
    return out
  } catch {
    return new Map()
  }
}

function writeSyncedPrints(values: Map<string, string>): void {
  const prints: Record<string, string> = {}
  for (const [k, v] of values) prints[k] = fingerprint(v)
  safeSetItem(SYNCED_KEYS_KEY, JSON.stringify(prints))
}

/**
 * Update the persisted fingerprints for SOME keys, leaving the rest alone.
 *
 * `updates` maps a key to its accepted value, or to `null` for a key the host
 * accepted a deletion of. Merging rather than replacing matters on the per-key
 * retry path: rebuilding the whole map from that round's few keys would erase the
 * fingerprints of every key the round never mentioned, and the next poll would
 * then re-upload their stale values over newer host preferences.
 */
function mergeSyncedPrints(updates: Map<string, string | null>): void {
  const prints = readSyncedPrints()
  for (const [k, v] of updates) {
    if (v === null) prints.delete(k)
    else prints.set(k, fingerprint(v))
  }
  const out: Record<string, string> = {}
  for (const [k, v] of prints) out[k] = v
  safeSetItem(SYNCED_KEYS_KEY, JSON.stringify(out))
}

/**
 * True when this profile has never successfully exchanged preferences with the
 * host — a fresh browser profile, a new origin (moved port), a relocated
 * Electron `userData`, or a boot whose fetch failed. That is exactly when the
 * backup should be read, and staying true after a failure is what makes the read
 * retry on the next boot instead of being forfeited.
 */
export function needsHydrate(): boolean {
  return safeGetItem(SYNCED_KEYS_KEY) === null
}

/**
 * Build the merge patch: changed and new keys as values, keys this profile
 * previously synced but no longer holds as `null`, so the backup follows a
 * deletion instead of resurrecting it.
 *
 * With no in-memory baseline (the first flush after a reload) the comparison runs
 * against the persisted fingerprints, so an unchanged value is NOT re-sent. That
 * matters beyond saving a request: re-sending everything from a profile holding
 * stale values would overwrite newer preferences another origin had already
 * backed up. A key this profile never synced is never nulled, which is what keeps
 * a second browser from deleting the first one's settings.
 */
function buildPatch(current: Map<string, string>): Record<string, string | null> {
  const patch: Record<string, string | null> = {}
  const prints = lastSent ? null : readSyncedPrints()
  for (const [key, value] of current) {
    const unchanged = lastSent
      ? lastSent.get(key) === value
      : prints!.get(key) === fingerprint(value)
    if (!unchanged) patch[key] = value
  }
  const previousKeys = lastSent ? new Set(lastSent.keys()) : new Set(prints!.keys())
  for (const key of previousKeys) {
    if (!current.has(key)) patch[key] = null
  }
  return patch
}

async function putPatch(
  patch: Record<string, string | null>,
  keepalive = false,
): Promise<Response | null> {
  try {
    return await fetch(ENDPOINT, {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ prefs: patch }),
      // Set on the pagehide flush only: a plain fetch is cancelled with the
      // document, so a change made inside the debounce window before closing
      // was never sent -- and reopening on a NEW origin then restored the stale
      // host value over it (close, upgrade, port moves: this PR's own scenario).
      // Browsers cap keepalive bodies at 64 KiB; a larger patch rejects here,
      // returns null, and is retried on the next boot at the same origin.
      keepalive,
    })
  } catch {
    return null
  }
}

/** Record a successful upload as the new baseline, on both sides. */
function commitSent(current: Map<string, string>): void {
  lastSent = current
  writeSyncedPrints(current)
}

/**
 * Re-send a refused patch one key at a time, so one unstorable value cannot
 * silently discard every other change in the same round. Keys the host accepts
 * enter the baseline; the offender stays out of it and is simply not backed up
 * (its local value still works, and the next flush tries it again).
 *
 * Only the keys THIS round touched are written to the baseline. Rebuilding it
 * from them would drop the fingerprints of every other synced key — after a
 * reload, where the in-memory baseline is empty, that is all of them — and the
 * next poll would re-upload their stale values over newer host preferences.
 */
async function retryPerKey(patch: Record<string, string | null>): Promise<void> {
  const hadBaseline = lastSent !== null
  const accepted = new Map<string, string>(lastSent ?? [])
  const landed = new Map<string, string | null>()
  for (const [key, value] of Object.entries(patch)) {
    const res = await putPatch({ [key]: value })
    if (res === null) break // went offline mid-retry: keep what landed, retry the rest
    if (!res.ok) continue
    landed.set(key, value)
    if (value === null) accepted.delete(key)
    else accepted.set(key, value)
  }
  if (landed.size === 0) return
  // Only adopt an in-memory baseline if there already WAS one. Built from an
  // empty start (the first flush after a reload) `accepted` holds just this
  // round's keys, and a PARTIAL in-memory baseline is worse than none: every key
  // missing from it reads as changed, so the next flush re-uploads stale values
  // over newer host preferences, and deletions of keys absent from it go
  // unreported. Left null, the next flush falls back to the persisted
  // fingerprints, which the merge below has just brought up to date.
  if (hadBaseline) lastSent = accepted
  mergeSyncedPrints(landed)
}

/**
 * Read the host-side backup into `localStorage`.
 *
 * Only keys that are ABSENT locally are written, so this can never clobber a
 * value the running profile already has. Returns the number of keys restored.
 * Resolves (0) rather than rejecting when the gateway is unreachable: a missing
 * backup is not an error. A successful fetch — even an empty one — records the
 * synced key set, which is what stops the next boot from asking again.
 */
export async function hydrateUiPrefs(): Promise<number> {
  const controller = new AbortController()
  const timer = setTimeout(() => controller.abort(), HYDRATE_TIMEOUT_MS)
  // A previous restore on this profile failed; local values for keys the
  // profile did NOT hold at that moment are untrusted, so for those the host
  // wins (see the key's doc). Null means no failure on record: local wins.
  const owned = ownedAtFailure()
  try {
    const res = await fetch(ENDPOINT, { signal: controller.signal })
    if (!res.ok) {
      markHydrateFailed()
      return 0
    }
    const body: unknown = await res.json()
    const prefs = (body as { prefs?: unknown } | null)?.prefs
    if (!prefs || typeof prefs !== 'object') return 0
    const hostValues = prefs as Record<string, unknown>
    let restored = 0
    for (const key of DURABLE_PREF_KEYS) {
      const value = hostValues[key]
      if (typeof value !== 'string') continue
      const local = safeGetItem(key)
      if (local === value) continue
      if (local !== null && (owned === null || owned.has(key))) continue
      if (safeSetItem(key, value)) restored += 1
    }
    // The host answered, so this profile is in touch with it. Baseline every
    // key the host holds with whatever is NOW in localStorage for it:
    //   * equal to the host's -- synced in the plain sense;
    //   * different from the host's (this profile kept its own value) -- also
    //     baselined, deliberately. This origin is by definition the one that has
    //     NOT been syncing; the host's copy came from an origin that has. Not
    //     baselining would make the first flush upload this profile's possibly
    //     months-stale value over the newer backup, unconditionally. Baselined,
    //     the local value stays in use here and is uploaded the moment the user
    //     changes it; the only way it is ever lost is a LATER storage wipe here,
    //     which then restores the host's -- a second failure, not the first.
    //   * a value `safeSetItem` had to drop (quota) stays OUT: recording it
    //     would make the first flush read it as a deletion and null out a good
    //     host backup.
    const syncedNow = new Map<string, string>()
    for (const key of DURABLE_PREF_KEYS) {
      if (typeof hostValues[key] !== 'string') continue
      const local = safeGetItem(key)
      if (local !== null) syncedNow.set(key, local)
    }
    writeSyncedPrints(syncedNow)
    // Cleared AFTER the synced marker is written, so a crash between the two
    // leaves the profile still pending rather than synced-with-untrusted-locals.
    try {
      localStorage.removeItem(HYDRATE_PENDING_KEY)
    } catch {
      /* best-effort */
    }
    if (restored > 0) {
      // Readers that already took their value need to re-read it. Some read it
      // at MODULE scope, before this function ran at all — static imports are
      // evaluated before any statement of the entry module, so e.g.
      // hooks/useBottomTerminal.ts captures its state into a module-level `let`
      // during import, and its own `storage` listener cannot help because
      // `storage` fires for OTHER documents, never for a write this document
      // made. Left unread, that stale module copy is persisted back over the
      // restored value by the first interaction.
      //
      // A same-document event cannot fix a value already captured, so the caller
      // reloads instead (see main.tsx). Announce the change as well for the
      // live listeners that CAN act on it, in case a caller chooses not to.
      window.dispatchEvent(new Event('mc-config-changed'))
    }
    return restored
  } catch {
    markHydrateFailed()
    return 0
  } finally {
    clearTimeout(timer)
  }
}

/** PUT whatever changed since the last successful flush. */
export async function flushUiPrefs(keepalive = false): Promise<void> {
  if (inFlight) {
    // Do NOT hand back the in-flight promise: it carries the OLD snapshot, so a
    // caller awaiting it (pagehide) would believe a newer change had been sent.
    // Mark dirty instead and let the running flush chain another round.
    dirtyDuringFlush = true
    return
  }
  const current = readLocalSnapshot()
  const patch = buildPatch(current)
  if (Object.keys(patch).length === 0) return

  inFlight = (async () => {
    try {
      const res = await putPatch(patch, keepalive)
      if (res === null) return // offline / gateway restarting — retry next trigger
      if (res.ok) {
        commitSent(current)
        return
      }
      if (res.status === 401 || res.status === 403) {
        // Either the access cookie lapsed (routine: useRefreshScheduler and the
        // API client's own recovery repair it within the session) or this is not
        // the owner's dashboard. Both look the same from here, and stopping for
        // the session on the first turned a routine cookie lapse into "nothing
        // changed after it is backed up until the next reload". So: treat like
        // 5xx -- baseline untouched, next trigger retries. A true non-owner
        // costs one refused PUT per change, at most every 30s.
        return
      }
      if (res.status >= 400 && res.status < 500) {
        // The server refuses a patch WHOLE, so nothing landed. Advancing the
        // baseline for every key would abandon the valid changes bundled with
        // the offending one.
        await retryPerKey(patch)
      }
      // 5xx: leave the baseline alone so the next trigger retries.
    } finally {
      inFlight = null
    }
  })()
  await inFlight
  if (dirtyDuringFlush) {
    dirtyDuringFlush = false
    await flushUiPrefs()
  }
}

function scheduleFlush(): void {
  if (flushTimer !== undefined) clearTimeout(flushTimer)
  flushTimer = setTimeout(() => {
    flushTimer = undefined
    void flushUiPrefs()
  }, FLUSH_DEBOUNCE_MS)
}

/** Unload-path flush: the request must outlive the document (see putPatch). */
function flushNow(): void {
  void flushUiPrefs(true)
}

function flushIfHidden(): void {
  if (document.visibilityState === 'hidden') flushNow()
}

/**
 * Start mirroring durable preferences to the host. Idempotent.
 *
 * The baseline starts empty on purpose, so the first flush uploads whatever
 * this profile currently holds. That is what seeds the backup for a user
 * upgrading into this feature, and on a just-hydrated cold profile it costs one
 * idempotent PUT of values the server already has — cheaper than a branch that
 * has to decide which case it is in.
 */
export function startUiPrefsSync(): void {
  if (started) return
  started = true
  lastSent = null

  window.addEventListener('mc-config-changed', scheduleFlush)
  // `storage` fires for OTHER tabs on the same origin, so a change made in one
  // window is backed up even while this one is idle.
  window.addEventListener('storage', scheduleFlush)
  // Best-effort catch of a change made just before the window goes away. A
  // `fetch` on unload may not complete; the debounce plus the poll are what
  // actually guarantee delivery, so nothing depends on this landing.
  window.addEventListener('pagehide', flushNow)
  document.addEventListener('visibilitychange', flushIfHidden)
  pollTimer = setInterval(scheduleFlush, POLL_INTERVAL_MS)
  // First flush right away: on a warm profile this is what creates the backup.
  scheduleFlush()
}

export function __resetUiPrefsSyncForTests(): void {
  started = false
  if (flushTimer !== undefined) clearTimeout(flushTimer)
  if (pollTimer !== undefined) clearInterval(pollTimer)
  flushTimer = undefined
  pollTimer = undefined
  window.removeEventListener('mc-config-changed', scheduleFlush)
  window.removeEventListener('storage', scheduleFlush)
  window.removeEventListener('pagehide', flushNow)
  document.removeEventListener('visibilitychange', flushIfHidden)
  lastSent = null
  inFlight = null
  dirtyDuringFlush = false
}
