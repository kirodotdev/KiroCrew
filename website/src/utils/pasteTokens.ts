import { safeSetItem } from './safeStorage'
import { mergeRecoveredDraft } from './chatDrafts'
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

/**
 * A fresh seq: `max + 1` when that is an exactly representable integer, else the
 * smallest positive integer not in `used`.
 *
 * Seqs are parsed from marker text, and marker text can carry any digit run —
 * `[ Paste #9007199254740991 · 1 lines ]` typed by hand parses to 2^53 - 1, a
 * longer run to `Infinity`. Past 2^53 a double cannot step by one (`2^53 + 1`
 * is `2^53`), so two `max + 1` allocations would coincide and two blocks would
 * share a seq — the collapse this whole family of helpers exists to prevent.
 * `used` must hold every seq a fresh one must avoid: the blocks' and every
 * marker's in the text, claimed or not.
 */
export function allocateSeq(max: number, used: ReadonlySet<number>): number {
  const next = max + 1
  if (Number.isSafeInteger(next) && next > max && !used.has(next)) return next
  let n = 1
  while (used.has(n)) n++
  return n
}

/** Next seq for a new paste = max existing + 1, starting at 1. */
export function nextSeq(blocks: PasteBlock[]): number {
  let max = 0
  const used = new Set<number>()
  for (const b of blocks) { if (b.seq > max) max = b.seq; used.add(b.seq) }
  return allocateSeq(max, used)
}

/**
 * Next seq for a paste landing in `text`: {@link nextSeq} that also reserves
 * every marker already in the text, claimed by a block or not. A literal
 * `[ Paste #1 · 1 lines ]` the user typed has no block and expands to itself;
 * if the next paste were minted at 1 the literal would name that block and the
 * send would replace the user's own text with the pasted content.
 */
export function nextSeqIn(text: string, blocks: PasteBlock[]): number {
  let max = 0
  const used = new Set<number>()
  for (const b of blocks) { if (b.seq > max) max = b.seq; used.add(b.seq) }
  PASTE_TOKEN_REGEX.lastIndex = 0
  let m: RegExpExecArray | null
  while ((m = PASTE_TOKEN_REGEX.exec(text)) !== null) {
    const seq = Number(m[1])
    if (seq > max) max = seq
    used.add(seq)
  }
  return allocateSeq(max, used)
}

/**
 * Re-sequence `carried` blocks whose `seq` is already taken by `used`, and rewrite
 * their markers in `text` to match.
 *
 * Markers resolve by `seq` alone, so re-using a seq makes two blocks collapse onto
 * one on expansion — one blob's content is sent twice and the other is dropped.
 * Rewriting must therefore happen in a SINGLE right-to-left pass over the located
 * ranges: a naive per-block `split/join` re-matches markers an earlier iteration
 * just emitted (the needle `[ Paste #N · M lines ]` collides whenever two blocks
 * share a line count), which cascades every marker onto the last block.
 *
 * `used` is mutated to include the assigned seqs. Blocks keep their identity and
 * content; only `seq` changes, and only when it has to.
 */
