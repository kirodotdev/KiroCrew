/**
 * Isolated capture entry for the hold-to-talk gesture (PR #5700).
 *
 * WHY ISOLATED: the gesture is a pointer state machine over REAL layout — the
 * cancel zone is measured in CSS px from where the finger landed, and the hold
 * target only exists at a phone width. happy-dom computes no layout, so the unit
 * suite (src/hooks/useTouchPushToTalk.test.ts) can pin the state machine's
 * transitions but not that the cue sits above the thumb or that the bar occupies
 * the composer's own box. That is what this entry exercises.
 *
 * WHAT IS REAL — everything under review:
 *   - the real `ChatInput`, at a real 390px viewport, with production classes
 *   - the real `useTouchPushToTalk` (reached through ChatInput, not called here)
 *   - the real `useVoiceInput`: real `getUserMedia`, real `MediaRecorder`, real
 *     level meter and Strands sample ref
 *   - the real prop wiring, mirroring what ChatPage passes
 *
 * WHAT IS STOOD IN — one HTTP boundary, nothing else:
 *   - `POST /api/stt/transcribe` is intercepted by the driving script
 *     (scripts/capture-hold-to-talk.mjs) so no gateway or speech backend is
 *     needed. The audio really is captured and really is posted; only the
 *     response is synthesised. Chromium supplies the microphone via
 *     --use-fake-device-for-media-stream.
 *
 * The gateway is deliberately absent rather than mocked: ChatPage's STT
 * enablement gate is a precondition of this feature, not part of it, so it is
 * supplied directly as props here instead of being faked at the network layer.
 *
 * Query string: ?theme=dark|light
 */
import { useCallback, useState } from 'react'
import { createRoot } from 'react-dom/client'
import { Provider } from 'react-redux'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'

import ChatInput from '../src/components/ChatInput'
import { useVoiceInput } from '../src/hooks/useVoiceInput'
import { initI18n } from '../src/i18n'
import { store } from '../src/store'
import '../src/index.css'

const params = new URLSearchParams(location.search)
const theme = params.get('theme') || 'dark'

document.documentElement.setAttribute('data-theme', theme === 'light' ? 'kiro-light' : 'kiro-dark')

/** Transcript lands in the composer exactly as ChatPage's handler puts it there. */
function Harness() {
  const [value, setValue] = useState('')
  const onText = useCallback((text: string) => {
    setValue(prev => (prev ? `${prev} ${text}` : text))
  }, [])
  const voice = useVoiceInput(onText)

  return (
    <div className="flex flex-col justify-end h-screen bg-bg text-text">
      {/* Enough transcript above the composer that the capture shows the hold bar
          in context rather than floating on an empty page. */}
      <div className="flex flex-col gap-2 px-3 pb-3 overflow-hidden" data-transcript>
        <div className="self-start max-w-[80%] rounded-xl bg-card text-card-fg px-3 py-2 text-[13px]">
          Did the safe-area PR go green?
        </div>
        <div className="self-end max-w-[80%] rounded-xl bg-accent-subtle px-3 py-2 text-[13px]">
          All 66 checks pass and every review lane is clear.
        </div>
        <div className="self-start max-w-[80%] rounded-xl bg-card text-card-fg px-3 py-2 text-[13px]">
          Then arm auto-merge on it.
        </div>
      </div>
      <ChatInput
        value={value}
        onChange={setValue}
        onSend={() => setValue('')}
        connected
        voiceRecording={voice.recording}
        voiceCaptureActive={voice.recording}
        voiceTranscribing={voice.transcribing}
        voiceError={voice.error}
        voiceLevel={voice.level}
        voiceDeviceLabel={voice.deviceLabel}
        voiceDeviceId={voice.deviceId}
        voicePartial={voice.partial}
        voiceSampleRef={voice.sampleRef}
        voiceStreaming={voice.streamEnabled}
        voiceDictationPanel
        onSelectVoiceDevice={voice.switchDevice}
        onClearVoiceError={voice.clearError}
        onVoiceToggle={voice.toggle}
        onVoiceCancel={voice.cancel}
        onVoicePrewarm={voice.prewarm}
        onVoiceStart={voice.start}
        onVoiceStop={voice.stop}
      />
    </div>
  )
}

initI18n('en')
/** Retries off: a capture must not sit waiting on a gateway that is not there. */
const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } })
createRoot(document.getElementById('root')!).render(
  <QueryClientProvider client={queryClient}>
    <Provider store={store}>
      <Harness />
    </Provider>
  </QueryClientProvider>,
)
