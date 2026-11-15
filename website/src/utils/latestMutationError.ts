import { useState } from 'react'

type Mutation = { submittedAt: number; error: unknown }
type Latest = { index: number; at: number }

/** True when `(at, index)` comes after `latest`; a same-millisecond tie goes
 *  to the later-listed mutation, so a tie resolves the same way every time. */
const isLater = (at: number, index: number, latest: Latest | null) =>
  !latest || at > latest.at || (at === latest.at && index > latest.index)

/**
 * The error of the most recently started mutation among several, or null.
 *
 * A react-query mutation keeps its `error` until that same mutation runs
 * again, so OR-ing several mutations' errors keeps an old failure on screen
 * after a different action succeeds, and lets an older failure hide a newer
 * one. Ranking by `submittedAt` makes one notice describe the latest action.
 *
 * The latest start is remembered across renders: a `reset()` puts that
 * mutation's `submittedAt` back to 0, and ranking only the live values would
 * then hand the notice back to an older failure the user already moved past.
 * After a reset of the latest action the notice is empty until a new one starts.
 */
export function useLatestMutationError(...mutations: Mutation[]): unknown {
  const [remembered, setRemembered] = useState<Latest | null>(null)
  let latest = remembered
  mutations.forEach((m, index) => {
    if (m.submittedAt > 0 && isLater(m.submittedAt, index, latest)) latest = { index, at: m.submittedAt }
  })
  // Adjusting state while rendering, React's pattern for "remember what an
  // earlier render saw"; it settles in one pass because `latest` only moves forward.
  if (latest !== remembered) setRemembered(latest)
  if (!latest) return null
  const m = mutations[latest.index]
  return m && m.submittedAt === latest.at ? m.error ?? null : null
}
