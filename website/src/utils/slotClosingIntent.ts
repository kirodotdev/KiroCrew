/**
 * The closing handshake: announce a pending close and let any live composer veto it.
 *
 * The dirty check and the network DELETE are two moments, and a draft typed between them
 * was never visible to the closing window: the other window publishes its claim on a
 * <=300ms debounce, so the slot read clean, the DELETE landed, and the composer unmounted
 * taking the only copy with it.
 *
 * So the close ASKS first, and a veto ABORTS rather than defers. The wait is bounded so an
 * unanswering window cannot wedge the close, but silence is not read as consent.
 *
 * Keys live under their OWN prefix: the dirty beacon classifies a double-prefixed key as a
 * write-failure record, so an intent parked there would hold every slot shut.
 */

import { safeSetItem } from './safeStorage'

/** Distinct from `SLOT_DIRTY_KEY_PREFIX`, and deliberately not a prefix of it. */
export const CLOSING_KEY_PREFIX = 'mc-slot-closing:'

/**
 * How long the closing window waits for an answer.
 *
 * A `storage` event is delivered as a task in the other window, so this only has to cover
 * event delivery plus a synchronous claim write -- the responder does NOT wait out its own
 * draft debounce, it flushes. Sized well above that round trip rather than tight, because
 * the cost of being early is a destroyed draft and the cost of being late is a pause.
 */
export const CLOSING_ACK_WINDOW_MS = 400

/**
 * How long a published intent stays answerable.
 *
 * Generous, because a bulk-archive request holds its intents for a whole server round-trip
 * and the guard must outlive that; it exists only so a caller killed mid-request cannot
 * leave an intent that every later close answers.
 */
export const INTENT_STALE_MS = 120_000

/** How long a storage-writability probe result is reused. */
export const VETO_PROBE_TTL_MS = 2_000

// The shipped value is the constant above; this only lets a suite avoid spending it on
// every close. Never read as a way to shorten the real wait.
let ackWindowMs: number = CLOSING_ACK_WINDOW_MS

/** @internal test-only: drive the ack window without a real 400ms pause per close. */
export function __setClosingAckWindowForTests(ms: number): void {
  ackWindowMs = ms
}

type Intent = { n: string; t: number }

/**
 * One key PER INTENT, not one per slot.
 *
 * Two windows can close the same slot at once, and a slot-keyed cell holds only the
 * last writer: the earlier nonce vanished, so a composer answering the slot refused
 * only the newer close while the older one read silence and deleted the draft. The
 * earlier window's teardown then removed that shared cell, taking the newer window's
 * live intent with it and leaving the next keystroke nothing to answer.
 *
 * The slot is encoded so it cannot contain the separator: without that, one slot's
 * scan prefix matches another slot's keys and the isolation is only apparent.
 */
const intentPrefix = (slot: string) =>
  `${CLOSING_KEY_PREFIX}intent:${encodeURIComponent(slot)}:`
const intentKey = (slot: string, nonce: string) => `${intentPrefix(slot)}${nonce}`
const vetoKey = (nonce: string, composerId: string) =>
  `${CLOSING_KEY_PREFIX}veto:${nonce}:${composerId}`

/**
 * Where a composer records that its veto could not be written.
 *
 * Lives in the storage both windows share, which is the whole point: the window that
 * must not delete is a DIFFERENT one from the window whose write failed, so a local
 * flag protects nobody. Disjoint from the `veto:` prefix, so neither scan sees the
 * other's keys.
 */
const vetoFailKey = (nonce: string) => `${CLOSING_KEY_PREFIX}veto-fail:${nonce}`

function newNonce(): string {
  try {
    if (typeof crypto !== 'undefined' && typeof crypto.randomUUID === 'function') {
      return crypto.randomUUID()
    }
  } catch {
    /* fall through */
  }
  return `${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 10)}`
}

/**
 * Announce that *slot* is about to be deleted. Returns the nonce, or null when storage
 * cannot hold the intent -- in which case no handshake is possible and the caller falls
 * back to the claim tiers, which already report `unverifiable` for unwritable storage.
 */
export function publishClosingIntent(slot: string): string | null {
  const nonce = newNonce()
  const payload: Intent = { n: nonce, t: Date.now() }
  return safeSetItem(intentKey(slot, nonce), JSON.stringify(payload)) ? nonce : null
}

