/**
 * localStorage garbage collection.
 *
 * Removes orphaned per-session keys that accumulate unboundedly and
 * eventually overflow the ~5 MB origin quota, white-screening the app.
 *
 * Two entry points:
 *   - `gcOrphanedStorage(liveIds)` — startup pass, removes keys for sessions
 *     that no longer exist. This is the authoritative collector.
 *
 * There is deliberately NO per-key collector. A client cannot prove a key is dead:
 * localStorage is shared across tabs, so another tab may hold or resume that session
 * while this one deletes it. Only the startup pass, which works from an authoritative
 * live list, may collect.
 */

/** Prefixes that are scoped per-session and should be cleaned up.
 *  localStorage key prefixes — storage identifiers, never rendered. Not UI copy.
 *  Each must stay byte-identical to the writer that produces it (the first is
 *  `LS_KEY_PREFIX` in `hooks/virtualizer/HeightCache.ts`, the second is
 *  `ANCHOR_KEY_PREFIX` in `hooks/virtualizer/ScrollAnchorCache.ts`); a
 *  translated or reworded entry silently stops collecting that family of keys. */
const SESSION_PREFIXES = [
  'vc_heights_',
  'vc_anchor3_',
  // The pre-version-bump anchor prefixes. `ScrollAnchorCache` reaps these
  // outright when it loads, but that only happens once the chat virtualizer is
  // on screen — so the boot sweep keeps owning them for a user who never opens a
  // chat. Dropping a line when the prefix was bumped would leave those keys
  // uncollectable for exactly the sessions that are already gone.
  'vc_anchor2_',
  'vc_anchor_',
  'kirocrew:touched-files:',
  'mc-panel-tabs:',
  'mc-activity-open:',
  'mc-webpreview-url:',
  'mc-webpreview-pending:',
  'mc-webpreview-applied:',
  'mc-busy-send-mode:',
] as const

/** Names that appear where a session id is expected but are not sessions.
 *
 *  The orphan sweep always spares them: it is *guessing* which names are dead,
 *  and a non-session name looks dead forever. An explicit per-session delete is
 *  different — the caller named its target — so it is honoured unless the key is
 *  shared state that target does not own. That is the whole difference between
 *  the two entries below, and it turns on ownership, not on convenience.
 *
 *  Each name must stay byte-identical to its writer; a divergence collects the
 *  parked value silently.
 *
 *  - `artifacts-gallery` — the virtualizer partitions its height cache by
 *    `sessionId`, and the gallery is not a chat session but still has to name a
 *    partition. Writer: `ARTIFACT_HEIGHT_NS` in `pages/ArtifactsPage.tsx`.
 *    Reserved under every prefix, and the partition IS owned by this name, so an
 *    explicit delete of it is honoured.
 *  - `no-slot` — slot-less consumers park a per-slot preference here. Writer:
 *    `busySendModeKey` in `components/BusySendButton.tsx`. Reserved under that
 *    one prefix so a real slot spelled the same still loses everything else it
 *    owns, and spared even on an explicit delete because the parked value is
 *    shared state belonging to the slot-less case rather than to any slot.
 *
 *  Residual: a slot named exactly like a reserved name is indistinguishable from
 *  the reserved value under a shared prefix. */
const RESERVED_NAMES: ReadonlyMap<string, {
  /** Prefixes the name is reserved under; `null` means every prefix. */
  prefixes: ReadonlySet<string> | null
  /** Whether an explicit per-session delete must spare it too. */
}> = new Map([
  ['artifacts-gallery', { prefixes: null }],
  ['no-slot', { prefixes: new Set(['mc-busy-send-mode:']) }],
])

/** Whether `sessionId` under `prefix` is a reserved name the given sweep must
 *  leave alone. One predicate, so a name registered for one sweep cannot be
 *  silently missing from the other. */
const isReservedName = (sessionId: string, prefix: string): boolean => {
  const reserved = RESERVED_NAMES.get(sessionId)
  if (!reserved) return false
  return reserved.prefixes === null || reserved.prefixes.has(prefix)
}

