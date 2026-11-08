import { useCallback, useContext, useEffect, useId, useLayoutEffect, useReducer, useRef, useState, useSyncExternalStore } from 'react'
import { QueryClientContext, useQuery } from '@tanstack/react-query'
import { Loader2, Mic, Square } from 'lucide-react'
import { api } from '../api/client'
import { useVoiceInput, voiceInputSupported } from '../hooks/useVoiceInput'
import { subscribeTranscripts } from '../hooks/voiceTranscriptInbox'
import { readMicOwner, setMicOwner, sttCanStream, subscribeMic, type SttConfig } from '../chat-core/composer/useComposerVoice'
import { i18nT } from '../i18n/t'

/**
 * Dictate into a comment box: press to record, press again to stop, and the
 * words are ADDED to the box for the person to read before they send. Nothing
 * is posted by this button.
 *
 * It rides the chat composer's engine (`useVoiceInput`) and its one-mic lock,
 * so a comment dictation never runs beside a chat dictation. Each mount speaks
 * for its own session id, `comment-voice:<id>`, so a transcript reaches only the
 * box that recorded it and never a chat draft. A host keys the button by what
 * the comment is about (thread, passage), so a new target is a new session and
 * an old dictation can never land in it.
 */
const SLOT_PREFIX = 'comment-voice:'

// A comment transcript can settle after its button unmounted (stop, then close
// or change thread before the transcriber answers). This sink owns every
// comment session, so that text is dropped instead of handed to the chat page
// as a draft for a chat that does not exist. It is `exclusive`: a comment
// failure reaches only comment owners, never a chat composer's notice. A
// mounted button subscribes later and so wins the inbox's last-subscribed tie.
subscribeTranscripts({ begin: () => {}, settle: () => {}, owns: s => !!s?.startsWith(SLOT_PREFIX), exclusive: true })

interface Props {
  onText: (text: string) => void
  /** A dictation of this button ended in failure (a message), or a new one
   *  started (null). The host shows it with `ErrorNotice` beside its box. */
  onError?: (message: string | null) => void
  disabled?: boolean
  className?: string
}

/** No button off a QueryClient (a bare embed or test): the voice settings
 *  cannot be read there, and a mic that cannot tell whether voice is on would
 *  record audio that is never transcribed. */
export function CommentVoiceButton(props: Props) {
  return useContext(QueryClientContext) != null ? <CommentVoiceButtonInner {...props} /> : null
}

function CommentVoiceButtonInner({ onText, onError, disabled, className = '' }: Props) {
  const id = useId()
  const slot = SLOT_PREFIX + id
  // While the settings load, the button stays hidden. A failed read hides it
  // too, and says so once through the host's notice.
  const { data: cfg, isError: cfgFailed } = useQuery({
    queryKey: ['sttConfig'],
    queryFn: () => api.sttConfig() as Promise<SttConfig>,
  })
  const onTextRef = useRef(onText)
  onTextRef.current = onText
  const onErrorRef = useRef(onError)
  onErrorRef.current = onError
  useEffect(() => {
    if (cfgFailed && !cfg) onErrorRef.current?.(i18nT('components.commentVoiceButton.settings_unavailable'))
  }, [cfgFailed, cfg])
  // A streaming final can arrive after unmount; it belongs to a box that is gone.
  const aliveRef = useRef(true)
  const deliver = useCallback((text: string, sid: string | null) => {
    if (sid === slot && aliveRef.current) onTextRef.current(text)
  }, [slot])
  const owns = useCallback((sid: string | null) => sid === slot, [slot])
  const voice = useVoiceInput(deliver, { streaming: sttCanStream(cfg), sessionId: slot, ownsSession: owns, acceptsUnowned: false })

  const owner = useSyncExternalStore(subscribeMic, readMicOwner, readMicOwner)
  const startingRef = useRef(false)
  const [tick, rerender] = useReducer((n: number) => n + 1, 0)
  // From this button's press until its dictation is over. Only then is the
  // engine's error this button's: the inbox also broadcasts chat failures.
  const [mine, setMine] = useState(false)
  const { recording, transcribing, sessionOwner, error, clearError } = voice
  useEffect(() => {
    // Let go of the lock once this button's capture and transcription are over.
    if (readMicOwner() === id && !startingRef.current && !recording && !transcribing) setMicOwner(null)
    if (!mine || startingRef.current || recording || sessionOwner === slot) return
    setMine(false)
    if (error) {
      onErrorRef.current?.(error)
      // The comment box offers no Retry, so the failed audio is let go too.
      clearError()
    }
  }, [tick, mine, id, slot, recording, transcribing, sessionOwner, error, clearError])
  // Closing the box (or moving it to another target) discards a dictation
  // that is starting, recording or still streaming its final. A layout cleanup,
  // so it runs before the engine's own unmount, which would otherwise finish
  // the startup or transcribe the audio for a box that is gone.
  const live = useRef({ cancel: voice.cancel, active: false })
  live.current = { cancel: voice.cancel, active: voice.recording || voice.drainCancellable }
  useLayoutEffect(() => {
    // Set again on every mount: StrictMode runs the cleanup once before it.
    aliveRef.current = true
    return () => {
      aliveRef.current = false
      if (startingRef.current || live.current.active) live.current.cancel()
      if (readMicOwner() === id) setMicOwner(null)
    }
  }, [id])

  const ready = voiceInputSupported && !!cfg?.enabled && cfg?.available !== false
  if (!ready) return null

  const ownBusy = voice.transcribing && voice.sessionOwner === slot
  const busyElsewhere = !voice.recording && ((owner !== null && owner !== id) || (voice.transcribing && !ownBusy))

  const toggle = () => {
    if (voice.recording) { voice.stop(); return }
    if (disabled || busyElsewhere || ownBusy || startingRef.current) return
    onErrorRef.current?.(null)
    setMine(true)
    setMicOwner(id, null)
    startingRef.current = true
    void Promise.resolve(voice.start()).finally(() => { startingRef.current = false; rerender() })
  }

  const label = voice.recording
    ? i18nT('components.commentVoiceButton.stop_dictation')
    : ownBusy
      ? i18nT('components.commentVoiceButton.transcribing')
      : busyElsewhere
        ? i18nT('components.commentVoiceButton.mic_in_use')
        : i18nT('components.commentVoiceButton.dictate_comment')
  const tone = voice.recording ? 'text-accent bg-bg-hover' : 'text-muted bg-transparent hover:text-accent hover:bg-bg-hover'

  return (
    <button
      type="button"
      data-testid="comment-voice"
      // Keep the caret in the comment box, like the box's other buttons.
      onMouseDown={e => e.preventDefault()}
      onClick={toggle}
      disabled={!voice.recording && (disabled || busyElsewhere || ownBusy)}
      aria-pressed={voice.recording}
      aria-label={label}
      title={label}
      className={`flex items-center justify-center w-7 h-7 rounded-md transition-colors cursor-pointer border-none disabled:opacity-40 disabled:cursor-default ${tone} ${className}`}
    >
      {ownBusy
        ? <Loader2 size={14} className="animate-spin" />
        : voice.recording
          ? <Square size={11} fill="currentColor" className="animate-pulse" />
          : <Mic size={14} />}
    </button>
  )
}
