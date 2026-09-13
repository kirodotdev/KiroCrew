/**
 * Per-slot persistence for a pane send the transport handed back — the text, the
 * attachments and the send id it was minted with.
 *
 * `ChatPane` holds the same payload in a component ref (`strandedSends`) for the tab's life, so
 * without this store the payload of a send that timed out before reaching the gateway would exist
 * in exactly one place that a reload destroys. The composer has already been cleared, and the
 * optimistic bubble is store-only, so nothing else carries the user's words. Both halves arrive
 * together in this change: the ref is not pre-existing state this store was extracted from.
 *
 * localStorage, deliberately, and on the same TTL as `chatDrafts`. This store owns BOTH
 * surfaces' records — the pane's payload and ChatPage's marker-only form — because they are
 * one concept and were previously two stores that had to expire together by hand. A payload in
 * sessionStorage would die on tab close while a marker survived, leaving a warning about a send
 * whose text is gone — strictly worse than losing both.
 */
import { safeSetItem, safeGetSessionItem, safeSetSessionItem } from './safeStorage'
import { DRAFT_MAX_ENTRIES, DRAFT_TTL_MS, RECOVERY_MAX_STORE_BYTES } from './draftConstants'

export const PANE_RECOVERY_KEY = 'mc-chat-pane-recovery'

export interface PaneRecovery {
  /** Empty on ChatPage's marker-only record: it names the send without carrying a payload. */
  text: string
  files: string[]
  sendId?: string
  /** Bumped on every write, so a receipt can tell the payload it consumed from a newer draft. */
  gen?: number
  /** The SEND's own fragment, distinct from `text` when the composer had mid-flight work merged
   *  into it. Gates the Discard exit, which must never offer to delete more than it restored. */
  sent?: string
  sentFiles?: string[]
  /** The browsing context that parked this. Two tabs on one slot share the store, so without it the
   *  slot-wide reader handed a sibling's send over and this tab's settlement then retired it. */
  tabId?: string
  /** Whether RESENDING this could duplicate it -- true only where delivery is genuinely in doubt.
   *  ABSENT means unknown, which arms: a spurious warning is recoverable, a silent duplicate is not. */
  mayDuplicate?: boolean
}

export type PaneRecoveries = Record<string, PaneRecovery>

/** Reject anything not shaped like a recovery, so a hand-edited or older value is
 *  dropped rather than restored as a half-record. */
const sanitize = (v: unknown): PaneRecovery | null => {
  if (typeof v !== 'object' || v === null) return null
  const r = v as Record<string, unknown>
  const text = typeof r.text === 'string' ? r.text : ''
  const files = Array.isArray(r.files) ? r.files.filter((f): f is string => typeof f === 'string') : []
  const sendId = typeof r.sendId === 'string' && r.sendId ? r.sendId : undefined
  // A record must carry SOMETHING: a payload, or the send id whose caption it drives.
  if (!text && !files.length && !sendId) return null
  const gen = typeof r.gen === 'number' && Number.isFinite(r.gen) ? r.gen : undefined
  const sent = typeof r.sent === 'string' ? r.sent : undefined
  const sentFiles = Array.isArray(r.sentFiles) ? r.sentFiles.filter((f): f is string => typeof f === 'string') : undefined
  const tabId = typeof r.tabId === 'string' && r.tabId ? r.tabId : undefined
  const mayDuplicate = typeof r.mayDuplicate === 'boolean' ? r.mayDuplicate : undefined
  return {
    text,
    files,
    ...(sendId ? { sendId } : {}),
    ...(gen !== undefined ? { gen } : {}),
    ...(sent !== undefined ? { sent } : {}),
    ...(sentFiles !== undefined ? { sentFiles } : {}),
    ...(tabId ? { tabId } : {}),
    ...(mayDuplicate !== undefined ? { mayDuplicate } : {}),
  }
}

