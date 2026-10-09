import { useCallback } from 'react'
import { motion } from 'framer-motion'
import { ArrowUp, ArrowUpFromLine, Loader2, Square } from 'lucide-react'
import BusySendButton, { readBusySendMode, useBusySendMode, type BusySendMode } from '../BusySendButton'
import type { RootState } from '../../store'
import { selectComposerBusy, selectSlotStreamState } from '../../store/chatSlice'
import { haptic } from '../../lib/haptic'
import { useOverLimitSendConfirm } from '../useOverLimitSendConfirm'
import type { PasteBlock } from '../../utils/pasteTokens'
import { offlineProps } from '../../utils/offline'
import type { SendMode } from '../../pages/chat/ChatSettings'
import { i18nT } from '../../i18n/t'
import type { ComposerBusyMode } from './props'

/* The composer's send path: `fireComposer` is every Enter and Send, idle or
   busy (it holds the send while a batch dictation transcribes, and holds an
   over-limit prompt until the send is repeated), and follow-up chips send
   through `sendFollowUp`. While the slot is busy both decide whether
   the send steers the running turn or queues behind it, and `BusySendControls`
   renders the stop controls that replace the send button through a stop's soft
   and hard phases. */

/** What a send does while its slot is busy: the decision behind the composer's
 *  default Send, kept in one place so every other send surface (a comment
 *  batch, a voice auto-submit, a follow-up answer) takes it instead of a copy. */
export function decideBusySend({ busyMode = 'split', mode, isRunning, stopState, canSteer, jevAutoAvailable }: {
  busyMode?: ComposerBusyMode
  /** The slot's persisted busy-send mode (`useBusySendMode` / `readBusySendMode`). */
  mode: BusySendMode
  isRunning: boolean
  stopState?: 'idle' | 'soft_pending' | 'killing'
  /** The surface has a steer path for this send. */
  canSteer: boolean
  jevAutoAvailable: boolean
}) {
  // Steer is the active Enter/send action only while the composer is busy and
  // not stopping, on a steer-capable slot, and the user hasn't switched the
  // split button to Queue. Everywhere else the composer falls back to onSend
  // (normal send, or server-side queue while busy).
  //
  // `steer-only` has no Queue to switch to, so the persisted per-slot mode is
  // not consulted: a slot that once picked Queue in the main chat must not
  // silently queue from a surface that never shows that choice.
  const steerOnly = busyMode === 'steer-only'
  const busyChoiceAvailable = isRunning && (!stopState || stopState === 'idle') && canSteer
  // A stored `auto` from a session where the seam WAS available resolves back to
  // the shipped default while it is not: consent can be withdrawn and a fleet can
  // pin the seam off, and a mode kept on screen after that would send a flag the
  // gateway refuses to act on — which is a steer either way, but one the sender
  // was told was a decision.
  const effectiveBusyMode: BusySendMode = mode === 'auto' && !jevAutoAvailable ? 'steer' : mode
  // `auto` is an ACTIVE steer: the send goes down the steer route carrying the
  // flag, and the gateway decides there. Its fallback on every refusal is that
  // same steer, so the composer's own reading of "acting now" is unchanged.
  const steerActive = busyChoiceAvailable && (steerOnly || effectiveBusyMode !== 'queue')
  const steerAuto = busyChoiceAvailable && !steerOnly && effectiveBusyMode === 'auto'
  return { steerOnly, busyChoiceAvailable, effectiveBusyMode, steerActive, steerAuto }
}

/** The `steer` flag a send from outside the composer carries: what the
 *  composer's default Send (no chord) on `slotKey` would do right now, with the
 *  inputs its hosts give it (`canSteer` = busy, Auto only while a turn runs).
 *  `undefined` is a plain send (idle, Queue, stopping), `true` steers (or, with
 *  only sub-agents running, starts a turn past their hold), `'auto'` lets the
 *  gateway decide. */
export function busySteerFlag({ slotKey, busy, turnRunning, stopState, jevAutoConsented, busyMode }: {
  slotKey: string | null
  busy: boolean
  turnRunning: boolean
  stopState?: 'idle' | 'soft_pending' | 'killing'
  jevAutoConsented: boolean
  busyMode?: ComposerBusyMode
}): true | 'auto' | undefined {
  const { steerActive, steerAuto } = decideBusySend({
    busyMode, mode: readBusySendMode(slotKey), isRunning: busy, stopState, canSteer: busy,
    jevAutoAvailable: jevAutoConsented && turnRunning,
  })
  return steerActive ? (steerAuto ? 'auto' : true) : undefined
}

