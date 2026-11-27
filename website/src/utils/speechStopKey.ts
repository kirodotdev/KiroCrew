/* Which Escape keydowns stopped voice read-back.

   The stop-speaking listener (useKeyboardShortcuts, capture phase) answers
   Escape by stopping speech, and deliberately does not preventDefault so the
   key still closes whatever it normally closes. Stopping speech clears
   `voicePlaying` synchronously, so by the time the same keydown reaches the
   composer the store no longer says speech was playing. The listener records
   the event here instead, and the composer's Escape gate and the page-wide
   Escape layers (the web-preview annotate toggle) read it: an Escape that
   stopped speech does nothing else. Without that, the key pressed to silence
   an auto-spoken reply would also cancel an in-flight optimize.

   A WeakSet keyed by the native event, so nothing outlives the keydown. */

const stoppedSpeech = new WeakSet<Event>()

/** Record that this Escape keydown stopped voice read-back. */
export function noteEscapeStoppedSpeech(e: Event): void {
  stoppedSpeech.add(e)
}

/** True when this Escape keydown already stopped voice read-back. */
export function escapeStoppedSpeech(e: Event): boolean {
  return stoppedSpeech.has(e)
}
