/** Streaming playback of the active slot's spoken reply.
 *
 *  Owns the synthesis request chain, the PCM player and the media-element
 *  queue, the per-message speech progress, the auto-speak preference, and the
 *  interruption rules; the socket routes the `voice_*` frames and the
 *  transcript's stream and turn boundaries here. */
import { useMemo, useRef } from 'react'
import { store, type AppDispatch } from '../../store'
import { setVoicePlaying, setVoiceAudio } from '../../store/chatSlice'
import { api } from '../../api/client'
import { VoicePcmPlayer, voiceBoundary, createVoiceRequestId } from '../../lib/voicePlayback'
import { reportVoiceFailure } from '../../lib/voiceFailure'
import type { ChatMessage } from '../../types'
import type { FrameData } from './frames'

type VoiceProgress = {
  slot: string
  messageId: string
  spokenLen: number
}

function voiceMessageId(message: ChatMessage): string {
  const clientId = message.meta?.clientTs
  if (typeof clientId === 'string' && clientId) return clientId
  if (message.ts) return message.ts
  const serverId = message.meta?.mid
  return typeof serverId === 'string' ? serverId : ''
}

export interface VoicePlayback {
  /** Audio frames have no reconnect replay: a lost socket ends the stream. */
  releaseVoiceOnSocketLoss(): void
  /** Re-read the auto-speak preference from the server. */
  refreshAutoSpeak(): void
  /** After a chunk flush landed text in the active slot: speak the sentences
   *  it completed. */
  speakStreamedDelta(activeSlot: string): void
  /** `chat_segment`: speak the rest of the streaming message. */
  speakSegmentTail(slot: string): void
  /** `chat_done`: speak the rest of the finished message. */
  speakTurnTail(slot: string): void
  /** `chat_done` while auto-speak is off or muted: the preference may have
   *  changed, so re-read it. */
  refreshAutoSpeakIfSilent(slot: string): void
  /** A new user turn or a new active slot starts a new playback context. */
  resetForNewTurn(): void
  onVoiceChunk(data: FrameData): void
  onVoiceComplete(data: FrameData): void
  onVoiceError(data: FrameData): void
  /** Listen for the page's voice events; returns the teardown. */
  attachWindowEvents(): () => void
  /** Unmount: stop playback and close the audio context. */
  dispose(): void
}