/** Where the instance-stamp ledger lives. Not session-scoped, so no sweep here can
 *  mistake it for a session's own key (it matches none of `SESSION_PREFIXES`).
 *  Exported so a test can arrange a prior instance instead of booting twice. */

/** FULL STORAGE KEY -> the instance that wrote THAT key, and when it was last written.
 *
 *  Keyed per key, never per session id. A session owns several
 *  independent key families -- heights, anchors, panel tabs, activity, web preview -- and
 *  a replacement instance typically writes only some of them. A per-session stamp
 *  advanced by any ONE write then vouches for every other family, so the families the
 *  new instance never touched keep the OLD instance's state and load into it.
 *
 *  `seenAt` exists only to order the capacity budget below; it is never evidence of
 *  deadness, and no key is deleted for being old. */
type OwnerEntry = { stamp: string; seenAt: number; generation?: number; epoch?: string }
type OwnerLedger = Record<string, OwnerEntry>

const OWNER_ENTRY_PREFIX = 'mc-storage-gc-owner:'

const ownerEntryKey = (key: string): string => OWNER_ENTRY_PREFIX + key

const parseOwnerEntry = (v: unknown): OwnerEntry | null => {
  if (typeof v === 'string' && v !== '') return { stamp: v, seenAt: 0 }
  if (v && typeof v === 'object' && !Array.isArray(v)) {
    const e = v as Record<string, unknown>
    if (typeof e.stamp === 'string' && e.stamp !== '') {
      return {
        stamp: e.stamp,
        seenAt: typeof e.seenAt === 'number' && Number.isFinite(e.seenAt) ? e.seenAt : 0,
        generation: typeof e.generation === 'number' && Number.isFinite(e.generation)
          ? e.generation
          : undefined,
        epoch: typeof e.epoch === 'string' && e.epoch !== '' ? e.epoch : undefined,
      }
    }
  }
  return null
}

const writeOwnerEntry = (key: string, entry: OwnerEntry): void => {
  try {
    localStorage.setItem(ownerEntryKey(key), JSON.stringify(entry))
  } catch {
    try { localStorage.removeItem(ownerEntryKey(key)) } catch { /* a full store must not break boot */ }
  }
}

const dropOwnerEntry = (key: string): void => {
  try { localStorage.removeItem(ownerEntryKey(key)) } catch { /* best-effort */ }
}

/** How many UNLISTED sessions may keep DERIVED cache state. Absence is not proof of
 *  deadness -- ids are reused and another tab can resume one -- so nothing real is ever
 *  deleted for being absent. But retaining every absent session without limit lets the
 *  origin quota fill and take persisted DRAFTS with it, so past this budget the coldest
 *  absent sessions lose their derived caches ONLY. */
export const MAX_ABSENT_SESSIONS = 24

/** Ceiling on non-derived keys reclaimed per sweep, so one boot cannot become a mass
 *  delete. A session left over the ceiling keeps its close marker and is reclaimed on a
 *  later boot. */
export const MAX_CLOSED_KEYS_PER_SWEEP = 64

/** Sources whose session id embeds the originating event, so the id is minted once and
 *  no later session can be resumed under it.
 *
 *  This is the whole reason non-derived state is collectable at all. A chat slot id is
 *  RECYCLED, so a tab resuming it would share these keys and lose state that has no
 *  rebuild path -- which is why the default stays retention. An id carrying its own
 *  event has no such resumer, so its panel and preview state is unreachable once the
 *  server confirms the close, and keeping it only fills the origin quota. */
const UNRESUMABLE_ID_PREFIXES: readonly string[] = [
  'slack:',
  'discord:',
  'telegram:',
  'webex:',
  'teams:',
  'whatsapp:',
]

const isUnresumableSessionId = (sessionId: string): boolean =>
  UNRESUMABLE_ID_PREFIXES.some(p => sessionId.startsWith(p))

