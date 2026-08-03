import { AlertTriangle, X } from 'lucide-react'

import MicSourceMenu from './MicSourceMenu'

import { i18nT } from '../i18n/t'
interface Props {
  /** True while actively capturing audio. */
  recording: boolean
  /** Live input level in [0, 1] for the meter. */
  level: number
  /** Active capture device label (e.g. "MacBook Pro Microphone"). */
  deviceLabel?: string
  /** Human-readable mic error, or null when none. */
  error?: string | null
  /** Dismiss the error. */
  onDismissError?: () => void
  /** Change the capture device. Receives a deviceId, or '' for system default. */
  onSelectDevice: (deviceId: string) => void
  /** True when a switch applies immediately rather than to the next recording. */
  deviceSwitchIsLive?: boolean
}

/**
 * Thin status strip at the top of the chat input. Shows a dismissible error
 * when the mic fails to start, otherwise a live recording indicator (pulsing
 * dot + input-level meter + active microphone name) while capturing. Renders
 * nothing when idle and error-free.
 */
export default function VoiceStatusBar({ recording, level, deviceLabel, error, onDismissError, onSelectDevice, deviceSwitchIsLive }: Props) {
  if (error) {
    return (
      <div
        role="alert"
        className="flex items-center gap-2 px-3 py-1.5 text-[12px] text-danger bg-danger-subtle border-b border-danger-subtle"
      >
        <AlertTriangle size={13} className="shrink-0" />
        <span className="flex-1 min-w-0">{error}</span>
        {onDismissError && (
          <button
            type="button"
            onClick={onDismissError}
            aria-label={i18nT('components.voiceStatusBar.dismiss_microphone_error')}
            className="shrink-0 text-danger opacity-70 hover:opacity-100 cursor-pointer bg-transparent border-none p-0 flex items-center"
          >
            <X size={13} />
          </button>
        )}
      </div>
    )
  }

  if (!recording) return null

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
        onSelect={onSelectDevice}
        recording
        liveSwitch={deviceSwitchIsLive}
        triggerClass="text-danger opacity-80 hover:opacity-100"
      />
    </div>
  )
}
