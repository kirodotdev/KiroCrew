import { CheckCircle2, Loader2, MicOff, X } from 'lucide-react'
import ErrorNotice from './ErrorNotice'

import MicSourceMenu from './MicSourceMenu'

import { i18nT } from '../i18n/t'
import { downloadLabel } from '../lib/sttProviders'
import type { SttModelProgress } from '../lib/sttProviders'
interface Props {
  /** True while actively capturing audio. */
  recording: boolean
  /** Live input level in [0, 1] for the meter. */
  level: number
  /** Active capture device label (e.g. "MacBook Pro Microphone"). */
  deviceLabel?: string
  /** deviceId of the track actually capturing — see MicSourceMenu.activeDeviceId. */
  deviceId?: string
  /** Human-readable mic error, or null when none. */
  error?: string | null
  /** Dismiss the error. */
  onDismissError?: () => void
  /** Change the capture device. Receives a deviceId, or '' for system default. */
  onSelectDevice: (deviceId: string) => void
  /** True when a switch applies immediately rather than to the next recording. */
  deviceSwitchIsLive?: boolean
  /** What the speech model this session waits on is doing: fetching, or loading. */
  download?: SttModelProgress | null
  /**
   * True while a released utterance sits in the STREAMING drain: capture is over,
   * the socket is held open, and `onCancelDrain` discards the whole session.
   *
   * Streaming only, and that restriction is the affordance's honesty. A batch
   * transcription's audio is already handed to the transcriber over HTTP, so a
   * discard there cannot stop the transcript from landing; a control offered for
   * it would change the strip and leave the work running. The caller passes this
   * true only where the discard really ends the session.
   */
  draining?: boolean
  /** Discard the drained utterance. Rendered as a pressable control, so the exit
   *  is reachable by a finger and not only by a key. */
  onCancelDrain?: () => void
  /**
   * A visible, non-error status line shown while idle — the reason the mic is
   * blocked ("Microphone in use in another chat"), or that a held dictation just
   * landed. Visible text, not a tooltip: a lone pane has nothing else to explain
   * a greyed mic, and touch has no hover (UX review, #9787).
   */
  /** `action.label` is a substring of `text` rendered as a button (the chat
   *  that holds the mic); the rest of the text stays plain. */
  notice?: { text: string; tone: 'muted' | 'ok'; action?: { label: string; onClick: () => void } } | null
}

/**
 * Thin status strip at the top of the chat input. Shows a dismissible error
 * when the mic fails to start, otherwise a live recording indicator (pulsing
 * dot + input-level meter + active microphone name) while capturing. Once
 * capture ends it shows what the speech model is doing while the retained
 * audio waits on it. Renders nothing when idle, error-free and with no model
 * work outstanding.
 */
/** The notice text with `action.label` rendered as a button, when the label
 *  occurs in the text (it is interpolated into it, so word order per locale
 *  is preserved). Falls back to plain text when it does not. */
/** Quotation marks (and the no-break space French puts inside « ») that the
 *  locales wrap around an interpolated chat name. */
const QUOTE_GLUE = '\u201c\u201d\u201e\u00ab\u00bb\u300c\u300d\u2018\u2019\u201a\u2039\u203a\u00a0'

function renderNoticeText(notice: NonNullable<Props['notice']>) {
  const action = notice.action
  if (!action) return notice.text
  const at = notice.text.indexOf(action.label)
  if (at < 0) return notice.text
  // The quotation marks around the name travel INSIDE the button, so a wrap
  // cannot strand an opening quote at the end of the line above (French keeps
  // a no-break space inside « »; it rides along too).
  let start = at
  let end = at + action.label.length
  while (start > 0 && QUOTE_GLUE.includes(notice.text[start - 1])) start--
  while (end < notice.text.length && QUOTE_GLUE.includes(notice.text[end])) end++
  return (
    <>
      {notice.text.slice(0, start)}
      <button
        type="button"
        onClick={action.onClick}
        className="inline bg-transparent border-none p-0 m-0 text-inherit text-left underline underline-offset-2 hover:text-text cursor-pointer"
      >
        {/* Word joiners: no line break may fall between a quote and the name. */}
        {notice.text.slice(start, at) + (start < at ? '\u2060' : '') + action.label + (end > at + action.label.length ? '\u2060' : '') + notice.text.slice(at + action.label.length, end)}
      </button>
      {notice.text.slice(end)}
    </>
  )
}

