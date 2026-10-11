/**
 * The shell both crewmate greetings (`mateGreeting.ts`: warm resume, cold
 * welcome) draw in: a message FROM the crewmate at the end of its chat, in the
 * same bubble and row its replies use (`crewmateBubbles`), with the one action
 * (hide) as a small button under it. The host hands it to ChatPane's
 * `crewmateGreeting`, so it is drawn in the transcript but never written to it.
 */
import type { ReactNode } from 'react'
import { Btn } from '../../components/ui'
import { crewmateBubbleClass } from '../../components/chat/crewmateBubbles'
import CrewmateMessage, { type CrewmateIdentity } from '../chat/CrewmateMessage'

export default function MateGreetingMessage({ crewmate, label, testId, dismissLabel, dismissTestId, onDismiss, children }: {
  crewmate: CrewmateIdentity
  label: string
  testId: string
  dismissLabel: string
  dismissTestId: string
  onDismiss: () => void
  children: ReactNode
}) {
  return (
    // The transcript's own row frame (ChatMessageList `row(…, tight)`), so the
    // greeting lines up with the replies above it.
    <div className="px-4 mx-auto w-full py-0" style={{ maxWidth: 'var(--mc-content-width, 900px)' }}>
      <CrewmateMessage pos="single" author={crewmate.label || crewmate.name}>
        <section aria-label={label} data-testid={testId} className="flex flex-col items-start gap-1">
          {/* Capped at the column too, not only at 72ch: the column is
              items-start, so a long one-line goal would size the bubble
              past a narrow chat, where the scroller clips it. */}
          <div
            className={`message-bubble mc-message-font-scope msg-content leading-relaxed text-text ${crewmateBubbleClass('single')}`}
            style={{ maxWidth: 'min(100%, 72ch)' }}
            data-testid="mate-greeting-bubble"
          >
            {children}
          </div>
          <Btn onClick={onDismiss} className="px-2 py-0.5 text-[12px]" data-testid={dismissTestId}>
            {dismissLabel}
          </Btn>
        </section>
      </CrewmateMessage>
    </div>
  )
}