export function remapCarriedBlocks(
  text: string,
  carried: PasteBlock[],
  used: Set<number>,
): { text: string; blocks: PasteBlock[] } {
  let free = 0
  for (const v of used) if (v > free) free = v
  free += 1
  // The next free seq: walk up from the seed, skipping anything reserved since.
  // `used` may carry a hand-typed literal's seq beyond 2^53 (or `Infinity`),
  // where `free++` cannot move; past the safe range fall back to the smallest
  // positive integer not in `used` instead of walking.
  const nextFree = (): number => {
    while (Number.isSafeInteger(free) && used.has(free)) free++
    if (!Number.isSafeInteger(free)) return allocateSeq(free, used)
    return free++
  }
  const remap = new Map<number, number>()
  const blocks: PasteBlock[] = []
  for (const b of carried) {
    // Re-derive `free` against `used` on every allocation. A block that KEEPS its
    // seq also lands in `used`, so a single max()-seed goes stale the moment a kept
    // seq is >= free — and the next block needing a new seq would be handed one the
    // kept block already holds, recreating the duplicate this function exists to
    // prevent. (Reachable when the live list has a seq gap, e.g. after a paste chip
    // is deleted, then a second failed recovery runs.)
    const seq = used.has(b.seq) ? nextFree() : b.seq
    used.add(seq)
    if (seq !== b.seq) remap.set(b.seq, seq)
    blocks.push(seq === b.seq ? b : { ...b, seq })
  }
  if (!remap.size) return { text, blocks }
  // Right-to-left so each splice leaves the earlier ranges' offsets valid, and so
  // no marker written by this pass is ever re-examined. EVERY occurrence moves,
  // not just the first: a second copy of `[ Paste #N … ]` left at #N would carry
  // the seq the kept block now owns, and merged ahead of that block's own marker
  // it would take the kept content on expansion (see `findAllTokenRanges`).
  let out = text
  const ranges = findAllTokenRanges(text, carried)
  for (let i = ranges.length - 1; i >= 0; i--) {
    const { start, end, block } = ranges[i]
    const mapped = remap.get(block.seq)
    if (mapped === undefined) continue
    out = out.slice(0, start) + formatToken({ ...block, seq: mapped }) + out.slice(end)
  }
  return { text: out, blocks }
}

/**
 * Bring the blocks behind a payload being handed back (`carried`, the ones its
 * tokens in `text` point at) into a composer that already holds `kept` blocks.
 *
 * The one owner of the recovery rule every composer host applies: a carried
 * block the composer already holds (same id — the clear had not flushed when
 * the payload was captured, or an undo put it back) is not added twice AND its
 * token is dropped from the payload text — every copy of it, since the composer
 * already shows that token — appending a second one would make the retry send
 * the paste twice, and a copy left behind would ride into the retried prompt as a
 * stray literal. The rest go through {@link remapCarriedBlocks} so a colliding `seq`
 * gets a fresh number and its token in `text` is rewritten. Returns the text to
 * merge (`text`, held tokens stripped), the same payload with every token still
 * in place (`full`, for the exact-duplicate test — see {@link mergeCarriedDraft})
 * and the full block list to install (kept first, then the carried ones).
 */
export interface CarriedPastes { text: string; full: string; pastes: PasteBlock[] }

export function carryPastes(
  text: string,
  carried: PasteBlock[],
  kept: PasteBlock[],
  /** The composer text the payload is about to be merged INTO (`mergeCarriedDraft`'s `keep`). */
  keepText = '',
): CarriedPastes {
  if (!carried.length) return { text, full: text, pastes: kept }
  const keptIds = new Set(kept.map(b => b.id))
  const fresh = carried.filter(b => !keptIds.has(b.id))
  let payload = text
  // A token resolves by seq, so only a seq no fresh carried block also claims
  // can be attributed to a held block with certainty.
  const freshSeqs = new Set(fresh.map(b => b.seq))
  const held = carried.filter(b => keptIds.has(b.id) && !freshSeqs.has(b.seq))
  if (held.length) {
    // Right-to-left, so each splice leaves the earlier ranges' offsets valid.
    // Every copy goes, not just the first: a surviving second copy would ride
    // into the carried text and the retried prompt as a stray literal.
    const ranges = findAllTokenRanges(payload, held)
    for (let i = ranges.length - 1; i >= 0; i--) {
      const { start, end } = ranges[i]
      // Take the token's own line break with it, so no blank line is left behind.
      const eatNewline = payload[end] === '\n' ? 1 : 0
      payload = payload.slice(0, start) + payload.slice(end + eatNewline)
    }
  }
  // The seqs a carried block must not land on: the kept blocks', every marker
  // already in the destination text (a literal `[ Paste #1 · 1 lines ]` typed
  // while the send was in flight has no block and expands to itself — a carried
  // block coming back as #1 would make the literal name it), and any inert
  // literal inside the payload itself (a marker no carried block is behind).
  // `remapCarriedBlocks` mutates the seq set it is given, so each pass gets its
  // own copy and both assign the same numbers.
  const reserved = new Set(kept.map(b => b.seq))
  const carriedSeqs = new Set(carried.map(b => b.seq))
  const reserveMarkers = (source: string, skip?: ReadonlySet<number>) => {
    PASTE_TOKEN_REGEX.lastIndex = 0
    let m: RegExpExecArray | null
    while ((m = PASTE_TOKEN_REGEX.exec(source)) !== null) {
      const seq = Number(m[1])
      if (!skip?.has(seq)) reserved.add(seq)
    }
  }
  reserveMarkers(keepText)
  reserveMarkers(text, carriedSeqs)
  const { text: remapped, blocks } = remapCarriedBlocks(payload, fresh, new Set(reserved))
  const full = payload === text ? remapped : remapCarriedBlocks(text, fresh, new Set(reserved)).text
  return { text: remapped, full, pastes: [...kept, ...blocks] }
}

