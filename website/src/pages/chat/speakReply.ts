import { api } from '../../api/client'
import { store, type AppDispatch } from '../../store'
import { setVoiceAudio } from '../../store/chatSlice'

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
  // clientFallback: on a headless remote gateway that cannot synthesize
  // (voice_unavailable), speak the reply with the webview's own engine instead
  // of only showing an error notice. A working gateway never emits that code,
  // so this leaves the normal path untouched.
  api.voiceSynthesize(slot, content, { clientFallback: true }).catch(() => {})
}