/** `busySteerFlag` for a slot read from the store. The active slot reads what
 *  its composer reads; any other slot may have no live run state yet after a
 *  reload, so its slots-stream row counts as running too. */
export function slotBusySteer(state: RootState, slot: string, jevAutoConsented: boolean): true | 'auto' | undefined {
  const row = state.dashboard.slots.find(s => s.key === slot)
  const active = slot === state.chat.activeSlot
  const rowRunning = !active && !!row?.running
  return busySteerFlag({
    slotKey: slot,
    busy: selectComposerBusy(state, slot) || rowRunning,
    turnRunning: (active ? !!state.chat.slotRunning : selectSlotStreamState(state, slot) !== 'idle') || rowRunning,
    stopState: row?.stop_state,
    jevAutoConsented,
  })
}

export function useComposerSend({ slotId, busyMode, isRunning, stopState, canSteer, onSteer, jevAutoAvailable, disabled, holdSend, voiceTranscribing, value, pasteBlocks, contextWindowTokens, pendingFilesCount, pendingSessionsCount, hasQuote, onSend, onStop, onFollowUpSend }: {
  slotId: string | null
  busyMode: ComposerBusyMode
  isRunning: boolean
  stopState?: 'idle' | 'soft_pending' | 'killing'
  canSteer?: boolean
  onSteer?: (opts?: { auto?: boolean; text?: string }) => void
  jevAutoAvailable: boolean
  disabled: boolean
  holdSend: boolean
  voiceTranscribing: boolean
  value: string
  pasteBlocks: PasteBlock[]
  contextWindowTokens?: number
  pendingFilesCount: number
  pendingSessionsCount: number
  /** A whole message is staged as the quote: a draft even with no text. */
  hasQuote: boolean
  onSend: () => void
  onStop?: () => void
  onFollowUpSend?: (text?: string, sourceKeyAtClick?: string | null) => void
}) {
  // Split send button while the composer is BUSY: 'steer' (default) vs 'queue'.
  // The mode is a persisted PER-SLOT preference — see BusySendButton.
  const [busySendMode, setBusySendMode] = useBusySendMode(slotId)
  const { steerOnly, busyChoiceAvailable, effectiveBusyMode, steerActive, steerAuto } = decideBusySend({
    busyMode, mode: busySendMode, isRunning, stopState, canSteer: !!canSteer && !!onSteer, jevAutoAvailable,
  })
  const { pending: overLimitPending, intercept: interceptOverLimitSend } = useOverLimitSendConfirm(
    value,
    pasteBlocks,
    contextWindowTokens,
    slotId,
  )
  /**
   * Fire the composer. `alternate === true` performs the OTHER busy action for
   * this one send — queue when the split button says steer, steer when it says
   * queue — the ⌘↩ / Ctrl+Enter gesture Claude Code and Codex users expect
   * (#4608). Strictly `=== true`: this callback is also wired straight to
   * `onClick`, which hands it a MouseEvent, and an event must read as "default",
   * never as "flip". Outside the busy split (idle, stopping, no steer path) the
   * flag is meaningless and a normal send happens. In `steer-only` there is no
   * other action to flip to — the surface has no queue — so the gesture is a
   * plain steer there too.
   */
  const fireComposer = useCallback((alternate?: unknown) => {
    if (disabled) return
    // A batch dictation is still transcribing: block the send so the pending
    // transcript isn't left behind. Otherwise Enter/Send fires the current draft
    // BEFORE the transcript lands, orphaning the dictation into the emptied
    // composer. The transcript appends within ~1-2s, after which a normal Enter
    // sends the complete text. Covers both Enter (handleKeyDown) and the Send
    // button, since both route through here.
    if (voiceTranscribing) return
    // Same hole for an attachment still uploading: the send would clear the
    // composer and leave without the file, and the file would then land in the
    // emptied composer as a stray attachment for the next message. The Send
    // controls render disabled for the same window; this covers Enter. Checked
    // before the over-limit hold so a held press does not spend its one warning.
    if (holdSend) return
    // An over-limit prompt is held once; repeating the send confirms it.
    if (interceptOverLimitSend()) { haptic('error'); return }
    const flip = alternate === true && busyChoiceAvailable && !steerOnly
    const steerNow = flip ? !steerActive : steerActive
    // A flipped send never asks: the chord is the sender answering the question
    // themselves for this one message, so handing it to the oracle anyway would
    // ignore the only explicit instruction on the send.
    // The message leaves the hand here, on every path (Enter, Send, steer) --
    // but only when there is one: an Enter on an empty composer reaches onSend
    // (which drops it) and must stay as silent as the Send button it disables.
    if (value.trim() || pendingFilesCount || pendingSessionsCount || hasQuote) haptic('light')
    if (steerNow && onSteer) onSteer(steerAuto && !flip ? { auto: true } : undefined)
    else onSend()
  }, [disabled, voiceTranscribing, holdSend, interceptOverLimitSend, busyChoiceAvailable, steerOnly, steerActive, steerAuto, onSteer, onSend, value, pendingFilesCount, pendingSessionsCount, hasQuote])
  // Every stop button in the row goes through this, so the tap and the truthiness
  // checks on `onStop` (which decide whether a button renders at all) stay apart.
  const stopWithTap = useCallback(() => {
    haptic('medium')
    onStop?.()
  }, [onStop])
  // A follow-up chip's send is a Send press carrying its own text, so it takes
  // fireComposer's default busy decision (no chord, so never flipped): a chip
  // steers wherever Send would, and queues only where Send would queue.
  const sendFollowUp = useCallback((text?: string, sourceKeyAtClick?: string | null) => {
    if (disabled) return
    if (steerActive && onSteer) onSteer({ ...(steerAuto ? { auto: true } : {}), ...(text ? { text } : {}) })
    else onFollowUpSend?.(text, sourceKeyAtClick)
  }, [disabled, steerActive, steerAuto, onSteer, onFollowUpSend])

  return { effectiveBusyMode, setBusySendMode, steerOnly, overLimitPending, fireComposer, stopWithTap, sendFollowUp }
}

