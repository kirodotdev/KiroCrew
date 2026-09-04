import { useCallback, useEffect, useRef } from 'react'
import { useMutation } from '@tanstack/react-query'
import { api, ApiError } from '../api/client'

/**
 * True when a follow-up chip label is an actual plan action — the only three
 * the plan endpoint accepts (chat_orchestrator's plan-action handler
 * lowercases and strips the incoming action and rejects anything else with a
 * 400), and the only labels a real plan chip can carry (the plan pipeline
 * normalizes every plan footer to `[OPTION: Go | Go All | Cancel]`).
 *
 * Hosts gate their plan-dispatch branch on this so a plan-SHAPED message with
 * non-protocol labels (e.g. an agent quoting a plan and offering its own
 * choices) falls through to the normal composer path instead of firing a
 * dispatch the server would reject — a rejected dispatch skips the append
 * path too, leaving a dead chip.
 *
 * Comparisons rather than a lookup table: these are wire values the server
 * compares with `.strip().lower()`, not user-visible copy.
 */
export function isPlanAction(label: string): boolean {
  const v = label.trim().toLowerCase()
  return v === 'go' || v === 'go all' || v === 'cancel'
}

const isCancel = (action: string) => action.trim().toLowerCase() === 'cancel'

/**
 * Per-slot latches, each holding the IDENTITY of the options-bearing
 * transcript row (`followUpSourceKey`) whose chip was dispatched. Module-level
 * on purpose, for two reasons the per-render mutation state cannot cover:
 *
 * 1. `mutation.isPending` is a render-time snapshot — two chip clicks landing
 *    before the next render both read `false` and both dispatch. These maps
 *    are synchronous: written before `mutate`.
 * 2. One session can occupy two grid panes (the session grid does not enforce
 *    slot uniqueness across leaves). Each pane holds its own hook instance,
 *    so an instance-local guard in pane A would not stop pane B from queueing
 *    a second `Go` and advancing an extra stage.
 *
 * LIFECYCLE — a dispatch is released by TRANSCRIPT ACKNOWLEDGEMENT, evaluated
 * against transcript STATE, never by an effect merely firing:
 *
 * - error   → released ONLY for a DEFINITIVE PRE-MUTATION rejection (see
 *   `isDefinitiveRejection`): a 4xx the server produced before touching the
 *   plan proves nothing was applied, so the user must be able to retry. An
 *   AMBIGUOUS failure (5xx, or a transport rejection with no response at all)
 *   keeps the latch: the server may well have committed the action and lost
 *   the response on the way back, and re-opening the latch there would let a
 *   retry `queue_append` a second `Go` or write a duplicate cancellation row.
 * - success → the latch stays held until the hook observes a DIFFERENT
 *   non-null `followUpSourceKey` — a different options-bearing row than the
 *   one the click acted on. An HTTP 200 is not proof the USER saw anything:
 *   if the WS is down while HTTP works, the pane keeps rendering the STALE
 *   chips of a plan that already advanced, and releasing on success — or on
 *   host remount, slot re-entry, or option-LABEL equality — would let a
 *   re-click on those stale chips `queue_append` an unintended extra Go
 *   turn. Row identity closes every one of those: a remounted pane or a
 *   restored slot re-derives the SAME stale row (same key → still held),
 *   while a genuine stage-2 footer is a NEW row (new `mid`/`ts`) even when
 *   its labels are byte-identical — including in a single-write reconnect
 *   hydration that never passes through the empty-options state.
 * - a null `followUpSourceKey` (chips cleared, streaming, user boundary)
 *   never releases: transient states prove nothing about what replaced the
 *   acted-on plan.
 *
 * The acknowledgement lives INSIDE the hook (hosts pass their derived
 * `followUpSourceKey` as an argument), so a host cannot render chips and
 * dispatch without wiring the release — forgetting it is a missing-argument
 * type error, not a silent permanent no-op.
 *
 * TWO maps, so the stop control is independent of the stage-advancing latch:
 * a Cancel must go through while a Go is in flight (dropping it would swallow
 * the user's abort of the very action being latched), and a Cancel's
 * lifecycle must not touch a latch it never took. Cancel still dedupes
 * against ITSELF over the same held-until-ack window: the server's cancel
 * path only guards `tracker.stop()` — the "🛑 Plan cancelled." transcript
 * append and its broadcasts run on every POST — so an unlatched re-Cancel
 * (double-click, or stale chips over a dead WS) would write duplicate
 * transcript rows.
 * (Letting Cancel through while a Go is in flight makes the server's
 * cancel-before-tracker window client-reachable — accepted, tracked as
 * #6046; the alternative, a swallowed stop control, is strictly worse.)
 *
 * Go and Go All share ONE map deliberately. The operative reason is not
 * duplicate stage advance: a Go All landing while a Go's request is in
 * flight would reach the server after `slot.running` flips and be degraded
 * to `queue_append("Go")` WITHOUT enabling auto-run — so letting it through
 * would equally fail to escalate, just less visibly. Dropping it is the
 * fail-safe direction (less autonomy, chip still present to re-click).
 *
 * Known trade-off: a dispatch that never settles — or one that fails
 * ambiguously (5xx / lost response), which is now treated the same way —
 * keeps its slot latched until a different plan row appears or a reload. On
 * the Go map that is the safe failure (a duplicate Go would be worse than a
 * stuck one); on the Cancel map it wedges the stop control itself for that
 * slot — accepted as the cost of double-row suppression, and bounded by a
 * reload. The user-visible cost is that the wedged retry has NO affordance:
 * the chip silently does nothing, because this hook renders no pending or
 * error state. That missing affordance is tracked as #6056 and is
 * deliberately NOT fixed here — it belongs in the shared FollowUpBar so
 * every chip surface gains it at once.
 */