export function useVoicePlayback(dispatch: AppDispatch): VoicePlayback {
  const voiceQueueRef = useRef<string[]>([])
  const voicePlayingRef = useRef(false)
  const pcmPlayerRef = useRef<VoicePcmPlayer | null>(null)
  const voiceEpochRef = useRef(0)
  const voiceRequestsRef = useRef(new Map<string, string>())
  const pendingVoiceRef = useRef<{ slot: string; text: string; request_id: string } | null>(null)
  const activeAudioRef = useRef<HTMLAudioElement | null>(null)
  const autoSpeakRef = useRef(false)
  // Speech offsets belong to one concrete message. A segment for another slot
  // must not reset the active message, and a new post-tool segment must start
  // from zero even while the previous message remains in the transcript.
  const voiceProgressRef = useRef<VoiceProgress | null>(null)
  const voiceMutedRef = useRef(false)  // suppress incoming chunks after interrupt
  const synthChainRef = useRef<Promise<unknown>>(Promise.resolve())  // serialize TTS calls

  return useMemo<VoicePlayback>(() => {
    const stopVoice = () => {
      voiceMutedRef.current = true
      voiceEpochRef.current++
      for (const [requestId, slot] of voiceRequestsRef.current) {
        void api.voiceCancel?.(slot, requestId).catch(() => {})
      }
      voiceRequestsRef.current.clear()
      pendingVoiceRef.current = null
      synthChainRef.current = Promise.resolve()
      pcmPlayerRef.current?.stop()
      if (activeAudioRef.current) {
        activeAudioRef.current.onended = null
        activeAudioRef.current.onerror = null
        activeAudioRef.current.pause()
        URL.revokeObjectURL(activeAudioRef.current.src)
        activeAudioRef.current = null
      }
      voiceQueueRef.current.forEach(u => URL.revokeObjectURL(u))
      voiceQueueRef.current = []
      voicePlayingRef.current = false
      dispatch(setVoicePlaying(false))
    }

    const releaseVoiceOnSocketLoss = () => {
      if (!voiceRequestsRef.current.size && !voicePlayingRef.current && !store.getState().chat.voicePlaying) return
      reportVoiceFailure({
        slot: store.getState().chat.activeSlot, code: 'voice_playback_failed',
      })
      // Audio frames have no reconnect replay, so release the incomplete stream.
      stopVoice()
    }

    const getPcmPlayer = () => {
      pcmPlayerRef.current ??= new VoicePcmPlayer(
        playing => dispatch(setVoicePlaying(playing)),
        code => {
          reportVoiceFailure({ slot: store.getState().chat.activeSlot, code })
          // A playback failure affects the stream, not just one 200 ms chunk.
          // Cancel synthesis so later chunks cannot repeatedly raise the same error.
          stopVoice()
        },
      )
      return pcmPlayerRef.current
    }

    const voiceProgressFor = (slot: string, message: ChatMessage): VoiceProgress | null => {
      const messageId = voiceMessageId(message)
      if (!messageId) return null
      const current = voiceProgressRef.current
      if (!current || current.slot !== slot || current.messageId !== messageId) {
        const next = { slot, messageId, spokenLen: 0 }
        voiceProgressRef.current = next
        return next
      }
      return current
    }

    const enqueueVoiceSynthesis = (slot: string, text: string) => {
      // The first sentence starts immediately. While it synthesizes, combine
      // completed sentences into the next request to amortize local model startup.
      const pending = pendingVoiceRef.current
      if (pending?.slot === slot && pending.text.length + text.length < 4000) {
        pending.text += '\n' + text
        return
      }
      const epoch = voiceEpochRef.current
      const request_id = createVoiceRequestId()
      const request = { slot, text, request_id }
      pendingVoiceRef.current = request
      voiceRequestsRef.current.set(request_id, slot)
      synthChainRef.current = synthChainRef.current.then(async () => {
        if (pendingVoiceRef.current === request) pendingVoiceRef.current = null
        if (epoch !== voiceEpochRef.current || voiceMutedRef.current) return
        try {
          await api.voiceSynthesize(slot, request.text, { request_id })
        } catch {
          if (!voiceRequestsRef.current.delete(request_id)) return
          if (epoch === voiceEpochRef.current && slot === store.getState().chat.activeSlot) {
            reportVoiceFailure({ slot, request_id, code: 'voice_synthesis_failed' })
          }
        }
      })
    }

    const flushVoiceTail = (slot: string, message: ChatMessage) => {
      const progress = voiceProgressFor(slot, message)
      if (!progress) return
      const remaining = message.content.slice(progress.spokenLen).trim()
      // Mark the whole message consumed so a later completion event cannot
      // reconsider or repeat a tail already queued for speech.
      progress.spokenLen = message.content.length
      if (remaining) enqueueVoiceSynthesis(slot, remaining)
    }

    const playNextVoiceChunk = () => {
      if (voicePlayingRef.current || voiceQueueRef.current.length === 0) return
      voicePlayingRef.current = true
      const url = voiceQueueRef.current.shift()!
      const audio = new Audio(url)
      activeAudioRef.current = audio
      const finished = () => {
        URL.revokeObjectURL(url)
        if (activeAudioRef.current !== audio) return
        activeAudioRef.current = null
        voicePlayingRef.current = false
        audio.onended = null
        audio.onerror = null
        if (voiceQueueRef.current.length) playNextVoiceChunk()
        else dispatch(setVoicePlaying(false))
      }
      audio.onended = finished
      audio.onerror = () => {
        if (activeAudioRef.current !== audio) return
        reportVoiceFailure({ slot: store.getState().chat.activeSlot, code: 'voice_playback_failed' })
        stopVoice()
      }
      audio.play().catch(error => {
        if (activeAudioRef.current !== audio) return
        reportVoiceFailure({
          slot: store.getState().chat.activeSlot,
          code: error?.name === 'NotAllowedError' ? 'voice_playback_blocked' : 'voice_playback_failed',
        })
        stopVoice()
      })
    }

    const refreshAutoSpeak = () => {
      api.voiceConfig().then(c => { autoSpeakRef.current = !!c.autoSpeak }).catch(() => {})
    }

    return {
      releaseVoiceOnSocketLoss,
      refreshAutoSpeak,
      speakStreamedDelta(activeSlot) {
        // Auto-speak the active slot's newly-streamed sentences once per flush,
        // after the batched content has landed in the store, so the scan reads
        // the post-dispatch streaming content.
        if (!autoSpeakRef.current) return
        const msgs = store.getState().chat.messages
        const streaming = [...msgs].reverse().find(m => m.role === 'streaming')
        if (streaming) {
          const progress = voiceProgressFor(activeSlot, streaming)
          if (!progress) return
          if (voiceMutedRef.current) return
          const full = streaming.content
          const lastBound = voiceBoundary(full, progress.spokenLen)
          if (lastBound > progress.spokenLen) {
            const newText = full.slice(progress.spokenLen, lastBound).trim()
            progress.spokenLen = lastBound
            if (newText) enqueueVoiceSynthesis(activeSlot, newText)
          }
        }
      },
      speakSegmentTail(slot) {
        if (autoSpeakRef.current && !voiceMutedRef.current && slot === store.getState().chat.activeSlot) {
          const streaming = [...store.getState().chat.messages].reverse().find(m => m.role === 'streaming')
          if (streaming) flushVoiceTail(slot, streaming)
        }
      },
      speakTurnTail(slot) {
        // Consume the tail while the streaming row still carries the same
        // identity used by sentence-boundary progress tracking.
        if (autoSpeakRef.current && !voiceMutedRef.current && slot === store.getState().chat.activeSlot) {
          const msgs = store.getState().chat.messages
          const last = [...msgs].reverse().find(m => m.role === 'streaming')
            ?? [...msgs].reverse().find(m => m.role === 'assistant')
          if (last) flushVoiceTail(slot, last)
        }
      },
      refreshAutoSpeakIfSilent(slot) {
        if ((!autoSpeakRef.current || voiceMutedRef.current) && slot === store.getState().chat.activeSlot) {
          // Re-check config in case it changed
          refreshAutoSpeak()
        }
      },
      resetForNewTurn() {
        stopVoice()
        voiceMutedRef.current = false
        voiceProgressRef.current = null
      },
      onVoiceChunk(data) {
        if (voiceMutedRef.current || data.slot !== store.getState().chat.activeSlot) return
        const { audio: b64, audioMime, request_id } = data as { audio: string; audioMime?: string; request_id?: string }
        if (!request_id || voiceRequestsRef.current.get(request_id) !== data.slot) return
        if (b64) {
          try {
            const bytes = Uint8Array.from(atob(b64), c => c.charCodeAt(0))
            if (audioMime === 'audio/wav' && typeof AudioContext !== 'undefined') {
              getPcmPlayer().enqueue(bytes.buffer)
            } else {
              const blob = new Blob([bytes], { type: audioMime === 'audio/wav' ? 'audio/wav' : 'audio/mpeg' })
              const url = URL.createObjectURL(blob)
              voiceQueueRef.current.push(url)
              dispatch(setVoicePlaying(true))
              playNextVoiceChunk()
            }
          } catch {
            reportVoiceFailure({ slot: data.slot, request_id, code: 'voice_playback_failed' })
          }
        }
      },
      onVoiceComplete(data) {
        const { audio: b64, request_id } = data as { audio?: string; request_id?: string }
        if (voiceMutedRef.current || data.slot !== store.getState().chat.activeSlot) return
        if (!request_id || !voiceRequestsRef.current.delete(request_id)) return
        if (b64) dispatch(setVoiceAudio(b64))
      },
      onVoiceError(data) {
        const { request_id, code } = data as { request_id?: string; code?: string }
        if (!request_id || !voiceRequestsRef.current.delete(request_id)) return
        if (voiceMutedRef.current || data.slot !== store.getState().chat.activeSlot || code === 'voice_cancelled') return
        reportVoiceFailure({ slot: data.slot, request_id, code: code || 'voice_synthesis_failed' })
      },
      attachWindowEvents() {
        const onVoiceStop = () => {
          stopVoice()
          if (autoSpeakRef.current && typeof AudioContext !== 'undefined') getPcmPlayer().unlock()
        }
        const onVoiceStart = (event: Event) => {
          const { slot, request_id } = (event as CustomEvent<{ slot: string; request_id: string }>).detail
          voiceRequestsRef.current.set(request_id, slot)
          if (slot === store.getState().chat.activeSlot) {
            voiceMutedRef.current = false
            if (typeof AudioContext !== 'undefined') getPcmPlayer().unlock()
          }
        }
        const onVoiceFailed = (event: Event) => {
          const detail = (event as CustomEvent<{ slot: string; request_id: string; code: string }>).detail
          if (!voiceRequestsRef.current.delete(detail.request_id)) return
          if (!voiceMutedRef.current && detail.slot === store.getState().chat.activeSlot) {
            reportVoiceFailure(detail)
          }
        }
        const onVoiceConfigChanged = (e: Event) => {
          const detail = (e as CustomEvent).detail
          autoSpeakRef.current = !!detail?.autoSpeak
          if (!autoSpeakRef.current) stopVoice()
        }
        window.addEventListener('voice-stop', onVoiceStop)
        window.addEventListener('voice-synthesis-start', onVoiceStart)
        window.addEventListener('voice-synthesis-failed', onVoiceFailed)
        window.addEventListener('voice-config-changed', onVoiceConfigChanged)
        return () => {
          window.removeEventListener('voice-stop', onVoiceStop)
          window.removeEventListener('voice-synthesis-start', onVoiceStart)
          window.removeEventListener('voice-synthesis-failed', onVoiceFailed)
          window.removeEventListener('voice-config-changed', onVoiceConfigChanged)
        }
      },
      dispose() {
        stopVoice()
        pcmPlayerRef.current?.close()
        pcmPlayerRef.current = null
      },
    }
  }, [dispatch])
}