/** ONE storage key per record.
 *
 *  A single shared blob made every write a cross-tab read-modify-write: two tabs that read the
 *  blob before either saved lost the earlier record, and a payload has no second copy once it is
 *  gone. Re-reading immediately before the write narrowed that window without closing it, because
 *  the interleaving happens between processes. Per-record keys remove the shared cell instead, so
 *  two sends cannot collide at all — no lock, and no window to serialize. */
const fullKey = (k: string): string => `${PANE_RECOVERY_KEY}:${k}`
const isFullKey = (s: string): boolean => s.startsWith(`${PANE_RECOVERY_KEY}:`)
const shortKey = (s: string): string => s.slice(PANE_RECOVERY_KEY.length + 1)

interface Stamped { v: unknown; ts: number }
type Live = Record<string, { rec: PaneRecovery; ts: number }>

const ls = (): Storage | null => {
  try { return localStorage } catch { return null }
}

const drop = (full: string): void => {
  try { ls()?.removeItem(full) } catch { /* a failed delete is retried on the next read */ }
}

/** Every live record, pruning anything expired or unparseable as it goes. */
const readLive = (): Live => {
  const out: Live = {}
  const s = ls()
  if (!s) return out
  const doomed: string[] = []
  for (let i = 0; i < s.length; i++) {
    const full = s.key(i)
    if (!full || !isFullKey(full)) continue
    try {
      const parsed = JSON.parse(s.getItem(full) || 'null') as Stamped | null
      const rec = parsed && typeof parsed.ts === 'number' ? sanitize(parsed.v) : null
      if (!rec || Date.now() - (parsed as Stamped).ts > DRAFT_TTL_MS) { doomed.push(full); continue }
      out[shortKey(full)] = { rec, ts: (parsed as Stamped).ts }
    } catch { doomed.push(full) }
  }
  for (const d of doomed) drop(d)
  return out
}

/** `safeSetItem`, not a bare `setItem`: this record is the only copy of a prompt the composer has
 *  already cleared, and `enforceBudget` runs AFTER the write and evicts only this store — so an
 *  origin filled by uncapped siblings left the write permanently failed and silently lost it.
 *
 *  Returns whether it LANDED. `safeSetItem` still answers false when the quota is exhausted and
 *  reclaim frees nothing, and discarding that made a failed write indistinguishable from a durable
 *  one — so the caller believed a prompt was recoverable when nothing had been stored. */
const writeOne = (key: string, rec: PaneRecovery, ts: number): boolean =>
  safeSetItem(fullKey(key), JSON.stringify({ v: rec, ts } satisfies Stamped))

/** A record still CARRYING work — the thing this store exists to be the only copy of. Markers
 *  (`setStagedSend` writes an empty one purely to name a send) carry none. */
const holdsPrompt = (rec: PaneRecovery): boolean =>
  rec.text.trim().length > 0 || rec.files.length > 0

/** Bounds the store WITHOUT ever deleting an unresolved prompt.
 *
 *  Only markers are evictable. Evicting by age here reached the oldest RECORD, which past the
 *  entry cap is the only durable copy of a prompt the composer already cleared — so an
 *  intermittently-offline user accumulating recoveries silently lost the earliest one. A prompt
 *  leaves only by settlement (discard, a definitive receipt) or by the TTL that expires stale
 *  content, and `setPaneRecoveryFor` does not consult this bound for a prompt-bearing record at
 *  all: the budget governs BOOKKEEPING, and a refusal would make the user's words temporary.
 *  So this answers FALSE only when markers alone cannot bring the marker-only total under. */