/**
 * Put a carried payload back into a composer's text under the shared recovery
 * rule. The exact-duplicate test runs against the payload WITH its tokens
 * (`full`): an undo that put the whole payload back — tokens included — is the
 * case the equality exists for, and the stripped text alone would never equal
 * it. Anything else appends the stripped text, so a held block's token is not
 * shown twice and a retry cannot send the paste twice.
 */
export function mergeCarriedDraft(keep: string | null | undefined, carried: CarriedPastes): string {
  const existing = keep ?? ''
  if (existing.trim() && existing.trim() === carried.full.trim()) return existing
  return mergeRecoveredDraft(existing, carried.text)
}

/** A located marker occurrence and the block its seq names. */
export interface TokenRange { start: number; end: number; block: PasteBlock }

/**
 * The one regex walk behind {@link findTokenRanges} and {@link findAllTokenRanges}:
 * every `[ Paste #N · M lines ]` in `text` whose seq a block carries, in document
 * order. With `firstOnly` a seq is claimed by its first occurrence and every later
 * copy is skipped (an inert literal); without it every copy is reported.
 */
function walkTokenRanges(text: string, blocks: PasteBlock[], firstOnly: boolean): TokenRange[] {
  if (!text || !blocks.length) return []
  const bySeq = new Map(blocks.map(b => [b.seq, b]))
  const claimed = new Set<number>()
  const out: TokenRange[] = []
  PASTE_TOKEN_REGEX.lastIndex = 0
  let m: RegExpExecArray | null
  while ((m = PASTE_TOKEN_REGEX.exec(text)) !== null) {
    const seq = Number(m[1])
    if (firstOnly && claimed.has(seq)) continue
    const block = bySeq.get(seq)
    if (block) {
      claimed.add(seq)
      out.push({ start: m.index, end: m.index + m[0].length, block })
    }
  }
  return out
}

/**
 * The FIRST token range for each block seq, in document order.
 *
 * One block backs exactly one marker: the first occurrence of its seq in text
 * order. A later copy of the same marker (a restored draft, a short paste of
 * the marker text) is inert literal text, not a pill, and must stay that way —
 * otherwise the send would replace the user's own characters with paste
 * content. Every consumer that reads, renders, hit-tests or EXPANDS a value
 * therefore wants first-only, and this is the locator they share:
 *
 * | caller                                      | wants      |
 * |---------------------------------------------|------------|
 * | `expandAll` (send), `pruneBlocks`,          | first-only |
 * |   `tokenRangeAt`, `mergePreservedPastes`    |            |
 * | `ChatInput` caret / hit-test / delete       | first-only |
 * | `LexicalComposerInput` `$replaceComposerValue` | first-only |
 * | `PasteHighlightLayer`, `PasteHoverLayer`    | first-only |
 * | `ChatPageMessageContent` bubble render       | first-only |
 * | `pinnedPrompt` (via `expandAll`)            | first-only |
 * | `remapCarriedBlocks` marker REWRITE         | ALL — {@link findAllTokenRanges} |
 * | `carryPastes` held-marker STRIP             | ALL — {@link findAllTokenRanges} |
 *
 * The two `ALL` rows edit the text rather than interpret it: a rewrite or strip
 * that touches only the first copy leaves the later copies carrying a seq that
 * now means something else. See {@link findAllTokenRanges}.
 */
