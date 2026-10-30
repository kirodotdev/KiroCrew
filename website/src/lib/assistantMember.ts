/**
 * The first crewmate.
 *
 * A fresh install creates one crewmate once (config key `mate`, shown as
 * Mate). It is an ordinary crewmate on the ordinary crewmate template:
 * renameable, deletable and never re-created. The key only marks it for the
 * first-run landing; its display name is a label like any other crewmate's.
 */

export const ASSISTANT_MEMBER_NAME = 'mate'

/** True for the first crewmate: its key, whatever template it runs. */
export function isAssistantMember(m: { name?: string } | null | undefined): boolean {
  return !!m && m.name === ASSISTANT_MEMBER_NAME
}

/** The reserved main agent. On every roster, and not a mate. */
const DEFAULT_AGENT_NAME = 'default'

/** True when the roster holds a mate the person made themselves: anything that
 *  is neither Mate nor the reserved `default` agent. The same split the server's
 *  welcome kickoff makes (`mate_welcome._member_entry` leaves `default` out), so
 *  "your first Mate" is said on a fresh install and the name alone once a
 *  second mate exists. */
export function hasOtherMates(rows: readonly { name?: string }[]): boolean {
  return rows.some(r => !isAssistantMember(r) && r.name !== DEFAULT_AGENT_NAME)
}

type RosterHistory = { name?: string; has_dm_message?: boolean; last_message?: string }

/** Mate's roster row while no message has been exchanged with it (the
 *  server's `has_dm_message`, or a live preview a member projection already
 *  pushed), else `undefined`: absent, deleted, or already chatted with. */
export function pendingMate<T extends RosterHistory>(rows: readonly T[]): T | undefined {
  const mate = rows.find(isAssistantMember)
  if (!mate) return undefined
  const history = mate.has_dm_message === true || (typeof mate.last_message === 'string' && mate.last_message.trim() !== '')
  return history ? undefined : mate
}