/** True only when `listed` is provably LATER than `recorded`.
 *
 *  A boot GET issued before a same-key replacement carries the OLDER `created`, so plain
 *  inequality held once the replacement had stamped the ledger and the sweep deleted the
 *  REPLACEMENT's non-derived state, which has no rebuild path. Ordering comes from parsed
 *  instants rather than string comparison, because the stamps are not lexicographically
 *  comparable across suffixes -- `Z` sorts after `+00:00` at the same instant.
 *
 *  Fails closed on every uncertainty -- an unparseable stamp on either side, or the same
 *  instant -- because neither proves supersession. */
function supersedes(listed: string, recorded: string): boolean {
  const a = Date.parse(listed)
  const b = Date.parse(recorded)
  if (Number.isNaN(a) || Number.isNaN(b)) return false
  return a > b
}

/** Doom the coldest groups past `budget`, oldest ledger write first. */
function evictColdest(
  groups: Map<string, { keys: string[]; seenAt: number }>,
  budget: number,
  doomed: string[],
): void {
  if (groups.size <= budget) return
  const coldestFirst = [...groups.entries()].sort((a, b) => a[1].seenAt - b[1].seenAt)
  for (const [, group] of coldestFirst.slice(0, groups.size - budget)) doomed.push(...group.keys)
}

/** Prefixes whose contents are PURELY DERIVED and rebuild themselves from the DOM.
 *
 *  The capacity budget below may touch nothing else: a boot snapshot
 *  omits a session another tab has resumed, so an evicted "absent" session can be fully
 *  LIVE, and no tab can prove otherwise. Restricting eviction to these keys makes that
 *  harmless -- the virtualizer re-measures heights from the DOM and a lost reading anchor
 *  only opens the session at the bottom -- while still bounding the dominant growth. Panel
 *  tabs, activity, web-preview, busy-send and touched-file state are NOT here: they are
 *  the live session's own state and are only ever collected on proof of supersession. */
const DERIVED_PREFIXES: readonly string[] = [
  'vc_heights_',
  'vc_anchor3_',
  'vc_anchor2_',
  'vc_anchor_',
]

const isDerivedCacheKey = (key: string): boolean =>
  DERIVED_PREFIXES.some(p => key.startsWith(p))

const readOwnerEntry = (key: string): OwnerEntry | null => {
  try {
    const raw = localStorage.getItem(ownerEntryKey(key))
    if (raw) {
      const parsed = parseOwnerEntry(JSON.parse(raw) as unknown)
      if (parsed) return parsed
    }
  } catch { /* a corrupt entry is simply no proof */ }
  return null
}

const readOwners = (): OwnerLedger => {
  const out: OwnerLedger = {}
  try {
    for (let i = 0; i < localStorage.length; i++) {
      const k = localStorage.key(i)
      if (!k || !k.startsWith(OWNER_ENTRY_PREFIX)) continue
      const raw = localStorage.getItem(k)
      if (!raw) continue
      try {
        const entry = parseOwnerEntry(JSON.parse(raw) as unknown)
        if (entry) out[k.slice(OWNER_ENTRY_PREFIX.length)] = entry
      } catch { /* a corrupt entry is simply no proof */ }
    }
  } catch { /* an unreadable store yields no proof, which deletes nothing */ }
  return out
}


/** Which instance each live id is currently running as, learned from applied slot
 *  lists. In memory only: it answers "who is writing NOW", which no persisted value
 *  can, and a stale answer is exactly the hazard the ledger exists to avoid. */
const ORDER_KEY = 'mc-storage-gc-order'

type WireOrder = { generation?: number; epoch?: string }

const rememberOrder = (order?: WireOrder): void => {
  if (typeof order?.generation !== 'number' || !order.epoch) return
  try {
    localStorage.setItem(ORDER_KEY, JSON.stringify(order))
  } catch { /* an unwritable store simply keeps the previous value */ }
}

