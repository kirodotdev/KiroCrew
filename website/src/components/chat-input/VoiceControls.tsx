import { ArrowUp, Keyboard, Loader2, Mic, MicOff, X } from 'lucide-react'
import VoiceStatusBar from '../VoiceStatusBar'
import VoiceDictationPanel from '../VoiceDictationPanel'
import { Btn } from '../ui'
import type { useTouchPushToTalk } from '../../hooks/useTouchPushToTalk'
import type { ComposerVoiceInputProps } from '../../chat-core/composer/Composer'
import { i18nT } from '../../i18n/t'

/* The composer's voice controls: the capture status (hold-to-talk cancel
   cue, dictation panel or status bar), the hold target, and the mic. State
   comes from `chat-input/voice.ts`; these render it. */

type TouchPtt = ReturnType<typeof useTouchPushToTalk>
type VoiceInput = Partial<ComposerVoiceInputProps>
/** Stable no-op so an unwired embedder does not remount the picker each render. */
const noopSelectDevice = () => {}

/** Above the editor: the hold gesture's cancel cue while a press is live, then
 *  the dictation panel while recording, else the thin status bar. */
export function VoiceCaptureStatus({ voiceHoldMode, touchPtt, showDictation, value, voicePartial, voiceDeviceLabel, voiceDeviceId, onSelectVoiceDevice, voiceDeviceSwitchIsLive, voiceStreaming, voiceDownload, voiceRecording, voiceLevel, voiceError, onClearVoiceError, voiceDrainCancellable, cancelVoiceDrain, onVoiceToggle, micHeldElsewhere, micHeldElsewhereLabel, micHeldElsewhereAction, voiceHeldLanded }: {
  voiceHoldMode: boolean
  touchPtt: TouchPtt
  showDictation: NonNullable<VoiceInput['voiceSampleRef']> | null
  value: string
  voicePartial: string
  voiceDeviceLabel: string
  voiceDeviceId: string
  onSelectVoiceDevice: VoiceInput['onSelectVoiceDevice']
  voiceDeviceSwitchIsLive: boolean
  voiceStreaming: boolean
  voiceDownload: VoiceInput['voiceDownload']
  voiceRecording: boolean
  voiceLevel: number
  voiceError: VoiceInput['voiceError']
  onClearVoiceError: VoiceInput['onClearVoiceError']
  voiceDrainCancellable: boolean
  cancelVoiceDrain?: () => void
  onVoiceToggle: VoiceInput['onVoiceToggle']
  micHeldElsewhere: boolean
  micHeldElsewhereLabel: string
  micHeldElsewhereAction?: { label: string; onClick: () => void }
  voiceHeldLanded: boolean
}) {
  return (
    <>
      {/* Cancel cue for the hold gesture. Rendered above the dictation panel so
          the drop zone is genuinely UP from the thumb, and only while a press is
          live — a permanent hint would be noise in a composer used mostly for
          typing. `aria-live` announces the arm/disarm flip, which is the only
          feedback a screen-reader user gets for a gesture with no focus change. */}
      {voiceHoldMode && touchPtt.phase !== 'idle' && (
        <div
          data-testid="hold-cancel-cue"
          aria-live="polite"
          className={`flex items-center justify-center gap-1.5 py-1.5 text-[11.5px] font-medium transition-colors ${
            touchPtt.armedCancel ? 'bg-danger text-danger-fg' : 'text-muted border-b border-dashed border-border-strong'
          }`}
        >
          {touchPtt.armedCancel ? (
            <><X size={12} className="shrink-0" />{i18nT('components.chatInput.release_to_cancel')}</>
          ) : (
            <><ArrowUp size={12} className="shrink-0" />{i18nT('components.chatInput.slide_up_to_cancel')}</>
          )}
        </div>
      )}


      {showDictation ? (
        /* `gestureDriven` carries the settling term because ownership ends at
           the release while this panel outlives it: `showDictation` is gated
           on `voiceRecording`, which stays true through the streaming drain.
           `bar === 'settling'` can only name the gesture's OWN drain (the
           hook records `draining` solely on its own commit path), so the
           keyboard hint stays suppressed for exactly the drain the finger
           just committed — and stays SHOWN for a keyboard-binding capture,
           where Esc/Enter genuinely work. */
        <VoiceDictationPanel sampleRef={showDictation} value={value} partial={voicePartial} deviceLabel={voiceDeviceLabel} deviceId={voiceDeviceId} onSelectDevice={onSelectVoiceDevice || noopSelectDevice} deviceSwitchIsLive={voiceDeviceSwitchIsLive} streaming={voiceStreaming} gestureDriven={voiceHoldMode || touchPtt.bar === 'settling'} download={voiceDownload} />
      ) : (
        <VoiceStatusBar
          recording={voiceRecording} level={voiceLevel} deviceLabel={voiceDeviceLabel} deviceId={voiceDeviceId} error={voiceError} onDismissError={onClearVoiceError} onSelectDevice={onSelectVoiceDevice || noopSelectDevice} deviceSwitchIsLive={voiceDeviceSwitchIsLive} download={voiceDownload}
          /* The released utterance's own window. Gated on the transport of the
             request IN FLIGHT, which is what `voiceDrainCancellable` reads: a
             streaming drain is held against an open socket and the discard
             closes it, while a batch transcription is already in the
             transcriber's hands over HTTP and the strip offers it no exit
             rather than an exit that leaves the work running.

             Not on `voiceStreaming`. That is the saved setting, so it describes
             the NEXT utterance; a setting flipped while one request is open
             names a transport nothing in flight is using, and the control then
             appears over a batch request whose transcript still lands. The flag
             is ownership-gated at its source, so a composer offers the discard
             for its OWN drain and never for a session another chat holds. */
          draining={voiceDrainCancellable}
          onCancelDrain={cancelVoiceDrain}
          /* Visible reasons, not tooltips: why the mic is blocked, or that a
             held dictation just arrived. Only while the mic is offered at all.
             Shown in hold mode too: one message, one shape, and the name
             button (the way to the capturing chat) stays reachable there —
             the disabled hold bar keeps its plain label. */
          notice={onVoiceToggle && micHeldElsewhere
            ? { text: micHeldElsewhereLabel, tone: 'muted', action: micHeldElsewhereAction }
            : voiceHeldLanded
              ? { text: i18nT('components.chatInput.dictation_added'), tone: 'ok' }
              : null}
        />
      )}
    </>
  )
}