const goLatchBySlot = new Map<string, string>()
const cancelLatchBySlot = new Map<string, string>()

const latchFor = (action: string) => (isCancel(action) ? cancelLatchBySlot : goLatchBySlot)

/**
 * Statuses that arrive as a 4xx but say nothing about whether the plan action
 * ran. 408 is the edge giving up on a request it may already have forwarded;
 * 429 is the tunnel's throttle, which the QueryClient's own retry ladder
 * (api/queryClient.ts) treats as retryable — a retryable rejection is by
 * definition not proof of non-mutation.
 */
const AMBIGUOUS_4XX = new Set([408, 429])

/**
 * Whether *e* proves the server rejected this dispatch BEFORE mutating the
 * plan — the only class of failure that may re-open the latch.
 *
 * `api.planAction` funnels through the `j()` helper, so a response the server
 * actually produced arrives as an `ApiError` carrying its `.status`; a request
 * that never got a response (DNS, TCP reset, the tab going offline mid-flight)
 * rejects as a bare `TypeError` from `fetch` with no status at all.
 *
 * That distinction is the whole test: a definitive 4xx (400 bad action, 403
 * auth, 404 dead slot, 409 wrong state) is the server declining to act, so
 * retry is safe and NOT retrying would leave a dead chip. Everything else is
 * ambiguous — a 5xx can be raised after `queue_append` has already landed, and
 * a transport rejection cannot distinguish "never arrived" from "committed,
 * response lost". Releasing on those turns one user click into two server-side
 * plan actions, which is strictly worse than a latch stuck until the next plan
 * row (see the trade-off note above, and #6056).
 */
const isDefinitiveRejection = (e: unknown): boolean =>
  e instanceof ApiError && e.status >= 400 && e.status < 500 && !AMBIGUOUS_4XX.has(e.status)

/**
 * Dispatches an orchestrator plan follow-up (Go / Go All / Cancel) to
 * `POST /api/chat/slots/{slot}/plan-action` — the slot-scoped endpoint behind
 * `api.planAction`.
 *
 * One hook, one convention: ChatPage and ChatPane render the same plan chips
 * via `deriveFollowUpOptions`, and the same chip must mean the same thing on
 * both surfaces (#5893: ChatPane used to drop `followUpIsPlan` and let an
 * approval label fall through to the composer as ordinary text).
 *
 * `slot` is the host's slot key and `followUpSourceKey` its CURRENT derived
 * options-row identity (from the same `deriveFollowUpOptions` result that
 * renders the chips). `mutate` is a per-slot single-flight per action class,
 * anchored to the row it acted on and held until a DIFFERENT row is observed
 * — see the latch lifecycle note above. A dispatch the server DEFINITIVELY
 * rejected (a 4xx raised before it touched the plan) releases its class
 * immediately for retry; an ambiguous failure (5xx or a lost response) stays
 * latched, because it may have committed. Cancel is never blocked by a
 * pending Go. Hosts do NOT add their own `isPending` pre-check — a
 * render-scoped check would swallow the stop control while a Go settles.
 *
 * Hosts also pass `clickedSourceKey` — the row identity captured when the chip
 * was CLICKED, which the debounced chip hands back to `onSelect`. A click whose
 * row has since been replaced is refused outright: see `mutate`.
 *
 * `mutateAsync` is deliberately NOT exposed: it would bypass the single-flight
 * this hook exists to guarantee.
 *
 * Fire-and-forget beyond that: no onSuccess invalidation (the plan advances
 * over the event stream); a failed dispatch is logged to the console. Nothing
 * else of the mutation state is currently rendered by either host.
 */
