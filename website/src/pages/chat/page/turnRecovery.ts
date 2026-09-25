import { useCallback, useEffect, useState, type MutableRefObject } from 'react'
import type { NavigateFunction } from 'react-router-dom'

import { api } from '../../../api/client'
import { settingsPath } from '../../../components/settingsPath'
import { SETTINGS_DEFAULT_MODEL_ID } from '../../../hooks/useSettingHighlight'
import { useAppSelector } from '../../../store'
import { selectContinuable, selectTrailingSendUnconfirmed, selectTurnInterrupted } from '../../../store/chatSlice'
import type { ChatMessage } from '../../../types'
import { KIRO_SIGN_IN_PATH } from '../../developer/kiroSignInLink'
import { featureRequestRefusalIsNewest, sessionStartRepeatIsNewest } from '../transcriptRenderers'

interface TurnRecoveryOptions {
  activeSlot: string | null
  slotRunning: boolean
  messages: ChatMessage[]
  navigate: NavigateFunction
  /** The model picker's anchor and open state, for the entitlement-error fix. */
  anchorModelBtn: (rect: DOMRect, trigger?: HTMLElement | null) => void
  setModelDropdown: (open: boolean) => void
  modelPickerReturnsFocusRef: MutableRefObject<boolean>
  showRefusedPress: (action: 'continue', e: unknown) => void
}

/**
 * Recovering a turn that ended without handing the floor back: whether Continue
 * is offered and how it describes itself, the press and its spinner, and the
 * fixes an error row can offer (pick a model, open the default-model setting,
 * sign in to Kiro, review a crewmate's capabilities).
 */