const rememberedOrder = (): WireOrder => {
  try {
    const raw = localStorage.getItem(ORDER_KEY)
    if (raw) {
      const v = JSON.parse(raw) as Record<string, unknown>
      if (typeof v.generation === 'number' && typeof v.epoch === 'string' && v.epoch !== '') {
        return { generation: v.generation, epoch: v.epoch }
      }
    }
  } catch { /* a corrupt value is simply no ordering */ }
  return {}
}

type LiveIdentity = { stamp: string; generation?: number; epoch?: string }
const liveInstances = new Map<string, LiveIdentity>()

/** Learn the DURABLE identity of every listed id. Call this with an APPLIED list, so a
 *  refused snapshot cannot re-date a session that has since been recreated.
 *
 *  `created`, deliberately NOT the process-local incarnation. These are two different
 *  questions and one field cannot answer both. A close tombstone asks "is this the instance
 *  I was closing, or a replacement?" and needs an identity minted per live object. Storage
 *  ownership asks "does this state belong to the session sitting on this key?" and needs one
 *  that SURVIVES a gateway restart: a restart rebuilds every slot object, so keying on the
 *  incarnation made the boot sweep erase the live panel, preview and activity state of every
 *  restored session. `created_at` is restored with the transcript, and a genuinely different
 *  session on a reused key brings its own, so it moves exactly when supersession is real.
 *  Retention where it cannot discriminate is bounded by the capacity budgets below, not by
 *  making this stamp more eager. */
/** Test-only: drop the per-session identities this module accumulates, so one suite's
 *  session cannot refuse a later suite's write. */
export function __resetSessionIdentities(): void {
  liveInstances.clear()
}

export function noteLiveSessionInstances(
  slots: readonly { key: string; created?: string }[],
  order?: WireOrder,
): void {
  rememberOrder(order)
  // A caller with no ordering must not ERASE the ordering a previous frame proved.
  const effective: WireOrder =
    typeof order?.generation === 'number' && order.epoch ? order : rememberedOrder()
  for (const s of slots) {
    if (s.created) {
      liveInstances.set(s.key, {
        stamp: s.created,
        generation: effective.generation,
        epoch: effective.epoch,
      })
    }
  }
}

/** Suffix a writer appends AFTER the session key. `useTouchedFiles` stamps its clear
 *  watermark at `<sessionKey>:toolClearedAt`, so the sweep strips it to recover the key
 *  and keep both in one group. A pattern, not a string, because the only thing needed
 *  here is the anchored match. */
const WATERMARK_SUFFIX_RE = /:toolClearedAt$/

/** Session id owning `key`, or null when the key is not session-scoped.
 *
 *  The id is the WHOLE remainder minus that suffix, never the first delimiter-separated
 *  field: a channel-scoped key is itself `slack:<ts>`, and `live` is keyed by the full
 *  session key, so truncating at the first colon yields `slack`, which matches no live
 *  session. Every such session then collapses into ONE absent group, so the capacity
 *  budget never sees more than one and their caches accumulate without bound, while a
 *  session that IS live reads as absent and becomes evictable. Stripping a known suffix
 *  rather than splitting also means a future key shape carrying a delimiter cannot
 *  reintroduce this. */
const sessionIdOf = (key: string): string | null => {
  for (const prefix of SESSION_PREFIXES) {
    if (key.startsWith(prefix)) {
      const id = key.slice(prefix.length).replace(WATERMARK_SUFFIX_RE, '')
      if (!id || isReservedName(id, prefix)) return null
      return id
    }
  }
  return null
}

/**
 * Record which instance is writing `key`, so the boot sweep can tell a live reused id
 * from a superseded one. Call it from every session-scoped write.
 *
 * THE LEDGER MUST BE WRITTEN BY THE WRITER. Stamping it from the
 * boot list instead recorded what the LIST said, not who wrote the bytes: a
 * deterministic slot recreated under its old key writes storage as the new instance
 * without touching the ledger, so the next boot sees the old stamp against the new
 * listing and deletes state that is live. Recording at the write keeps the two in step.
 *
 * A no-op for a key that is not session-scoped, and for an id whose current instance is
 * unknown -- an unstamped key is simply never collectable, which is the safe direction.
 */
