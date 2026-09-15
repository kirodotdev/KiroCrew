/**
 * Does the composer hold work the user has not sent yet?
 *
 * ONE definition, imported by every caller, because the answer gates a
 * DESTRUCTIVE action: the `close` option action tears its tab down, and the
 * composer is the only copy of whatever is staged in it. Two call sites are
 * consulted at different moments —
 *
 *  - `ChatInput` asks at RENDER time, to decide whether the action chip is even
 *    clickable; and
 *  - the shared close dispatcher asks again immediately BEFORE closing, because
 *    the dispatch awaits a network write and a draft typed inside that window is
 *    invisible to the render-time gate. Nothing else moves in that window: typing
 *    appends no transcript row, so the source-key staleness check cannot see it.
 *
 * ## Why every field is REQUIRED
 *
 * This was variadic (`...attachments: (unknown[] | boolean | undefined)[]`), and
 * that shape let a host omit a whole category of staged work with no type error —
 * on the argument list of a destructive gate. It happened twice: the first
 * version counted text and files only, and a KNOWLEDGE selection (which leaves no
 * token, so every text-derived term reads false) sailed through both seams; and
 * the two hosts drifted to 2-arg and 5-arg calls of the same predicate without a
 * word from the compiler.
 *
 * Named REQUIRED fields make both impossible. A host that genuinely cannot stage
 * a category has to say so — `knowledge: false` is a claim someone made, where a
 * missing argument was only ever an absence nobody noticed. Adding a category
 * later breaks every PRODUCTION call site on purpose, which is the point: each
 * host must then answer for it.
 *
 * That guarantee stops at the project boundary, and the gap is worth knowing:
 * `tsconfig.app.json` excludes `src/test` and every `*.test.ts(x)`, so a TEST
 * fixture that omits a field compiles anyway and the missing term reads
 * `undefined` — falsy, i.e. the old behaviour, asserted green. So a fixture here
 * is not protected by the type and has to be updated by hand when a category is
 * added; the field list below is the checklist for that.
 */
export interface ComposerWork {
  /** Composer text. Whitespace-only is NOT work — a stray space would otherwise
   *  disable the chip with no visible cause, and closing over it loses nothing. */
  text: string
  /** Staged file attachments. */
  files: readonly unknown[]
  /** Staged directory tokens. Pass `[]` where they are DERIVED from `text` (as in
   *  `ChatPage`, via `parseDirTokens`) — they cannot be non-empty while the text
   *  is empty, so counting them there would imply an independence they lack. */
  dirs: readonly unknown[]
  /** Staged session references. */
  sessionRefs: readonly unknown[]
  /** Collapsed paste blocks. */
  pasteBlocks: readonly unknown[]
  /** A pending knowledge selection. The one category with NO textual trace, and
   *  therefore the one a text-derived predicate silently misses. */
  knowledge: boolean
  /**
   * An upload the user started that has not landed yet.
   *
   * `files` is only populated by the upload RESULT, so between the file picker
   * closing and the response arriving every other term reads false while the user
   * has already committed an attachment. Closing in that window deletes the pane
   * and the upload resolves into a slot that no longer exists — the selected file
   * is simply gone, with no error and nothing staged to recover it.
   *
   * This is the same shape of hole `knowledge` was: state that IS unsent work but
   * leaves no trace in any of the collections above. It is a separate term rather
   * than folded into `files` because the two answer different questions — one is
   * "what is staged", the other "is something on its way" — and a caller must be
   * made to answer both.
   */
  uploading: boolean
  /**
   * Speech the user has already produced that has not landed in the composer yet:
   * a capture in flight, or a transcription still resolving.
   *
   * The third instance of the same hole as `knowledge` and `uploading` — real
   * unsent work with no trace in any collection above. It is the worst of the
   * three, because the window is WIDEST when the composer looks emptiest: a
   * streaming capture that has produced no partial yet leaves `text` empty, so
   * every text-derived term reads false while the user is mid-sentence. Closing
   * there disarms voice as the slot goes away and the final transcript is dropped.
   *
   * Answered with the UNGATED capture flag, not the ownership-gated one, and that
   * distinction is load-bearing rather than incidental: `useVoiceInput` assigns
   * `sessionOwner` only after the server handshake resolves, so for the length of
   * that handshake real audio exists while the gated flag still reads false —
   * exactly the cold window this finding names. The mic is one shared device, so
   * the ungated flag effectively means "the one mic is capturing"; over-blocking a
   * chip for a moment is the right side of that trade against losing speech.
   */
  voiceCapture: boolean
}

export function hasUnsentComposerWork(work: ComposerWork): boolean {
  if (hasComposerTextOrFiles(work)) return true
  if (work.knowledge || work.uploading || work.voiceCapture) return true
  return (
    work.dirs.length > 0
    || work.sessionRefs.length > 0
    || work.pasteBlocks.length > 0
  )
}

/**
 * The text-and-files half of the predicate, on its own.
 *
 * `ChatInput` has its own `composerHasDraft` for the mic / hold-to-talk mode, and
 * it re-spelled exactly these two terms — a second definition of "the composer has
 * something in it" sitting beside a module whose whole premise is that there is
 * only one. Sharing the terms rather than the whole predicate is deliberate:
 * `composerHasDraft` decides whether the mic acts as a mode switch, and pulling in
 * `knowledge`, `uploading` or `sessionRefs` there would silently change the voice
 * behaviour of a refs-only composer, which is a different question from whether a
 * destructive close would lose something.
 */
export function hasComposerTextOrFiles(
  work: Pick<ComposerWork, 'text' | 'files'>,
): boolean {
  return !!work.text.trim() || work.files.length > 0
}

/**
 * Does each category's work survive the tab dying — the TTL tier a dirty claim leans on?
 *
 * Typed over `keyof ComposerWork`, so adding a category to the interface above FAILS TO
 * COMPILE here until its tier is stated. A new unsent-work kind must not inherit a
 * recoverable claim by omission and silently fall back to the 90s TTL.
 */
export const WORK_IS_RECOVERABLE: Record<keyof ComposerWork, boolean> = {
  // Written to storage by the debounced side-draft save, so a reload finds it.
  text: true,
  // Derived from `text`, so it is recoverable exactly when the text is.
  dirs: true,
  // Staged in memory only: nothing re-derives these after a tear-down.
  files: false,
  sessionRefs: false,
  pasteBlocks: false,
  knowledge: false,
  uploading: false,
  voiceCapture: false,
}
