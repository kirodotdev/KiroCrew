/** Speak text on the CLIENT with the webview's own speech engine, as a fallback
 * for when the gateway host cannot synthesize at all.
 *
 * Every TTS provider synthesizes on the gateway host (macOS `say`, Linux
 * espeak-ng, Amazon Polly). A desktop app connected to a HEADLESS remote
 * gateway — a dev desktop over SSH — reaches none of them: the host has no
 * speech engine and Polly needs consent plus
 * credentials. The gateway reports that as `voice_unavailable`. The desktop
 * app's own machine does have a working engine (its webview's Web Speech API),
 * so on that one signal we speak the reply locally instead of only showing an
 * error notice.
 *
 * Deliberately narrow: this is used only on the MANUAL Read aloud action and
 * only on `voice_unavailable`. It does not touch a gateway that can synthesize,
 * and it does not join the auto-speak sentence stream. */

/** Does this webview expose a usable Web Speech synthesis API? Browsers and
 * most desktop-app webviews do; a stripped embedder may not, and then there is
 * nothing to fall back to. */
export function clientSpeechAvailable(): boolean {
  return (
    typeof window !== 'undefined' &&
    typeof window.speechSynthesis !== 'undefined' &&
    typeof window.SpeechSynthesisUtterance !== 'undefined'
  )
}

let activeUtterance: SpeechSynthesisUtterance | null = null

/** Stop any client-side speech this module started. Safe to call when idle. */
export function stopClientSpeech(): void {
  if (!clientSpeechAvailable()) return
  activeUtterance = null
  try {
    window.speechSynthesis.cancel()
  } catch {
    /* a webview that exposes the API but refuses cancel() is still stopped from
     * our side: we have dropped the utterance reference, so onend/onerror below
     * will no-op. */
  }
}

/** Speak *text* on the client. `onPlaying` brackets the audible span (true at
 * start, false when speech ends, errors, or is cancelled) so the caller can
 * drive the same "playing" state the gateway path uses, and `onError` fires
 * only on a genuine synthesis error (not on a user cancel). Returns whether
 * speech was started at all. */
export function speakOnClient(
  text: string,
  { onPlaying, onError }: { onPlaying: (playing: boolean) => void; onError: (code: string) => void },
): boolean {
  if (!clientSpeechAvailable()) return false
  const spoken = text.trim()
  if (!spoken) return false
  // Replace any in-flight client speech; the gateway path is stopped by the
  // caller before it reaches here.
  stopClientSpeech()
  let utterance: SpeechSynthesisUtterance
  try {
    utterance = new window.SpeechSynthesisUtterance(spoken)
  } catch {
    return false
  }
  activeUtterance = utterance
  const settle = (playing: boolean) => {
    if (activeUtterance !== utterance) return
    activeUtterance = null
    onPlaying(playing)
  }
  utterance.onend = () => settle(false)
  utterance.onerror = (event: SpeechSynthesisErrorEvent) => {
    if (activeUtterance !== utterance) return
    activeUtterance = null
    onPlaying(false)
    // A cancel is how we STOP speech, not a failure the user needs told about.
    if (event.error !== 'canceled' && event.error !== 'interrupted') {
      onError('voice_playback_failed')
    }
  }
  try {
    onPlaying(true)
    window.speechSynthesis.speak(utterance)
  } catch {
    settle(false)
    return false
  }
  return true
}
