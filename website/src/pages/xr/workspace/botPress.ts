/** A press under way on a Bot's box, as `Crew.tsx` keeps it. */
export interface BotPressNow { pointerId: number; lifted: boolean; holding?: boolean }

/**
 * Whether the leave of `pointerId` from a Bot's box ends the press under way there, so the Bot is no longer lit and
 * no tap can come. A lifted Bot and a hold keep their press: they go on wherever the pointer is. `drag` is whether the
 * Crew was given empty space's hand: the flat preview gives it, a session does not (`EmptySpace.tsx` hands its
 * children `undefined` there), so a press that leaves the Bot ends only in a session. In the preview the box holds the
 * pointer from the down, so no leave comes while the press is on. Whether the box holds the pointer (its capture)
 * is not asked (M225): in a session the box takes the pointer only for a hold or a lift, which keep their press
 * anyway. The probe reads the capture itself (`pressing.held` in the Crew's report).
 */
export function leaveEndsPress(press: BotPressNow | null, pointerId: number, at: { drag: boolean }): boolean {
  return !at.drag && press?.pointerId === pointerId && !press.lifted && !press.holding
}

/**
 * Whether a change of the session's `visibilityState` takes a press under way on a Bot (D185, M223): one that is not
 * `visible` (a system layer over the room, or no session: the store reads nothing), and a session that begins (`prev`
 * read nothing). `prev` is `null` at the first read, at the mount, where no press is under way yet.
 */
export function visibilityTakes(prev: string | undefined | null, now: string | undefined): boolean {
  return now !== 'visible' || prev === undefined
}