/**
 * The hold target. A real <button>, not the textarea: a long press on a
 * text field opens iOS's selection loupe and swallows the pointermoves the
 * cancel gesture is measured from, so "hold the input box" cannot be built
 * on the input box. `touch-action:none` stops the page claiming the drag as
 * a scroll, and the two -webkit rules stop the long-press callout.
 * `flex-1` mirrors the textarea so a manually-resized composer does not
 * fight the persisted height.
 */
export function HoldToTalkBar({ manualHeight, setHoldTarget, touchPtt, disabled, micBlocked, optimizing, voiceSettling, holdBarLabel }: {
  manualHeight: number | null
  setHoldTarget: (el: HTMLButtonElement | null) => void
  touchPtt: TouchPtt
  disabled: boolean
  micBlocked: boolean
  optimizing: boolean
  voiceSettling: boolean
  holdBarLabel: string
}) {
  return (
    <div className={`flex px-2.5 pt-2 pb-0.5 ${manualHeight !== null ? 'flex-1 min-h-0' : ''}`}>
      <Btn
        type="button"
        ref={setHoldTarget}
        data-testid="hold-to-talk"
        style={{ touchAction: 'none', WebkitUserSelect: 'none', WebkitTouchCallout: 'none' }}
        // `flex-1` inside a flex row, NOT `flex` on its own: a <button> sizes
        // to fit-content even as a block-level flex container (UA form-control
        // sizing), so a bare display swap leaves a small pill where the whole
        // point is a target a thumb can hit without aiming.
        // `primary` while holding, not just accent classes: Btn's default
        // variant carries `hover:bg-bg-hover`, and a finger (or a mouse) on
        // the bar IS a hover, so the accent fill was overridden the moment
        // it mattered and a live capture read as a switched-off button
        // (UX review, light theme). The primary variant's hover stays accent.
        primary={touchPtt.bar === 'holding'}
        className={`flex-1 min-h-[44px] justify-center rounded-xl font-semibold select-none ${
          touchPtt.bar === 'armed-cancel'
            ? 'border-dashed border-danger bg-danger-subtle text-danger'
            : touchPtt.bar === 'holding'
              ? ''
              : 'border-border-strong bg-card text-text-strong'
        }`}
        disabled={disabled || micBlocked || optimizing || voiceSettling}
        aria-label={holdBarLabel}
      >
        <Mic size={15} className="shrink-0" />
        {holdBarLabel}
      </Btn>
    </div>
  )
}

