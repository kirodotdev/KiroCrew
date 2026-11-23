/**
 * Terminals to show without focusing them, once.
 *
 * A Run-in-terminal action on a touch device opens a terminal with the command
 * already typed in. Focusing its input there raises the on-screen keyboard
 * over output the user wanted to read, so the action marks the terminal it is
 * about to show, and the terminal's next show skips the focus. A tap on the
 * terminal still focuses it, as always.
 *
 * Mark only a terminal that is about to be shown: the mark lasts until that
 * terminal's next show, so one left on a terminal already on screen would
 * swallow a later, ordinary show's focus.
 */
const quiet = new Set<string>()

export function skipFocusOnNextShow(sessionId: string): void {
  quiet.add(sessionId)
}

/** Whether this show should skip the focus. Clears the mark. */
export function takeSkipFocusOnShow(sessionId: string): boolean {
  return quiet.delete(sessionId)
}
