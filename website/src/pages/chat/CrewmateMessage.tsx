/**
 * CrewmateMessage — one of the crewmate's messages in its chat: the bubble,
 * placed in the row its run position dictates. The chat is a 1:1 thread with
 * one speaker besides the user, and the DM header already names that speaker
 * (face + name chip), so the message carries NO author line — no avatar, no
 * name, no time row — and no avatar gutter; the bubble takes the full text
 * column. Consecutive messages still read as one speaker through the grouped
 * corners `crewmateBubbleClass` paints along a run.
 *
 * The bubble itself is the ordinary AssistantMessage (markdown, option chips,
 * hover actions all intact); this component only places it.
 */
import type { ReactNode } from 'react'
import { crewmateRowClass, type CrewmateRunPosition } from '../../components/chat/crewmateBubbles'

/** Who is speaking: the crewmate's display name and its avatar record. The
 *  pane and the Members page resolve it once and hand it to the renderer,
 *  whose presence check on it is what routes a chat through the crewmate
 *  bubble at all; the reply-thread parent quote draws it. */
export interface CrewmateIdentity {
  name: string
  avatar?: unknown
  /** Presentation label shown in place of `name` when set. `name` stays the
   *  immutable identity — routes, API calls and avatar seeds key on it. */
  label?: string
}

export default function CrewmateMessage({ pos, author, children }: { pos: CrewmateRunPosition; author?: string; children: ReactNode }) {
  // `author` names the speaker for a screen reader only: the visible author
  // line stays gone (the DM header names the one speaker), but a listener
  // hearing the transcript has no header in view, so each message is a group
  // labelled with who said it.
  return (
    <div
      data-testid="crewmate-message"
      role={author ? 'group' : undefined}
      aria-label={author || undefined}
      className={`min-w-0 ${crewmateRowClass(pos)}`}
    >
      {children}
    </div>
  )
}
