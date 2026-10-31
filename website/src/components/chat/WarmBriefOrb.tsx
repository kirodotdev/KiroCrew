/**
 * WarmBriefOrb — the catch-up's progress picture: a glowing orb whose liquid
 * height is the gateway's `accepted / total`, brighter as it fills, with
 * sparkles once full, and the things waiting on the owner floating above it as
 * plain text cards. A fixed component fed by gateway data: the model never draws it.
 * With no count (no goal, or a goal with no items) only the cards show.
 *
 * Every card text is untrusted and drawn as a plain text node that wraps. The
 * first {@link SHOWN} cards show; the rest fold behind one "N more" toggle, so
 * nothing waiting is dropped and many items never crowd the bubble.
 */
import { useState } from 'react'
import { i18nT } from '../../i18n/t'
import './warmBriefOrb.css'

export interface WarmBriefCardItem {
  title: string
  ask: string
}

export const SHOWN = 3
// Fixed spots, so a reload draws the same picture.
const SPARKS: readonly [number, number, number][] = [
  [22, 30, 0], [64, 18, 0.3], [78, 52, 0.6], [40, 62, 0.9], [18, 70, 1.2], [58, 80, 1.5],
]

export default function WarmBriefOrb({
  progress,
  cards,
  unlisted,
}: {
  progress: { accepted: number; total: number } | null
  cards: WarmBriefCardItem[]
  /** Waiting items the gateway counted but did not list. */
  unlisted: number
}) {
  const [open, setOpen] = useState(false)
  const level = progress ? progress.accepted / progress.total : 0
  const full = progress !== null && progress.accepted === progress.total
  const shown = open ? cards : cards.slice(0, SHOWN)
  const folded = cards.length - shown.length + (open ? 0 : unlisted)
  const count = progress
    ? i18nT('components.warmBriefCard.progress_count', { accepted: progress.accepted, total: progress.total })
    : null
  return (
    <div className="wb-orb-wrap" data-testid="warm-brief-orb">
      {cards.length > 0 && (
        <ul className="wb-orb-cards" data-testid="warm-brief-needs-you" aria-label={i18nT('components.warmBriefCard.needs_you')}>
          {shown.map((card, i) => (
            <li key={i} className="wb-orb-card" data-testid="warm-brief-waiting-card">
              <span className="wb-orb-card-title">{card.title}</span>
              {card.ask && <span className="wb-orb-card-ask">{card.ask}</span>}
            </li>
          ))}
        </ul>
      )}
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
      {/* No count means no orb: an empty sphere says nothing the owner can use. */}
      {progress && (
        <>
          <div
            className="wb-orb"
            style={{ ['--wb-level' as string]: String(level) }}
            role="progressbar"
            aria-valuemin={0}
            aria-valuemax={progress.total}
            aria-valuenow={progress.accepted}
            aria-label={`${i18nT('components.warmBriefCard.progress')}: ${count}`}
            data-level={level}
          >
            <div className="wb-orb-liquid" />
            {full && SPARKS.map(([x, y, delay], i) => (
              <i key={i} className="wb-orb-spark" data-testid="warm-brief-spark" style={{ left: `${x}%`, top: `${y}%`, animationDelay: `${delay}s` }} />
            ))}
          </div>
          <span className="text-[12px] leading-4 tabular-nums text-muted" data-testid="warm-brief-progress-count">{count}</span>
        </>
      )}
    </div>
  )
}
