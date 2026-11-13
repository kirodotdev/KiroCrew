import { api } from '../../api/client'
import { store, type AppDispatch } from '../../store'
import { setVoiceAudio } from '../../store/chatSlice'
import { createVoiceRequestId } from '../../lib/voicePlayback'
import { getVoiceSpeakMode } from '../../lib/voiceSpeakMode'

/** The id of the newest Read aloud this module started, so an older request's
 *  late summary answer can tell it was replaced. */
let latestReadAloud = ''

/** The reply menu's Read aloud, for any chat that offers it (the Sessions chat
 *  and a crewmate's chat): stop the reading when audio plays, else read
 *  `content` aloud for `slot`. Playback accepts the audio only for the slot on
 *  screen (`lib/storeFocusedSlot.ts`), so a host passes its own slot. */
export function speakReply(dispatch: AppDispatch, slot: string, content: string): void {
  if (store.getState().chat.voicePlaying) {
    window.dispatchEvent(new Event('voice-stop'))
    dispatch(setVoiceAudio(null))
    return
  }
  dispatch(setVoiceAudio(null))
  // A synthesis still loading its model has no playing audio yet, but owns
  // the same output channel. Replace it before starting a manual request.
  window.dispatchEvent(new Event('voice-stop'))
  // Any new Read aloud replaces a summary still in flight, whatever its mode.
  latestReadAloud = ''
  // clientFallback: on a headless remote gateway that cannot synthesize
  // (voice_unavailable), speak the reply with the webview's own engine instead
  // of only showing an error notice. A working gateway never emits that code,
  // so this leaves the normal path untouched.
  if (getVoiceSpeakMode() !== 'summary') {
    api.voiceSynthesize(slot, content, { clientFallback: true }).catch(() => {})
    return
  }
  // Summary mode: ask the gateway what to speak first. The start event goes out
  // NOW, inside the click, so the playback owner unlocks its audio context in
  // the user's gesture and shows "preparing" while the summary is made; the
  // synthesis call below re-announces the same id with the text it speaks.
  // A failed summary request speaks the reply itself, never nothing.
  const request_id = createVoiceRequestId()
  latestReadAloud = request_id
  window.dispatchEvent(new CustomEvent('voice-synthesis-start', { detail: { slot, request_id, clientFallback: true, text: content } }))
  api.voiceSummarize(content, request_id)
    .then(r => (r && typeof r.text === 'string' && r.text.trim()) ? r.text : content)
    .catch(() => content)
    .then(spoken => {
      // Stop pressed, or another Read aloud picked, while the summary was in
      // flight: the owner dropped this id, so a late answer must not start
      // speaking. The id check covers the second case, where a newer request
      // has "preparing" on; the global flag alone would let this one through
      // to a 409 and a false failure notice.
      if (latestReadAloud !== request_id || !store.getState().chat.voicePreparing) return
      api.voiceSynthesize(slot, spoken, { request_id, clientFallback: true }).catch(() => {})
    })
}
