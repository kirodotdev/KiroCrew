import { useCallback, useRef } from 'react'
import { useMutation, useQueryClient } from '@tanstack/react-query'
import { useDispatch } from 'react-redux'
import { api } from '../api/client'
import { appendSlotMessage } from '../store/chatSlice'
import { i18nT } from '../i18n/t'
import {
  hasUnsentComposerWork,
  type ComposerWork,
} from '../utils/composerWork'
import { claimsAreReadable } from '../utils/slotDirtyBeacon'
import { slotHasUnsentWorkHere, slotUnsentWorkSource, type UnsentWorkSource } from '../utils/slotComposerRegistry'
import { useSlotComposerRegistration } from './useSlotComposerRegistration'
import { useSessionActions } from './useSessionActions'
import type { OptionAction } from '../app-sdk/protocol/options'

/**
 * The `[OPTION-ACTIONS:]` close dispatch — ONE copy, shared by every host.
 *
 * ## Why this is a hook and not two hand-mirrored copies
 *
 * It was duplicated across `ChatPage` and `ChatPane`, ~130 lines each, and the
 * comments admitted it ("mirrors ChatPage's dispatch"). Two independent review
 * lanes named the duplication, and it had ALREADY DIVERGED: the settle-time
 * composer recheck was called with 2 arguments in one host and 5 in the other, so
 * one host closed over staged work the other refused to. That is precisely the
 * failure class this dispatch exists to prevent — a tab torn down with unsent work
 * in it, or a breadcrumb lost — reintroduced by the copy rather than by the logic.
 *
 * Shaped after `usePlanActionMutation`, the repo's existing mechanism for
 * host-shared chip dispatch, rather than inventing a second pattern.
 *
 * ## The state machine, in order, and why each step is where it is
 *
 * 1. **Enum guard.** Only `close` acts. A future member must opt IN rather than
 *    fall through to a tab teardown.
 * 2. **Staleness, first check.** A click can outlive the row it was made on:
 *    content chips are debounced and a byte-identical replacement row re-renders
 *    without remounting. Already stale on arrival → write NOTHING, because a
 *    breadcrumb would record a decision against a row the user had left.
 * 3. **Slot resolution**, read fresh (not captured), because the closure's slot can
 *    be one the user has already navigated away from under lag.
 * 4. **CONFIRM.** Before the write, and this ordering is a fix: the breadcrumb is a
 *    permanent `inject` row, so writing it first left the transcript asserting a
 *    close the user then cancelled — a durable record of something that never
 *    happened.
 * 5. **Breadcrumb write.** Inside the close's own pre-delete window, so the
 *    `appended === true` gate below still governs whether the delete proceeds.
 * 6. **Append gate.** MEASURED: a note answering `appended: false` is held in
 *    memory and is NOT durably recorded at the moment this decision is made, and
 *    the close answers 200 either way, so there is no error to catch. `close_slot`
 *    DOES now flush held notes — this PR adds that — so a close no longer destroys
 *    one; the gate stands because a note the backend has not committed is still not
 *    the record the action exists to leave. Server-side counterpart:
 *    the `TestDeferredNoteLostOnClose` suite.
 * 7. **Staleness, second check.** The write is a round trip; a slow one lands after
 *    the user has started another turn, by which point the derived key has moved (a
 *    `user` row ends the scan). Closing then would dismiss the tab AND cancel that
 *    turn. The breadcrumb is KEPT — it landed, and it honestly records the pick.
 * 8. **Composer recheck.** The chip is disabled while the composer holds work, but
 *    that gate is evaluated when the row PAINTS. This handler awaits a network
 *    write, and work staged inside that window passes every check above — the
 *    source key cannot catch it, because typing appends no transcript row.
 */
