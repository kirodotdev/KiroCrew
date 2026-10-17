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

/** `rows` without Mate while the Crewmates preview is off. Mate is created in the
 *  background on every install, but nothing about it is shown until the person
 *  turns the Crewmates preview on, so every list that offers crew records (the
 *  agent pickers, Customize, a template's crews) drops it first. */
export function hideMateWithoutPreview<T extends { name?: string; source?: string }>(rows: readonly T[], crewPreview: boolean): T[] {
  // Only the row Kiro Crew created (`source: builtin`) is hidden: a crewmate the
  // person already had under the key `mate` stays where it always was.
  return crewPreview ? [...rows] : rows.filter(r => !(isAssistantMember(r) && r.source === 'builtin'))
}
