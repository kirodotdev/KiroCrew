/**
 * Which slots hold unsent composer work, ACROSS windows?
 *
 * `slotComposerRegistry` is a module Map, so it knows only the composers mounted in ITS
 * OWN window, and the persisted drafts do not close that hole because their write is
 * debounced — between a keystroke in a popout and the flush, another window reads clean
 * state and deletes the slot.
 *
 * This beacon is the synchronous half: `localStorage` writes are visible to every
 * same-origin window the moment they return, so publishing on each dirty transition
 * answers across windows without waiting for the debounce.
 *
 * Biased to OVER-report: claims are retracted on unmount and on `beforeunload`. A crash
 * runs neither, so each claim carries a timestamp its owner REFRESHES while it stays
 * dirty, and a claim stale past `CLAIM_TTL_MS` is ignored on read. The refresh is what
 * makes the expiry safe: without it a draft left untouched would read as abandoned and
 * lose its protection, which is the failure the beacon exists to prevent.
 */
import { safeSetItem } from './safeStorage'

/**
 * One key PER COMPOSER, rather than one key holding every claim.
 *
 * A shared cell forced every publish to read-modify-write it, and `localStorage` offers no
 * cross-window transaction — the spec's storage mutex was removed — so two windows
 * interleaving between that read and that write left the later whole-map write without the
 * earlier claim, and the close gate then read the slot clean and deleted it. Giving each
 * composer its own key removes the shared cell: a window only ever writes or removes the
 * one key it owns, so there is no value to merge and nothing to lose.
 */
export const SLOT_DIRTY_KEY_PREFIX = 'mc-slot-dirty:'

/**
 * How long a claim outlives its last refresh.
 *
 * Must exceed `SLOT_DIRTY_REFRESH_MS` by enough that a busy or backgrounded window does
 * not expire itself — a browser throttles timers in a hidden tab, so the margin covers
 * several missed refreshes rather than one.
 */
export const CLAIM_TTL_MS = 90_000

/** How often a dirty composer re-stamps its claim. */
export const SLOT_DIRTY_REFRESH_MS = 25_000

/** How long a storage-writability probe result is reused. */
export const CLAIM_PROBE_TTL_MS = 2_000

/**
 * The outer bound on a claim whose work NO store can answer for.
 *
 * The short TTL is safe only where a persisted copy answers after it lapses. For a
 * pending knowledge selection, an in-flight upload or a live voice capture there is no
 * second answer, and a browser FREEZES a background tab's timers — so that window misses
 * its re-stamp while still holding the work, and expiring on the refresh scale hands
 * another window a clean slot to delete.
 *
 * Bounded rather than exempt, because a window that CRASHES mid-upload would otherwise
 * hold that slot shut forever over work that went with it, with nothing on screen to
 * explain why. A live composer re-stamps every `SLOT_DIRTY_REFRESH_MS`, so only an
 * abandoned claim ever reaches this. Generous by design: a floor under "no window lives
 * this long silent", not a guess at how long an upload takes.
 */
export const UNRECOVERABLE_CLAIM_TTL_MS = 12 * 60 * 60 * 1_000

/**
 * How long a LAPSED unrecoverable claim still degrades the answer to unverifiable.
 *
 * With no outer bound, crossing the TTL made a crashed window's claim report unverifiable
 * FOREVER: every later close of that slot paid a confirm for work no store can answer for
 * and nothing in the product can clear. Past this the claim is reclaimed and the slot reads
 * as what it is.
 *
 * REFRESH-scale on purpose. A TTL-scale grace DOUBLED the exposure this file documents,
 * making the real worst case two tiers rather than one. A live window re-stamps every
 * `SLOT_DIRTY_REFRESH_MS`, so the grace only has to outlast a few missed beats.
 */
export const LAPSED_CLAIM_GRACE_MS = 4 * SLOT_DIRTY_REFRESH_MS

/**
 * One composer's claim: `s` names the slot it holds, `t` is the refresh stamp.
 *
 * `u: 1` marks work NO store can answer for, which earns the longer
 * `UNRECOVERABLE_CLAIM_TTL_MS` instead of the refresh-scale one. Absent means
 * recoverable — the safe default for the COMMON case and for an older build's claim,
 * which should age out normally rather than pin a slot for hours.
 *
 * Expiry governs the READ alone: no window removes another's key, so a throttled tab
 * whose refresh is merely late keeps its entry and counts again from its next stamp. The
 * cost is that a hard crash leaves one small entry behind, ignored but not reclaimed.
 */
