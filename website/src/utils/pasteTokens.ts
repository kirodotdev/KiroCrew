import { safeSetItem } from './safeStorage'
/**
 * Paste-token utilities.
 *
 * Large pastes into the chat input are collapsed into inline tokens of the
 * form `⌜ Paste #N · M lines ⌟` so the textarea stays readable. The sequence
 * number N is unique within the current input session and drives reliable
 * pairing between a token occurrence in text and its backing PasteBlock.
 *
 * Seq numbers are stable once assigned — if the user deletes token #2 and
 * pastes again, the new block is assigned a fresh seq (max+1), not renumbered.
 */

/** A collapsed paste block stored alongside the input/message. */
export interface PasteBlock {
  id: string       // unique id (React key; not embedded in token text)
  seq: number      // monotonic per-session number visible in the token (`#N`)
  lines: number    // line count displayed in the token (`M lines`)
  content: string  // original pasted text
}

export const PASTE_THRESHOLD_LINES = 3
export const PASTE_THRESHOLD_CHARS = 200

/** Global regex for extracting token occurrences. (1)=seq, (2)=lines. */
export const PASTE_TOKEN_REGEX = /\[ Paste #(\d+) · (\d+) lines \]/g

export function formatToken(block: PasteBlock): string {
  return `[ Paste #${block.seq} · ${block.lines} lines ]`
}

export function shouldCollapse(text: string): boolean {
  if (!text) return false
  return countLines(text) >= PASTE_THRESHOLD_LINES || text.length >= PASTE_THRESHOLD_CHARS
}

export function countLines(text: string): number {
  if (!text) return 0
  return text.split('\n').length
}

/** React-only id; not embedded in token. */
export function makePasteId(): string {
  const t = Date.now().toString(36)
  const r = Math.floor(Math.random() * 1296).toString(36).padStart(2, '0')
  return `${t}${r}`
}

/** Next seq for a new paste = max existing + 1, starting at 1. */
export function nextSeq(blocks: PasteBlock[]): number {
  let max = 0
  for (const b of blocks) { if (b.seq > max) max = b.seq }
  return max + 1
}

/** Ranges for each token whose seq is present in `blocks`, in document order. */
export function findTokenRanges(
  text: string,
  blocks: PasteBlock[],
): Array<{ start: number; end: number; block: PasteBlock }> {
  if (!text || !blocks.length) return []
  const bySeq = new Map(blocks.map(b => [b.seq, b]))
  const out: Array<{ start: number; end: number; block: PasteBlock }> = []
  PASTE_TOKEN_REGEX.lastIndex = 0
  let m: RegExpExecArray | null
  while ((m = PASTE_TOKEN_REGEX.exec(text)) !== null) {
    const seq = Number(m[1])
    const block = bySeq.get(seq)
    if (block) out.push({ start: m.index, end: m.index + m[0].length, block })
  }
  return out
}

export function tokenRangeAt(
  text: string,
  blocks: PasteBlock[],
  caret: number,
): { start: number; end: number; block: PasteBlock } | null {
  for (const r of findTokenRanges(text, blocks)) {
    if (caret >= r.start && caret <= r.end) return r
  }
  return null
}

export function pruneBlocks(text: string, blocks: PasteBlock[]): PasteBlock[] {
  if (!blocks.length) return blocks
  const survivors = new Set(findTokenRanges(text, blocks).map(r => r.block.id))
  const next = blocks.filter(b => survivors.has(b.id))
  return next.length === blocks.length ? blocks : next
}

export function expandAll(text: string, blocks: PasteBlock[]): string {
  if (!text || !blocks.length) return text
  const ranges = findTokenRanges(text, blocks)
  if (!ranges.length) return text
  let out = text
  for (let i = ranges.length - 1; i >= 0; i--) {
    const r = ranges[i]
    out = out.slice(0, r.start) + r.block.content + out.slice(r.end)
  }
  return out
}

/**
 * Inverse of {@link expandAll}: given fully-expanded content and its backing
 * blocks, substitute each block's verbatim content back to its
 * `[ Paste #N · M lines ]` token.
 *
 * This is the render-time safety net for history load. The backend stores and
 * re-serves the EXPANDED content (what the LLM saw) alongside `meta.pastes`.
 * `mergePreservedPastes` only re-collapses when it can match against an
 * in-memory optimistic bubble or a localStorage side-table entry — both of
 * which are absent on a fresh tab or after the side-table evicts the entry.
 * When that happens a multi-hundred-KB paste is otherwise handed raw to the
 * markdown renderer, which parses + lays out tens of thousands of lines on the
 * main thread and freezes the tab (Mesh: big-paste chat freeze). Because the
 * blocks travel with the message, re-collapse can be derived deterministically
 * from `content` + `meta.pastes` with no external state.
 *
 * Replaces the FIRST non-overlapping occurrence of each block in document
 * order, so repeated sends of the same paste each collapse to their own token.
 * A block whose content is not found verbatim is skipped (that region renders
 * as-is). Returns `content` unchanged when nothing matched.
 */
export function recollapsePastes(content: string, blocks: PasteBlock[]): string {
  if (!content || !blocks.length) return content
  interface Hit { start: number; end: number; block: PasteBlock }
  const hits: Hit[] = []
  const claimed: Array<[number, number]> = []
  // First occurrence of `needle` not already claimed by an earlier block
  // (handles the rare case where one paste's content is a substring of
  // another's). Returns -1 when every occurrence overlaps a claim or none exist.
  const firstUnclaimed = (needle: string): number => {
    if (!needle) return -1
    let from = 0
    while (from <= content.length) {
      const idx = content.indexOf(needle, from)
      if (idx < 0) return -1
      if (!claimed.some(([s, e]) => idx < e && idx + needle.length > s)) return idx
      from = idx + 1
    }
    return -1
  }
  for (const b of blocks) {
    if (!b.content) continue
    // Prefer a verbatim match. Fall back to the trailing-whitespace-trimmed
    // block content: the backend strips trailing whitespace from the stored
    // message (mergePreservedPastes keys on trimEnd() for the same reason), so
    // a paste that was the LAST thing in the message loses its own trailing
    // newline/spaces and won't match verbatim — without the fallback the huge
    // paste falls through to the raw markdown renderer and the freeze it guards
    // against is not prevented for that shape. Verbatim is tried fully first so
    // an interior block (whose trailing whitespace is preserved) is unaffected.
    const trimmed = b.content.trimEnd()
    let needle = b.content
    let idx = firstUnclaimed(needle)
    if (idx < 0 && trimmed && trimmed !== b.content) {
      needle = trimmed
      idx = firstUnclaimed(needle)
    }
    if (idx < 0) continue
    const end = idx + needle.length
    hits.push({ start: idx, end, block: b })
    claimed.push([idx, end])
  }
  if (!hits.length) return content
  hits.sort((a, b) => a.start - b.start)
  let out = ''
  let pos = 0
  for (const h of hits) {
    if (h.start < pos) continue // defensive: an overlap survived the claim check
    out += content.slice(pos, h.start) + formatToken(h.block)
    pos = h.end
  }
  out += content.slice(pos)
  return out
}

/**
 * Merge preserved paste state from `existing` onto `incoming` (from backend
 * refresh). For each user message in `existing` with `meta.pastes`, the
 * tokenized content + pastes are re-applied to the matching incoming user
 * message — matched by expansion equality (`expandAll(old.content, old.pastes)
 * === new.content`). Consumed FIFO so repeated sends don't collide.
 *
 * Falls back to `readStoredPaste(incoming.content)` for messages that have no
 * in-memory counterpart (e.g. after page reload or chat switch) — this reads
 * from the localStorage side table populated by `saveStoredPaste`.
 *
 * Why: the backend only sees/stores the LLM-facing expanded text. Without
 * this merge, the user bubble would "expand" to full text as soon as the
 * refreshSlot after chat_done replaces the optimistic message.
 */
export function mergePreservedPastes<M extends { role: string; content: string; meta?: Record<string, unknown> }>(
  existing: M[],
  incoming: M[],
): M[] {
  const preserved: Array<{ content: string; pastes: PasteBlock[]; expanded: string; files: string[] | null }> = []
  for (const m of existing) {
    const pastes = (m.meta?.pastes as PasteBlock[] | undefined) || []
    if (m.role === 'user' && pastes.length) {
      const files = (m.meta?.files as string[] | undefined) ?? null
      // Normalize trailing whitespace — the backend strips it before storing,
      // so our expanded text (which may have a trailing newline/space from the
      // token + newline pattern) won't match the incoming content byte-for-byte.
      preserved.push({ content: m.content, pastes, expanded: expandAll(m.content, pastes).trimEnd(), files })
    }
  }
  const queue = preserved.slice()
  // A backend-served user message that carries its own `meta.pastes` but whose
  // content is still fully expanded (no `[ Paste #N ]` token) needs fallback 3
  // (self-contained re-collapse) even when there is no optimistic bubble and no
  // side-table hit — so it must NOT be short-circuited away.
  const needsSelfCollapse = (m: M): boolean => {
    if (m.role !== 'user') return false
    const own = (m.meta?.pastes as PasteBlock[] | undefined) || []
    return own.length > 0 && findTokenRanges(m.content, own).length === 0
  }
  // Short-circuit: if no existing user messages have paste metadata AND no
  // incoming user message has a matching entry in the localStorage side table
  // AND none needs self-contained re-collapse, return the `incoming` array
  // reference unchanged. This preserves reference equality for callers that use
  // Object.is / toBe checks, and avoids an unnecessary array allocation in the
  // common no-pastes case.
  if (
    !queue.length &&
    !incoming.some(m => m.role === 'user' && readStoredPaste(m.content.trimEnd())) &&
    !incoming.some(needsSelfCollapse)
  ) {
    return incoming
  }
  return incoming.map(m => {
    if (m.role !== 'user') return m
    // 1) In-memory preservation (optimistic bubble still present)
    if (queue.length) {
      // Compare against trimEnd()'d incoming content — backend strips trailing
      // whitespace on storage, so our expanded text (pre-strip) wouldn't match.
      const incomingTrimmed = m.content.trimEnd()
      const idx = queue.findIndex(p => p.expanded === incomingTrimmed)
      if (idx >= 0) {
        const match = queue.splice(idx, 1)[0]
        const newMeta: Record<string, unknown> = { ...m.meta, pastes: match.pastes }
        // meta.files is lost on the backend-served message — preserve it
        // from the existing optimistic bubble so file chips stay clickable.
        if (match.files && match.files.length) newMeta.files = match.files
        return { ...m, content: match.content, meta: newMeta }
      }
    }
    // 2) localStorage side table (survives refresh/chat-switch)
    const stored = readStoredPaste(m.content.trimEnd())
    if (stored) {
      const newMeta: Record<string, unknown> = { ...m.meta, pastes: stored.pastes }
      if (stored.files && stored.files.length) newMeta.files = stored.files
      return { ...m, content: stored.displayTxt, meta: newMeta }
    }
    // 3) Self-contained re-collapse. The backend re-serves `meta.pastes`
    // alongside the fully-expanded content, so when neither the optimistic
    // bubble nor the side table can re-collapse (fresh tab, evicted entry),
    // fold the message's own blocks back into `[ Paste #N ]` tokens. Without
    // this a huge paste stays expanded in state and the virtualizer measures /
    // the renderer parses hundreds of KB on the main thread, freezing the tab.
    const ownPastes = (m.meta?.pastes as PasteBlock[] | undefined) || []
    if (ownPastes.length && !findTokenRanges(m.content, ownPastes).length) {
      const collapsed = recollapsePastes(m.content, ownPastes)
      if (collapsed !== m.content) return { ...m, content: collapsed }
    }
    return m
  })
}

/* ---- localStorage side table: content-addressed paste preservation ---- */

export const STORE_KEY = 'mc-paste-store-v1'
export const STORE_CAP = 200
// Discard entries not touched within this window. Mirrors the 30-day draft
// TTL (DRAFT_TTL_MS in chatDrafts) so sent-paste rehydration data ages out on
// the same schedule as the unsent drafts it complements.
export const STORE_TTL_MS = 30 * 24 * 60 * 60 * 1000
// Byte ceiling for the serialized store. The STORE_CAP entry count alone does
// NOT bound size — 200 large pastes (logs, files, transcripts) can reach ~5 MB
// and exhaust the localStorage quota, after which every other setItem (e.g.
// saveChatConfig) throws QuotaExceededError and silently breaks the UI. A
// byte-aware LRU keeps only the newest entries that fit. Matches the
// DRAFT_MAX_STORE_BYTES budget the slot-draft stores adopt in.
export const STORE_MAX_BYTES = 2 * 1024 * 1024

// `seq` is a monotonic insertion counter used as the recency tiebreaker.
// `savedAt` (wall-clock ms) is too coarse: a burst of pastes within the same
// millisecond all share a savedAt, and a stable sort would then preserve
// insertion order (oldest-first) — floating the OLDEST entries to the front of
// a "newest-first" sort and evicting newer ones. `seq` is strictly increasing
// per write (derived as max(existing)+1, so it survives reloads), giving an
// unambiguous recency order under sub-millisecond writes.
interface StoredPaste { displayTxt: string; pastes: PasteBlock[]; files?: string[]; savedAt: number; seq?: number }
type Store = Record<string, StoredPaste>

/** True if a stored entry is structurally valid and within the TTL window. */
function isFresh(v: StoredPaste, cutoff: number): boolean {
  return !!v && typeof v.savedAt === 'number' && v.savedAt >= cutoff
}

/** Next monotonic insertion seq = max existing + 1 (1 when empty). Derived from
 *  the store itself so it stays monotonic across page reloads without a
 *  module-level counter that would reset to 0 and collide with persisted seqs. */
function nextStoreSeq(store: Store): number {
  let max = 0
  for (const v of Object.values(store)) {
    if (typeof v.seq === 'number' && v.seq > max) max = v.seq
  }
  return max + 1
}

function readStore(): Store {
  if (typeof localStorage === 'undefined') return {}
  try {
    const raw = localStorage.getItem(STORE_KEY)
    if (!raw) return {}
    const parsed = JSON.parse(raw)
    if (typeof parsed !== 'object' || !parsed) return {}
    // Drop entries past the TTL so stale paste content is never rehydrated;
    // the physical removal happens on the next writeStore.
    const cutoff = Date.now() - STORE_TTL_MS
    const fresh: Store = {}
    for (const [k, v] of Object.entries(parsed as Store)) {
      if (isFresh(v, cutoff)) fresh[k] = v
    }
    return fresh
  } catch {
    return {}
  }
}

function writeStore(store: Store): void {
  if (typeof localStorage === 'undefined') return
  // Bound the store on three axes, newest-first so the most recent pastes
  // always survive: (1) drop TTL-expired entries, (2) cap entry count, and
  // (3) cap total serialized bytes via a byte-aware LRU. The newest entry is
  // never evicted (the count > 0 guard), even if it alone exceeds the budget.
  const cutoff = Date.now() - STORE_TTL_MS
  const entries = Object.entries(store)
    .filter(([, v]) => isFresh(v, cutoff))
    // Newest first. Tiebreak same-millisecond savedAt by the monotonic `seq`
    // so a burst of writes orders by true insertion recency, not stable-sort
    // insertion order (which would float the oldest entry to the front).
    .sort((a, b) => (b[1].savedAt - a[1].savedAt) || ((b[1].seq ?? 0) - (a[1].seq ?? 0)))
  const kept: Store = {}
  let bytes = 2 // enclosing "{}"
  let count = 0
  for (const [k, v] of entries) {
    if (count >= STORE_CAP) break
    // Approx serialized contribution of this entry: "key":value plus comma.
    const entryBytes = JSON.stringify(k).length + 1 + JSON.stringify(v).length + 1
    if (count > 0 && bytes + entryBytes > STORE_MAX_BYTES) break
    kept[k] = v
    bytes += entryBytes
    count++
  }
  try {
    safeSetItem(STORE_KEY, JSON.stringify(kept))
  } catch { /* quota exceeded or storage unavailable — ignore */ }
}

/** Persist paste tokenization for a message so it survives refresh/chat switch.
 *  Keyed by the fully-expanded content (what the backend stores).
 *  Stores `files` alongside so @-file chips stay clickable after refresh. */
export function saveStoredPaste(
  expandedContent: string,
  displayTxt: string,
  pastes: PasteBlock[],
  files?: string[],
): void {
  if (!pastes.length || !expandedContent) return
  const store = readStore()
  // Key by trimEnd() to match what the backend stores (it strips trailing whitespace).
  const key = expandedContent.trimEnd()
  // Compute seq BEFORE inserting so a re-save of an existing key still advances
  // its recency. Delete-then-reinsert isn't needed — eviction sorts on seq.
  const seq = nextStoreSeq(store)
  store[key] = {
    displayTxt,
    pastes,
    ...(files && files.length ? { files } : {}),
    savedAt: Date.now(),
    seq,
  }
  writeStore(store)
}

/** Look up persisted paste tokenization by expanded content. Returns null if absent. */
export function readStoredPaste(expandedContent: string): StoredPaste | null {
  if (!expandedContent) return null
  const store = readStore()
  return store[expandedContent] ?? null
}