/** True when this instance provably does NOT own `key`: someone recorded a stamp that ours
 *  neither matches nor supersedes. Unknown territory answers false -- a key with no recorded
 *  owner, one that is not session-scoped, and an id whose current instance we cannot name are
 *  all cases where nothing has been proven, and refusing those writes would lose live state
 *  rather than protect it. */
const writeWouldBeStale = (key: string): boolean => {
  const sessionId = sessionIdOf(key)
  if (!sessionId) return false
  const live = liveInstances.get(sessionId)
  if (!live) return false
  const recorded = readOwnerEntry(key)
  if (!recorded || recorded.stamp === live.stamp) return false
  // A clock correction can invert the stamps; a frame counter cannot.
  if (typeof live.generation === 'number' && typeof recorded.generation === 'number') {
    if (live.epoch === undefined) return false
    if (recorded.epoch !== live.epoch) {
      if (rememberedOrder().epoch === live.epoch) return false
      return supersedes(recorded.stamp, live.stamp)
    }
    return recorded.generation > live.generation
  }
  return supersedes(recorded.stamp, live.stamp)
}

export function recordSessionStorageOwner(key: string): void {
  if (typeof localStorage === 'undefined') return
  const sessionId = sessionIdOf(key)
  if (!sessionId) return
  const live = liveInstances.get(sessionId)
  if (!live) return
  if (writeWouldBeStale(key)) return
  clearClosed(sessionId)
  writeOwnerEntry(key, {
    stamp: live.stamp,
    seenAt: Date.now(),
    generation: live.generation,
    epoch: live.epoch,
  })
}

/** Write a session-scoped value AND stamp its owner. The pairing lives here rather than at
 *  each call site: a writer that stamps by hand is one a future writer can forget, and the
 *  ledger then dates state nobody recorded, which the boot sweep reads as collectable.
 *
 *  Ownership is checked BEFORE the value lands. Checking it only while stamping left a stale
 *  tab's bytes on top of the replacement session's own state and merely declined to re-date
 *  them, so the ledger stayed right about an owner whose data had already been overwritten. */
export function sessionOwnerStamp(sessionId: string): string | undefined {
  return liveInstances.get(sessionId)?.stamp
}

const isDevBuild = (): boolean => {
  try {
    return typeof import.meta !== 'undefined' && Boolean((import.meta as { env?: { DEV?: boolean } }).env?.DEV)
  } catch {
    return false
  }
}

const warnDroppedWrite = (key: string, reason: 'stale-owner' | 'stale-frame'): void => {
  // eslint-disable-next-line no-console -- the drop is correct; being silent about it was the finding
  if (isDevBuild()) console.warn(`storageGc: dropped session-scoped write to ${key} (${reason})`)
}

export function setSessionScopedItem(
  storage: Storage,
  key: string,
  value: string,
  writerOwner?: string,
): void {
  if (writerOwner !== undefined) {
    const sessionId = sessionIdOf(key)
    const live = sessionId ? liveInstances.get(sessionId) : undefined
    if (live && live.stamp !== writerOwner) {
      warnDroppedWrite(key, 'stale-owner')
      return
    }
  }
  if (writeWouldBeStale(key)) {
    warnDroppedWrite(key, 'stale-frame')
    return
  }
  storage.setItem(key, value)
  recordSessionStorageOwner(key)
}

const CLOSED_PREFIX = 'mc-storage-gc-closed:'

const closedKey = (sessionId: string): string => CLOSED_PREFIX + sessionId

type ClosedEvidence = { stamp: string; generation: number; epoch: string }

