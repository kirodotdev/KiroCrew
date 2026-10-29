/**
 * Which forks of a session were cut at which of its rows.
 *
 * A fork child carries its parent (`forked_from`, the parent's history key) and
 * the row it was cut after (`fork_point_mid`, falling back to `fork_point_ts`
 * for a row saved before ids were minted). The parent's transcript reads this
 * index to draw a marker under each fork point. A fork made before the point
 * was recorded has neither field and draws nothing, as before.
 */
import type { ChatMessage, ChatSlot } from '../../types'
import { sessionDisplayTitle } from '../../utils/sessionDisplayTitle'

export interface ForkChild {
  key: string
  title: string
  direction: 'head' | 'tail'
}

export interface ForkPointIndex {
  byMid: ReadonlyMap<string, readonly ForkChild[]>
  byTs: ReadonlyMap<string, readonly ForkChild[]>
}

export const EMPTY_FORK_POINTS: ForkPointIndex = { byMid: new Map(), byTs: new Map() }

/** The bare slot key a child's `forked_from` names (the history key minus `dashboard:`). */
export function forkParentKey(forkedFrom: string | null | undefined): string | null {
  return forkedFrom ? forkedFrom.replace(/^dashboard:/, '') : null
}

type ForkSlot = Pick<ChatSlot, 'key' | 'title' | 'forked_from' | 'fork_point_mid' | 'fork_point_ts' | 'fork_direction'>

function push(map: Map<string, ForkChild[]>, at: string, child: ForkChild): void {
  const list = map.get(at)
  if (list) list.push(child)
  else map.set(at, [child])
}

/** Index *slots* that were forked from *parentKey* by the row they were cut after. */
export function forkPointsOf(slots: readonly ForkSlot[], parentKey: string | null): ForkPointIndex {
  if (!parentKey) return EMPTY_FORK_POINTS
  const byMid = new Map<string, ForkChild[]>()
  const byTs = new Map<string, ForkChild[]>()
  for (const s of slots) {
    if (forkParentKey(s.forked_from) !== parentKey) continue
    const child: ForkChild = {
      key: s.key,
      title: sessionDisplayTitle(s),
      direction: s.fork_direction === 'tail' ? 'tail' : 'head',
    }
    if (s.fork_point_mid) push(byMid, s.fork_point_mid, child)
    else if (s.fork_point_ts) push(byTs, s.fork_point_ts, child)
  }
  if (byMid.size === 0 && byTs.size === 0) return EMPTY_FORK_POINTS
  return { byMid, byTs }
}

/**
 * A stable signature of the fork-point fields, so the index (and the transcript
 * renderers that depend on it) is rebuilt only when a fork appears, goes or is
 * renamed, not on every slot-list frame.
 */
export function forkPointsSignature(slots: readonly ForkSlot[], parentKey: string | null): string {
  if (!parentKey) return ''
  const rows: string[][] = []
  for (const s of slots) {
    if (forkParentKey(s.forked_from) !== parentKey) continue
    if (!s.fork_point_mid && !s.fork_point_ts) continue
    rows.push([s.key, sessionDisplayTitle(s), s.fork_point_mid || '', s.fork_point_ts || '', s.fork_direction || ''])
  }
  return rows.length ? JSON.stringify(rows) : ''
}

/** The forks cut after row *m*: matched by its id, or by its timestamp for a child that recorded no id. */
export function forkChildrenAt(index: ForkPointIndex, m: ChatMessage): readonly ForkChild[] {
  const mid = typeof m.meta?.mid === 'string' ? m.meta.mid : ''
  const byMid = mid ? index.byMid.get(mid) : undefined
  const byTs = m.ts ? index.byTs.get(m.ts) : undefined
  if (byMid && byTs) return [...byMid, ...byTs]
  return byMid || byTs || []
}
