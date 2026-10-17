import { useDeferredValue, useMemo, useState } from 'react'

/**
 * `useDeferredValue`, scoped to one VISIT to the session that produced the value.
 *
 * A deferred value is the PREVIOUS value until React finds room for the
 * background re-render, and a chat page under steady urgent updates (title
 * typewriter, composer keystrokes, heartbeat-driven state) can hold it back for
 * hundreds of milliseconds. For a streaming flush inside one session that lag
 * is the point: the last committed transcript stays up while the regrouped one
 * renders at leisure. Across a SESSION SWITCH it is a defect: the transcript
 * still on screen belongs to the session the user just left, and a new chat's
 * first send painted the previous tab's messages under the new tab's URL until
 * the deferred render landed (#8526 — the offline E2E's `[data-role=assistant]`
 * locator matched those ghost rows, then watched them vanish).
 *
 * So the deferral is keyed: while the deferred frame still belongs to another
 * session, the CURRENT value renders at urgent priority; once React catches up
 * the two agree and the deferred path resumes. A switch therefore renders the
 * right transcript in the first commit (what it did before the deferral was
 * added), and only same-session updates are ever deferred.
 *
 * The key is the visit, not the slot alone. Send in A, open B, come back to A:
 * if React never committed B's deferred render in between (a short look at B,
 * or a background render kept restarting by A's own streaming flushes), the
 * deferred frame is still the one A held when the user LEFT it. A slot-only
 * key reads that as "same session, keep deferring", so A came back showing its
 * pre-send transcript, and the new message only appeared once the deferred
 * render finally committed, often not until the stream went quiet. Counting
 * visits makes that frame belong to an earlier visit, so it is never shown.
 */
export function useSlotDeferredValue<T>(slot: string | null | undefined, value: T): T {
  const key = slot ?? null
  // Visit counter: bumped each time the slot changes. Derived during render
  // (the documented "store info from previous renders" pattern) so this very
  // render already uses the new number; the setState only persists it.
  const [seen, setSeen] = useState({ slot: key, visit: 0 })
  const visit = seen.slot === key ? seen.visit : seen.visit + 1
  if (seen.slot !== key) setSeen({ slot: key, visit })
  const frame = useMemo(() => ({ visit, value }), [visit, value])
  const deferred = useDeferredValue(frame)
  return deferred.visit === frame.visit ? deferred.value : value
}