type DirtyClaim = { s: string; t: number; u?: 1 }

function claimKey(composerId: string): string {
  return `${SLOT_DIRTY_KEY_PREFIX}${composerId}`
}

function parseClaim(raw: string | null): DirtyClaim | null {
  if (!raw) return null
  try {
    const parsed: unknown = JSON.parse(raw)
    if (!parsed || typeof parsed !== 'object') return null
    const obj = parsed as { s?: unknown; t?: unknown; u?: unknown }
    if (typeof obj.s !== 'string' || typeof obj.t !== 'number') return null
    return obj.u === 1 ? { s: obj.s, t: obj.t, u: 1 } : { s: obj.s, t: obj.t }
  } catch {
    return null
  }
}

/** Every claim key currently present, collected before any removal shifts the indices. */
function claimKeys(): string[] {
  const keys: string[] = []
  for (let i = 0; i < localStorage.length; i += 1) {
    const key = localStorage.key(i)
    if (key && key.startsWith(SLOT_DIRTY_KEY_PREFIX)) keys.push(key)
  }
  return keys
}

/**
 * Set when this window could not persist its own claim.
 *
 * A claim that did not reach storage is invisible to every other window, so a close
 * fired elsewhere reads a clean slot and discards the draft. Quota is per-origin, so a
 * write THIS window cannot make is one no window could — which makes a local failure
 * usable evidence that the absence of claims is unreliable rather than informative.
 *
 * Local evidence only, and that is the limit this flag cannot pass: it says nothing
 * about a window whose write failed while THIS one is healthy. `claimFailureKey`
 * carries that case, because it lives in the storage both windows share.
 *
 * That is also why the failure tier is not simply deleted: without the shared record, a
 * window whose claim write failed is invisible to the healthy window doing the closing,
 * which is the fail-open this guard exists to prevent.
 */
let claimWriteFailed = false

/**
 * Where ONE composer records that it could not persist its claim.
 *
 * Keyed per composer, because the record is evidence about that composer's work. A single
 * shared key made a healthy window's successful publish erase the failure a DIFFERENT
 * window had recorded, so the close gate read a clean slot and destroyed its unsent work.
 * Clearing is therefore scoped to the composer that recovered.
 *
 * Read by EVERY window, which is the whole point: a module-local flag protects only the
 * window that hit the failure, and the destructive case is the window doing the closing.
 *
 * Composed from the prefix rather than spelled with a suffix because the catalogue gate
 * reads a string literal here as display text. Prefixing also makes the key unownable and
 * self-discriminating: a composer id is always `composer-<uuid>-<n>`, so no claim key can
 * begin with the prefix twice, and neither collides with the bare-prefix probe key.
 */
export function claimFailureKey(composerId: string): string {
  return claimKey(SLOT_DIRTY_KEY_PREFIX + composerId)
}

/** Does *key* hold a failure record rather than a claim? */
function isFailureKey(key: string): boolean {
  return key.startsWith(SLOT_DIRTY_KEY_PREFIX + SLOT_DIRTY_KEY_PREFIX)
}

/** Publish or clear THIS composer's dirty claim on *slot*, synchronously.
 *
 *  Returns whether the claim is now durable enough to be seen by another window.
 */
export function publishSlotDirty(
  composerId: string,
  slot: string | null,
  dirty: boolean,
  workIsRecoverable: boolean,
): boolean {
  const key = claimKey(composerId)
  // A composer holds exactly one key, so moving to another slot overwrites that entry
  // rather than stranding a claim on the slot it left.
  if (!dirty || !slot) {
    try {
      localStorage.removeItem(key)
    } catch {
      /* storage unavailable — nothing to clear */
    }
    // A removal proves nothing about whether WRITES land, so it must not clear the flag.
    return true
  }
  const claim: DirtyClaim = workIsRecoverable
    ? { s: slot, t: Date.now() }
    : { s: slot, t: Date.now(), u: 1 }
  const stored = safeSetItem(key, JSON.stringify(claim))
  claimWriteFailed = !stored
  // Recorded where every window reads it, and scoped to THIS composer: clearing a shared
  // record on success erased the evidence another window had written.
  if (stored) {
    try {
      localStorage.removeItem(claimFailureKey(composerId))
    } catch {
      /* the claim landed; a stale failure record ages out on its own */
    }
  } else {
    safeSetItem(claimFailureKey(composerId), JSON.stringify({ f: Date.now() }))
  }
  return stored
}

