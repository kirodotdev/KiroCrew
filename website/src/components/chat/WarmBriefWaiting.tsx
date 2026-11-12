/**
 * WarmBriefWaiting — the things waiting on the owner, as plain text cards. A
 * fixed component fed by gateway data: the model never draws it.
 *
 * Every card text is untrusted and drawn as a plain text node that wraps. The
 * first {@link SHOWN} cards show; the rest fold behind one "N more" toggle, so
 * nothing waiting is dropped and many items never crowd the bubble.
 */
import { useState } from 'react'
import { i18nT } from '../../i18n/t'
import './warmBriefWaiting.css'

export interface WarmBriefCardItem {
  title: string
  ask: string
}

export const SHOWN = 3

export default function WarmBriefWaiting({
  cards,
  unlisted,
}: {
  cards: WarmBriefCardItem[]
  /** Waiting items the gateway counted but did not list. */
  unlisted: number
}) {
  const [open, setOpen] = useState(false)
  const shown = open ? cards : cards.slice(0, SHOWN)
  const folded = cards.length - shown.length + (open ? 0 : unlisted)
  return (
    <div className="wb-wait-wrap" data-testid="warm-brief-waiting">
      <ul className="wb-wait-cards" data-testid="warm-brief-needs-you" aria-label={i18nT('components.warmBriefCard.needs_you')}>
        {shown.map((card, i) => (
          <li key={i} className="wb-wait-card" data-testid="warm-brief-waiting-card">
            <span className="wb-wait-card-title">{card.title}</span>
            {card.ask && <span className="wb-wait-card-ask">{card.ask}</span>}
          </li>
        ))}
      </ul>
      {cards.length > SHOWN && (
        <button
          type="button"
          className="text-[12px] leading-4 text-accent hover:underline"
          data-testid="warm-brief-more"
          aria-expanded={open}
          onClick={() => setOpen(v => !v)}
        >
          {open ? i18nT('components.warmBriefCard.show_fewer') : i18nT('components.warmBriefCard.more_waiting', { count: folded })}
        </button>
      )}
      {open && unlisted > 0 && (
        <div className="text-[12px] leading-4 text-muted" data-testid="warm-brief-unlisted">
          {i18nT('components.warmBriefCard.unlisted', { count: unlisted })}
        </div>
      )}
    </div>
  )
}