const enforceBudget = (keep?: string): boolean => {
  const live = readLive()
  const bytes = (): number =>
    Object.entries(live).reduce((n, [k, e]) => n + fullKey(k).length + JSON.stringify({ v: e.rec, ts: e.ts }).length, 0)
  const over = (): boolean =>
    Object.keys(live).length > DRAFT_MAX_ENTRIES || bytes() > RECOVERY_MAX_STORE_BYTES
  // Only BYTES are hard. The entry cap still yields to an unresolved prompt: exceeding a count
  // exhausts nothing, and refusing there would discard work the store exists to be the copy of.
  const overBytes = (): boolean => bytes() > RECOVERY_MAX_STORE_BYTES
  const evictable = (): string[] =>
    Object.entries(live)
      .filter(([k, e]) => k !== keep && !holdsPrompt(e.rec))
      .sort((a, b) => a[1].ts - b[1].ts)
      .map(([k]) => k)
  let next = evictable()
  while (over() && next.length) {
    drop(fullKey(next[0]))
    delete live[next[0]]
    next = evictable()
  }
  // Markers ONLY. A record still carrying an unsent prompt is never evicted for budget: this store is
  // that prompt's only durable home, so reclaiming it is an unrecoverable loss of the user's words.
  return !overBytes()
}

/** @internal test-only */
export const __resetPaneRecoveryForTests = (): void => {
  const s = ls()
  if (s) {
    const doomed: string[] = []
    for (let i = 0; i < s.length; i++) { const k = s.key(i); if (k && isFullKey(k)) doomed.push(k) }
    for (const d of doomed) drop(d)
  }
}

/** ChatPage's marker for a restored send, in this same store under a SURFACE-QUALIFIED key.
 *
 *  Qualified because the two surfaces can address ONE slot at the same time: `MembersPage`
 *  renders `<ChatPane slotKey={activeSlot}>`, so an unqualified key would let the page's
 *  marker overwrite the pane's payload for that slot — losing exactly the words this store
 *  exists to keep. One store, one record shape, one TTL; two records that cannot collide. */
const pageKey = (slot: string): string => {
  // Owner-qualified on the WRITE side only, so one tab's dismiss cannot retire another's
  // duplicate-send warning. `loadStagedSend` reads every owner's marker for the slot.
  const mine = paneTabId()
  return mine ? `page:${slot}|${mine}` : `page:${slot}`
}

/** A pane payload is keyed by SLOT **and SEND**, because two tabs can each hand back a failed
 *  send for the same slot: one shared key made the later write replace the earlier prompt, and
 *  a payload has no in-system recovery once overwritten (unlike a draft, which the composer
 *  still holds). `slotDraftStore`'s accepted last-write-wins covers drafts for that reason and
 *  does not extend here. */
/** Identifies THIS browsing context for the recovery store.
 *
 *  sessionStorage is the only per-tab store that ALSO survives a reload, and both halves are load
 *  bearing: `api/tabId`'s in-memory id is per page LOAD, so owning by it would refuse a reload its
 *  own park -- the case this store exists for. Read rather than memoized so a new context is exactly
 *  an empty sessionStorage.
 */
const TAB_OWNER_KEY = `${PANE_RECOVERY_KEY}:tab`

/** The owner id plus the page load that settled it, so an INHERITED entry is recognisable. */
interface TabOwner { id: string; origin: number }

/** A legacy entry is the bare id, written before the load was recorded alongside it. */
const readOwner = (raw: string): Partial<TabOwner> | undefined => {
  try {
    return JSON.parse(raw) as Partial<TabOwner>
  } catch {
    return undefined
  }
}

// Deliberately OUTSIDE `PANE_RECOVERY_KEY:`, which `readLive` sweeps: anything under that prefix
// that is not a stamped record is dropped as corrupt, so a claim stored there deleted itself.
const TAB_CLAIM_PREFIX = 'mc-chat-pane-claim:'
const CLAIM_REFRESH_MS = 60_000
/** Generous against background-tab timer throttling, which can starve the refresh to about 1/min. */
const CLAIM_LIVE_MS = 300_000

