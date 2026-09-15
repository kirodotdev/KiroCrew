import { useDeferredValue, useMemo } from 'react'

/**
 * `useDeferredValue`, scoped to the session that produced the value.
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
 */
export function useSlotDeferredValue<T>(slot: string | null | undefined, value: T): T {
  const frame = useMemo(() => ({ slot: slot ?? null, value }), [slot, value])
  const deferred = useDeferredValue(frame)
  return deferred.slot === frame.slot ? deferred.value : value
}