/** Drop the claim held by *composerId* — call on unmount and on unload. */
export function retractSlotDirty(composerId: string): void {
  // `true` is inert here: the retraction branch takes `!dirty` and removes the key, so no
  // tier is ever derived from it. Stated rather than defaulted, per the required-arg rule.
  publishSlotDirty(composerId, null, false, true)
  // A retracted composer holds nothing, so its failure record must not outlive it — a clear
  // is the one case where removing the record cannot hide unsent work.
  try {
    localStorage.removeItem(claimFailureKey(composerId))
  } catch {
    /* storage unavailable — the record ages out on its own */
  }
}

/**
 * Does ANY window report unsent work for *slot*?
 *
 * FAILS CLOSED. A clean answer is only trustworthy when a claim could have been written
 * and read: with storage disabled or full, another window's draft is invisible here and
 * reporting `false` would let a confirmed close destroy it. Over-reporting costs a
 * confirm the user can dismiss; under-reporting costs the only copy of their text.
 */
export function anyWindowSlotDirty(slot: string): boolean {
  const now = Date.now()
  try {
    for (const key of claimKeys()) {
      // The probe's own leftover, if a removal ever failed. Inert by design: reading it
      // as an unattributable claim would pin EVERY slot permanently.
      if (key === SLOT_DIRTY_KEY_PREFIX) continue
      const raw = localStorage.getItem(key)
      // Any composer's failure record, not just this window's: whoever could not persist a
      // claim is invisible in the scan below, so its record is the only evidence of it.
      if (isFailureKey(key)) {
        if (failureIsLive(raw, now)) return true
        continue
      }
      const claim = parseClaim(raw)
      // Present but unreadable: a truncated write is what a quota failure looks like from
      // here, and it cannot be attributed to a slot — so it cannot clear one either.
      if (!claim) {
        if (raw !== null) return true
        continue
      }
      if (claim.s !== slot) continue
      // A claim from the future is treated as live: a clock skew between windows must
      // not silently drop protection, and over-reporting is the safe direction here.

      // `u: 1` earns the LONG bound, not exemption: no store answers for that work, so
      // its claim is the only record -- but an abandoned one must still let go.
      const ttl = claim.u === 1 ? UNRECOVERABLE_CLAIM_TTL_MS : CLAIM_TTL_MS
      if (now - claim.t < ttl) return true
    }
  } catch {
    // Storage unreadable, so no claim could be found whether or not one exists.
    return true
  }
  return claimWriteFailed || !storageAcceptsClaims()
}

/**
 * Is an UNRECOVERABLE claim for `slot` alive only under the LONG bound?
 *
 * A live composer re-stamps every `SLOT_DIRTY_REFRESH_MS`, so a `u: 1` claim older than the
 * refresh-scale TTL has missed its stamps: the window may have crashed. It still counts as
 * dirty — the work had no second store — but naming "another window" asserts a live owner
 * nobody can see, which sends the user hunting for a draft that is not on any screen.
 */
export function anyWindowClaimRefreshStale(slot: string): boolean {
  const now = Date.now()
  try {
    for (const key of claimKeys()) {
      if (key === SLOT_DIRTY_KEY_PREFIX || isFailureKey(key)) continue
      const claim = parseClaim(localStorage.getItem(key))
      if (!claim || claim.s !== slot || claim.u !== 1) continue
      const age = now - claim.t
      if (age >= CLAIM_TTL_MS && age < UNRECOVERABLE_CLAIM_TTL_MS) return true
    }
  } catch {
    // Unreadable storage is already handled by the readability probe at the call site.
    return false
  }
  return false
}

/**
 * Did an UNRECOVERABLE claim for `slot` age out rather than never exist?
 *
 * The two are indistinguishable to `anyWindowSlotDirty`, which answers one boolean, and
 * conflating them is a data-loss path: an OS suspend freezes a live owner's timers, so a
 * window still holding an in-flight upload can miss every re-stamp for longer than the
 * bound. Expiring then reads as a clean slot and the work is deleted with no prompt.
 *
 * Kept separate from the dirty read so the bound still does its job — a lapsed claim no
 * longer BLOCKS, it only stops the slot claiming to be empty, which downgrades the answer
 * to unverifiable and leaves a confirm in the path.
 */