/** Whether a LIVE context still CONFIRMS it holds this owner id.
 *
 *  Confirmation is a heartbeat its holder keeps refreshing, never the mere presence of the key. A
 *  renderer crash cannot run the release, so a presence test let one abandoned claim orphan the only
 *  copy of an unsent prompt for the store's whole TTL -- the loss this store exists to prevent.
 *  Navigation type cannot make this call either: an ordinary in-app navigation and a duplicated tab
 *  both report `navigate`, so keying on that abandoned a parked send on every navigation. */
const claimConfirmedElsewhere = (id: string): boolean => {
  try {
    const raw = globalThis.localStorage?.getItem(TAB_CLAIM_PREFIX + id)
    const at = raw ? Number(raw) : Number.NaN
    return Number.isFinite(at) && Date.now() - at < CLAIM_LIVE_MS
  } catch {
    return false
  }
}

/** Whether this load REPLACED the same document. No separate tab can present as a reload, so this is
 *  self-confirmation that the id is still ours -- and it recovers a crash before the heartbeat ages. */
const reachedByReload = (): boolean => {
  try {
    const nav = performance?.getEntriesByType?.('navigation')?.[0] as { type?: string } | undefined
    return nav?.type === 'reload'
  } catch {
    return false
  }
}

const refreshClaim = (id: string, force = false): void => {
  try {
    const raw = globalThis.localStorage?.getItem(TAB_CLAIM_PREFIX + id)
    const at = raw ? Number(raw) : Number.NaN
    if (!force && Number.isFinite(at) && Date.now() - at < CLAIM_REFRESH_MS / 2) return
    globalThis.localStorage?.setItem(TAB_CLAIM_PREFIX + id, String(Date.now()))
  } catch { /* unrefreshed, the claim simply stops confirming, which keeps records adoptable */ }
}

/** Start this context's heartbeat and stop it as the document goes away. Release is an optimisation,
 *  not the guarantee: an unreleased claim ages out, so a crash cannot strand a record for good. */
const holdClaim = (id: string): void => {
  try {
    globalThis.localStorage?.setItem(TAB_CLAIM_PREFIX + id, String(Date.now()))
    globalThis.setInterval?.(() => refreshClaim(id), CLAIM_REFRESH_MS)
    globalThis.addEventListener?.('pagehide', (e: Event) => {
      // A BFCache pagehide FREEZES the document rather than ending it, so releasing there hands a
      // still-live tab's records to another context; the restore fires `pageshow` instead.
      if ((e as PageTransitionEvent).persisted) return
      try {
        globalThis.localStorage?.removeItem(TAB_CLAIM_PREFIX + id)
      } catch { /* the heartbeat stops either way, so the claim stops confirming on its own */ }
    })
    // Timers are frozen while cached, so the claim can have aged out; restate it on restore.
    globalThis.addEventListener?.('pageshow', () => refreshClaim(id, true))
    // A FROZEN tab is not a BFCached one, so no `pageshow` fires when it thaws: without this the
    // claim ages out while the tab is alive and another context adopts its records.
    globalThis.document?.addEventListener?.('resume', () => refreshClaim(id, true))
  } catch { /* no localStorage: the cross-tab guard degrades, recovery does not */ }
}

const paneTabId = (): string | undefined => {
  try {
    const s = globalThis.sessionStorage
    if (!s) return undefined
    const origin = Math.round(performance?.timeOrigin ?? 0)
    const raw = s.getItem(TAB_OWNER_KEY)
    const held = raw ? readOwner(raw) : undefined
    const id = typeof held?.id === 'string' && held.id ? held.id : undefined
    // Settled by THIS load already, so return it rather than re-deciding on every read.
    if (id && held?.origin === origin) { refreshClaim(id); return id }
    // sessionStorage is copied verbatim into a duplicated tab, so an inherited id would make two live
    // contexts one owner -- but only a live holder's own confirmation may take the id away.
    const keep = id !== undefined && (reachedByReload() || !claimConfirmedElsewhere(id))
    const next: TabOwner = {
      id: keep ? id : (globalThis.crypto?.randomUUID?.() ?? `t-${Math.random().toString(36).slice(2)}-${Date.now()}`),
      origin,
    }
    s.setItem(TAB_OWNER_KEY, JSON.stringify(next))
    holdClaim(next.id)
    return next.id
  } catch {
    // No sessionStorage (private mode, blocked storage): stamp nothing, so records stay adoptable
    // exactly as before. Losing the cross-tab guard beats losing recovery outright.
    return undefined
  }
}