function parseIntent(raw: string | null): Intent | null {
  if (!raw) return null
  try {
    const parsed: unknown = JSON.parse(raw)
    if (!parsed || typeof parsed !== 'object') return null
    const { n, t } = parsed as Partial<Intent>
    if (typeof n !== 'string' || typeof t !== 'number') return null
    // An intent is now held across a server round-trip, so a caller that dies mid-request
    // would otherwise leave one readable forever and every later close would answer it.
    return Date.now() - t > INTENT_STALE_MS ? null : { n, t }
  } catch {
    return null
  }
}

/**
 * EVERY live intent for *slot*, newest last.
 *
 * Plural because concurrent closes are the case this module got wrong: a composer that
 * answers only one of them leaves the others reading silence.
 */
export function readClosingIntents(slot: string): Intent[] {
  const prefix = intentPrefix(slot)
  const live: Intent[] = []
  try {
    for (let i = 0; i < localStorage.length; i += 1) {
      const key = localStorage.key(i)
      if (!key || !key.startsWith(prefix)) continue
      const intent = parseIntent(localStorage.getItem(key))
      if (intent) live.push(intent)
    }
  } catch {
    return []
  }
  return live.sort((a, b) => a.t - b.t)
}

/** The newest live intent for *slot*. Never use it to decide a veto: see `vetoLiveClosingIntent`. */
export function readClosingIntent(slot: string): Intent | null {
  const live = readClosingIntents(slot)
  return live.length === 0 ? null : live[live.length - 1]
}

/**
 * Set when THIS window could not write a veto, bounded like an intent.
 *
 * Quota is per-origin, so a write this window cannot make is one no window could --
 * which makes a local failure usable evidence that the absence of vetoes is unreliable.
 * Timestamped rather than latched: a failure older than the longest-lived intent cannot
 * pertain to any close still answerable, and a permanent flag would hold every later
 * close in this window shut.
 */
let vetoWriteFailedAt = 0

/**
 * Refuse a close: this composer holds work that the delete would destroy.
 *
 * Returns whether the refusal is durable enough for the CLOSING window to see. A
 * discarded failure here was read downstream as silence, and silence is consent for a
 * slot the user already consented to -- so the loss was total and silent.
 */
export function vetoClosingIntent(nonce: string, composerId: string): boolean {
  if (safeSetItem(vetoKey(nonce, composerId), '1')) {
    vetoWriteFailedAt = 0
    return true
  }
  // A veto that did not land is still a refusal; it just has to be recorded somewhere
  // the closing window reads. If even this fails, only the local flag is left.
  if (safeSetItem(vetoFailKey(nonce), String(Date.now()))) return false
  vetoWriteFailedAt = Date.now()
  return false
}

/**
 * Refuse any close ALREADY in flight for `slot`.
 *
 * The storage-event responder fires only when an intent is WRITTEN, so it answers for the
 * work that existed at that instant. Work appearing afterwards -- a keystroke landing while
 * another window awaits its delete -- published a claim and recorded no veto at all, and the
 * closer never re-reads. A composer turning dirty therefore has to answer for itself.
 * No-ops when no close is pending, which is the common case.
 */
export function vetoLiveClosingIntent(slot: string, composerId: string): boolean {
  // EVERY live intent, because concurrent closers each hold their own nonce and a
  // refusal aimed at one of them leaves the others reading silence.
  let recorded = true
  for (const intent of readClosingIntents(slot)) {
    if (!vetoClosingIntent(intent.n, composerId)) recorded = false
  }
  return recorded
}

/**
 * Did anyone refuse?
 *
 * Fail closed on an unreadable store: a veto we cannot see is exactly the case this
 * handshake exists for, so an enumeration failure counts as a refusal rather than as
 * silence. For the same reason an UNWRITABLE store counts too -- with no veto found,
 * "nobody refused" and "nobody could refuse" are indistinguishable from here, and only
 * one of them is safe to act on.
 */
export function closingIntentVetoed(nonce: string): boolean {
  const prefix = `${CLOSING_KEY_PREFIX}veto:${nonce}:`
  const failKey = vetoFailKey(nonce)
  try {
    for (let i = 0; i < localStorage.length; i += 1) {
      const key = localStorage.key(i)
      if (!key) continue
      // A recorded write failure is a refusal that could not be spelled out, from any
      // window: the composer whose veto never landed is invisible in the scan above.
      if (key === failKey || key.startsWith(prefix)) return true
    }
  } catch {
    return true
  }
  if (Date.now() - vetoWriteFailedAt < INTENT_STALE_MS) return true
  return !storageAcceptsVetoes()
}

