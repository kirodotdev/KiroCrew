/**
 * The close mark of a panel, as a plain rule. The mark is at the right end of the panel's top edge and shows
 * while a ray or the pointer is on the edge. A pinch or a tap on it closes the page, but only when the mark was
 * there to be seen before the press: everything a hand can take lights before the pinch, and a press that
 * happens to land where the mark would be closes nothing.
 */
export const CLOSE_MARK = {
  /** The mark has been shown at least this long before a press on it counts. */
  shownMs: 150,
  /** A finger cannot rest over a thing without pressing it: what it lit stays lit this long after it lifts. */
  lingerMs: 2500,
  /** The area that takes the press is at least this wide and high on the screen, in its pixels: what a fingertip covers. */
  pressPx: 44,
  /** The mark itself is drawn at least this big on the screen. */
  drawPx: 20,
  /** A mark that came by itself (at rest, on the page in front) takes no tap this long after it came. */
  restCalmMs: 1000,
  /** A mark lit this little before its page became the page in front was lit by what brought it there. */
  cameWithinMs: 400,
} as const

/**
 * How big the mark is on a panel, in metres: the side of the square that takes the press, and of the square the
 * mark is drawn in. In a session both follow the edge, which holds the angle a hand can keep a ray on. On a small
 * screen the edge is a few pixels thin, so both grow to what a finger can see and hit. `edgeM` is how thick the
 * panel's edge is, and `mPerPx` how many metres of the panel one pixel of the screen covers.
 */
export function closeSize(edgeM: number, mPerPx: number): { press: number; draw: number } {
  return { press: Math.max(2 * edgeM, CLOSE_MARK.pressPx * mPerPx), draw: Math.max(0.45 * edgeM, CLOSE_MARK.drawPx * mPerPx) }
}

/**
 * Whether a press on the mark at `now` closes the page. `shownSince` is when the mark came to be seen, or null.
 * `quietUntil` is when the pages last stood still again after the flat preview moved them (`previewLook.ts`): a
 * mark whose light began before that was lit for another page's mark, or by a touch that was not aimed at it.
 */
export const closes = (shownSince: number | null, now: number, quietUntil = 0): boolean => shownSince !== null && shownSince >= quietUntil && now - shownSince >= CLOSE_MARK.shownMs

/**
 * When the mark counts as seen after a press on it at `now`. A press on a mark that was lit before the pages
 * last stood still closes nothing and starts the count anew, so that the next press closes.
 */
/**
 * Whether a press at `now` on a mark that shows at rest closes the page: a mark that is seen closes on the first
 * tap, but one that came by itself (`restSince`, when this page became the page in front) takes no tap in its first
 * second, counted from the later of its coming and the pages last standing still (`quietUntil`).
 */
export const closesAtRest = (restSince: number | null, now: number, quietUntil = 0): boolean => restSince !== null && now - Math.max(restSince, quietUntil) >= CLOSE_MARK.restCalmMs

/**
 * Whether a mark is in its first second after it came by itself (`restSince`): then it takes no tap at all, however
 * it is lit and whatever the pointer is (a mouse that rests where it came lights it at once). A mark that was lit
 * well before it came (`shownSince` more than `cameWithinMs` before `restSince`: the page in front is looked for
 * every 200 ms, so a mark lit by the turn is often lit first) did not come by itself.
 */
export const calmAfterComing = (restSince: number | null, shownSince: number | null, now: number, quietUntil = 0): boolean =>
  restSince !== null && (shownSince === null || shownSince >= restSince - CLOSE_MARK.cameWithinMs) && now - Math.max(restSince, quietUntil) < CLOSE_MARK.restCalmMs

export const shownAfterPress = (shownSince: number | null, now: number, quietUntil = 0): number | null => (shownSince !== null && shownSince < quietUntil ? now : shownSince)

/**
 * Whether a press on the mark at `now` closes the page: not in the calm second after it came by itself, and once
 * it was seen long enough or at rest. The mark is drawn white only while this holds (D50, F2): white means a press
 * closes; in the calm second it is drawn faint and not lit.
 */
export const pressCloses = (restSince: number | null, shownSince: number | null, now: number, quietUntil = 0): boolean =>
  !calmAfterComing(restSince, shownSince, now, quietUntil) && (closes(shownSince, now, quietUntil) || closesAtRest(restSince, now, quietUntil))
