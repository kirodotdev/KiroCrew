import { useEffect, useRef } from 'react'
import { motion, useIsPresent } from 'framer-motion'
import LevelBars from './LevelBars'
import { useReducedMotion } from '../hooks/useReducedMotion'
import type { AudioSample } from '../hooks/mic'
import MicSourceMenu from './MicSourceMenu'
import RecordingElapsed from './RecordingElapsed'
import VoiceMuteButton from './VoiceMuteButton'
import { downloadLabel } from '../lib/sttProviders'
import type { SttModelProgress } from '../lib/sttProviders'
import { i18nT } from '../i18n/t'

/**
 * True when the animated panel should be used. Kept as a hook (rather than a
 * module constant) so a runtime change to the OS reduced-motion preference
 * takes effect without a reload.
 *
 * Under reduced motion we fall back to the status bar's bar meter rather than
 * freezing the bars on a static frame: a frozen frame communicates nothing
 * about input level, which is the entire job of this surface.
 */
export function useDictationPanelUsable(enabled: boolean): boolean {
  const reduced = useReducedMotion()
  return enabled && !reduced
}

interface Props {
  /** Live audio features. Handed to the level bars as a ref, never as a value. */
  sampleRef: { current: AudioSample }
  /** Live text, shown only while the touch hold target has replaced the textarea. */
  transcript?: string
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
  /** True when a hold gesture is driving this session. */
  gestureDriven?: boolean
  /** `stt.submit_on_release`: clicking the mic sends in either transport. */
  submitOnRelease?: boolean
  /** What the speech model this session waits on is doing: fetching, or loading. */
  download?: SttModelProgress | null
  /** The recording is muted: the mic is open, nothing is sent, the timer runs. */
  muted?: boolean
  /** Mute or unmute the recording. The control is shown only when this is set. */
  onToggleMute?: () => void
}

/** The capture status row and live waveform shown above the composer's text box. */
export default function VoiceDictationPanel({ sampleRef, transcript, deviceLabel, deviceId, onSelectDevice, deviceSwitchIsLive, streaming, gestureDriven, submitOnRelease, download, muted = false, onToggleMute }: Props) {
  const capturing = useIsPresent()
  const transcriptRef = useRef<HTMLDivElement>(null)
  const followTranscriptRef = useRef(true)

  useEffect(() => {
    const element = transcriptRef.current
    if (element && followTranscriptRef.current) element.scrollTop = element.scrollHeight
  }, [transcript])

  return (
    <motion.div
      initial={{ height: 0, opacity: 0 }}
      animate={{ height: 'auto', opacity: 1 }}
      exit={{ height: 0, opacity: 0 }}
      transition={{ duration: 0.22, ease: 'easeOut' }}
      className="overflow-hidden"
      data-testid="voice-dictation-panel"
    >
      <div className="flex flex-col gap-1.5 px-[18px] pt-3">
        <div className={`flex flex-wrap items-center gap-x-2 gap-y-0.5 text-[11.5px] font-medium ${!capturing || muted ? 'text-muted' : 'text-danger'}`}>
          <span className="relative flex h-2 w-2 shrink-0" aria-hidden="true">
            {capturing && !muted && <span className="absolute inline-flex h-full w-full rounded-full bg-danger opacity-60 animate-ping" />}
            <span className={`relative inline-flex h-2 w-2 rounded-full ${!capturing || muted ? 'bg-muted' : 'bg-danger'}`} />
          </span>
          <span aria-live="polite">
            {i18nT(muted ? 'components.voiceDictationPanel.muted' : 'components.voiceDictationPanel.listening')}
          </span>
          <RecordingElapsed frozen={!capturing} className="font-mono text-[11px]" />
          {onToggleMute && capturing && (
            <VoiceMuteButton muted={muted} onToggle={onToggleMute} className={`hover:text-text ${muted ? 'text-text' : 'text-muted'}`} />
          )}
          {deviceLabel && (
            <span className="max-w-full min-w-0 flex items-center">
              <MicSourceMenu
                deviceLabel={deviceLabel}
                activeDeviceId={deviceId}
                onSelect={onSelectDevice}
                recording={capturing}
                liveSwitch={capturing && deviceSwitchIsLive}
                triggerClass="text-muted font-normal hover:text-text"
              />
            </span>
          )}
          {capturing && !gestureDriven && (
            <span className="ml-auto whitespace-nowrap text-text font-normal font-mono text-[11px]">
              {submitOnRelease
                ? i18nT('components.voiceDictationPanel.esc_to_cancel_click_mic_to_send')
                : streaming
                  ? i18nT('components.voiceDictationPanel.esc_to_cancel_enter_to_send')
                  : i18nT('components.voiceDictationPanel.esc_to_cancel_click_mic_to_finish')}
            </span>
          )}
        </div>
        <LevelBars sampleRef={sampleRef} />
        {capturing && streaming && !gestureDriven && (
          <div className="text-[11.5px] text-muted" data-testid="voice-dictation-edit-locked">
            {i18nT('components.voiceDictationPanel.editing_resumes_when_you_stop')}
          </div>
        )}
        {transcript && (
          <div
            ref={transcriptRef}
            aria-hidden="true"
            data-testid="hold-to-talk-transcript"
            onScroll={event => {
              const element = event.currentTarget
              followTranscriptRef.current = element.scrollHeight - element.scrollTop - element.clientHeight <= 1
            }}
            className="max-h-12 overflow-y-auto overscroll-contain whitespace-pre-wrap text-[15px] leading-snug text-text"
          >
            {transcript}
          </div>
        )}
        {download && (
          <div aria-live="polite" className="text-[12px] text-muted" data-testid="voice-dictation-download">
            {downloadLabel(download)}
          </div>
        )}
      </div>
    </motion.div>
  )
}
