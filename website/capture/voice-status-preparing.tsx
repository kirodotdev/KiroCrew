/**
 * Isolated capture entry for the speech-model wait on the recording strip.
 *
 * Mounts the REAL VoiceStatusBar against the real stylesheet, theme tokens and
 * live i18n catalog. Nothing is stubbed: the strip is a pure function of its
 * props, so the scene IS the prop set, and a scene that renders here renders
 * the same way in the composer.
 *
 * Scene from the query string:
 *   ?scene=recording-preparing|released-preparing|released-downloading
 *          |released-expired|audio-module-failed|audio-start-failed
 *   &theme=dark|light&lang=en|zh-CN&width=<composer px, default 760>
 *
 * `released-*` is the scene the mic no longer holds: capture has ended, the
 * retained audio is waiting on the model, and the strip is the only thing on
 * screen that says so. `audio-*-failed` are the two audio start-up failures,
 * each with its own message, carrying the dismiss control the composer gives
 * every error on this strip.
 */
import { createRoot } from 'react-dom/client'

import VoiceStatusBar from '../src/components/VoiceStatusBar'
import { initI18n } from '../src/i18n/all'
import { i18nT } from '../src/i18n/t'
import type { SttModelProgress } from '../src/lib/sttProviders'
import '../src/index.css'

const params = new URLSearchParams(location.search)
const theme = params.get('theme') || 'dark'
const scene = params.get('scene') || 'released-preparing'
const lang = params.get('lang') || 'en'
const width = Number(params.get('width')) || 760

document.documentElement.setAttribute('data-theme', theme === 'light' ? 'kiro-light' : 'kiro-dark')

initI18n(lang)

const preparing: SttModelProgress = { done: 0, total: 0, stage: 'preparing' }
// Mid-transfer, so the percentage and both byte figures are all non-trivial.
const downloading: SttModelProgress = { done: 612_000_000, total: 1_540_000_000, stage: 'downloading' }

const scenes: Record<string, { recording: boolean; level: number; download: SttModelProgress | null; error?: string }> = {
  'recording-preparing': { recording: true, level: 0.42, download: preparing },
  'released-preparing': { recording: false, level: 0, download: preparing },
  'released-downloading': { recording: false, level: 0, download: downloading },
  // Where the wait runs out: `cleanup()` has discarded the retained audio and
  // retired the progress line, so the strip carries the expiry message alone.
  'released-expired': {
    recording: false,
    level: 0,
    download: null,
    error: i18nT('hooks.useStreamingStt.stt_model_still_loading'),
  },
  // The capture module failed to download, so the start ends before capture.
  'audio-module-failed': {
    recording: false,
    level: 0,
    download: null,
    error: i18nT('hooks.useStreamingStt.audio_module_load_failed'),
  },
  // The module loaded and the browser refused to start the audio context.
  'audio-start-failed': {
    recording: false,
    level: 0,
    download: null,
    error: i18nT('hooks.useStreamingStt.audio_start_failed'),
  },
}
const chosen = scenes[scene] || scenes['released-preparing']

createRoot(document.getElementById('root')!).render(
  // Composer width, and a body below the strip, because the thing under review
  // is whether this line is visible and legible where it actually sits rather
  // than how it looks alone on a page.
  <div className="min-h-screen bg-canvas p-4">
    <div className="mx-auto rounded-lg border border-border overflow-hidden bg-chrome" style={{ width }}>
      <VoiceStatusBar
        recording={chosen.recording}
        level={chosen.level}
        deviceLabel="MacBook Pro Microphone"
        deviceId="default"
        download={chosen.download}
        error={chosen.error}
        onDismissError={chosen.error ? () => {} : undefined}
        onSelectDevice={() => {}}
      />
      <div className="px-3 py-6 text-[13px] text-muted">
        {/* The composer's OWN placeholder for each state, localised, rather than
            a line written for this harness: a blind reviewer reads whatever is
            on the page, so invented copy here is copy that gets reviewed and
            never shipped.

            Every non-recording scene shows the DEFAULT prompt, which is what the
            composer shows in each of them. While a model is fetching or loading
            it withholds "Transcribing, please wait" -- the strip above names the
            stage and nothing is being transcribed yet -- and on expiry
            `cleanup()` clears `draining` as the error lands. */}
        {chosen.recording
          ? i18nT('components.chatInput.recording_click_mic_to_stop')
          : i18nT('components.chatInput.message_placeholder', { bot: 'Kiro' })}
      </div>
    </div>
  </div>,
)
