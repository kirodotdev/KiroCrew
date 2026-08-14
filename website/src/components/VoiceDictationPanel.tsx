import { useEffect, useState } from 'react'
import Strands, { strandsSupported } from './Strands'
import type { AudioSample } from '../hooks/mic'
import MicSourceMenu from './MicSourceMenu'
import { i18nT } from '../i18n/t'

/**
 * True when the animated panel should be used. Kept as a hook (rather than a
 * module constant) so a runtime change to the OS reduced-motion preference
 * takes effect without a reload.
 *
 * Under reduced motion we fall back to the bar meter rather than freezing the
 * shader on a static frame: a frozen frame communicates nothing about input
 * level, which is the entire job of this surface.
 */
export function useDictationPanelUsable(enabled: boolean): boolean {
  const [reduced, setReduced] = useState(
    () =>
      typeof window !== 'undefined' &&
      typeof window.matchMedia === 'function' &&
      window.matchMedia('(prefers-reduced-motion: reduce)').matches === true,
  )
  const [supported] = useState(strandsSupported)

  useEffect(() => {
    if (typeof window === 'undefined' || typeof window.matchMedia !== 'function') return
    const mql = window.matchMedia('(prefers-reduced-motion: reduce)')
    const handler = () => setReduced(mql.matches)
    mql.addEventListener('change', handler)
    return () => mql.removeEventListener('change', handler)
  }, [])

  return enabled && supported && !reduced
}

interface Props {
  /** Live audio features. Handed to the shader as a ref, never as a value. */
  sampleRef: { current: AudioSample }
  /** Full composer text (frozen prefix + committed transcript + partial). */
  value: string
  /** Latest partial hypothesis, when streaming STT is on. */
  partial?: string
  /** Active capture device label. */
  deviceLabel?: string
  /** deviceId of the track actually capturing — see MicSourceMenu.activeDeviceId. */
  deviceId?: string
  /** Change the capture device. Receives a deviceId, or '' for system default. */
  onSelectDevice: (deviceId: string) => void
  /** True when a switch applies immediately rather than to the next recording. */
  deviceSwitchIsLive?: boolean
  /** True for streaming STT (live transcript in the composer → Enter sends). In
   *  batch STT there is no transcript until the mic is stopped, so the hint must
   *  point at the mic instead of promising Enter can send. */
  streaming?: boolean
}

/**
 * Dictation panel shown in place of the composer's status bar while recording.
 *
 * The transcript is rendered over the shader: text already committed by the STT
 * backend is solid, the in-flight partial hypothesis is muted. Both come from
 * the composer's own value, so what is shown here is exactly what will be sent.
 */
export default function VoiceDictationPanel({ sampleRef, value, partial, deviceLabel, deviceId, onSelectDevice, deviceSwitchIsLive, streaming }: Props) {
  // Split committed vs partial without coupling to STT internals: the partial
  // is appended to the composer value, so it is the suffix — but only trust
  // that when it actually matches (the user may have typed since).
  const hasPartial = !!partial && value.endsWith(partial)
  const committed = hasPartial ? value.slice(0, value.length - partial.length) : value

  return (
    <div
      className="relative h-[168px] overflow-hidden border-b border-border bg-bg"
      data-testid="voice-dictation-panel"
    >
      <Strands sampleRef={sampleRef} />
      <div className="absolute inset-0 z-[3] flex flex-col justify-between px-[18px] py-3.5 pointer-events-none">
        <div className="flex items-center gap-2 text-[11.5px] font-medium text-danger">
          <span className="relative flex h-2 w-2 shrink-0" aria-hidden="true">
            <span className="absolute inline-flex h-full w-full rounded-full bg-danger opacity-60 animate-ping" />
            <span className="relative inline-flex h-2 w-2 rounded-full bg-danger" />
          </span>
          <span aria-live="polite">{i18nT('components.voiceDictationPanel.listening')}</span>
          {/* The overlay is `pointer-events-none` so the shader stays visible and
              un-clickable; the picker is the one interactive child, so it opts
              itself back in. Still gated on a known label, preserving this
              panel's deliberate difference from VoiceStatusBar (an extra row at
              17px over a live shader is noise, and the panel's job is the
              transcript) — by the time the panel is up the label has resolved,
              so the picker is reachable in practice. `max-w-[40%]` keeps a long
              device name from pushing the keyboard hint out of the row. */}
          {deviceLabel && (
            <span className="pointer-events-auto max-w-[40%] min-w-0 flex items-center">
              <MicSourceMenu
                deviceLabel={deviceLabel}
                activeDeviceId={deviceId}
                onSelect={onSelectDevice}
                recording
                liveSwitch={deviceSwitchIsLive}
                triggerClass="text-muted font-normal hover:text-text"
              />
            </span>
          )}
          <span className="ml-auto text-muted font-normal font-mono text-[11px]">
            {streaming
              ? i18nT('components.voiceDictationPanel.esc_to_cancel_enter_to_send')
              : i18nT('components.voiceDictationPanel.esc_to_cancel_click_mic_to_finish')}
          </span>
        </div>
        {/* Text sits over a live shader, so it carries its own shadow floor
            rather than relying on the background staying dark. */}
        <div
          className="text-[17px] leading-[1.45] text-text-strong max-h-20 overflow-hidden [text-shadow:0_1px_12px_var(--bg),0_0_3px_var(--bg)]"
          data-testid="voice-dictation-transcript"
        >
          {committed}
          {hasPartial && <span className="text-muted">{partial}</span>}
        </div>
      </div>
    </div>
  )
}