export function useTurnRecovery({
  activeSlot,
  slotRunning,
  messages,
  navigate,
  anchorModelBtn,
  setModelDropdown,
  modelPickerReturnsFocusRef,
  showRefusedPress,
}: TurnRecoveryOptions) {
  // ---- Continue the thread ---------------------------------------------------
  // A turn can end without the assistant handing the floor back: the connection
  // dropped, the gateway restarted during an app update, the app was force-quit,
  // or the runner's own recovery ladder gave up. Some of those leave evidence (an
  // unanswered user row, a trailing error card) and some leave none at all — a
  // force-quit runs no cleanup, so its transcript is indistinguishable from a
  // clean finish. Continue is therefore offered on any idle slot with a
  // conversation, and `interrupted` only decides how the button describes itself.
  //
  // The two COMPOSE at the ErrorCard; neither alone is right. `continuable` is the
  // availability half (running, stopping, pending turn, subagents,
  // queue) and `interrupted` is the placement half — `i === lastErrorIdx` means
  // "newest error row", never "the transcript ends badly", so on
  // `[user, error, user, assistant]` availability alone would put a Continue
  // button on a superseded failure card that acts on a LATER request. Dropping
  // `continuable` instead is the mirror-image bug: `selectTurnInterrupted` carries
  // none of the busy checks, so a card would offer a Continue that `handleContinue`
  // early-returns on — a dead control in the one place recovery is promised.
  const continuable = useAppSelector(selectContinuable)
  const interrupted = useAppSelector(selectTurnInterrupted)
  // The footer's plain running indicator yields while the newest send is a
  // bubble whose receipt never came (see `selectTrailingSendUnconfirmed`).
  const sendUnconfirmed = useAppSelector(selectTrailingSendUnconfirmed)
  const [continuing, setContinuing] = useState(false)
  // Why the refusal is rendered rather than logged: the server re-checks under
  // the slot lock and can refuse a press the client believed was available
  // (`slot_running`, `slot_subagents_running`, an approval still pending). Left
  // in the console, that refusal reached the user as the button flicking to
  // disabled and straight back — a control that promises recovery and then says
  // nothing at all. `showRefusedPress` is the shared surface for exactly that.
  useEffect(() => { setContinuing(false) }, [activeSlot])
  // The turn taking over is the success signal; clear the spinner then.
  useEffect(() => { if (continuing && slotRunning) setContinuing(false) }, [continuing, slotRunning])
  // Backstop: a request that neither starts a turn nor rejects must not strand
  // the button in a disabled state. Mirrors the regenerate safety timeout.
  useEffect(() => {
    if (!continuing) return
    const t = setTimeout(() => { setContinuing(false) }, 30_000)
    return () => clearTimeout(t)
  }, [continuing])
  // Fix affordances on a model-entitlement error row. The picker is the same
  // portal the composer's model chip opens, anchored to that chip so it lands
  // where the user already knows to look; when the chip is not on screen (a
  // collapsed composer) the picker still opens, anchored to the composer edge.
  const openModelPickerFromError = useCallback(() => {
    const chip = document.querySelector<HTMLElement>('[data-testid="composer-model-chip"]')
    const rect = chip?.getBoundingClientRect()
      ?? new DOMRect(16, Math.max(0, window.innerHeight - 96), 160, 28)
    anchorModelBtn(rect, chip)
    // Opened from a transcript row, not from the composer: nothing to return to.
    modelPickerReturnsFocusRef.current = false
    setModelDropdown(true)
  }, [anchorModelBtn, setModelDropdown, modelPickerReturnsFocusRef])
  // The Default Model setting lives only on the full dashboard's Settings →
  // Chat tab. /embed/settings is a different page (Display), and a popout has
  // no settings route at all, so on both surfaces the affordance is omitted
  // rather than pointed at a page that does not carry the setting.
  const openDefaultModelSetting = useCallback(() => {
    navigate(settingsPath({ tab: 'chat', sub: 'models', highlight: SETTINGS_DEFAULT_MODEL_ID }))
  }, [navigate])
  // The Kiro sign-in card (an `auth_required` error row's fix) lives on the
  // full dashboard's Settings > Agent Harness tab, under the switch that
  // selects the KAS backend the row can only come from; same surface rule as
  // the Default Model link above.
  const openKiroSignIn = useCallback(() => {
    navigate(KIRO_SIGN_IN_PATH)
  }, [navigate])
  // A `materialization_changed` row's fix: the member's Capabilities pane in
  // the crew editor, where the changed agent file is reviewed and saved.
  const openMemberCapabilities = useCallback((member: string) => {
    navigate(`/capabilities?tab=crews&crew=${encodeURIComponent(member)}&pane=capabilities`)
  }, [navigate])
  // The non-inference exit for a feature request the plan could not afford
  // (#13342) is decided per row in the shared row set, from the row alone: the
  // user row the header's "Request a Feature" action sent carries the flow's
  // stamp in its `meta`, so the form is offered on that turn's own refusal,
  // while a usage limit in an ordinary chat, or after the user typed on in
  // this one, keeps today's card. The card withholds Resume on that refusal
  // because a retry replays the rejection; the composer must not urge it
  // beneath the same card, so its Resume and "press Resume" hint yield too
  // (same rule, same row).
  const featureRequestRefused = featureRequestRefusalIsNewest(messages)
  // Same rule for a session start that failed twice in a row: the card has
  // withheld Resume (a third press re-runs the same start, and the server
  // refuses it with `session_start_repeat`) and names the remedy, so the
  // composer must not urge the press beneath it. Typing still works and is
  // what resets the count.
  const sessionStartRepeated = sessionStartRepeatIsNewest(messages)

  const handleContinue = useCallback(() => {
    if (!activeSlot || continuing || !continuable) return
    setContinuing(true)
    // No optimistic transcript mutation: the backend appends the continuation as
    // an `inject` row and the WS `slots` update flips `running`, so the UI
    // converges from the server. Nothing to roll back on failure.
    api.continueSlot(activeSlot).catch((e: unknown) => {
      showRefusedPress('continue', e)
      setContinuing(false)
    })
  }, [activeSlot, continuing, continuable, showRefusedPress])
  // (The newest-error index that gates the Continue button is derived inside
  // the shared row set from the transcript it is handed -- see
  // transcriptRenderers.tsx `lastErrorIndex`.)
  return {
    continuable, interrupted, sendUnconfirmed, continuing, handleContinue,
    openModelPickerFromError, openDefaultModelSetting, openKiroSignIn, openMemberCapabilities,
    featureRequestRefused, sessionStartRepeated,
  }
}
