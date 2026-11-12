/**
 * Model-facing text for a dictated message: the line `withDictationNote` ends it
 * with when `stt.dictation_note` is on.
 *
 * English on purpose, like every `*.prompt.ts` module: this line is READ BY THE
 * AGENT, so it says how to read the words above it. It also appears in the
 * transcript, which is why the i18n boundary is this named module rather than a
 * shape exemption.
 *
 * `withDictationNote` skips text that already ends with this exact line, so a
 * change here lets an old row edited and re-sent carry both lines. Keep it stable.
 */
export function dictationNoteText(): string {
  return '[This message was dictated. It may contain transcription errors, odd typos, misplaced words, or punctuation.]'
}
