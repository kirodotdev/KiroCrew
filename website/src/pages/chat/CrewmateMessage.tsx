/**
 * CrewmateMessage — one of the crewmate's messages in its chat: the author line
 * (avatar + name + time) when the message opens a run, then the bubble in the
 * text column to the right of the avatar gutter. Every bubble of a run sits in
 * that same column, so consecutive messages read as one speaker.
 *
 * The bubble itself is the ordinary AssistantMessage (markdown, option chips,
 * hover actions all intact); this component only places it.
 */
import type { ReactNode } from 'react'
import CrewAvatar from '../../components/CrewAvatar'
import {
  CREWMATE_AVATAR_PX,
  crewmateRowClass,
  opensCrewmateRun,
  type CrewmateRunPosition,
} from '../../components/chat/crewmateBubbles'
import { fmtMessageTime, fmtMessageTimeFull } from './messageTime'

/** Who is speaking: the crewmate's display name and its avatar record. */
export interface CrewmateIdentity {
  name: string
  avatar?: unknown
  /** Presentation label shown in place of `name` when set. `name` stays the
   *  immutable identity — routes, API calls and avatar seeds key on it. */
  label?: string
}

/** Avatar + gap: the text column every bubble aligns to. */
const GUTTER_CLS = 'pl-[38px]'

export default function CrewmateMessage({
  crewmate, pos, ts, children,
}: {
  crewmate: CrewmateIdentity
  pos: CrewmateRunPosition
  ts?: string
  children: ReactNode
}) {
  const opens = opensCrewmateRun(pos)
  const time = ts ? fmtMessageTime(ts) : ''
  return (
    <div data-testid="crewmate-message" className={`min-w-0 ${crewmateRowClass(pos)}`}>
      {opens && (
        <div className="flex items-center gap-2.5 mb-1.5 min-w-0" data-testid="crewmate-author">
          <CrewAvatar seed={crewmate.name} avatar={crewmate.avatar} size={CREWMATE_AVATAR_PX} />
          <span className="text-[13px] leading-5 font-semibold text-text truncate">{crewmate.label || crewmate.name}</span>
          {time && (
            <span className="text-[11px] leading-4 text-muted tabular-nums shrink-0" title={fmtMessageTimeFull(ts)}>{time}</span>
          )}
        </div>
      )}
      <div className={`min-w-0 ${GUTTER_CLS}`}>{children}</div>
    </div>
  )
}
