/** The dispatched-turn inject contract, in one neutral module.
 *
 *  `meta.injectKind` values the gateway stamps on an `inject` row that dispatched a
 *  turn. Every other inject row opens nothing. Mirrors `_TURN_INJECT_KINDS` in
 *  `dashboard/state.py`. Keyed by `InjectKind` (see `pages/chat/RecoveryCard.tsx`)
 *  so a new kind does not compile until it is classified here, the same guard
 *  `INJECT_KIND_OPENS_TURN` carries. Wider than that record on purpose: it
 *  answers "did a dispatch happen", and walks past `recovery` / `user_replay`
 *  because they resume the same turn; the failure-streak selector asks "did a
 *  dispatch happen that got no reply", and a recovery or replay dispatch that
 *  died is exactly such a turn.
 *
 *  Lives here rather than in `selectors.ts` because `transcript.ts` -- a pure
 *  leaf the paging, warm and switch reducers all import -- needs the predicate
 *  too, and a leaf must not pull the selector layer (and through it
 *  `dashboardSlice`) into itself. Only the TYPE is imported from the pages tree;
 *  it erases at compile time. */
import type { ChatMessage } from '../../types'
import type { InjectKind } from '../../pages/chat/RecoveryCard'

const TURN_INJECT_DISPATCHED: Readonly<Record<InjectKind, boolean>> = {
  cron: true,
  mcp_app: true,
  recovery: true,
  synthesis: true,
  user_replay: true,
}

export const TURN_INJECT_KINDS: ReadonlySet<unknown> = new Set<string>(
  (Object.keys(TURN_INJECT_DISPATCHED) as InjectKind[]).filter((k) => TURN_INJECT_DISPATCHED[k]),
)

/** True when an `inject` row records a turn the gateway DISPATCHED. A dispatched
 *  inject row is appended by the gateway at the position it persists, so both a
 *  live window and a later page hold it in the same place, which is what lets
 *  the transcript reconciliation anchor a segment on it. A note (`isNoteRow`), a
 *  hook-halt card or a policy notice carries no kind, is appended client-first
 *  and flushed later, and therefore never anchors a position. */
export function injectDispatchedTurn(m: Pick<ChatMessage, 'role' | 'meta'>): boolean {
  return m.role === 'inject' && TURN_INJECT_KINDS.has(m.meta?.injectKind)
}
