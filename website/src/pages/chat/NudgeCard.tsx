import { memo } from 'react'
import { ChevronRight, RefreshCw } from 'lucide-react'
import type { ChatMessage } from '../../types'

import { i18nT } from '../../i18n/t'
import { useRowDisclosure } from './rowDisclosure'
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
 * Compact inline card for auto-nudge turns.
 *
 * The nudge instruction blob is machine-facing context, not something the user
 * needs to re-read every cycle, so it collapses to a one-line chip. Clicking
 * the chip expands the full instruction text; clicking the cycle badge opens
 * the auto-nudge loop popover.
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
  const [expanded, setExpanded] = useRowDisclosure(disclosureKey, false)
  const { cycle, body } = parseNudgeMessage(message)
  const firstLine = body.split('\n').find(l => l.trim().length > 0)?.trim() ?? ''
  const label = cycle !== null ? i18nT('pages.chat.nudgeCard.auto_nudge_cycle', { count: cycle }) : i18nT('pages.chat.nudgeCard.auto_nudge')

  return (
    <div
      className="self-center w-full max-w-full min-w-0 rounded-md border border-border bg-card text-muted animate-scale-in"
      data-testid="nudge-card"
      data-cycle={cycle ?? ''}
    >
      <div className="flex items-center gap-1.5 px-2.5 py-1.5 min-w-0">
        <button
          type="button"
          onClick={() => setExpanded(v => !v)}
          aria-expanded={expanded}
          aria-label={expanded ? i18nT('pages.chat.nudgeCard.hide_nudge_instructions') : i18nT('pages.chat.nudgeCard.show_nudge_instructions')}
          className="flex items-center gap-1.5 min-w-0 flex-1 text-left text-[13px] hover:text-fg transition-colors"
          data-testid="nudge-card-toggle"
        >
          <ChevronRight
            size={13}
            className={`lucide-inline shrink-0 transition-transform ${expanded ? 'rotate-90' : ''}`}
            aria-hidden="true"
          />
          <RefreshCw size={13} className="lucide-inline shrink-0" aria-hidden="true" />
          <span className="font-medium shrink-0">{label}</span>
          {!expanded && firstLine && (
            <span className="truncate text-[12px] opacity-70 min-w-0">{firstLine}</span>
          )}
        </button>
        {onOpenLoop && (
          <button
            type="button"
            onClick={onOpenLoop}
            className="shrink-0 text-[11px] px-1.5 py-0.5 rounded border border-border hover:text-fg transition-colors"
            data-testid="nudge-card-open-loop"
          >
            {i18nT('pages.chat.nudgeCard.loop')}
          </button>
        )}
      </div>
      {expanded && (
        <div
          className="px-2.5 pb-2.5 pt-0 text-[12px] font-mono leading-relaxed whitespace-pre-wrap overflow-hidden"
          style={{ overflowWrap: 'anywhere', wordBreak: 'break-word' }}
          data-testid="nudge-card-body"
        >
          {body}
        </div>
      )}
    </div>
  )
})
