import { useCallback } from 'react'
import { useNavigate } from 'react-router-dom'
import { useAppDispatch, useAppStore } from '../../store'
import { createSlot, appendSlotMessage, startLocalTurn, endLocalTurn } from '../../store/chatSlice'
import { mintSendId } from '../../utils/sendDelivery'
import { sendTurn } from '../../chat-core/transport/sendTurn'
import { api } from '../../api/client'
import { FEATURE_REQUEST_PROMPT_FALLBACK, FEATURE_REQUEST_ROW_META_KEY } from '../../prompts/featureRequest'
import { i18nT } from '../../i18n/t'

/** The top bar's "Request a Feature": a new session that files the request as an agent turn. */
export function useRequestFeature(colorTheme: string) {
  const dispatch = useAppDispatch()
  const navigate = useNavigate()
  // The Provider's store, for reads inside async callbacks (requestFeature):
  // the module-level singleton would bypass a test-injected store.
  const appStore = useAppStore()
  const requestFeature = useCallback(async () => {
    const result = await dispatch(createSlot(undefined)).unwrap()
    const slot = result.key
    // This flow is an agent turn by design (the skill drafts and files the
    // request), so it consumes metered inference and a spent plan allowance
    // refuses it. The transcript can offer the non-inference route -- the
    // repo's feature-request form -- on that refusal ONLY if it knows the
    // refused turn was this one, which nothing else records (#13342). The
    // record is the ROW: the send's `meta` carries the flow's stamp beside its
    // `sendId` (`FEATURE_REQUEST_ROW_META_KEY`), and the gateway persists a
    // send's `meta` verbatim on the user row and echoes it back, so the same
    // stamp is on the optimistic bubble below, on the echo that reconciles it,
    // on the row a reload rebuilds and in every other tab of the slot --
    // nothing is kept on this client. A message the user types later in the
    // same slot is an unstamped row, so its limit hit keeps today's card.
    const sendId = mintSendId()
    const meta = { sendId, [FEATURE_REQUEST_ROW_META_KEY]: true }
    const visibleMessage = i18nT('app.i_d_like_to_request_a_feature')
    navigate('/chat')
    // Both optimistic writes are addressed to the slot this flow CREATED, not
    // the active one: createSlot.fulfilled has a switched-away guard, so when
    // the user changes session while the create round-trip is in flight the
    // new slot is registered but never activated — an active-slot append would
    // put the bubble in an unrelated session's transcript, and an
    // unconditional running flag would mark that session busy for a turn it
    // never started (review finding on #4198). The running flag goes through
    // `startLocalTurn`, the same mark a composer send leaves: it flips the
    // visible footer and records the send as UNCONFIRMED, so a switch away
    // while the POST is in flight parks the slot idle rather than busy -- a
    // refused receipt after that switch has no active mirror left to clear.
    // The mark is dispatched only while the created slot is still ACTIVE:
    // `pendingTurnSlot` is one field for the whole store, so marking a slot
    // the user already left would overwrite the guard of whatever slot they
    // are sending from now, and a stale idle snapshot could unlock that
    // composer mid-send. A slot the user left before the create settled
    // simply gets no optimistic running flag, exactly as before.
    dispatch(appendSlotMessage({ slot, message: { role: 'user', content: visibleMessage, cls: '', ts: new Date().toISOString(), meta } }))
    if (appStore.getState().chat.activeSlot === slot) dispatch(startLocalTurn(slot))
    // A send the server never accepted has to say so where the request landed
    // (#4198): an HTTP 4xx/5xx RESOLVES rather than rejecting, so the catch
    // alone never saw the errors that matter — a refused send left the
    // optimistic bubble on screen next to a slot stuck `running`, with nothing
    // said. The error row is addressed to the slot that OWNS the bubble, not
    // the active one (the user can switch sessions while the POST is in
    // flight); the optimistic `running` is undone only while that slot is
    // still on screen, because `slotRunning` describes the ACTIVE slot and
    // clearing it after a switch would clobber another session's live
    // indicator (a stale flag on this slot self-heals from the server snapshot
    // on the next switch-back). The payload is a canned constant, so unlike
    // the chat composers there is no typed text to hand back — the retry
    // affordance is the feedback pill itself. `endLocalTurn` is the inverse of
    // the mark above: slot-keyed, it drops the unconfirmed mark only if it is
    // still THIS slot's and touches the footer only while this slot is on
    // screen, so it is safe to dispatch whether or not the mark was set.
    const reportFailedSend = (reason?: string) => {
      // FRAMED, not bare: a raw backend reason ("slot agent mismatch") reads
      // as the agent erroring mid-work, not as "your request never went out".
      // Both keys are core-owned siblings, so an app's catalog cannot reword a
      // core error row.
      dispatch(appendSlotMessage({
        slot,
        message: {
          role: 'error',
          content: reason ? i18nT('pages.chatPage.send_failed_with_error', { error: reason }) : i18nT('pages.chatPage.send_failed'),
          cls: '',
        },
      }))
      dispatch(endLocalTurn(slot))
    }
    try {
      // maxAge bounds the seed's lifetime: if the visible send below fails,
      // the queued instructions expire server-side (drain_pending_context
      // discards expired entries) instead of silently attaching the
      // feature-request workflow to a later, unrelated message.
      await api.chatSlotContext(slot, FEATURE_REQUEST_PROMPT_FALLBACK, { source: 'feature-request', maxAge: 60 })
    } catch { /* Send the visible request even if hidden context is unavailable. */ }
    // The chat-core transport owns the receipt contract (`?ws=1` JSON receipt,
    // HTTP 4xx/5xx RESOLVE rather than reject, deadline) and never rejects.
    // `meta` rides the wire exactly as a composer send's does: the gateway
    // persists it on the user row and echoes it, so the echo reconciles the
    // optimistic bubble by `sendId` and the persisted row keeps the stamp the
    // transcript reads the refusal by.
    const receipt = await sendTurn({ message: visibleMessage, slot, colorTheme, meta })
    // Resolution is not success: `refused` means the server accepted neither
    // `ok` nor `queued`, so no turn started and no WS response is coming, and
    // `transport-error` means the request never left. Both get the error row.
    // The indeterminate statuses are deliberately silent -- `unknown` (a 2xx
    // whose body would not parse) means the request WAS accepted, and
    // `response-late` (deadline before a receipt) means it may have been; in
    // both a turn may be running, and this row is the only signal the pill
    // has: claiming a failure it cannot prove tells the user to resend a
    // request that already went out.
    if (receipt.status === 'refused') reportFailedSend(receipt.reason)
    else if (receipt.status === 'transport-error') reportFailedSend()
  }, [dispatch, navigate, colorTheme, appStore])
  return requestFeature
}