/** Record the identity the server CONFIRMED closed. */
export function noteSessionClosed(sessionId: string): void {
  if (typeof localStorage === 'undefined') return
  const live = liveInstances.get(sessionId)
  if (!live || typeof live.generation !== 'number' || !live.epoch) return
  try {
    localStorage.setItem(
      closedKey(sessionId),
      JSON.stringify({ stamp: live.stamp, generation: live.generation, epoch: live.epoch }),
    )
  } catch { /* best-effort: without it the keys are simply retained */ }
}

const readClosed = (sessionId: string): ClosedEvidence | null => {
  try {
    const raw = localStorage.getItem(closedKey(sessionId))
    if (!raw) return null
    const v = JSON.parse(raw) as Record<string, unknown>
    if (
      typeof v.stamp === 'string' && v.stamp !== '' &&
      typeof v.generation === 'number' && Number.isFinite(v.generation) &&
      typeof v.epoch === 'string' && v.epoch !== ''
    ) {
      return { stamp: v.stamp, generation: v.generation, epoch: v.epoch }
    }
  } catch { /* a corrupt record is simply no evidence */ }
  return null
}

const clearClosed = (sessionId: string): void => {
  try { localStorage.removeItem(closedKey(sessionId)) } catch { /* best-effort */ }
}

/** Drop close proofs minted by a gateway process this client has moved past. */
const dropUnusableClosed = (order?: WireOrder): void => {
  if (!order?.epoch) return
  const stale: string[] = []
  try {
    for (let i = 0; i < localStorage.length; i++) {
      const k = localStorage.key(i)
      if (!k || !k.startsWith(CLOSED_PREFIX)) continue
      const id = k.slice(CLOSED_PREFIX.length)
      const closed = readClosed(id)
      if (!closed || closed.epoch !== order.epoch) stale.push(id)
    }
  } catch { /* an unreadable store leaves them for the next boot */ }
  for (const id of stale) clearClosed(id)
}

/** Closure evidence usable by a sweep whose own snapshot POSTDATES the close. */
const closedProof = (sessionId: string, order?: WireOrder): ClosedEvidence | null => {
  const closed = readClosed(sessionId)
  if (!closed) return null
  if (!order?.epoch || order.epoch !== closed.epoch) return null
  if (typeof order.generation !== 'number' || order.generation <= closed.generation) return null
  return closed
}

/** True when the two stamps name ONE instant, however each is spelled. */
function sameInstant(a: string, b: string): boolean {
  const x = Date.parse(a)
  const y = Date.parse(b)
  if (Number.isNaN(x) || Number.isNaN(y)) return false
  return x === y
}

/** True when the LISTED instance provably replaced the one that wrote `entry`. */
const listedSupersedes = (sessionId: string, stamp: string, entry: OwnerEntry): boolean => {
  if (entry.stamp === stamp) return false
  // `supersedes` fails closed on one instant; the generation branch below reaches its
  // comparison without ever parsing, so `Z` against `+00:00` read as a replacement.
  if (sameInstant(stamp, entry.stamp)) return false
  const live = liveInstances.get(sessionId)
  // A clock correction can invert the stamps; a frame counter cannot.
  if (typeof live?.generation === 'number' && typeof entry.generation === 'number') {
    if (live.epoch === undefined || entry.epoch !== live.epoch) return false
    return live.generation > entry.generation
  }
  return supersedes(stamp, entry.stamp)
}

/**
 * Remove localStorage keys whose owning session instance has provably been REPLACED.
 * Call once on app boot after fetching the slot list.
 *
 * ABSENCE FROM A LIST IS NEVER EVIDENCE, AND NEITHER IS AGE.
 * `localStorage` is shared by every tab on the origin while the list is one tab's
 * snapshot, and SESSION KEYS ARE REUSED. So an aged orphan is not a dead one: a key
 * legitimately idle past any grace can be resumed under the SAME id by another tab
 * after this boot's snapshot serialized, and deleting it then destroys that tab's live
 * UI state. Age proves only that the id WAS unlisted, which is the wrong proposition.
 *
 * The evidence used instead is SUPERSESSION, and it is the WRITER's stamp that supplies
 * it: `recordSessionStorageOwner` records which instance wrote the state, so a key is
 * deleted only when the live list presents its id as a DIFFERENT instance than the one
 * that wrote it. This sweep never stamps the ledger itself -- doing so would date state
 * it did not write and would delete a live reused session's keys.
 *
 * Returns the number of keys removed.
 */