export default function VoiceStatusBar({ recording, level, deviceLabel, deviceId, error, onDismissError, onSelectDevice, deviceSwitchIsLive, download, draining, onCancelDrain, notice }: Props) {
  if (error) {
    return (
      <div className="flex items-center px-3 py-1.5 text-[12px] bg-danger-subtle border-b border-danger-subtle">
        {/* Through ErrorNotice, not a hand-written alert (AUTOSDE
            errors-use-error-notice). No `askAgent` hand-off here: the composer
            this bar sits on holds an unsaved draft, and the hand-off navigates
            away from it — a mic error is fixed in the browser/OS permission
            prompt, not by the agent. */}
        <ErrorNotice
          variant="inline"
          className="flex-1 min-w-0"
          message={error}
          onDismiss={onDismissError}
          testId="voice-status-error"
        />
      </div>
    )
  }

  if (!recording) {
    // Capture ends the moment the key is released, but the model this session
    // waits on may still be fetching or loading, and the retained audio is
    // held for it. That wait is the longest thing the user experiences here,
    // so the line explaining it outlives the recorder rather than vanishing
    // with it and leaving a composer that simply does nothing.
    //
    // `drainCancel` is null unless the session is genuinely discardable, which
    // keeps the control and the mechanism in step: the strip offers an exit
    // exactly when pressing it ends the session, and offers none when the wait
    // cannot be ended. A control that changed the strip without stopping the
    // work would read as a stop that had happened.
    const drainCancel = draining && onCancelDrain ? onCancelDrain : null
    const cancelButton = drainCancel
      ? (
        <button
          type="button"
          data-testid="voice-drain-cancel"
          onClick={drainCancel}
          aria-label={i18nT('components.voiceStatusBar.discard_dictation')}
          /* A real target, not a 12px glyph: this row is the only exit on a
             device with no Escape key, so it is sized for a fingertip and keeps
             its label rather than relying on an icon the user must decode. */
          className="shrink-0 inline-flex items-center gap-1 self-start min-h-[28px] px-2 py-1 rounded-md text-[12px] text-muted hover:text-text hover:bg-bg-hover focus-visible:outline focus-visible:outline-1 focus-visible:outline-accent"
        >
          <X size={12} aria-hidden="true" />
          {i18nT('components.voiceStatusBar.discard_dictation')}
        </button>
      )
      : null
    if (download) {
      return (
        <div
          role="status"
          data-testid="voice-status-download"
          className="flex items-start gap-2 px-3 py-1.5 text-[12px] leading-snug border-b border-border bg-chrome/50 text-muted"
        >
          <Loader2 size={13} className="shrink-0 mt-0.5 animate-spin" aria-hidden="true" />
          <span className="flex-1 min-w-0 break-words">
            {downloadLabel(download)}
            {/* Only after release, and only here. The words are already recorded
                and the composer is empty, so without this the wait is
                indistinguishable from having lost the sentence, and the sensible
                response to that is to give up and retype it. The recording strip
                has no room for it and does not need it: the mic is still live
                there, so nothing looks lost yet.

                Its OWN line, not appended to the stage text: the download
                variant ends on a byte figure with no terminal punctuation, so
                side by side the two read as a single sentence — "(612MB of
                1.5GB) Your dictation is kept". Separating them here rather than
                adding a full stop to that string, which the settings panel also
                shows on its own in thirteen catalogs. Not dimmed either: this is
                the sentence that stops the user retyping a dictation that is
                still on its way. */}
            <span className="block">{i18nT('components.voiceStatusBar.dictation_kept_for_model')}</span>
          </span>
          {cancelButton}
        </div>
      )
    }
    if (drainCancel) {
      // The drain before the backend has announced a stage. There is no figure
      // to report and no stage to name, so the line says only the true thing —
      // something is being waited on and the words are safe — and the exit sits
      // beside it. Without this the strip renders nothing at all, and a composer
      // that accepts no dictation while showing no reason is the shape a user
      // reads as broken.
      return (
        <div
          role="status"
          data-testid="voice-status-draining"
          className="flex items-start gap-2 px-3 py-1.5 text-[12px] leading-snug border-b border-border bg-chrome/50 text-muted"
        >
          <Loader2 size={13} className="shrink-0 mt-0.5 animate-spin" aria-hidden="true" />
          <span className="flex-1 min-w-0 break-words">
            {i18nT('components.voiceStatusBar.waiting_for_speech_model')}
            <span className="block">{i18nT('components.voiceStatusBar.dictation_kept_for_model')}</span>
          </span>
          {cancelButton}
        </div>
      )
    }
    if (!notice) return null
    return (
      <div
        role="status"
        data-testid="voice-status-notice"
        className={`flex items-start gap-2 px-3 py-1.5 text-[12px] leading-snug border-b border-border bg-chrome/50 ${notice.tone === 'ok' ? 'text-ok' : 'text-muted'}`}
      >
        {notice.tone === 'ok' ? <CheckCircle2 size={13} className="shrink-0 mt-0.5" aria-hidden="true" /> : <MicOff size={13} className="shrink-0 mt-0.5" aria-hidden="true" />}
        {/* Wraps rather than truncates: the chat name is the row's only action,
            and a `truncate` span clipped the whole button behind "…" at pane
            width whenever the auto-generated title did not fit. */}
        <span className="flex-1 min-w-0 break-words">{renderNoticeText(notice)}</span>
      </div>
    )
  }

  const pct = Math.round(Math.min(1, Math.max(0, level)) * 100)
  return (
    <div
      aria-live="polite"
      className="flex items-center gap-2 px-3 py-1.5 text-[12px] text-danger bg-danger-subtle border-b border-danger-subtle"
    >
      {/* pulsing live dot */}
      <span className="relative flex h-2 w-2 shrink-0" aria-hidden="true">
        <span className="absolute inline-flex h-full w-full rounded-full bg-danger opacity-60 animate-ping" />
        <span className="relative inline-flex h-2 w-2 rounded-full bg-danger" />
      </span>
      <span className="font-medium shrink-0">{i18nT('components.voiceStatusBar.recording')}</span>
      {/* live input-level meter */}
      <span
        className="w-20 shrink-0 h-1.5 rounded-full bg-danger-subtle overflow-hidden"
        aria-hidden="true"
      >
        <span
          className="block h-full bg-danger rounded-full transition-[width] duration-75 ease-out"
          style={{ width: `${pct}%` }}
        />
      </span>
      <MicSourceMenu
        deviceLabel={deviceLabel}
        activeDeviceId={deviceId}
        onSelect={onSelectDevice}
        recording
        liveSwitch={deviceSwitchIsLive}
        triggerClass="text-danger opacity-80 hover:opacity-100"
      />
      {/* Placed AFTER the device picker and allowed to truncate: a first-run
          download is the most important thing on this strip, but it must not
          push the controls that end the recording off the row. */}
      {download && (
        <span className="ml-auto min-w-0 truncate text-muted font-normal">
          {downloadLabel(download)}
        </span>
      )}
    </div>
  )
}
