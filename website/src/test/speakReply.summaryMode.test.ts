/**
 * Read aloud under `speakMode: 'summary'` (`pages/chat/speakReply.ts`): the
 * gateway is asked what to speak first, the start event still fires inside the
 * click so the audio context unlocks, a failed summary speaks the reply itself,
 * and a Stop during the wait keeps the late answer silent.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { api } from '../api/client'
import { store } from '../store'
import { setVoicePlaying, setVoicePreparing } from '../store/chatSlice'
import { setVoiceSpeakMode } from '../lib/voiceSpeakMode'
import { speakReply } from '../pages/chat/speakReply'

const flush = async () => { await Promise.resolve(); await Promise.resolve(); await Promise.resolve() }

describe('speakReply in summary mode', () => {
  const dispatch = vi.fn()
  let synthesize: ReturnType<typeof vi.spyOn>
  let summarize: ReturnType<typeof vi.spyOn>
  let started: Array<{ request_id: string; text?: string }>

  beforeEach(() => {
    store.dispatch(setVoicePlaying(false))
    store.dispatch(setVoicePreparing(false))
    setVoiceSpeakMode('summary')
    started = []
    window.addEventListener('voice-synthesis-start', onStart)
    synthesize = vi.spyOn(api, 'voiceSynthesize').mockResolvedValue({ ok: true })
    summarize = vi.spyOn(api, 'voiceSummarize').mockResolvedValue({ ok: true, text: 'The gist.', summarized: true })
  })
  afterEach(() => {
    window.removeEventListener('voice-synthesis-start', onStart)
    setVoiceSpeakMode('full')
    vi.restoreAllMocks()
  })
  function onStart(e: Event) {
    started.push((e as CustomEvent).detail)
    // The playback owner marks the request as preparing when it hears the start.
    store.dispatch(setVoicePreparing(true))
  }

  it('announces the request in the click, then speaks the summary under the same id', async () => {
    speakReply(dispatch, 'slot-1', 'A long reply with many details in it.')
    // Synchronous: the start event is what unlocks audio inside the gesture.
    expect(started).toHaveLength(1)
    expect(started[0].text).toBe('A long reply with many details in it.')
    expect(synthesize).not.toHaveBeenCalled()
    await flush()
    expect(summarize).toHaveBeenCalledWith('A long reply with many details in it.', started[0].request_id)
    expect(synthesize).toHaveBeenCalledWith('slot-1', 'The gist.', { request_id: started[0].request_id, clientFallback: true })
  })

  it('speaks the reply itself when the summary request fails', async () => {
    summarize.mockRejectedValue(new Error('gateway busy'))
    speakReply(dispatch, 'slot-1', 'The whole reply.')
    await flush()
    expect(synthesize).toHaveBeenCalledWith('slot-1', 'The whole reply.', expect.objectContaining({ clientFallback: true }))
  })

  it('speaks the reply itself when the gateway echoes it unsummarized', async () => {
    summarize.mockResolvedValue({ ok: true, text: 'The whole reply.', summarized: false })
    speakReply(dispatch, 'slot-1', 'The whole reply.')
    await flush()
    expect(synthesize).toHaveBeenCalledWith('slot-1', 'The whole reply.', expect.objectContaining({ clientFallback: true }))
  })

  it('stays silent when Stop ended the wait before the summary arrived', async () => {
    let answer!: (value: { ok: boolean; text: string; summarized: boolean }) => void
    summarize.mockReturnValue(new Promise(resolve => { answer = resolve }))
    speakReply(dispatch, 'slot-1', 'A long reply.')
    expect(store.getState().chat.voicePreparing).toBe(true)
    // Stop: the owner drops the request and ends "preparing".
    store.dispatch(setVoicePreparing(false))
    answer({ ok: true, text: 'Too late.', summarized: true })
    await flush()
    expect(synthesize).not.toHaveBeenCalled()
  })

  it('lets a newer Read aloud win: the replaced request speaks nothing when its summary lands late', async () => {
    let answerA!: (value: { ok: boolean; text: string; summarized: boolean }) => void
    summarize
      .mockReturnValueOnce(new Promise(resolve => { answerA = resolve }))
      .mockResolvedValueOnce({ ok: true, text: 'Gist of B.', summarized: true })
    speakReply(dispatch, 'slot-1', 'Reply A.')
    // The second pick replaces the first; "preparing" is on for B, not A.
    speakReply(dispatch, 'slot-1', 'Reply B.')
    await flush()
    expect(synthesize).toHaveBeenCalledTimes(1)
    expect(synthesize).toHaveBeenCalledWith('slot-1', 'Gist of B.', { request_id: started[1].request_id, clientFallback: true })
    answerA({ ok: true, text: 'Gist of A, too late.', summarized: true })
    await flush()
    expect(synthesize).toHaveBeenCalledTimes(1)
  })

  it('leaves the full-mode path untouched', async () => {
    setVoiceSpeakMode('full')
    speakReply(dispatch, 'slot-1', 'Read me as written.')
    expect(synthesize).toHaveBeenCalledWith('slot-1', 'Read me as written.', { clientFallback: true })
    await flush()
    expect(summarize).not.toHaveBeenCalled()
  })
})