export function anyWindowClaimLapsed(slot: string): boolean {
  const now = Date.now()
  let lapsed = false
  try {
    const abandoned: string[] = []
    for (const key of claimKeys()) {
      if (key === SLOT_DIRTY_KEY_PREFIX || isFailureKey(key)) continue
      const claim = parseClaim(localStorage.getItem(key))
      if (!claim || claim.s !== slot || claim.u !== 1) continue
      const age = now - claim.t
      // Reclaimed rather than merely ignored: a key left in place keeps being scanned, and
      // nothing else in the product can clear one.
      if (age >= UNRECOVERABLE_CLAIM_TTL_MS + LAPSED_CLAIM_GRACE_MS) abandoned.push(key)
      else if (age >= UNRECOVERABLE_CLAIM_TTL_MS) lapsed = true
    }
    for (const key of abandoned) localStorage.removeItem(key)
  } catch {
    // Unreadable storage is already fail-closed by the dirty read; nothing to add here.
    return false
  }
  return lapsed
}

/**
 * Can the claim store answer the dirty question AT ALL?
 *
 * `anyWindowSlotDirty` is fail-closed: with `localStorage` unwritable -- private browsing,
 * blocked site data, exhausted quota -- it answers "dirty" for EVERY slot, because a claim it
 * cannot read is a claim it cannot rule out. That is the right answer to "might work be
 * lost", and the wrong input to a REFUSAL: there is no other window to visit and no draft to
 * send, so a refusal naming one cannot be acted on and the chip never works again.
 *
 * Callers that refuse must ask this first and downgrade to a dismissible confirm when it is
 * false, which is what the other close routes already do with the same signal.
 */
export function claimsAreReadable(): boolean {
  return !claimWriteFailed && storageAcceptsClaims()
}

/**
 * Is a recorded claim-write failure still current?
 *
 * Bounded on `CLAIM_TTL_MS` like a claim, and for the same reason: the failing window
 * re-attempts every `SLOT_DIRTY_REFRESH_MS` while it stays dirty, so a live one keeps the
 * record fresh, and a window that died mid-failure stops holding every slot shut.
 */
function failureIsLive(raw: string | null, now: number): boolean {
  if (raw === null) return false
  try {
    const parsed: unknown = JSON.parse(raw)
    const at = (parsed as { f?: unknown } | null)?.f
    // Unreadable here means a window DID record a failure and the record itself is
    // damaged, which is not grounds to assume storage recovered.
    if (typeof at !== 'number') return true
    return now - at < CLAIM_TTL_MS
  } catch {
    return true
  }
}

/**
 * Can storage hold a claim at all?
 *
 * Consulted only when NO claim was found, so the cost lands on the answer that would
 * otherwise be a silent false negative. Memoised briefly because the close gate and the
 * chip's render gate both ask, and a probe write per render would be its own defect.
 */
let probeAt = 0
let probeOk = true

// The largest write this guard vouches for, derived from the real builders so it cannot
// drift below them; `claimFailureKey` carries the double prefix, so it is the longest key.
const PROBE_ID_PAD = 48
const PROBE_SLOT_PAD = 64
export const WORST_CLAIM_WRITE_BYTES =
  claimFailureKey('p'.repeat(PROBE_ID_PAD)).length
  + JSON.stringify({ s: 'p'.repeat(PROBE_SLOT_PAD), t: Date.now(), u: 1 }).length

function storageAcceptsClaims(): boolean {
  const now = Date.now()
  if (now - probeAt < CLAIM_PROBE_TTL_MS) return probeOk
  probeAt = now
  // The BARE prefix, which is a key no composer can own: every id is non-empty, so this
  // collides with no claim, and `parseClaim` ignores the value if a removal ever fails.
  // The VALUE carries the byte budget, because quota is charged on key + value together:
  // a 28-byte probe passed in the band where the claim and the failure record both failed.
  const probeValue = 'p'.repeat(Math.max(1, WORST_CLAIM_WRITE_BYTES - SLOT_DIRTY_KEY_PREFIX.length))
  probeOk = safeSetItem(SLOT_DIRTY_KEY_PREFIX, probeValue)
  try {
    localStorage.removeItem(SLOT_DIRTY_KEY_PREFIX)
  } catch {
    /* the probe write already answered the question */
  }
  return probeOk
}

/** @internal test-only: drop every claim. */
export function __resetSlotDirtyForTests(): void {
  claimWriteFailed = false
  probeAt = 0
  probeOk = true
  try {
    for (const key of claimKeys()) localStorage.removeItem(key)
  } catch {
    /* storage unavailable — nothing to reset */
  }
}
