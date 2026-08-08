import { createContext, useCallback, useContext, useMemo, useRef, type ReactNode } from 'react'
import { useQuery } from '@tanstack/react-query'
import { useVoiceInput } from '../hooks/useVoiceInput'
import { createAudioSample } from '../hooks/mic'
import { useAppSelector } from '../store'
import { api } from '../api/client'
import { loadDrafts, saveDrafts, setDraft } from '../utils/chatDrafts'

/**
 * VoiceSessionProvider — owns the ONE `useVoiceInput` session ABOVE the router.
 *
 * Why it lives here and not in ChatPage: each top-level view is its own route
 * (`/chat`, `/schedule`, `/artifacts`, …), so navigating away UNMOUNTS ChatPage.
 * When the voice hook lived inside ChatPage, that unmount tore down the recorder
 * and orphaned any in-flight transcription — the user saw "transcribing"
 * silently cancel and the words vanish. Hoisting the session above `<Routes>`
 * means a route change no longer destroys it: the in-flight transcription and
 * its "transcribing" state survive, so when the user returns to /chat the
 * spinner + transcript are still there. An ACTIVE recording is a different
 * matter — its meter and stop control unmount with ChatPage, so ChatPage stops
 * a hot mic on unmount (it must not keep capturing off-route); that stop still
 * transcribes and the words land in the originating slot's draft.
 *
 * ChatPage still owns everything about the LIVE composer (caret-splice, frozen
 * snapshots, per-slot draft routing). It registers those as a "sink" via
 * `registerVoiceSink` while mounted. When NO sink is mounted (the user is on
 * Schedule/Artifacts/etc.), the final transcript falls back to the originating
 * slot's PERSISTED draft — see `deliverToDraft` — so it is never lost.
 */

export interface VoiceSink {
  /** Deliver a final transcript to the live composer (splice / per-slot route). */
  onText?: (text: string, sessionId: string | null) => void
  /** Live streaming hypothesis for the on-screen composer. */
  onPartial?: (text: string, sessionId: string | null) => void
  /** Semantic-endpointing verdict: auto-submit the composer. */
  onEndpoint?: () => void
}

type VoiceHook = ReturnType<typeof useVoiceInput>

export interface VoiceSessionContextValue extends VoiceHook {
  /** Install the live-composer sink; returns an unregister fn for unmount. */
  registerVoiceSink: (sink: VoiceSink) => () => void
}

const VoiceSessionContext = createContext<VoiceSessionContextValue | null>(null)

/**
 * Append a transcript to the originating slot's persisted draft when no live
 * composer sink is mounted. The draft store is module-level + localStorage
 * backed, so it survives ChatPage unmount; the remounted ChatPage picks the text
 * up via `loadDrafts()`. Reached only for a final that resolves with no on-screen
 * composer — a batch capture that finished off-route (ChatPage stops the mic on
 * unmount) or a stop/config race. A streaming session is cancelled on nav-away
 * (its hypothesis is already in the draft), so it never drains a duplicate final
 * into here.
 */
function deliverToDraft(sessionId: string | null, text: string): void {
  if (!sessionId || !text) return
  const drafts = loadDrafts()
  const base = drafts[sessionId] ?? ''
  const next = base ? (base.endsWith(' ') ? base + text : base + ' ' + text) : text
  setDraft(drafts, sessionId, next)
  saveDrafts(drafts)
}

export function VoiceSessionProvider({ children }: { children: ReactNode }) {
  const activeSlot = useAppSelector(s => s.chat.activeSlot)
  const { data: sttCfg } = useQuery({
    queryKey: ['sttConfig'],
    queryFn: () => api.sttConfig() as Promise<{ streaming?: boolean }>,
  })
  const streaming = !!sttCfg?.streaming

  // The live composer sink installed by whichever ChatPage is currently mounted.
  // Null when no chat surface is on screen. A ref (not state) so the hook's
  // stable callbacks always read the CURRENT sink without re-creating the hook.
  const sinkRef = useRef<VoiceSink | null>(null)
  const registerVoiceSink = useCallback((sink: VoiceSink) => {
    sinkRef.current = sink
    return () => { if (sinkRef.current === sink) sinkRef.current = null }
  }, [])

  const onText = useCallback((text: string, sessionId: string | null) => {
    const sink = sinkRef.current
    if (sink?.onText) { sink.onText(text, sessionId); return }
    // No live composer (chat route unmounted). Persist EVERY sinkless final to
    // the originating slot's draft so the words survive the navigation,
    // regardless of the current STT mode. Keying the drop on the live streaming
    // flag would silently discard an in-flight BATCH result if the user flipped
    // streaming on mid-transcription. A streaming session is cancelled on
    // nav-away (its hypothesis is already in the draft), so no duplicate
    // streaming final ever reaches here.
    deliverToDraft(sessionId, text)
  }, [])
  const onPartial = useCallback((text: string, sessionId: string | null) => {
    sinkRef.current?.onPartial?.(text, sessionId)
  }, [])
  const onEndpoint = useCallback(() => {
    sinkRef.current?.onEndpoint?.()
  }, [])

  const voice = useVoiceInput(onText, { streaming, sessionId: activeSlot, onPartial, onEndpoint })

  const value = useMemo<VoiceSessionContextValue>(
    () => ({ ...voice, registerVoiceSink }),
    [voice, registerVoiceSink],
  )
  return <VoiceSessionContext.Provider value={value}>{children}</VoiceSessionContext.Provider>
}

/**
 * Inert "voice unavailable" session used as the fallback when `useVoiceSession`
 * is read outside a provider. Degrading gracefully (mic controls simply do
 * nothing) is preferable to throwing, which would white-screen the whole view.
 * In the real app main.tsx always mounts <VoiceSessionProvider> above <Routes>,
 * so this is only reached in isolation — e.g. a unit test that renders a chat
 * surface without wrapping it in the provider.
 */
const INERT_VOICE_SESSION: VoiceSessionContextValue = {
  recording: false,
  transcribing: false,
  sessionOwner: null,
  streamEnabled: false,
  error: null,
  level: 0,
  deviceLabel: '',
  partial: '',
  deviceSwitchIsLive: false,
  sampleRef: { current: createAudioSample() },
  toggle: () => {},
  cancel: () => {},
  prewarm: () => {},
  clearError: () => {},
  switchDevice: async () => {},
  registerVoiceSink: () => () => {},
}

/** Consume the hoisted voice session; inert fallback (see above) outside a provider. */
export function useVoiceSession(): VoiceSessionContextValue {
  return useContext(VoiceSessionContext) ?? INERT_VOICE_SESSION
}