export function findTokenRanges(text: string, blocks: PasteBlock[]): TokenRange[] {
  return walkTokenRanges(text, blocks, true)
}

/**
 * EVERY token range whose seq a block carries, in document order — the same
 * shape as {@link findTokenRanges} without the first-occurrence claim.
 *
 * For the two callers that MUTATE marker text, first-only is a defect:
 *
 * - `remapCarriedBlocks` moves a carried block from seq N to a fresh M. If only
 *   the first `#N` is rewritten, a second copy keeps `#N` — a seq the KEPT block
 *   now owns. Merged ahead of the kept block's own marker, first-occurrence
 *   expansion binds the kept content to that stray and sends the kept marker
 *   raw: content attributed to a different paste. Rewriting every copy to `#M`
 *   is safe — the extras become inert literals of a seq that collides with
 *   nothing (and the Lexical invariant plugin re-seqs the pill away from them).
 * - `carryPastes` strips a held block's marker from a refused payload because
 *   the composer already shows it. A second copy left behind rides into the
 *   carried text and the retried prompt as a stray literal.
 */
export function findAllTokenRanges(text: string, blocks: PasteBlock[]): TokenRange[] {
  return walkTokenRanges(text, blocks, false)
}

/**
 * Canonical form of a composer value: every backed marker occurrence names its
 * OWN block, no two blocks share a seq, and surplus occurrences stay literal.
 * This text-only layer decodes and never mints records; provenance-owning layers
 * (paste events, tree node identity, and drag node keys) are the only minting sites.
 *
 * A marker resolves by seq alone, so a value that holds the same marker twice (a
 * restored draft, a small paste that contained the marker text) must not map both
 * occurrences onto one block: `expandAll` would replace user-authored literal
 * text with unrelated paste content. The block LIST can carry the same seq twice
 * as well — a draft persisted while two same-seq pills had already diverged holds
 * `[original #1, edited #1]` — so canonicalisation must preserve both records.
 *
 * Rule, in text order: the k-th occurrence of a seq pairs with the k-th block
 * record carrying that seq (a tree snapshot lists one record per pill in
 * document order, so this reproduces exactly what the pills showed). The first
 * pair keeps its seq; every later paired record gets a fresh seq (max so far + 1);
 * an occurrence with no record left stays untouched and unbacked; a same-seq
 * record no occurrence claims is re-sequenced rather than dropped, so nothing is
 * lost and no seq stays ambiguous. Rewritten markers are spliced right-to-left,
 * as in `remapCarriedBlocks`, for the same reason.
 * Finally ids are made unique as well — the first holder keeps its id, every
 * later record carrying it gets a fresh one — because the textarea composer
 * removes a pill by id; a duplicated id alone (distinct seqs, e.g. a draft
 * re-sequenced before this rule) is enough to trigger that repair.
 *
 * Returns the SAME `text` / `blocks` references when nothing had to change, so a
 * caller can detect a rewrite by identity.
 */