/** Whether this context may take a record. Withheld only while a LIVE context still confirms the
 *  record's owner, so an UNOWNED record stays adoptable -- one parked before this shipped, or by a
 *  context with no sessionStorage -- and so does one whose owner is gone, however its id was lost.
 *  Refusing either would strand a real prompt, the same harm class as the cross-tab overwrite. */
const ownedHere = (rec: PaneRecovery): boolean => {
  const mine = paneTabId()
  if (!rec.tabId || !mine || rec.tabId === mine) return true
  return !claimConfirmedElsewhere(rec.tabId)
}

const paneKey = (slot: string, sendId?: string): string =>
  sendId ? `pane:${slot}|${sendId}` : `pane:${slot}`

const ownsSlot = (key: string, slot: string): boolean =>
  key === paneKey(slot) || key.startsWith(`${paneKey(slot)}|`)

/** The newest record parked for a slot, by the timestamp it was PARKED at.
 *
 *  Not by `gen`: that counts edits to ONE record, so a record edited twice outranked a record
 *  parked later and a reload restored the older composer content. */
export const loadPaneRecovery = (slot: string): PaneRecovery | undefined => {
  let best: { rec: PaneRecovery; ts: number } | undefined
  for (const [k, e] of Object.entries(readLive())) {
    if (!ownsSlot(k, slot) || !ownedHere(e.rec)) continue
    if (!best || e.ts >= best.ts) best = e
  }
  return best?.rec
}

const REFUSED_FALLBACK_PREFIX = 'mc-chat-refused-recovery:'

/** A refused write's payload, kept where a reload can still find it. Not a substitute for the store --
 *  it is per-tab and per-session -- but it answers the reload that would otherwise lose the words. */
let refusedSeq = 0

const parkRefused = (key: string, rec: PaneRecovery): void => {
  try {
    // Sequenced as well as stamped: two refusals inside one millisecond carry the same `ts`, and then
    // "newest" is decided by whatever order the scan happens to visit the keys in.
    safeSetSessionItem(REFUSED_FALLBACK_PREFIX + key,
      JSON.stringify({ v: rec, ts: Date.now(), n: ++refusedSeq }))
  } catch { /* nothing more to fall back to; the caller still holds it in memory */ }
}

/** Retires a parked payload. Called wherever the durable record is retired: a discarded prompt that
 *  survives only in the fallback comes back on the next restore, which is a prompt the user rejected. */
const dropRefused = (key: string): void => {
  try {
    globalThis.sessionStorage?.removeItem(REFUSED_FALLBACK_PREFIX + key)
  } catch { /* the record is already unreachable if sessionStorage is gone */ }
}

/** The payload of a write this store refused, for a caller rebuilding after a reload. */
const readRefused = (key: string): { rec: PaneRecovery; ts: number; n: number } | undefined => {
  try {
    const raw = safeGetSessionItem(REFUSED_FALLBACK_PREFIX + key)
    const parsed = raw ? (JSON.parse(raw) as { v?: unknown; ts?: unknown; n?: unknown }) : null
    const rec = parsed ? sanitize(parsed.v) : null
    if (!rec) return undefined
    return {
      rec,
      ts: typeof parsed?.ts === 'number' ? parsed.ts : 0,
      n: typeof parsed?.n === 'number' ? parsed.n : 0,
    }
  } catch {
    return undefined
  }
}