export function usePlanActionMutation(slot: string | null, followUpSourceKey: string | null) {
  // The acknowledgement: a DIFFERENT non-null source row than the one a
  // latched dispatch acted on releases that latch. Evaluated against the
  // observed transcript state — a remount or slot re-entry re-derives the
  // same stale row and therefore releases nothing.
  useEffect(() => {
    if (!slot || followUpSourceKey === null) return
    for (const latch of [goLatchBySlot, cancelLatchBySlot]) {
      const held = latch.get(slot)
      if (held !== undefined && held !== followUpSourceKey) latch.delete(slot)
    }
  }, [slot, followUpSourceKey])

  // Read at dispatch time through a ref so the captured identity is the row
  // whose chips are actually on screen for the click.
  const sourceKeyRef = useRef(followUpSourceKey)
  sourceKeyRef.current = followUpSourceKey

  const mutation = useMutation({
    mutationFn: ({ slot: s, action }: { slot: string; action: string; source?: string }) => api.planAction(s, action),
    onError: (e, vars) => {
      // Release only when the failure PROVES the plan was not mutated (a
      // definitive pre-mutation 4xx). An ambiguous failure — any 5xx, a
      // retryable 408/429, or a transport rejection with no response — may
      // have committed server-side with the response lost on the way back;
      // freeing the latch there lets the retry queue a second Go or write a
      // duplicate cancellation row.
      //
      // AND only if the latch still holds THIS dispatch's row: a late
      // failure (response lost, next footer already dispatched) must not
      // free a NEWER dispatch's latch and re-open a duplicate submit. The
      // two conditions are independent — the classification narrows WHICH
      // failures qualify, the source-key guard narrows WHOSE latch is freed.
      //
      // Options-level callback, so it fires even for a mutation whose
      // observer has been superseded.
      if (isDefinitiveRejection(e)) {
        const latch = latchFor(vars.action)
        if (latch.get(vars.slot) === vars.source) latch.delete(vars.slot)
      }
      // No host reads this mutation's error state (both callers hold it in a ref
      // purely to dispatch through), so a Go/Cancel chip whose POST never landed is
      // otherwise a click with no effect and no message anywhere.
      // eslint-disable-next-line no-console -- only trace of an undelivered plan click
      console.error('plan action failed', e)
    },
    // Deliberately NO success/settled release: success holds the latch until
    // the acknowledgement effect above observes a different source row.
  })
  const { mutate: rawMutate } = mutation
  const mutate = useCallback((vars: { slot: string; action: string; clickedSourceKey?: string | null }) => {
    // No options-bearing row on screen means there is nothing a plan click
    // could legitimately have come from — refuse rather than latch a dispatch
    // whose acknowledgement row could never be identified. Unreachable from
    // the hosts (rendered chips imply a source row); defensive against
    // future callers.
    const source = sourceKeyRef.current
    if (source === null) return
    // A chip click is DEBOUNCED (FollowUpBar's FOLLOWUP_CHIP_DEBOUNCE_MS), and
    // a byte-identical replacement footer re-renders the same chips WITHOUT
    // remounting them — so the pending timer outlives the row it was armed on
    // and this callback can run after the transcript already advanced. Acting
    // then would approve a stage the user never saw: the click they made was
    // on the OLD footer, and the acknowledgement effect above has already
    // freed the latch for the new row, so neither the single-flight nor the
    // null check stops it. The clicked row's identity is captured at click
    // time and passed through, so the mismatch is visible here — refuse it,
    // before touching the latch (a stale click must not consume the new row's
    // single-flight slot either).
    //
    // `undefined` means the caller supplied no key at all (a legacy or
    // programmatic caller, or a chip surface not yet wired): that path keeps
    // its previous behaviour exactly rather than being refused wholesale,
    // which would silently disable dispatch for it. `null` IS a supplied
    // key — chips derived from no row, which cannot match a live row.
    const { clickedSourceKey } = vars
    if (clickedSourceKey !== undefined && clickedSourceKey !== source) return
    const latch = latchFor(vars.action)
    if (latch.has(vars.slot)) return
    latch.set(vars.slot, source)
    rawMutate({ slot: vars.slot, action: vars.action, source })
  }, [rawMutate])
  const { mutateAsync: _dropped, ...rest } = mutation
  return { ...rest, mutate }
}