export function useOptionActionDispatch(params: {
  /** Resolved fresh at dispatch time, never captured. `null` aborts. */
  resolveSlot: () => string | null
  /** The composer's staged work, read at SETTLE time via a ref. */
  composerWork: ComposerWork
  /** Identity of the row these chips came from, for the staleness checks. */
  sourceKey: string | null | undefined
  /**
   * Does THIS host write its composer to storage every window can read?
   *
   * Only a host that persists to localStorage may pass `true`. Omitting it means no, which
   * is the protective default: the claim then earns the long bound instead of expiring on
   * the refresh scale and letting another window delete work it could never see.
   */
}) {
  const dispatch = useDispatch()
  const { close: closeSessionWithConfirm } = useSessionActions()

  // Refs, not the closed-over values: the dispatcher's closure is created at click
  // time and both of these move underneath it while the write is in flight.
  const sourceKeyRef = useRef(params.sourceKey)
  sourceKeyRef.current = params.sourceKey
  const composerWorkRef = useRef(false)
  composerWorkRef.current = hasUnsentComposerWork(params.composerWork)
  // Derived HERE from the work object rather than asked of each host, so a host cannot
  // answer inconsistently -- but the work KIND is only half the question.

  // `resolveSlot` goes through a ref for the same reason plus one more: its own
  // contract above is "resolved fresh at dispatch time, never captured", and a ref
  // is what makes that structural rather than conventional. Keeping it in the
  // dependency array instead would also re-create the callback on every render,
  // because callers pass `params` as an inline object literal, so the memo would
  // buy nothing.
  const resolveSlotRef = useRef(params.resolveSlot)
  resolveSlotRef.current = params.resolveSlot

  // Registered so the gate below can ask about the SLOT rather than about this host.
  // Shared with every embedded surface, so a host that offers no chip still counts.
  // FALSE, unconditionally: the shared draft store is last-write-wins over one key, so no
  // host can honestly claim another window recovers its drafts. Take an argument when one can.
  useSlotComposerRegistration(
    () => resolveSlotRef.current(),
    composerWorkRef.current,
    false,
  )

  // Invalidated by PREFIX: `ChatPane` keys the transcript `['slot-messages', slot,
  // hydrateLimit]` and the limit varies per pane, so two segments reach them all.
  const queryClient = useQueryClient()
  const noteMutation = useMutation({
    mutationFn: (vars: { slot: string; content: string }) =>
      api.chatSlotNote(vars.slot, vars.content, { source: 'option-action', visibleOnly: true }),
    onSuccess: (_result, vars) => {
      queryClient.invalidateQueries({ queryKey: ['slot-messages', vars.slot] })
    },
  })
  const noteMutateRef = useRef(noteMutation.mutateAsync)
  noteMutateRef.current = noteMutation.mutateAsync

  const dispatchFollowUpAction = useCallback(async (action: OptionAction, sourceKeyAtClick?: string | null) => {
    if (action.action !== 'close') return
    // `undefined` means the caller supplied no key at all, which keeps its previous
    // behaviour rather than being refused wholesale — exactly as
    // `usePlanActionMutation.mutate` treats it. `null` IS a supplied key (chips
    // derived from no row) and cannot match a live row.
    const isStale = () =>
      sourceKeyAtClick !== undefined && sourceKeyAtClick !== sourceKeyRef.current
    if (isStale()) return
    const slot = resolveSlotRef.current()
    if (!slot) return

    const notify = (content: string) =>
      dispatch(appendSlotMessage({ slot, message: { role: 'error', content, cls: '' } }))

    // Refused BEFORE the confirm, not after it. The slot's draft is already
    // knowable here, so asking is taking consent this path will not honour.

    // That was the dead end: the prompt promised the draft would be lost, the
    // recheck refused anyway, and every retry left another permanent row.
    const unsentAt = slotUnsentWorkSource(slot)
    // A REFUSAL needs a knowable answer, and the claim store fail-closes to "dirty" when it
    // cannot be read; `'here'` is exempt because the registry answers it without storage.
    if (unsentAt !== null && (unsentAt === 'here' || claimsAreReadable())) {
      notify(i18nT(unsentNoticeKey(unsentAt)))
      return
    }

    try {
      await closeSessionWithConfirm(slot, {
        // The affordance was authored by a MODEL, not by the product: a
        // `close=That's all` chip is one click and its label is arbitrary prose,
        // while `confirmCloseSession` defaults to false. So this path confirms
        // regardless of that preference.
        forceConfirm: true,
        // Names the chip's own label and where the transcript goes. The generic
        // prompt restated neither, after a click on arbitrary model prose.
        confirmMessage: i18nT('hooks.useOptionActionDispatch.close_confirm', {
          label: action.label,
          // The sidebar's OWN translated header, not an English literal: the pane
          // this names is localised, so naming it in English contradicted it.
          section: i18nT('pages.chatSidebar.older_sessions_2'),
        }),
        // Runs only once the user has confirmed, and its `false` return aborts the
        // delete — which is what lets the write sit after the confirm without
        // giving up the append gate.
        beforeDelete: async () => {
          let result: Awaited<ReturnType<typeof api.chatSlotNote>> | null = null
          try {
            // The row records the REQUEST, not the outcome, and that is forced by
            // the ordering rather than chosen for tone. The write has to happen
            // BEFORE the two rechecks below, because those exist to catch state
            // that moved DURING this POST — a draft typed inside that window is
            // invisible to the render-time gate. So a recheck can abort the close
            // after the row is already durable, and a row reading "Session closed"
            // would then be a permanent false statement in the transcript: the tab
            // is still there and an error row sits beneath it.
            //
            // Moving the write after the gates was the other candidate fix and is
            // strictly worse: it would put both rechecks before the only await,
            // which is exactly the blind spot they were added for, trading a
            // mis-worded row for closing over the user's unsent draft.
            //
            // Provenance still travels in `source`; the sentence is what a human
            // reads, and it names the actor and the action so the row cannot read
            // as something the USER said. No machine-shaped prefix: `parseOptions`
            // strips only `[OPTIONS:`/`[OPTION-ACTIONS:`, so a bracketed tag
            // rendered raw.
            result = await noteMutateRef.current({
              slot,
              content: i18nT('hooks.useOptionActionDispatch.close_requested', { label: action.label }),
            })
          } catch {
            result = null
          }
          if (result?.appended !== true || result.deliveryConditional === true) {
            // `appended` is not durability on its own. The handler computes
            // `delivery_conditional = deferred or not slot.linked_session_key`.

            // So an UNBOUND slot answers appended=true WITH deliveryConditional=true,
            // and both halves of the note resolve their destination late.

            // Closing on that answer persists the breadcrumb into whichever session
            // claims the slot next — a row this session's user never sees.

            // Both branches name the OUTCOME, not just the fault: the session
            // stayed open, which the borrowed agent-switch copy never said.

            // Deferred is checked FIRST because it also sets the conditional flag,
            // and "a turn is running" is the more specific of the two truths.
            notify(
              result?.visibleDeferred
                ? i18nT('hooks.useOptionActionDispatch.close_deferred_turn')
                : result?.deliveryConditional === true
                  // Same interpolation as `close_stale_row` below, so the route out is
                  // named by the menu item's own key and cannot drift from it.
                  ? i18nT('hooks.useOptionActionDispatch.close_unbound_session', {
                    action: i18nT('components.sessionActionsMenu.close_session'),
                  })
                  : i18nT('hooks.useOptionActionDispatch.close_not_recorded'))
            return false
          }
          if (isStale()) {
            // `isStale()` compares ROW KEYS, not turn state, so this fires with no turn
            // running, and staleness has already removed the chip.

            // Points at the SESSION MENU, not the tab ✕: `closeTab` only rewrites the tab
            // list, so the ✕ would not close the session this message says is open.

            // The label is interpolated from the menu item's own key, so copy cannot drift.
            notify(i18nT('hooks.useOptionActionDispatch.close_stale_row', {
              action: i18nT('components.sessionActionsMenu.close_session'),
            }))
            return false
          }
          const lateUnsentAt = slotUnsentWorkSource(slot)
          if (lateUnsentAt !== null) {
            // Asked of the SLOT, not of this host: a second mounted pane showing the
            // same slot holds its draft in its own state, invisible to this one.

            // Reached only by a draft typed DURING the POST — the early guard above
            // already refused anything present before the confirm.
            notify(i18nT(unsentNoticeKey(lateUnsentAt)))
            return false
          }
          return true
        },
      })
    } catch { /* close refused — any breadcrumb that landed is already durable */ }
  }, [dispatch, closeSessionWithConfirm])

  /**
   * Does the SLOT block the action right now, as far as a render may ask?
   *
   * The chip used to paint from the host's own composer alone, while the click asked about the
   * whole slot, so a chip in full danger paint refused on click and left a permanent error row.
   * Paint answers from the registry only: the cross-window tiers enumerate storage keys, which
   * on a render path costs a scan per paint and breaks callers that assert no storage access.
   * The dispatch below still applies the full slot-wide test and carries the reason.
   */
  const slotBlocksAction = (): boolean => {
    const slot = resolveSlotRef.current()
    if (!slot) return false
    return slotHasUnsentWorkHere(slot)
  }

  return { dispatchFollowUpAction, slotBlocksAction }
}

/**
 * The notice for each place the draft can be, so the user is sent to ONE surface.
 *
 * The registry and the claim already distinguish this window from another, so hedging
 * across both made the reader check two places when the code knew which. `elsewhere`
 * keeps the hedge because there the surface genuinely is not identifiable.
 */
function unsentNoticeKey(at: UnsentWorkSource): string {
  if (at === 'here') return 'hooks.useOptionActionDispatch.close_aborted_unsent_here'
  if (at === 'other-window') return 'hooks.useOptionActionDispatch.close_aborted_unsent_window'
  return 'hooks.useOptionActionDispatch.close_aborted_unsent'
}
