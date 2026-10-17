import { safeSetSessionItem } from './safeStorage'

/**
 * Where a comment composer's typed-but-unsent text lives between teardowns the
 * toolbar cannot guard (a chat-slot switch replaces the whole side panel; a
 * page navigation unmounts the artifact page). One store per host document
 * (`key`), one slot per passage inside it (`anchor` + `start`), so drafts on two
 * passages of one file coexist and neither can overwrite or clear the other.
 *
 * Two layers: `sessionStorage` (survives a reload of this tab) and an in-memory
 * snapshot per key (survives a refusing storage — quota, private mode — and is
 * what a fresh instance over the same key in the SAME tab reads). The snapshot
 * also carries TOMBSTONES: a slot this tab cleared stays cleared even when the
 * storage write that recorded the deletion was refused, so a stale storage
 * record can never resurrect a draft of a comment that was already posted. A
 * later write to the same slot lifts its tombstone.
 *
 * A draft whose post is in flight is HELD — by its text, not its slot: while
 * the host is still answering, a read that would return exactly that pending
 * text returns null instead, so a second composer instance over the same
 * passage (the side panel's full-screen layer, opened mid-post) does not
 * restore it and post it twice. The slot itself stays writable: text typed
 * into that second box is the user's NEWER draft and is kept. The flight's own
 * settle then clears the slot only if it still holds the text that was posted
 * (`clear` with `onlyIf`) — a newer draft written meanwhile survives a success
 * and a refusal alike — or leaves it (refusal); a refusal that finds the slot
 * EMPTY (the newer draft was itself discarded) writes the pending text back,
 * since the flight is then its only copy. "Empty" is `isEmpty`, the unmasked
 * check: a `read` of null may be a newer draft whose own post is in flight,
 * and that one is never overwritten. Holds count per text: two
 * posts outstanding with the same text (a box closed mid-flight, a new box
 * posted over the same text) stay held until the LAST one releases.
 *
 * Every operation is non-throwing: a full or refusing storage is a mundane
 * condition, not a crash.
 */
export interface ComposerDraftStore {
  read: (anchor: string, start: number) => string | null
  write: (text: string, anchor: string, start: number) => void
  /** Drop the passage's draft — only if it still equals `onlyIf` when given,
   *  so a post's success clears the text it posted and never a newer draft. */
  clear: (anchor: string, start: number, onlyIf?: string) => void
  /** Mark `text`'s post on this passage as in flight (see module doc). */
  hold: (anchor: string, start: number, text: string) => void
  release: (anchor: string, start: number, text: string) => void
  /** Whether the passage has NO draft at all — held or not. `read` masks a
   *  held draft as null, so a refused flight deciding whether to put its text
   *  back must ask this, never `read`: a null there may be a NEWER draft whose
   *  own post is still in flight, and overwriting it would lose it. */
  isEmpty: (anchor: string, start: number) => boolean
}

type Slots = Record<string, string>
interface Snapshot { slots: Slots; gone: Set<string> }

const composerDraftMemory = new Map<string, Snapshot>()
/** `JSON.stringify([key, start, anchor, text])` → posts in flight carrying exactly that text, tab-wide. */
const pendingPosts = new Map<string, number>()

const slotKey = (anchor: string, start: number) => `${start}|${anchor}`

function parse(raw: string | null): Slots {
  if (!raw) return {}
  try {
    const parsed = JSON.parse(raw) as unknown
    if (!parsed || typeof parsed !== 'object') return {}
    const out: Slots = {}
    for (const [k, v] of Object.entries(parsed as Record<string, unknown>)) if (typeof v === 'string') out[k] = v
    return out
  } catch { return {} }
}

export function composerDraftStoreFor(key: string): ComposerDraftStore {
  // Storage seeds what this tab has not yet written; the snapshot's slots win
  // over it and its tombstones remove from it.
  const load = (): Slots => {
    let fromSession: Slots = {}
    try { fromSession = parse(window.sessionStorage.getItem(key)) } catch { /* unavailable */ }
    const mem = composerDraftMemory.get(key)
    if (!mem) return fromSession
    const out: Slots = {}
    for (const [k, v] of Object.entries(fromSession)) if (!mem.gone.has(k)) out[k] = v
    return { ...out, ...mem.slots }
  }
  const save = (slots: Slots, gone: Set<string>) => {
    composerDraftMemory.set(key, { slots, gone })
    if (Object.keys(slots).length === 0) {
      try { window.sessionStorage.removeItem(key) } catch { /* unavailable */ }
      return
    }
    safeSetSessionItem(key, JSON.stringify(slots))
  }
  const gone = () => new Set(composerDraftMemory.get(key)?.gone ?? [])
  // A collision-free tuple: the anchor is the selected passage itself and may
  // hold newlines, so a delimiter-joined key would let (`A`, "B\nC") and
  // (`A\nB`, "C") mask each other's drafts.
  const pendingKey = (anchor: string, start: number, text: string) => JSON.stringify([key, start, anchor, text])
  return {
    read: (anchor, start) => {
      const text = load()[slotKey(anchor, start)]
      if (text === undefined || pendingPosts.has(pendingKey(anchor, start, text))) return null
      return text
    },
    write: (text, anchor, start) => {
      const slots = load(); const k = slotKey(anchor, start)
      slots[k] = text
      const g = gone(); g.delete(k)
      save(slots, g)
    },
    clear: (anchor, start, onlyIf) => {
      const slots = load(); const k = slotKey(anchor, start)
      if (onlyIf !== undefined && slots[k] !== onlyIf) return
      delete slots[k]
      const g = gone(); g.add(k)
      save(slots, g)
    },
    isEmpty: (anchor, start) => load()[slotKey(anchor, start)] === undefined,
    hold: (anchor, start, text) => { const k = pendingKey(anchor, start, text); pendingPosts.set(k, (pendingPosts.get(k) ?? 0) + 1) },
    release: (anchor, start, text) => {
      const k = pendingKey(anchor, start, text); const n = (pendingPosts.get(k) ?? 0) - 1
      if (n > 0) pendingPosts.set(k, n); else pendingPosts.delete(k)
    },
  }
}
