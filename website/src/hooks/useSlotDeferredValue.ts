import { useDeferredValue, useLayoutEffect, useMemo, useRef, useState } from 'react'

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
 *
 * `sameShape` opts a value out of the deferral when it only grew in place: it
 * is asked whether `next` differs from the last value handed to the deferral
 * in a way cheap enough to render at urgent priority (a streaming reply's tail
 * row getting longer). When it says yes, the deferral keeps its earlier input,
 * so React schedules no background pass, and the current value renders in this
 * commit. A deferred render of an earlier, real change that is still pending
 * keeps showing its previous value, as without `sameShape`.
 */
export function useSlotDeferredValue<T>(
  slot: string | null | undefined,
  value: T,
  sameShape?: (deferredInput: T, next: T) => boolean,
): T {
  const key = slot ?? null
  // Visit counter: bumped each time the slot changes. Derived during render
  // (the documented "store info from previous renders" pattern) so this very
  // render already uses the new number; the setState only persists it.
  const [seen, setSeen] = useState({ slot: key, visit: 0 })
  const visit = seen.slot === key ? seen.visit : seen.visit + 1
  if (seen.slot !== key) setSeen({ slot: key, visit })
  const frame = useMemo(() => ({ visit, value }), [visit, value])
  // Both refs describe COMMITTED renders only, so they are written in a layout
  // effect: a background render that React interrupts and throws away must not
  // leave its frame behind for the next urgent render to compare against.
  //
  // fedRef: the frame last handed to useDeferredValue.
  // liveRef: the last value rendered live, and the deferral input it grew from.
  // A tail-only run keeps the deferral on the frame it started from while the
  // live values are what the reader sees; when a real change then defers, that
  // frame is still the committed deferred one, and returning it would roll the
  // reply back to its first chunk, so the last live value is returned instead.
  const fedRef = useRef(frame)
  const liveRef = useRef<{ base: typeof frame; value: T } | null>(null)
  const fed = fedRef.current
  const input = fed !== frame && fed.visit === visit && sameShape?.(fed.value, value) ? fed : frame
  const deferred = useDeferredValue(input)
  const rendersLive = deferred.visit !== visit || deferred === input
  useLayoutEffect(() => {
    fedRef.current = input
    if (rendersLive && deferred === input) liveRef.current = { base: input, value }
  })
  if (rendersLive) return value
  const live = liveRef.current
  return live && live.base === deferred ? live.value : deferred.value
}
