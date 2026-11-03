/**
 * Section goals run as ONE loop on the page's conductor (PoC). A doc's headings or a
 * board's sections each carry their own goal text, all checked at one rate set for
 * the whole page; the conductor's loop wakes at that rate and checks every goal as
 * the file stands then.
 *
 * The gateway arms that loop from the saved file (`dashboard/doc_goal_loop.py`), so
 * goals run with the page closed. What the page keeps is how it recognizes a goal
 * wake in the conductor's transcript: by the banner the loop shows in place of its
 * instruction, and by the instruction's own markers.
 */

/** The banner every goal wake shows in the transcript: how the page tells a goal
 *  check from the person's own asks. The same text is in `doc_goal_loop.py`. */
export const GOALS_TAG = '[Goals]'
const OPEN = '<<goals>>'
export const GOALS_BANNER = `${GOALS_TAG} Checking the goal sections`

export const DEFAULT_GOAL_EVERY = 10

/** Whether a transcript message is a goal wake. */
export const isGoalWake = (text: string) => text.includes(GOALS_TAG) || text.includes(OPEN)