/**
 * Can storage hold a veto at all?
 *
 * Consulted only when NO veto was found, so the cost lands on the answer that would
 * otherwise be a silent false negative. Memoised briefly because a batch asks once per
 * key and again on every recheck.
 */
let probeAt = 0
let probeOk = true

// Derived from the real builders so the budget cannot drift below them; quota is charged
// on key and value together, so a token-sized probe passes in the band a veto fails.
const PROBE_NONCE_PAD = 36
const PROBE_ID_PAD = 48
export const WORST_VETO_WRITE_BYTES =
  vetoKey('p'.repeat(PROBE_NONCE_PAD), 'p'.repeat(PROBE_ID_PAD)).length + 1

function storageAcceptsVetoes(): boolean {
  const now = Date.now()
  if (now - probeAt < VETO_PROBE_TTL_MS) return probeOk
  probeAt = now
  // The BARE prefix, a key no intent, veto, failure record or presence stamp can own, so
  // it collides with nothing and every scan in this module ignores it.
  const padding = Math.max(1, WORST_VETO_WRITE_BYTES - CLOSING_KEY_PREFIX.length)
  probeOk = safeSetItem(CLOSING_KEY_PREFIX, 'p'.repeat(padding))
  try {
    localStorage.removeItem(CLOSING_KEY_PREFIX)
  } catch {
    /* the probe write already answered the question */
  }
  return probeOk
}

/**
 * Drop THIS closer's intent and every answer to it, whatever the outcome.
 *
 * Scoped to the nonce. A slot-wide removal took a concurrent closer's live intent with
 * it, after which a keystroke during that window's round-trip found nothing to answer
 * and its draft was archived with no veto and no trace.
 */
export function clearClosingIntent(slot: string, nonce: string | null): void {
  try {
    if (!nonce) {
      // No nonce means the publish failed, so this closer owns no key to remove.
      return
    }
    localStorage.removeItem(intentKey(slot, nonce))
    const prefix = `${CLOSING_KEY_PREFIX}veto:${nonce}:`
    const failKey = vetoFailKey(nonce)
    const doomed: string[] = []
    for (let i = 0; i < localStorage.length; i += 1) {
      const key = localStorage.key(i)
      if (key && (key === failKey || key.startsWith(prefix))) doomed.push(key)
    }
    for (const key of doomed) localStorage.removeItem(key)
  } catch {
    /* nothing to clear if storage is gone */
  }
}

/** Wait out the answer window. Split out so a test can drive it without a real timer. */
export function awaitClosingAcks(ms: number = ackWindowMs): Promise<void> {
  return new Promise(resolve => { setTimeout(resolve, ms) })
}

const localPresence = new Set<string>()

/**
 * Record that THIS window holds a live composer, so other windows know to wait.
 * Returns the undo. Ids written here are remembered locally, which is what lets
 * `anotherWindowHoldsComposer` tell a sibling window's composer from our own.
 *
 * Deliberately NOT keyed by slot: a composer's slot is dynamic (`getSlot()`), so a stamped
 * slot would go stale the moment it moved, and a stale presence is worse than a coarse one.
 */
/**
 * How long a presence stamp is trusted. Bounded for the same reason `popoutController`
 * prunes its heartbeat map: an unstamped key from a CRASHED window is immortal, and it
 * would make every later close in every window pay the ack wait for a holder that is
 * gone. Comfortably above the re-stamp interval so a live composer is never missed.
 */
export const PRESENCE_STALE_MS = 90_000

export function publishComposerPresence(composerId: string): () => void {
  const key = `${CLOSING_KEY_PREFIX}present:${composerId}`
  localPresence.add(key)
  // Result unused: a dropped stamp only costs the next close its ack wait.
  safeSetItem(key, String(Date.now()))
  return () => {
    localPresence.delete(key)
    try {
      localStorage.removeItem(key)
    } catch {
      /* nothing to withdraw if storage is gone */
    }
  }
}

/**
 * Does some OTHER window hold a live composer?
 *
 * Fails CLOSED (true) if storage cannot be enumerated: a holder we cannot see is exactly
 * the case the handshake exists for, so we pay the wait rather than skip it. When this is
 * false there is nobody to answer, so the close needs no ack window at all -- which is why
 * a single-window close stays synchronous.
 */
