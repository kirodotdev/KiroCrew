/**
 * Speech in and out: STT config, status, model prepare, ffmpeg download,
 * prewarm, polish and transcribe, and TTS voice config, voice lists,
 * synthesis and cancel.
 */

import { createVoiceRequestId } from '../../lib/voicePlayback'
import { parseErrorCode } from '../../utils/errorReport'
import { ApiError } from '../apiError'
import type { ClientTransport } from './transport'

export function createVoiceEndpoints({ post, put, del, j, jfetch: fetch }: ClientTransport) {
  const speechToText = {
    // STT
    sttConfig: () => fetch('/api/config/stt').then(j),
    saveSttConfig: (body: {
      enabled?: boolean
      provider?: string
      model?: string
      streaming?: boolean
      silence_ms?: number
      partial_interval_ms?: number
      endpointing?: boolean
      dictation_panel?: boolean
      transcribe_region?: string
      transcribe_profile?: string
      transcribe_vocabulary?: string
      language_code?: string
      polish?: boolean
    }) => put('/api/config/stt', body).then(j),
    // Recogniser availability plus the model catalog and the progress of any
    // download in flight. Separate from `sttConfig` because it is POLLED while a
    // model is being fetched, and polling the config endpoint would re-read and
    // re-probe configuration several times a second.
    sttStatus: () => fetch('/api/stt/status').then(j),
    // The Amazon Transcribe custom vocabularies the configured profile and region
    // can use, for the vocabulary picker. `listed: false` with no names unless
    // `transcribe` is the provider and its AWS use is confirmed at call time;
    // echoes the profile and region it listed, so a reply about a target the user
    // has since changed can be told apart from one about the current target.
    sttVocabularies: () => fetch('/api/stt/vocabularies').then(j),
    // Fetch a model now, so the cost is paid at a moment the user chose rather
    // than in the middle of their first dictation. Returns as soon as the transfer
    // is under way; progress is read from `sttStatus`.
    sttPrepare: (model: string) => post('/api/stt/prepare', { model }).then(j),
    // Delete one downloaded model file. The gateway refuses (409, with a `code`)
    // the selected model, the one loaded in the recogniser, and one downloading.
    sttDeleteModel: (model: string) =>
      del(`/api/stt/models/${encodeURIComponent(model)}`).then(j) as Promise<{ model: string; removed: boolean }>,
    // Fetch the audio decoder (ffmpeg) into the gateway's digest-verified store,
    // for a source install whose OS ships no ffmpeg package. Same 202-then-poll
    // shape as `sttPrepare`; progress arrives on `sttStatus().ffmpeg.download`.
    sttFfmpegDownload: () => post('/api/stt/ffmpeg/download', {}).then(j),
    // Load the model and run one throwaway decode so the first real utterance does
    // not pay for the graph allocation. Fire-and-forget at every call site: a
    // failure only costs the latency it was meant to hide.
    sttPrewarm: () => post('/api/stt/prewarm', {}).then(j),
    // Hand a FINISHED transcript to a fast model for punctuation and spacing, and
    // get back both strings. Off unless `stt.polish` is on, and refused with 403
    // when it is off — the switch is the consent, so this is never called
    // speculatively. Returns `changed: false` (with `text === original`) whenever
    // the model declined or its reply failed the server's length guard, which the
    // caller treats as "keep what you have" rather than as a failure.
    sttPolish: (text: string) => post('/api/stt/polish', { text }).then(j) as Promise<{
      ok: boolean
      changed: boolean
      text: string
      original: string
    }>,
    sttTranscribe: (blob: Blob, ext = 'webm') => {
      const fd = new FormData()
      fd.append('audio', blob, `recording.${ext}`)
      return fetch('/api/stt/transcribe', { method: 'POST', body: fd }).then(j)
    },
  }

  const voiceSettings = {
    // Voice
    voiceConfig: () => fetch('/api/voice/config').then(j),
    updateVoiceConfig: (body: object) => put('/api/voice/config', body).then(j),
    voiceVoices: () => fetch('/api/voice/voices').then(j),
    voiceSystemVoices: () => fetch('/api/voice/system-voices').then(j),
  }

  const synthesis = {
    voiceSynthesize: (slot: string, text: string, opts?: { voice?: string; engine?: string; rate?: string; pitch?: string; request_id?: string }) => {
      const request_id = opts?.request_id || createVoiceRequestId()
      window.dispatchEvent(new CustomEvent('voice-synthesis-start', { detail: { slot, request_id } }))
      return post('/api/voice/synthesize', { slot, text, ...opts, request_id }).then(j).catch(error => {
        const code = error instanceof ApiError ? parseErrorCode(error.body) : undefined
        window.dispatchEvent(new CustomEvent('voice-synthesis-failed', { detail: { slot, request_id, code: code || 'voice_synthesis_failed' } }))
        throw error
      })
    },
    voiceCancel: (slot: string, request_id: string) =>
      post('/api/voice/cancel', { slot, request_id }).then(j),
  }

  return { speechToText, voiceSettings, synthesis }
}