/** `readRefused` filtered by ownership, for the paths that ADOPT a record rather than probe for one.
 *
 *  sessionStorage is copied verbatim into a duplicated tab, so the fallback hands the copy its
 *  source's record. The durable readers have always applied this; without it here, a tab that never
 *  owned the send could restore and resend it. */
const readRefusedOwned = (key: string): { rec: PaneRecovery; ts: number; n: number } | undefined => {
  const found = readRefused(key)
  return found && ownedHere(found.rec) ? found : undefined
}

/** The newest refused record the slot owns, keeping its stamp so a caller can compare it against the
 *  durable store. One implementation, so the slot-only scan cannot drift from the load order. */
const loadRefusedRecoveryStamped = (slot: string): { rec: PaneRecovery; ts: number; n: number } | undefined => {
  // A refused write is parked under its ID-QUALIFIED key, so rebuilding `pane:<slot>` finds nothing:
  // a reload knows the slot but cannot reconstruct the id. Resolve the newest record the slot owns.
  let best: { rec: PaneRecovery; ts: number; n: number } | undefined
  try {
    const s = globalThis.sessionStorage
    for (let i = 0; i < (s?.length ?? 0); i++) {
      const full = s?.key(i)
      if (!full || !full.startsWith(REFUSED_FALLBACK_PREFIX)) continue
      const key = full.slice(REFUSED_FALLBACK_PREFIX.length)
      if (!ownsSlot(key, slot)) continue
      const found = readRefusedOwned(key)
      if (found && (!best || found.ts > best.ts || (found.ts === best.ts && found.n > best.n))) best = found
    }
  } catch { /* no sessionStorage: the fallback degrades, the in-memory copy is unaffected */ }
  return best
}

export const loadRefusedRecovery = (slot: string, sendId?: string): PaneRecovery | undefined =>
  sendId ? readRefusedOwned(paneKey(slot, sendId))?.rec : loadRefusedRecoveryStamped(slot)?.rec

/** The newest record for a slot across BOTH stores: the durable one and the refused fallback.
 *
 *  Not `durable ?? refused`: a refusal is what this store does when it is FULL, so a newer refused
 *  record routinely sits beside an older durable one. Precedence then restored the stale durable text
 *  and the words the user typed most recently were gone with no way back. On an equal stamp the
 *  refused record wins, because it is the copy that only this fallback still holds. */
export const loadNewestRecoveryForSlot = (slot: string): PaneRecovery | undefined => {
  let durable: { rec: PaneRecovery; ts: number } | undefined
  for (const [k, e] of Object.entries(readLive())) {
    if (!ownsSlot(k, slot) || !ownedHere(e.rec)) continue
    if (!durable || e.ts >= durable.ts) durable = e
  }
  const refused = loadRefusedRecoveryStamped(slot)
  if (!refused) return durable?.rec
  if (!durable) return refused.rec
  return refused.ts >= durable.ts ? refused.rec : durable.rec
}

/** Reclaims marker-only records to free ORIGIN bytes, oldest first, and reports whether any went.
 *
 *  Same rule `enforceBudget` applies -- prompts are never taken -- but run for a different reason: the
 *  origin is shared, so a sibling store can exhaust it while this store sits under its own budget. */
const evictMarkers = (keep: string): boolean => {
  const live = readLive()
  const spare = Object.entries(live)
    .filter(([k, e]) => k !== keep && !holdsPrompt(e.rec))
    .sort((a, b) => a[1].ts - b[1].ts)
    .map(([k]) => k)
  for (const k of spare) drop(fullKey(k))
  return spare.length > 0
}

/** Re-reading no longer matters: this writes ONE key, so a concurrent tab's record for another
 *  send is untouched rather than merely likely to survive.
 *
 *  Returns whether the record is actually DURABLE. A caller holding the only other copy must keep
 *  it when this answers false, rather than clearing on the assumption that the store took it. */
