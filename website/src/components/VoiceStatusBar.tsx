import { CheckCircle2, MicOff } from 'lucide-react'
import ErrorNotice from './ErrorNotice'

import MicSourceMenu from './MicSourceMenu'

import { i18nT } from '../i18n/t'
import { downloadLabel } from '../lib/sttProviders'
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
  /** Byte progress of the one-time speech-model download this session waits on. */
  download?: { done: number; total: number } | null
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
 * dot + input-level meter + active microphone name) while capturing. Renders
 * nothing when idle and error-free.
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
        className="inline bg-transparent border-none p-0 m-0 font-inherit text-inherit text-left underline underline-offset-2 hover:text-text cursor-pointer"
      >
        {/* Word joiners: no line break may fall between a quote and the name. */}
        {notice.text.slice(start, at) + (start < at ? '\u2060' : '') + action.label + (end > at + action.label.length ? '\u2060' : '') + notice.text.slice(at + action.label.length, end)}
      </button>
      {notice.text.slice(end)}
    </>
  )
}

export default function VoiceStatusBar({ recording, level, deviceLabel, deviceId, error, onDismissError, onSelectDevice, deviceSwitchIsLive, download, notice }: Props) {
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