/** The mic: a record toggle, or on touch the switch between the keyboard and
 *  hold-to-talk (see `micIsModeSwitch` in `chat-input/voice.ts`). */
export function MicButton({ voiceHoldMode, voiceRecording, micIsModeSwitch, transcribeInFlight, micHeldElsewhere, micBlocked, toggleVoiceMode, onVoiceToggle, onVoicePrewarm, disabled, optimizing, micLabel }: {
  voiceHoldMode: boolean
  voiceRecording: boolean
  micIsModeSwitch: boolean
  transcribeInFlight: boolean
  micHeldElsewhere: boolean
  micBlocked: boolean
  toggleVoiceMode: () => void
  onVoiceToggle: NonNullable<VoiceInput['onVoiceToggle']>
  onVoicePrewarm: VoiceInput['onVoicePrewarm']
  disabled: boolean
  optimizing: boolean
  micLabel: string
}) {
  return (
    <button
      type="button"
      // In hold mode the switch carries a visible label: its `title` is
      // hover-only and hold mode is a touch surface, so a bare icon read
      // as "no idea what it toggles".
      className={`${voiceHoldMode ? 'px-2.5 gap-1.5 text-[12px] font-medium' : 'w-8'} h-8 rounded-lg flex items-center justify-center cursor-pointer transition-all border-none ${
        // The recording tint belongs to the RECORD button. As a mode
        // switch (hold mode) this button hands the keyboard back; a red
        // pulse on it read as an alarm on an unexplained control.
        voiceRecording && !micIsModeSwitch ? 'bg-danger-subtle text-danger animate-pulse' : (!micIsModeSwitch && transcribeInFlight) ? 'bg-accent-subtle text-accent' : voiceHoldMode ? 'bg-accent-subtle text-accent' : 'text-muted hover:text-text hover:bg-bg-hover bg-transparent'
      } disabled:opacity-30`}
      // The mic does whichever voice thing is AVAILABLE right now, which is
      // what keeps it from becoming a dead control. On an empty composer
      // that is the mode switch. With a draft, hold mode is suspended
      // anyway (a hold bar cannot show text you need to read and edit),
      // so the mic reverts to the job it had before this feature: tap to
      // dictate, transcript spliced in at the caret.
      //
      // Without that second branch the switch was disabled on every draft,
      // on every coarse-pointer device — including for someone who never
      // opened hold mode — and since the mic is the only voice entry point
      // on touch, dictating onto existing text became impossible. Speak,
      // glance, speak again is how a long message actually gets composed
      // on a phone, so losing it is not a cost of the new mode; it would
      // have been an unconditional regression in the old one.
      onClick={micIsModeSwitch ? toggleVoiceMode : onVoiceToggle}
      // Prewarm only when the press will actually record. On the switch it
      // would acquire the mic for a press that changes layout, and in hold
      // mode the gesture's own pointerdown opens capture earlier anyway.
      onPointerDown={micIsModeSwitch ? undefined : onVoicePrewarm}
      /* A foreign transcription blocks STARTING a capture, so it gates the
         mic only while the mic is the record button. As a MODE SWITCH the
         click starts nothing — it hands the keyboard back — and disabling
         it there strands the user in voice mode, unable to type or send
         until unrelated work in another session finishes. */
      /* Enabled mid-capture too. A press the hold bar owns is discarded
         when its target unmounts (`useTouchPushToTalk.abandon`), so the
         switch cancels the capture and hands the keyboard back — the
         greyed control beside an identical enabled one in a sibling pane
         read as "no idea why it's off" (UX review on #9787). */
      disabled={disabled || optimizing || (micIsModeSwitch ? false : micBlocked)}
      aria-label={micLabel}
      title={micLabel}
    >
      {!micIsModeSwitch && transcribeInFlight ? <Loader2 size={18} className="animate-spin" /> : !micIsModeSwitch && micHeldElsewhere ? <MicOff size={18} /> : voiceHoldMode ? <><Keyboard size={18} /><span className="leading-none">{i18nT('components.chatInput.type_label')}</span></> : <Mic size={18} />}
    </button>
  )
}