export const setPaneRecoveryFor = (slot: string, rec: PaneRecovery): boolean => {
  const key = paneKey(slot, rec.sendId)
  const owned = rec.tabId ? rec : { ...rec, ...(paneTabId() ? { tabId: paneTabId() } : {}) }
  // Captured BEFORE the write, because `writeOne` OVERWRITES this key: a rollback that merely dropped
  // it destroyed a record that was already durable, so an over-budget payload cost the caller both.
  const prior = readLive()[key]
  let landed = writeOne(key, owned, Date.now())
  // A quota refusal can come from a SIBLING store filling the shared origin, which this store's own
  // budget cannot see. Reclaim our markers and retry before conceding the prompt to session-only.
  if (!landed && evictMarkers(key)) landed = writeOne(key, owned, Date.now())
  // The cap binds EVERY record, prompts included: skipping it let unresolved sends accumulate under
  // per-send keys until the shared origin quota went, taking the sibling draft stores with it.
  if (!enforceBudget(key)) {
    // Retained outside localStorage rather than dropped, and `false` tells the caller to keep its own
    // copy: a refused prompt is reachable for this session instead of being lost outright.
    parkRefused(key, owned)
    if (prior) writeOne(key, prior.rec, prior.ts)
    else drop(fullKey(key))
    return false
  }
  // Read back rather than trusting the return alone: the budget pass runs after the write, and a
  // failure there would leave a `true` naming a record that is no longer on disk.
  const durable = landed && readLive()[key] !== undefined
  // A write can fail while this store stays UNDER its own budget, because the origin is shared: a
  // sibling store can exhaust the quota, and then only the caller's in-memory copy is left.
  if (!durable) parkRefused(key, owned)
  return durable
}

/** The record for ONE named send, for a caller that must not act on merely the newest. */
export const loadPaneRecoveryById = (slot: string, sendId: string): PaneRecovery | undefined => {
  const rec = readLive()[paneKey(slot, sendId)]?.rec
  return rec && ownedHere(rec) ? rec : undefined
}

/** Re-key an UNIDENTIFIED record onto the send now resending it, and return it.
 *
 *  A refusal restores without a send id, so its record lands under the bare slot key with nothing
 *  able to retire it: the retry's settlement found no match and a reload resurrected a prompt the
 *  server had since accepted. Binding it here gives that settlement something to retire. */
export const adoptPaneRecovery = (slot: string, sendId: string): PaneRecovery | undefined => {
  const bareKey = paneKey(slot)
  // BOTH homes: a durable refusal is the very case this adoption exists for, so the bare record it
  // must bind is routinely the one the store rejected and only the session fallback still holds.
  const durableBare = readLive()[bareKey]?.rec
  const bare = durableBare ?? readRefused(bareKey)?.rec
  if (!bare || !ownedHere(bare)) return undefined
  const bound = { ...bare, sendId }
  const boundKey = paneKey(slot, sendId)
  const landed = writeOne(boundKey, bound, Date.now()) && readLive()[boundKey] !== undefined
  if (!landed) {
    // A DURABLE bare record is the only durable copy, so it stays put: parking the bound record in
    // the session fallback instead would trade it for a copy that dies with the tab.
    if (durableBare) return undefined
    // The bare record was itself refused, so no durable copy exists to lose. Park the BOUND one where
    // the retry's settlement can reach it; under the bare key nothing could retire it.
    parkRefused(boundKey, bound)
    if (!readRefused(boundKey)) return undefined
  }
  drop(fullKey(bareKey))
  dropRefused(bareKey)
  return bound
}

/** Retires ONE send's record, named by its id, leaving another tab's parked send for the same slot.
 *
 *  The id is REQUIRED, and deliberately so: two tabs on one slot is a shape this feature's own
 *  tests exercise, and a slot-wide sweep there deleted a sibling's only copy. With no positive
 *  ownership identity the correct action is to retain, so there is no arm that can guess. An
 *  unidentified record is retired by `adoptPaneRecovery` re-keying it onto the send that resends it. */
