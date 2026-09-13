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

// The shipped value is the constant above; this only lets a suite avoid spending it on
// every close. Never read as a way to shorten the real wait.
let ackWindowMs: number = CLOSING_ACK_WINDOW_MS

/** @internal test-only: drive the ack window without a real 400ms pause per close. */
export function __setClosingAckWindowForTests(ms: number): void {
  ackWindowMs = ms
}

type Intent = { n: string; t: number }

const intentKey = (slot: string) => `${CLOSING_KEY_PREFIX}intent:${slot}`
const vetoKey = (nonce: string, composerId: string) =>
  `${CLOSING_KEY_PREFIX}veto:${nonce}:${composerId}`

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
  return safeSetItem(intentKey(slot), JSON.stringify(payload)) ? nonce : null
}

/** The live intent for *slot*, for a window deciding whether to answer. */
export function readClosingIntent(slot: string): Intent | null {
  try {
    const raw = localStorage.getItem(intentKey(slot))
    if (!raw) return null
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

/** Refuse a close: this composer holds work that the delete would destroy. */
export function vetoClosingIntent(nonce: string, composerId: string): void {
  safeSetItem(vetoKey(nonce, composerId), '1')
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
export function vetoLiveClosingIntent(slot: string, composerId: string): void {
  const intent = readClosingIntent(slot)
  if (intent) vetoClosingIntent(intent.n, composerId)
}

/**
 * Did anyone refuse?
 *
 * Fail closed on an unreadable store: a veto we cannot see is exactly the case this
 * handshake exists for, so an enumeration failure counts as a refusal rather than as
 * silence.
 */
export function closingIntentVetoed(nonce: string): boolean {
  const prefix = `${CLOSING_KEY_PREFIX}veto:${nonce}:`
  try {
    for (let i = 0; i < localStorage.length; i += 1) {
      const key = localStorage.key(i)
      if (key && key.startsWith(prefix)) return true
    }
    return false
  } catch {
    return true
  }
}

/** Drop the intent and every answer to it, whatever the outcome. */
export function clearClosingIntent(slot: string, nonce: string | null): void {
  try {
    localStorage.removeItem(intentKey(slot))
    if (!nonce) return
    const prefix = `${CLOSING_KEY_PREFIX}veto:${nonce}:`
    const doomed: string[] = []
    for (let i = 0; i < localStorage.length; i += 1) {
      const key = localStorage.key(i)
      if (key && key.startsWith(prefix)) doomed.push(key)
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
  try {
    localStorage.setItem(key, String(Date.now()))
  } catch {
    /* presence is an optimisation; without it the close simply waits */
  }
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
