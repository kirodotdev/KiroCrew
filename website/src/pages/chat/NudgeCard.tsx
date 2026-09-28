import { memo } from 'react'
import { ChevronRight, RefreshCw } from 'lucide-react'
import type { ChatMessage } from '../../types'

import { i18nT } from '../../i18n/t'
import { useRowDisclosure } from './rowDisclosure'
import { useLanguageGeneration } from '../../i18n/useLanguageGeneration'
/** Matches the `[auto-nudge cycle N]` prefix the gateway prepends to nudge turns. */
const NUDGE_TAG_RE = /^\[auto-nudge cycle (\d+)\]\n?/

/**
 * Parse a nudge message into `{ cycle, body }`.
 *
 * Prefers the structured `meta.nudge` payload the dashboard nudge path
 * attaches. Falls back to parsing the `[auto-nudge cycle N]` text tag so
 * history-restored rows (and the slack/discord producers, which attach no
 * meta) still render as a card instead of a wall of text.
 *
 * The gateway deliberately does NOT write `body` into meta — it is derivable
 * from `content` minus the tag, and duplicating a multi-KB payload into every
 * persisted row and WS broadcast is exactly the growth this card exists to
 * avoid. `body` is still read when present so any row written by an older
 * gateway keeps rendering from its meta.
 */
export function parseNudgeMessage(
  message: ChatMessage,
): { cycle: number | null; body: string } {
  const meta = message.meta as Record<string, unknown> | undefined
  const nudgeMeta = meta?.nudge as Record<string, unknown> | undefined
  const raw = message.content ?? ''
  const match = NUDGE_TAG_RE.exec(raw)
  const cycleFromMeta =
    typeof nudgeMeta?.cycle === 'number' && Number.isFinite(nudgeMeta.cycle)
      ? (nudgeMeta.cycle as number)
      : null
  const cycle = cycleFromMeta ?? (match ? Number(match[1]) : null)
  const bodyFromMeta = typeof nudgeMeta?.body === 'string' ? (nudgeMeta.body as string) : null
  const body = (bodyFromMeta ?? (match ? raw.slice(match[0].length) : raw)).trim()
  return { cycle, body }
}

/** The row's one-line label for a nudge turn. */
function nudgeLabel(cycle: number | null): string {
  return cycle !== null
    ? i18nT('pages.chat.nudgeCard.auto_nudge_cycle', { count: cycle })
    : i18nT('pages.chat.nudgeCard.auto_nudge')
}

/**
 * True when this nudge row belongs to the loop that is currently active.
 *
 * The Loop button opens the popover for whatever loop is bound to the slot
 * *now*. A slot can outlive its loop — remove one, create another — so a
 * historical card must not offer controls for an unrelated successor loop.
 * Rows with no `loop_id` (legacy, or slack/discord producers) never match.
 */
export function nudgeMatchesLoop(message: ChatMessage, activeLoopId?: string | null): boolean {
  const meta = message.meta as Record<string, unknown> | undefined
  const nudgeMeta = meta?.nudge as Record<string, unknown> | undefined
  const loopId = typeof nudgeMeta?.loop_id === 'string' ? (nudgeMeta.loop_id as string) : null
  return !!loopId && !!activeLoopId && loopId === activeLoopId
}

/**
 * One-line system row for auto-nudge turns.
 *
 * The nudge instruction blob is machine-facing context, not something the user
 * needs to re-read every cycle — and it is not something the user SAID, so it
 * must not look like a message either. Drawn as a quiet, centred, muted line
 * (the same register as a "N earlier steps" divider): the cycle label plus an
 * expand/collapse word, and the Loop button when this row's loop is the slot's
 * active one. Clicking the label reveals the raw payload underneath in a
 * monospace panel; the row itself never grows into a card.
 */
export default memo(function NudgeCard({
  message,
  onOpenLoop,
  disclosureKey,
}: {
  message: ChatMessage
  onOpenLoop?: () => void
  disclosureKey?: string
}) {
  useLanguageGeneration() // memo() bails out of the provider-level repaint; subscribe directly
  const [expanded, setExpanded] = useRowDisclosure(disclosureKey, false)
  const { cycle, body } = parseNudgeMessage(message)
  const label = nudgeLabel(cycle)
  const toggleWord = expanded
    ? i18nT('pages.chat.nudgeCard.collapse')
    : i18nT('pages.chat.nudgeCard.expand')

  return (
    <div
      className="self-center w-full max-w-full min-w-0 text-muted animate-scale-in"
      data-testid="nudge-card"
      data-cycle={cycle ?? ''}
      data-expanded={expanded ? 'true' : 'false'}
    >
      <div className="flex items-center justify-center gap-2 min-w-0 px-3 py-0.5">
        <button
          type="button"
          onClick={() => setExpanded(v => !v)}
          aria-expanded={expanded}
          aria-label={expanded ? i18nT('pages.chat.nudgeCard.hide_nudge_instructions') : i18nT('pages.chat.nudgeCard.show_nudge_instructions')}
          // Sighted users get what AT already gets: the toggle word alone
          // ("Expand") read as "what happened this round"; the payload is the
          // loop's INSTRUCTION, and the tooltip says so.
          title={expanded ? i18nT('pages.chat.nudgeCard.hide_nudge_instructions') : i18nT('pages.chat.nudgeCard.show_nudge_instructions')}
          className="flex items-center gap-1.5 min-w-0 text-[12px] leading-5 hover:text-text transition-colors rounded px-1 -mx-1"
          data-testid="nudge-card-toggle"
        >
          <RefreshCw size={12} className="lucide-inline shrink-0 opacity-70" aria-hidden="true" />
          <span className="truncate">{label}</span>
          <span aria-hidden="true" className="opacity-50">·</span>
          <span className="shrink-0 inline-flex items-center gap-0.5 underline-offset-2 hover:underline">
            {toggleWord}
            <ChevronRight
              size={11}
              className={`lucide-inline shrink-0 transition-transform ${expanded ? 'rotate-90' : ''}`}
              aria-hidden="true"
            />
          </span>
        </button>
        {onOpenLoop && (
          // Reads as a CONTROL, not a tag: the row has no card chrome any more,
          // so a bare outlined chip beside a centred divider looked like a
          // status label nobody would dare click (UX review). Hover fill, a
          // darker border and a trailing chevron say "this opens something".
          <button
            type="button"
            onClick={onOpenLoop}
            // Names the destination: "View loop" beside "Expand" both promise
            // "more about this row" until the tooltip says WHICH more.
            title={i18nT('pages.chat.nudgeCard.view_loop_title')}
            className="shrink-0 inline-flex items-center gap-0.5 text-[11px] leading-4 pl-1.5 pr-1 py-0.5 rounded border border-border hover:border-text/40 hover:bg-bg-hover hover:text-text active:bg-accent/10 transition-colors cursor-pointer"
            data-testid="nudge-card-open-loop"
          >
            {i18nT('pages.chat.nudgeCard.loop')}
            <ChevronRight size={11} className="lucide-inline shrink-0" aria-hidden="true" />
          </button>
        )}
      </div>
      {expanded && (
        <div
          className="mt-1 rounded-md ring-1 ring-inset forced-colors:border ring-border bg-card px-3 py-2 text-[12px] font-mono leading-5 whitespace-pre-wrap overflow-hidden animate-rise motion-reduce:animate-none"
          style={{ overflowWrap: 'anywhere', wordBreak: 'break-word' }}
          data-testid="nudge-card-body"
        >
          {body}
        </div>
      )}
    </div>
  )
})