export function splitDuplicateMarkers(
  text: string,
  blocks: PasteBlock[],
): { text: string; blocks: PasteBlock[] } {
  if (!text || !blocks.length) return { text, blocks }
  // Same-seq records, in list order; and whether any id is carried twice.
  const recordsBySeq = new Map<number, PasteBlock[]>()
  const ids = new Set<string>()
  // Every seq a fresh one must avoid: the blocks' and every marker's in the text.
  const used = new Set<number>()
  let max = 0
  let duplicateRecords = false
  let duplicateIds = false
  for (const b of blocks) {
    if (b.seq > max) max = b.seq
    used.add(b.seq)
    const list = recordsBySeq.get(b.seq)
    if (list) { list.push(b); duplicateRecords = true } else recordsBySeq.set(b.seq, [b])
    if (ids.has(b.id)) duplicateIds = true
    ids.add(b.id)
  }
  // Marker occurrences that name a block, in text order. EVERY marker's seq
  // raises the floor for fresh seqs, claimed or not: marker text with no record
  // behind it is inert (expansion leaves it as the literal characters), and it
  // must stay inert — a fresh seq equal to it would make the literal name the
  // twin's block, and the send would replace the user's own text with an
  // unrelated paste's content.
  const occurrences: Array<{ start: number; end: number; seq: number }> = []
  const seen = new Map<number, number>()
  let repeatedOccurrence = false
  PASTE_TOKEN_REGEX.lastIndex = 0
  let m: RegExpExecArray | null
  while ((m = PASTE_TOKEN_REGEX.exec(text)) !== null) {
    const seq = Number(m[1])
    if (seq > max) max = seq
    used.add(seq)
    if (!recordsBySeq.has(seq)) continue
    occurrences.push({ start: m.index, end: m.index + m[0].length, seq })
    const k = seen.get(seq) ?? 0
    if (k > 0) repeatedOccurrence = true
    seen.set(seq, k + 1)
  }
  if (!duplicateRecords && !repeatedOccurrence && !duplicateIds) return { text, blocks }

  const canonicalOf = new Map<PasteBlock, PasteBlock>()
  const rewrites: Array<{ start: number; end: number; block: PasteBlock }> = []
  const claimed = new Map<number, number>()
  // `allocateSeq` steps past `max` while that is exact and falls back to the first
  // free small integer beyond 2^53 (a hand-typed literal can put `max` there).
  const fresh = (): number => {
    const seq = allocateSeq(max, used)
    used.add(seq)
    if (seq > max) max = seq
    return seq
  }
  for (const o of occurrences) {
    const k = claimed.get(o.seq) ?? 0
    claimed.set(o.seq, k + 1)
    if (k === 0) continue // first occurrence: keeps its seq, pairs with the first record
    const records = recordsBySeq.get(o.seq)!
    if (k >= records.length) continue
    const block = { ...records[k], seq: fresh() }
    canonicalOf.set(records[k], block)
    rewrites.push({ start: o.start, end: o.end, block })
  }
  // A same-seq record no occurrence claimed still gets its own seq.
  for (const records of recordsBySeq.values()) {
    for (let i = 1; i < records.length; i++) {
      if (!canonicalOf.has(records[i])) canonicalOf.set(records[i], { ...records[i], seq: fresh() })
    }
  }
  if (!canonicalOf.size && !rewrites.length && !duplicateIds) return { text, blocks }
  let out = text
  for (let i = rewrites.length - 1; i >= 0; i--) {
    const { start, end, block } = rewrites[i]
    out = out.slice(0, start) + formatToken(block) + out.slice(end)
  }
  // The textarea composer removes a pill by id, so two records under one id
  // would both vanish on one ✕. Ids are carried twice by twins minted from one
  // block, and by a draft re-sequenced before this rule existed (distinct seqs,
  // one id). Assemble the output in original order and let the first holder of
  // an id keep it; every later record carrying it — kept or re-sequenced — gets
  // a fresh id. (The id is not part of the marker text, so no rewrite depends
  // on it.)
  const claimedIds = new Set<string>()
  const withUniqueId = (b: PasteBlock): PasteBlock => {
    let id = b.id
    while (claimedIds.has(id)) id = makePasteId()
    claimedIds.add(id)
    return id === b.id ? b : { ...b, id }
  }
  return {
    text: out,
    blocks: blocks.map(b => withUniqueId(canonicalOf.get(b) ?? b)),
  }
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
 * main thread and freezes the tab. Because the
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
