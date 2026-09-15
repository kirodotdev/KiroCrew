import { toDate } from '../i18n/format'
import type { ChatSlot } from '../types'

/** The fraction of a millisecond `Date` truncates away, in milliseconds. */
function subMillis(raw: string | undefined): number {
  // Group 1 is everything after the three fractional digits `Date` keeps, so
  // `.015001` yields `001` -> 0.001 ms. Fewer than three digits cannot have been
  // truncated and correctly does not match.
  const extra = /\.\d{3}(\d+)/.exec(raw ?? '')
  return extra ? Number(`0.${extra[1]}`) : 0
}

/**
 * A slot's last-activity time as an INSTANT (epoch ms), for ordering.
 *
 * `last_activity_ts` is forwarded verbatim from the transcript row that produced
 * it (`slot_projection.py` reads `message.get("ts")` and passes the string
 * through), and the backend states that those rows do not share one format:
 * current builds stamp offset-aware values via `monotonic_transcript_ts`, while
 * transcripts written by older builds still hold naive
 * `datetime.now().isoformat()` rows. `history.transcript_sort_key` exists on the
 * Python side for exactly this reason, and its docstring names the failure —
 * comparing the two as STRINGS orders them by their text, so a naive `10:00:00`
 * sorts before an aware `09:30:00+00:00` that actually happened later.
 *
 * The same holds for two aware values written under different offsets (two hosts,
 * or one host across a DST boundary): `…T09:00:00+08:00` is 01:00Z and
 * `…T02:30:00+00:00` is 02:30Z, so the later instant is the smaller string.
 *
 * Parsing resolves both cases the way the backend does: an offset-bearing value
 * is an absolute instant, and a naive one is read as local time, which is the
 * zone of the writer that produced it.
 *
 * Absent or unparseable sorts last (0), matching the `|| ''` fallback these call
 * sites already used.
 *
 * The value is fractional when the stamp carries sub-millisecond digits, because
 * `Date` does not: it keeps three fractional digits and TRUNCATES the rest, so a
 * plain `getTime()` reads `...00.015001` and `...00.015000` as the same instant.
 * That pair is not contrived — `history.monotonic_transcript_ts` writes a row as
 * `previous + 1 microsecond` whenever the host clock has not advanced between two
 * writes, which on Windows (~15.6 ms system-clock granularity) is the ordinary
 * case. Collapsing the two leaves the comparator returning 0, and a stable sort
 * then answers "most recently active" with whatever order the slots arrived in.
 * Truncation is what makes the remainder safe to add back: it is always the part
 * `Date` dropped, never a value it rounded past.
 */
export function slotActivityMs(slot: Pick<ChatSlot, 'last_activity_ts'>): number {
  const at = toDate(slot.last_activity_ts)?.getTime()
  if (at === undefined) return 0
  return at + subMillis(slot.last_activity_ts)
}

/** Most-recent-first comparator for `Array.prototype.sort`. */
export function byRecentActivity(
  a: Pick<ChatSlot, 'last_activity_ts'>,
  b: Pick<ChatSlot, 'last_activity_ts'>,
): number {
  return slotActivityMs(b) - slotActivityMs(a)
}
