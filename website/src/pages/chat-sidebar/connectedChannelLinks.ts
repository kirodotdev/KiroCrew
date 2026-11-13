import type { SessionLink } from '../../types'

/** The channels a session row wears a brand mark for: one per channel that is
 * currently connected, in first-seen order.
 *
 * Connected means at least one delivery on that channel is not paused — the
 * same rule the session menu's Connect/Disconnect row uses to pick its verb, so
 * the mark on the row and the verb in the menu can never disagree. `direction`
 * plays no part: a channel the session was born in and a channel it was later
 * connected to are the same fact to the reader of the list, and the one thing a
 * disconnect changes on either is `paused`.
 *
 * One per channel, not one per link. A session born in Discord and mirrored to
 * Discord carries two links for it, and a reader should see one Discord mark,
 * not two.
 *
 * Exported for its own test; the row is the only production caller. */
export function connectedChannelLinks(links: readonly SessionLink[] | undefined): SessionLink[] {
  const byChannel = new Map<string, SessionLink>()
  for (const link of links ?? []) {
    if (link.paused || byChannel.has(link.channel)) continue
    byChannel.set(link.channel, link)
  }
  return [...byChannel.values()]
}