export function gcOrphanedStorage(
  liveSlots: readonly { key: string; created?: string }[],
  order?: WireOrder,
): number {
  if (typeof localStorage === 'undefined') return 0
  let removed = 0
  const live = new Map<string, string>()
  for (const s of liveSlots) live.set(s.key, s.created ?? '')
  noteLiveSessionInstances(liveSlots, order)
  const owners = readOwners()
  const survivors: OwnerLedger = { ...owners }
  // Collect doomed keys first — removing during iteration shifts indices.
  const doomed: string[] = []
  const closedIds = new Set<string>()
  const deferredClosed = new Set<string>()
  let closedBudget = MAX_CLOSED_KEYS_PER_SWEEP
  const seen = new Set<string>()
  // Absent session id -> its keys, and the newest write seen across them.
  const absent = new Map<string, { keys: string[]; seenAt: number }>()
  for (let i = 0; i < localStorage.length; i++) {
    const key = localStorage.key(i)
    if (!key) continue
    const sessionId = sessionIdOf(key)
    if (!sessionId) continue
    seen.add(key)
    const stamp = live.get(sessionId)
    const entry = owners[key]
    if (stamp === undefined) {
      const closed = closedProof(sessionId, order)
      // Only REBUILDABLE keys: a tab resuming this id shares this storage.
      if (isDerivedCacheKey(key) && closed) {
        doomed.push(key)
        closedIds.add(sessionId)
        continue
      }
      if (
        closed && entry && entry.stamp === closed.stamp &&
        isUnresumableSessionId(sessionId)
      ) {
        if (closedBudget > 0) {
          closedBudget -= 1
          doomed.push(key)
          closedIds.add(sessionId)
        } else {
          deferredClosed.add(sessionId)
        }
        continue
      }
      // Unlisted. ONLY re-derivable caches are capacity candidates: real state is deleted
      // on proof, never on a budget, because this session may be live in another tab.
      if (isDerivedCacheKey(key)) {
        const group = absent.get(sessionId) ?? { keys: [], seenAt: 0 }
        group.keys.push(key)
        group.seenAt = Math.max(group.seenAt, entry?.seenAt ?? 0)
        absent.set(sessionId, group)
      }
      continue
    }
    // Delete ONLY on positive proof that the listed instance SUPERSEDES this key's writer.
    // Inequality is not that proof: it holds in both directions. See `supersedes`.
    if (stamp !== '' && entry && listedSupersedes(sessionId, stamp, entry)) doomed.push(key)
  }
  // Coldest-first by the ledger's own seenAt, never an age gate: every session-scoped write
  // stamps it, so a session live in ANOTHER TAB stays warmest and is the last candidate.
  evictColdest(absent, MAX_ABSENT_SESSIONS, doomed)
  for (const id of closedIds) if (!deferredClosed.has(id)) clearClosed(id)
  for (const s of liveSlots) clearClosed(s.key)
  dropUnusableClosed(order)
  for (const key of doomed) {
    try { localStorage.removeItem(key); removed++ } catch { /* best-effort */ }
    delete survivors[key]
  }
  // A key that no longer exists drops out, which is what keeps the ledger bounded. A
  // surviving key keeps the stamp its OWN writer recorded, never a sibling family's.
  for (const k of Object.keys(survivors)) {
    if (!seen.has(k)) delete survivors[k]
  }
  for (const k of Object.keys(owners)) {
    if (!Object.prototype.hasOwnProperty.call(survivors, k)) dropOwnerEntry(k)
  }
  if (removed > 0 && import.meta.env.DEV) {
    // eslint-disable-next-line no-console
    console.log(`[storageGc] removed ${removed} superseded key(s)`)
  }
  return removed
}

