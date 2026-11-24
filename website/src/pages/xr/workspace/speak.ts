/**
 * Speak to a Bot (P2, D33; D14 "语音转文字能用", D18 "就像领导批示工作一样"): he looks at a Bot, pinches it and holds
 * without moving, and speaks. The Bot holds up a blank sheet, and his words are written on it in his pen's blue as he
 * speaks (the note's own line, `useComment.ts`). He lets go: the sheet goes to that Bot as his message, once, and its
 * answer comes back as sheets on its clipboard (TALK slice 1). Plain rules here; `Crew.tsx` tells the hold
 * (`onHold`), `useBoards.ts` holds the sheet.
 *
 * - The hold: a press on a Bot held still for `HOLD.ms` (in the preview within `HOLD.slopPx` of its down, in a session
 *   within `PINCH.stillM` of the hand's travel). A shorter press is a tap and raises its words, as before.
 * - A hold that moves past the same measure is the drag (D10): the sheet goes down, and nothing is sent.
 * - The sheet comes up at `HOLD.ms`, but the recognizer starts only `HOLD.listenAfterMs` later, while the hold is still
 *   on: a drag that moves before then never opened the microphone (nor, in a session, its permission prompt).
 * - Exactly one message per finished hold, to the Bot he held, with the words that the sheet showed; none from a tap,
 *   a drag, a hold that was cut off, or silence.
 */
import { PINCH } from './pinchRoute'
import { TURN_DRAG_PX } from './previewLook'
import { noteHeard } from './useComment'

/** The hold's time and its slop: the click's own time, and the drag's own measures. */
export const HOLD = { ms: PINCH.clickMs, slopPx: TURN_DRAG_PX, slopM: PINCH.stillM, listenAfterMs: 300 } as const

/** How a hold ended: its words went; he let go having said nothing; it moved (the drag); or it was cut off. */
export type SpeakEnd = 'sent' | 'silent' | 'moved' | 'cut'

/**
 * What a hold that ended `how` does with the sheet's words: `comment` (written) and `heard` (still being heard, shown
 * after them). Only his release with words sends them, and what it sends is what the sheet showed.
 */
export function speakEnd(how: 'let' | 'drag' | 'cancel', comment: string, heard: string): { end: SpeakEnd; words: string } {
  const words = noteHeard(comment, heard).trim()
  if (how === 'drag') return { end: 'moved', words }
  if (how === 'cancel') return { end: 'cut', words }
  return words ? { end: 'sent', words } : { end: 'silent', words: '' }
}