export function anotherWindowHoldsComposer(): boolean {
  const marker = `${CLOSING_KEY_PREFIX}present:`
  const now = Date.now()
  try {
    for (let i = 0; i < localStorage.length; i += 1) {
      const key = localStorage.key(i)
      if (!key || !key.startsWith(marker) || localPresence.has(key)) continue
      const stamp = Number(localStorage.getItem(key))
      // An unparseable stamp is treated as LIVE: unreadable presence is the fail-closed case.
      if (!Number.isFinite(stamp) || now - stamp <= PRESENCE_STALE_MS) return true
    }
  } catch {
    return true
  }
  return false
}

/** A held handshake: who refused, and the caller's obligation to end it. */
export interface CleanupGuard {
  /** Slots whose composer refused, or that still hold unsent work. */
  refused: string[]
  /**
   * Drop the published intents. The caller MUST call this when the archive request
   * SETTLES, not when the acknowledgement window closes -- releasing early is the
   * whole defect this shape exists to prevent.
   */
  release: () => void
  /**
   * Re-read the vetoes NOW and name the slots refusing.
   *
   * `refused` is the answer at the moment the acknowledgement window closed. A
   * composer turning dirty after that records a veto which nothing else re-reads, so
   * the caller must ask again immediately before it commits.
   */
  recheck: () => string[]
}

/**
 * Run the handshake across a whole cleanup batch and HOLD it for the caller.
 *
 * The intents stay published after this resolves, because the acknowledgement window is
 * a few hundred milliseconds while the archive itself is a server round-trip: a draft
 * typed in that gap met no intent, so it was neither refused nor recorded, and the
 * archive destroyed it silently. Holding the intents means a composer that wakes in that
 * window still claims its work (unrecoverable) before the delete lands, so the loss
 * leaves a durable trace instead of none.
 *
 * `hasUnsentWork` is injected rather than imported because the composer registry
 * imports THIS module to answer intents, so importing it back would cycle.
 * A slot in `consented` is skipped: the user was already warned about that draft
 * and accepted losing it, so re-reading it would make the confirm unanswerable.
 */
export async function awaitCleanupRefusals(
  keys: string[],
  consented: ReadonlySet<string>,
  hasUnsentWork: (key: string) => boolean,
): Promise<CleanupGuard> {
  const intents = new Map<string, string>()
  // A publish that FAILED is a refusal, not a pass: nothing can answer for a key whose
  // intent never reached storage. Per KEY, so one failure costs only its own slot.
  const unpublishable: string[] = []
  for (const key of keys) {
    const nonce = publishClosingIntent(key)
    if (nonce !== null) intents.set(key, nonce)
    else unpublishable.push(key)
  }
  const release = () => {
    for (const [key, nonce] of intents) clearClosingIntent(key, nonce)
    intents.clear()
  }
  const recheck = () => keys.filter(key => {
    const nonce = intents.get(key)
    return nonce !== undefined && closingIntentVetoed(nonce)
  })
  // No intent held means storage refused EVERY write. There is nothing to wait for, but
  // that is not consent: every key is refused, because none of them could be asked about.
  if (intents.size === 0) return { refused: [...unpublishable], release, recheck }
  try {
    await awaitClosingAcks()
    const refused = keys.filter(key => {
      // Unasked, so unanswerable: excluded whatever the local read says.
      if (unpublishable.includes(key)) return true
      const nonce = intents.get(key)
      // A veto is work that arrived DURING acknowledgement, so consent cannot have covered it.
      if (nonce !== undefined && closingIntentVetoed(nonce)) return true
      return !consented.has(key) && hasUnsentWork(key)
    })
    return { refused, release, recheck }
  } catch (err) {
    // Nothing downstream can release a guard whose creation threw.
    release()
    throw err
  }
}

/** @internal test-only: drop every intent and veto. */
export function __resetClosingIntentForTests(): void {
  vetoWriteFailedAt = 0
  probeAt = 0
  probeOk = true
  try {
    const doomed: string[] = []
    for (let i = 0; i < localStorage.length; i += 1) {
      const key = localStorage.key(i)
      if (key && key.startsWith(CLOSING_KEY_PREFIX)) doomed.push(key)
    }
    for (const key of doomed) localStorage.removeItem(key)
  } catch {
    /* storage unavailable -- nothing to reset */
  }
}
