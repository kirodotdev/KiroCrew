import type { ChatSlot } from '../../types'

/** Status marker a session row/card gets, in the sidebar's own precedence
 *  order: an approval request outranks an owed answer, an owed answer outranks
 *  activity, and activity outranks unread (a running slot is self-evidently
 *  unread). Returns null when the row is quiet.
 *
 *  Shared by SessionFlyout (pointer/hover) and SessionSwitcher (keyboard) so the
 *  two surfaces cannot drift on which marker a slot earns. */
export function statusOf(slot: ChatSlot): 'approval' | 'question' | 'running' | 'unread' | null {
  if (slot.pending_approval) return 'approval'
  // Above running deliberately: a blocking question card parks the turn, so the
  // slot reports running while nothing can advance without the user.
  if (slot.needs_input) return 'question'
  if (slot.running) return 'running'
  return null
}

/** Dot colour for a resolved status (or an unread-but-quiet row), as a token
 *  class. `null` status + `unread` is the drained-but-unseen marker. `quietClass`
 *  is the colour a fully quiet row gets: a spacious card wants a visible dot
 *  (`bg-border-strong`), a dense flyout row wants none (`bg-transparent`). Kept
 *  beside statusOf so the colour ladder and the precedence move together, shared
 *  by SessionFlyout and SessionSwitcher so the two cannot drift. */
export function statusDotClass(
  status: ReturnType<typeof statusOf>,
  unread: boolean,
  quietClass: string,
): string {
  if (status === 'approval') return 'bg-warn'
  if (status === 'question') return 'bg-info'
  if (status === 'running') return 'bg-accent animate-pulse motion-reduce:animate-none'
  if (unread) return 'bg-ok'
  return quietClass
}