/** The send slot while a turn runs or a stop is in progress. Stop escalates
 *  from a soft stop to a force kill; a draft offers steer or queue. */
export function BusySendControls({ stopState, killingEscaped, stopWithTap, isQueued, composerHasDraft, canSteer, onSteer, steerOnly, fireComposer, disabled, holdSend, holdSendReason, connected, effectiveBusyMode, setBusySendMode, onBusyModePicked, sendOnEnter, jevAutoAvailable, onStop, stopDeclinedArmed = false, terminalActive = false }: {
  stopState?: 'idle' | 'soft_pending' | 'killing'
  /** The press before this one was declined (compaction); the backend treats
   *  the next press as the force stop, and the armed Stop's hint says so. */
  stopDeclinedArmed?: boolean
  killingEscaped: boolean
  stopWithTap: () => void
  isQueued: boolean
  composerHasDraft: boolean
  canSteer?: boolean
  onSteer?: (opts?: { auto?: boolean }) => void
  steerOnly: boolean
  fireComposer: (alternate?: unknown) => void
  disabled: boolean
  holdSend: boolean
  holdSendReason: string
  connected: boolean
  effectiveBusyMode: BusySendMode
  setBusySendMode: ReturnType<typeof useBusySendMode>[1]
  /** Forwarded to `BusySendButton.onPicked`: the composer's focus hand-back after a steer/queue pick. */
  onBusyModePicked?: () => boolean
  sendOnEnter: SendMode
  jevAutoAvailable: boolean
  onStop?: () => void
  terminalActive?: boolean
}) {
  const heldBusyAction = effectiveBusyMode === 'queue'
    ? i18nT('components.chatInput.queue_message')
    : effectiveBusyMode === 'auto'
      ? i18nT('components.chatInput.auto_jev')
      : i18nT('components.chatInput.steer')
  return (
    stopState === 'killing' ? (
      killingEscaped ? (
        <div className={`flex items-center gap-1.5${terminalActive ? ' min-w-0 [&>button]:shrink-0' : ''}`}>
          <button
            className="w-8 h-8 rounded-lg bg-danger text-danger-fg border-none flex items-center justify-center cursor-pointer hover:bg-danger/80 transition-all"
            onClick={stopWithTap}
            title={i18nT('components.chatInput.force_reset_taking_longer_than_expected')}
            aria-label={i18nT('components.chatInput.force_reset_session_taking_longer_than_expected')}
            data-testid="stop-button-escape-hatch"
          >
            <Square size={18} fill="currentColor" />
          </button>
          <span className={`text-xs text-muted${terminalActive ? ' min-w-0 whitespace-normal [overflow-wrap:anywhere]' : ' whitespace-nowrap'}`} data-testid="stop-escape-hint">{i18nT('components.chatInput.taking_longer_than_expected')}</span>
        </div>
      ) : (
        <button className="w-8 h-8 rounded-lg bg-danger text-danger-fg border-none flex items-center justify-center cursor-not-allowed transition-all" disabled title={i18nT('components.chatInput.killing')} aria-label={i18nT('components.chatInput.killing_session')} data-testid="stop-button-killing">
          <Loader2 size={18} className="animate-spin" />
        </button>
      )
    ) : stopState === 'soft_pending' ? (
      <div className={`flex items-center gap-1.5${terminalActive ? ' min-w-0 [&>button]:shrink-0' : ''}`}>
        {/* Pulse floor 0.8 with a faint danger fill: at 0.6 on a
            transparent background the light-theme button bottomed
            out near white-on-white mid-pulse, and this is the only
            force-stop path while a cancel hangs (#9548 UX review). */}
        <motion.button
          className="w-8 h-8 rounded-lg bg-danger/10 border-none text-danger hover:bg-danger/20 flex items-center justify-center cursor-pointer transition-all"
          onClick={stopWithTap}
          title={i18nT('components.chatInput.force_kill_discards_in_progress_work_and_queued')}
          aria-label={i18nT('components.chatInput.force_kill_session_discards_in_progress_work_and')}
          animate={{ opacity: [0.8, 1, 0.8] }}
          transition={{ duration: 1.2, repeat: Infinity }}
          data-testid="stop-button-pulsing"
        >
          <Square size={18} fill="currentColor" />
        </motion.button>
        {terminalActive && <span aria-hidden="true" className="text-xs text-muted min-w-0 [overflow-wrap:anywhere] @min-[400px]/composer:hidden">{i18nT('components.chatInput.terminal_force_stop')}</span>}
        <span className={`text-xs text-muted whitespace-nowrap${terminalActive ? ' sr-only @min-[400px]/composer:not-sr-only @min-[400px]/composer:whitespace-normal' : ''}`} data-testid="stop-force-hint">{i18nT('components.chatInput.click_again_to_force_stop')}</span>
      </div>
    ) : isQueued ? (
      <button className="w-8 h-8 rounded-full bg-warn text-warn-fg border-none flex items-center justify-center cursor-pointer hover:bg-warn/80 transition-all" onClick={stopWithTap} title={i18nT('components.chatInput.stopping')} aria-label={i18nT('components.chatInput.stopping_2')}>
        <Loader2 size={18} className="animate-spin" />
      </button>
    ) :
    // Deliberately NOT gated on hasSessionRefs, unlike the idle send
    // button the composer renders in this slot. This branch is the mid-turn split button, whose
    // steer mode refuses a payload of refs alone (ChatPage's steer()
    // bails on `!raw && !files.length`, because a failed steer cannot
    // restore what it cleared). Including refs here would enable a
    // primary button whose press does nothing — and that state was
    // unreachable before session refs existed, since an empty composer
    // mid-turn rendered the stop button instead. A bare ref therefore
    // waits for the turn to end and rides the idle send button.
    composerHasDraft ? (
      canSteer && onSteer ? (
        steerOnly ? (
          // No queue concept on this surface: the busy send is the
          // SAME control as the idle one (colour, glyph, name), and
          // pressing it steers. Nothing splits, nothing to pick.
          <button
            className="primary w-8 h-8 rounded-full bg-accent text-accent-fg border-none flex items-center justify-center cursor-pointer hover:bg-accent-hover disabled:opacity-30 disabled:cursor-not-allowed transition-all"
            onClick={fireComposer}
            disabled={disabled || holdSend || !connected}
            title={holdSend ? `${i18nT('components.chatInput.send')} — ${holdSendReason}` : undefined}
            aria-label={i18nT('components.chatInput.send')}
            data-testid="steer-only-send"
            {...offlineProps(connected, 'send', i18nT('components.chatInput.send'))}
          >
            <ArrowUp size={18} />
          </button>
        ) : (
        <BusySendButton
          mode={effectiveBusyMode}
          onModeChange={setBusySendMode}
          onPicked={onBusyModePicked}
          onFire={fireComposer}
          disabled={disabled || holdSend}
          disabledReason={holdSend ? `${heldBusyAction} — ${holdSendReason}` : undefined}
          altChordAvailable={sendOnEnter === 'enter'}
          autoAvailable={jevAutoAvailable}
        />
        )
      ) : (
        <button className="w-8 h-8 rounded-full bg-warn text-warn-fg border-none flex items-center justify-center cursor-pointer hover:bg-warn/80 disabled:opacity-30 disabled:cursor-not-allowed transition-all" onClick={fireComposer} disabled={disabled || holdSend} title={holdSend ? `${i18nT('components.chatInput.queue_message')} — ${holdSendReason}` : i18nT('components.chatInput.queue_message')} aria-label={i18nT('components.chatInput.queue_message')}>
          <ArrowUpFromLine size={18} />
        </button>
      )
    ) : onStop && stopDeclinedArmed ? (
      // The press before this one was declined (compaction); the backend treats
      // the next press as the force stop, and the hint names the cost before
      // the user finds out by pressing. min-w-0 + wrap: a long localized hint
      // shrinks beside the fixed control instead of overflowing a narrow composer.
      <div className="flex items-center gap-1.5 min-w-0">
        <button className="w-8 h-8 shrink-0 rounded-lg bg-transparent border-none text-danger hover:bg-danger/10 flex items-center justify-center cursor-pointer transition-all" onClick={stopWithTap} title={i18nT('components.chatInput.force_kill_discards_in_progress_work_and_queued')} aria-label={i18nT('components.chatInput.force_kill_session_discards_in_progress_work_and')} data-testid="stop-button-armed">
          <Square size={18} fill="currentColor" />
        </button>
        <span className="text-[13px] leading-4 text-muted min-w-0 break-words" data-testid="stop-declined-hint">{i18nT('components.chatInput.click_again_to_force_stop_resets_session')}</span>
      </div>
    ) : onStop ? (
      <button className="w-8 h-8 rounded-lg bg-transparent border-none text-danger hover:bg-danger/10 flex items-center justify-center cursor-pointer transition-all" onClick={stopWithTap} title={i18nT('components.chatInput.stop_generation')} aria-label={i18nT('components.chatInput.stop_generation')} data-testid="stop-button-armed">
        <Square size={18} fill="currentColor" />
      </button>
    ) : steerOnly ? (
      // Same shape-stability rule as the split case below, with the
      // surface's own (plain) send button.
      <button
        className="primary w-8 h-8 rounded-full bg-accent text-accent-fg border-none flex items-center justify-center cursor-not-allowed disabled:opacity-30 transition-all"
        disabled
        aria-label={i18nT('components.chatInput.send')}
        data-testid="steer-only-send"
      >
        <ArrowUp size={18} />
      </button>
    ) : (
      // No stop affordance and nothing typed: keep the split button
      // in place (disabled) so the composer's shape does not jump
      // when the first character lands.
      <BusySendButton
        mode={effectiveBusyMode}
        onModeChange={setBusySendMode}
        onPicked={onBusyModePicked}
        onFire={fireComposer}
        disabled
        altChordAvailable={sendOnEnter === 'enter'}
        autoAvailable={jevAutoAvailable}
      />
    )
  )
}

/** The send slot while an automatic compaction holds the session and no turn
 *  runs. Not a button: a stop-shaped control that does nothing invites a press.
 *  A plain spinner in the Stop control's slot and a visible hint saying Stop is
 *  unavailable, with the same 13px trailing-hint shape as the armed state so the
 *  corner keeps one shape across the stop states. */
export function CompactingIndicator() {
  return (
    <div className="flex items-center gap-1.5 min-w-0" data-testid="compacting-indicator">
      <span
        className="w-8 h-8 shrink-0 rounded-lg text-muted flex items-center justify-center"
        aria-hidden="true"
        data-testid="compacting-spinner"
      >
        <Loader2 size={18} className="animate-spin" />
      </span>
      <span className="text-[13px] leading-4 text-muted min-w-0 break-words" role="status" aria-live="polite" data-testid="compacting-hint">{i18nT('components.chatInput.compacting_context_stop_unavailable')}</span>
    </div>
  )
}
