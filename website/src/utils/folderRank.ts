import type { ChatFolder } from '../types'

/**
 * Fractional rank keys for sidebar folder order: the sidebar's mirror of
 * `src/kiro_crew/dashboard/folder_rank.py`.
 *
 * A folder's place among its siblings is a short `rank` string; siblings sort by
 * comparing those strings, read as the digits of a base-62 fraction. The gateway
 * computes every stored rank from a `before`/`after` anchor. The sidebar
 * computes the same keys only to draw a drag before the server answers, so each
 * function here must return exactly what its Python twin returns; the shared
 * fixture `test/fixtures/chat_folder_rank.json` is where that is checked.
 */

/** Code units `from`..`to` inclusive, as a string. */
const codeRange = (from: number, to: number): string =>
  String.fromCharCode(...Array.from({ length: to - from + 1 }, (_, i) => from + i))

/** Rank digits `0-9`, `A-Z`, `a-z` in ascending code-unit order, so `<` on
 *  strings is numeric order. Built from code points rather than written out: it
 *  is an alphabet, not text anyone reads. */
export const RANK_DIGITS = codeRange(0x30, 0x39) + codeRange(0x41, 0x5a) + codeRange(0x61, 0x7a)
const BASE = RANK_DIGITS.length
/** Longest rank accepted or generated; mirrors `MAX_RANK_LEN`. */
export const MAX_RANK_LEN = 48

const digitValue = (c: string): number => RANK_DIGITS.indexOf(c)

/** `value` when it is a usable rank, else `null` (mirrors `valid_rank`). */
export function validRank(value: unknown): string | null {
  if (typeof value !== 'string' || value.length === 0 || value.length > MAX_RANK_LEN) return null
  if (value[value.length - 1] === '0') return null
  for (const c of value) if (digitValue(c) < 0) return null
  return value
}

/** A rank strictly between `lo` and `hi`, or `null` (mirrors `rank_between`). */
export function rankBetween(lo: string | null, hi: string | null): string | null {
  if (lo !== null && validRank(lo) === null) return null
  if (hi !== null && validRank(hi) === null) return null
  const low = lo ?? ''
  if (hi !== null && !(low < hi)) return null
  let upper: string | null = hi
  let out = ''
  const steps = low.length + (hi ?? '').length + 2
  for (let i = 0; i < steps; i++) {
    const a = i < low.length ? digitValue(low[i]) : 0
    const b = upper === null ? BASE : i < upper.length ? digitValue(upper[i]) : 0
    if (a === b) {
      out += RANK_DIGITS[a]
      continue
    }
    if (b - a > 1) {
      out += RANK_DIGITS[Math.floor((a + b) / 2)]
      return out.length <= MAX_RANK_LEN ? out : null
    }
    out += RANK_DIGITS[a]
    upper = null
  }
  return null
}

/** `count` ascending, evenly spaced ranks (mirrors `spread_ranks`). */
export function spreadRanks(count: number): string[] {
  if (count <= 0) return []
  let width = 1
  while (BASE ** width < count + 1) width++
  width++
  // BigInt: BASE ** width passes 2**53 only for sections far past the folder
  // cap, but the arithmetic must match Python's exactly for any count.
  const step = BigInt(BASE) ** BigInt(width) / BigInt(count + 1)
  const out: string[] = []
  for (let i = 1; i <= count; i++) {
    let value = BigInt(i) * step
    let digits = ''
    for (let d = 0; d < width; d++) {
      digits = RANK_DIGITS[Number(value % BigInt(BASE))] + digits
      value /= BigInt(BASE)
    }
    out.push(digits.replace(/0+$/, ''))
  }
  return out
}

/**
 * The ranked half of the `custom` sibling order: ranked folders first by rank,
 * then id; `null` when neither folder carries a valid rank, so the caller falls
 * back to the legacy `order`/name comparison for the pair.
 */
export function compareRanks(a: ChatFolder, b: ChatFolder): number | null {
  const ra = validRank(a.rank)
  const rb = validRank(b.rank)
  if (ra === null && rb === null) return null
  if (ra === null) return 1
  if (rb === null) return -1
  if (ra !== rb) return ra < rb ? -1 : 1
  const ia = typeof a.id === 'string' ? a.id : ''
  const ib = typeof b.id === 'string' ? b.id : ''
  return ia < ib ? -1 : ia > ib ? 1 : 0
}

/**
 * The moved folder's rank and any sibling re-spread for a drop at `index`
 * among `siblings` (sorted, without the moved folder). Mirrors `plan_position`.
 */
export function planPosition(
  siblings: readonly ChatFolder[],
  index: number,
): { rank: string; respread: Map<string, string> } {
  const at = Math.max(0, Math.min(index, siblings.length))
  const ranks = siblings.map(s => validRank(s.rank))
  const sectionRanked = ranks.every(r => r !== null) && new Set(ranks).size === ranks.length
  if (sectionRanked) {
    const lo = at > 0 ? ranks[at - 1] : null
    const hi = at < siblings.length ? ranks[at] : null
    const rank = rankBetween(lo, hi)
    if (rank !== null) return { rank, respread: new Map() }
  }
  const fresh = spreadRanks(siblings.length + 1)
  const [own] = fresh.splice(at, 1)
  const respread = new Map<string, string>()
  siblings.forEach((s, i) => {
    if (s.rank !== fresh[i]) respread.set(s.id, fresh[i])
  })
  return { rank: own, respread }
}
