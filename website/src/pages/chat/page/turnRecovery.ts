import { useCallback, useEffect, useState, type MutableRefObject } from 'react'
import type { NavigateFunction } from 'react-router-dom'

import { api } from '../../../api/client'
import { settingsPath } from '../../../components/settingsPath'
import { useContinuePress } from '../../../hooks/useContinuePress'
import { SETTINGS_DEFAULT_MODEL_ID, SETTINGS_TURN_TIME_LIMIT_HIGHLIGHT } from '../../../hooks/useSettingHighlight'
import { useAppSelector } from '../../../store'
import { selectContinuable, selectTrailingSendUnconfirmed, selectTurnInterrupted } from '../../../store/chatSlice'
import type { ChatMessage } from '../../../types'
import { KIRO_SIGN_IN_PATH } from '../../developer/kiroSignInLink'
import type { CapabilityFix, FreshSessionState } from '../ErrorCard'
import { newestErrorOwnsRecovery } from '../transcriptRenderers'

interface TurnRecoveryOptions {
  activeSlot: string | null
  slotRunning: boolean
  messages: ChatMessage[]
  navigate: NavigateFunction
  /** The model picker's anchor and open state, for the entitlement-error fix. */
  anchorModelBtn: (rect: DOMRect, trigger?: HTMLElement | null) => void
  setModelDropdown: (open: boolean) => void
  modelPickerReturnsFocusRef: MutableRefObject<boolean>
  showRefusedPress: (action: 'continue' | 'fresh_session', e: unknown) => void
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
  // The press and its spinner are `useContinuePress`, owned once with the panes.
  // Why the refusal is rendered rather than logged: the server re-checks under
  // the slot lock and can refuse a press the client believed was available
  // (`slot_running`, `slot_subagents_running`, an approval still pending). Left
  // in the console, that refusal reached the user as the button flicking to
  // disabled and straight back — a control that promises recovery and then says
  // nothing at all. `showRefusedPress` is the shared surface for exactly that.
  const { continuing, press: handleContinue } = useContinuePress({
    slot: activeSlot,
    turnRunning: slotRunning,
    continuable,
    onRefused: (e) => showRefusedPress('continue', e),
  })
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
  // A `turn_timeout` row's fix: the Turn Time Limit field on Settings → Chat →
  // Advanced. Same surface rule as the Default Model link above.
  const openTurnLimitSetting = useCallback(() => {
    navigate(settingsPath({ tab: 'chat', sub: 'advanced', highlight: SETTINGS_TURN_TIME_LIMIT_HIGHLIGHT }))
  }, [navigate])
  // A capability refusal row's fix: the member's Approvals section in the
  // crewmate editor, where a changed agent file is reviewed and saved
  // (`pane=capabilities` is the link's stable spelling), or the editor itself,
  // whose Built from field picks the agent (`foreign_private_copy`,
  // `parent_identity_changed`).
  const openMemberCapabilities = useCallback((member: string, fix: CapabilityFix = 'capabilities') => {
    const pane = fix === 'capabilities' ? '&pane=capabilities' : ''
    navigate(`/capabilities?tab=crews&crew=${encodeURIComponent(member)}${pane}`)
  }, [navigate])
  // The newest error row can be one whose card has withheld Resume and names a
  // remedy of its own: the issue form for the seeded feature-request turn the
  // plan could not afford (#13342), the restart hint for a session start that
  // failed twice, a fresh session for a conversation too big for the model, a
  // refused app-owned agent turn. A retry replays the same rejection, so a
  // composer beneath the card saying "press Resume" would argue with it; its
  // Resume and hint yield too. Typing still works. One predicate spells the
  // set (`newestErrorOwnsRecovery`), and the panes read the same one.
  const cardOwnsRecovery = newestErrorOwnsRecovery(messages)

  // The fix on a `context_overflow` row: drop the native conversation so the
  // next message starts a fresh one from a text summary of this chat. One entry
  // per slot, so a request that settles after the user switched tabs (or
  // pressed in another tab) only ever writes its own slot's state. The server
  // refuses (409) while a turn or sub-agent runs; that refusal is rendered by
  // the shared refused-press surface, like Continue's.
  const [freshSessionBySlot, setFreshSessionBySlot] = useState<Readonly<Record<string, FreshSessionState>>>({})
  const setSlotFreshSession = useCallback((slot: string, state: FreshSessionState) => {
    setFreshSessionBySlot(prev => {
      if ((prev[slot] ?? 'idle') === state) return prev
      const next = { ...prev }
      if (state === 'idle') delete next[slot]
      else next[slot] = state
      return next
    })
  }, [])
  const freshSession: FreshSessionState = (activeSlot && freshSessionBySlot[activeSlot]) || 'idle'
  const handleFreshSession = useCallback(() => {
    if (!activeSlot || freshSession !== 'idle' || slotRunning) return
    const slot = activeSlot
    setSlotFreshSession(slot, 'pending')
    api.resetConversation(slot).then(
      () => setSlotFreshSession(slot, 'done'),
      (e: unknown) => {
        showRefusedPress('fresh_session', e)
        setSlotFreshSession(slot, 'idle')
      },
    )
  }, [activeSlot, freshSession, slotRunning, showRefusedPress, setSlotFreshSession])
  // A new turn on the active slot (the user sent again) starts its state over.
  useEffect(() => { if (slotRunning && activeSlot) setSlotFreshSession(activeSlot, 'idle') }, [slotRunning, activeSlot, setSlotFreshSession])

  // (The newest-error index that gates the Continue button is derived inside
  // the shared row set from the transcript it is handed -- see
  // transcriptRenderers.tsx `lastErrorIndex`.)
  return {
    continuable, interrupted, sendUnconfirmed, continuing, handleContinue,
    openModelPickerFromError, openDefaultModelSetting, openKiroSignIn, openMemberCapabilities, openTurnLimitSetting,
    cardOwnsRecovery,
    freshSession, handleFreshSession,
  }
}