export const clearPaneRecoveryFor = (slot: string, sendId: string): void => {
  drop(fullKey(paneKey(slot, sendId)))
  dropRefused(paneKey(slot, sendId))
}

/** Retires the slot's UNIDENTIFIED record — the one a refusal restored without a send id.
 *
 *  Deliberately ONE deterministic key rather than a sweep over everything `ownsSlot` matches: the
 *  bare key is itself a positive identity, and it cannot name a sibling tab's `slot|sendId` record. */
export const clearUnidentifiedPaneRecovery = (slot: string): void => {
  drop(fullKey(paneKey(slot)))
  dropRefused(paneKey(slot))
}

/** Every marker key for a slot, whatever context wrote it — the `page:` analogue of `ownsSlot`. */
const ownsPageSlot = (key: string, slot: string): boolean =>
  key === `page:${slot}` || key.startsWith(`page:${slot}|`)

/** Marker fallback in a quota pool the record store does not share: sessionStorage survives the
 *  reload at risk, and the full localStorage that refuses the marker does not exhaust it. */
const STAGED_FALLBACK_PREFIX = 'mc-chat-staged-send:'

/** The slot's staged send, read ACROSS owners while `setStagedSend` still writes per owner.
 *
 *  The draft this marker describes is SLOT-scoped and shared between tabs, and is deliberately left
 *  populated for a revisit-and-resend — so a marker only its author could see let a sibling tab
 *  restore that draft unwarned and resend an accepted turn. Reading wide is safe where writing wide
 *  was not: a marker carries no prompt (`holdsPrompt` is false for it), so no sibling's words can be
 *  lost, and a stale one costs at worst a duplicate-send warning nobody needed — the safe direction.
 *  Ownership is deliberately NOT consulted: `ownedHere` governs who may TAKE a record, and warning
 *  about a possible duplicate is not taking one. */
export const loadStagedSend = (slot: string): string | undefined => {
  let best: { sendId?: string; ts: number } | undefined
  for (const [k, e] of Object.entries(readLive())) {
    if (!ownsPageSlot(k, slot)) continue
    if (!best || e.ts >= best.ts) best = { sendId: e.rec.sendId, ts: e.ts }
  }
  return best?.sendId ?? (safeGetSessionItem(STAGED_FALLBACK_PREFIX + slot) || undefined)
}

/** Whether the marker is DURABLE; a refused write used to be swallowed here. */
export const setStagedSend = (slot: string, sendId: string): boolean => {
  const key = pageKey(slot)
  const landed = writeOne(key, { text: '', files: [], sendId }, Date.now())
  const bounded = enforceBudget(key)
  if (!bounded) drop(fullKey(key))
  // Read back rather than trusting the write: the budget pass runs after it.
  if (bounded && landed && readLive()[key] !== undefined) return true
  return safeSetSessionItem(STAGED_FALLBACK_PREFIX + slot, sendId)
}

/** Retires THIS context's marker, plus any whose owning context is gone.
 *
 *  A tab can now see a sibling's marker, so it must be able to dismiss one that nothing is left to
 *  settle; a LIVE owner's marker is left, which is the isolation `pageKey` exists for. */
export const clearStagedSend = (slot: string): void => {
  drop(fullKey(pageKey(slot)))
  try {
    globalThis.sessionStorage?.removeItem(STAGED_FALLBACK_PREFIX + slot)
  } catch { /* no sessionStorage: there was no fallback to retire either */ }
  const prefix = `page:${slot}|`
  for (const k of Object.keys(readLive())) {
    if (!k.startsWith(prefix)) continue
    const owner = k.slice(prefix.length)
    if (owner && !claimConfirmedElsewhere(owner)) drop(fullKey(k))
  }
}
