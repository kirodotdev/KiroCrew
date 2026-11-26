import { memo } from 'react'
import { Bell, Timer, X } from 'lucide-react'
import { useAppSelector } from '../../store'
import { Glass } from '../../components/Glass'
import { Btn } from '../../components/ui'
import { useSecondClock } from '../../hooks/useSecondClock'
import { saveChatCompleteNotify, useChatCompleteNotify } from '../../hooks/chatCompleteNotify'
import {
  canOfferChatCompleteNotify,
  chatCompleteOfferSeconds,
  dismissChatCompleteOffer,
  useChatCompleteOfferDismissed,
} from '../../hooks/chatCompleteOffer'
import { i18nT } from '../../i18n/t'
import { fmtElapsed } from '../../i18n/format'
import { useLanguageGeneration } from '../../i18n/useLanguageGeneration'
import { serverNowMs } from '../../utils/serverClock'

/** Whole seconds from the gateway stamp *startedTs* to the browser's *nowMs*,
 *  read on the gateway's clock (`utils/serverClock.ts`), never negative. */
function secondsSince(startedTs: number, nowMs: number): number {
  return Math.max(0, Math.floor(serverNowMs(nowMs) / 1000 - startedTs))
}

/** The ticking label, its own leaf so only it repaints each second. */
function TurnElapsed({ startedTs }: { startedTs: number }) {
  const secs = useSecondClock(now => secondsSince(startedTs, now))
  const elapsed = fmtElapsed(secs * 1000, { wholeSeconds: true })
  return (
    // Not a live region: a screen reader would otherwise hear it every second.
    <span aria-live="off" className="tabular-nums text-[13px] font-mono text-text-strong" data-testid="turn-clock">
      {i18nT('pages.chat.turnClock.running_for', { elapsed })}
    </span>
  )
}

/**
 * How long the running turn has taken, above the chat composer, for as long as
 * it runs: streaming text included, which hides the footer's "Thinking…" row.
 *
 * Counts up from the gateway's `run_started_ts`, the stamp the run's final
 * `chat_done` measures `run_seconds` from, so a reload or a second window shows
 * the same figure, counted on the gateway's clock so a remote dashboard's skew
 * does not show. Renders nothing while no turn runs or the gateway sent no stamp.
 *
 * Once the turn passes `chatCompleteOfferSeconds()` it offers the turn-done
 * toast in one click, while that toast is off, the browser could show it, and
 * the person has not declined the offer. The host selects only whether the turn
 * passed that threshold, so it repaints once when it does; the label below ticks.
 */
const TurnClock = memo(function TurnClock({ slot }: { slot: string | null }) {
  useLanguageGeneration() // memo() bails out of the provider-level repaint; subscribe directly
  const startedTs = useAppSelector(s => {
    const row = (s.dashboard.slots ?? []).find(x => x.key === slot)
    return row?.running && typeof row.run_started_ts === 'number' ? row.run_started_ts : null
  })
  const notifyOn = useChatCompleteNotify()
  const dismissed = useChatCompleteOfferDismissed()
  const offerable = startedTs !== null && !notifyOn && !dismissed
  // Read once per render, not per tick: it is a localStorage read.
  const threshold = offerable ? chatCompleteOfferSeconds() : 0
  const pastThreshold = useSecondClock(
    now => startedTs !== null && secondsSince(startedTs, now) >= threshold,
    offerable,
  )
  if (!slot || startedTs === null) return null
  const offer = offerable && pastThreshold && canOfferChatCompleteNotify()
  return (
    // `relative z-[2]` clears the transcript's bottom mask, as TaskProgressBar's does.
    <div className="px-4 mx-auto w-full relative z-[2]" style={{ maxWidth: 'var(--mc-content-width, 900px)' }}>
      <Glass variant="chip" thickness="thick" radius={16} className="mb-1 inline-flex max-w-full items-center gap-2 px-3 py-1 glass-accent">
        <Timer size={14} className="text-accent shrink-0" aria-hidden="true" />
        <TurnElapsed startedTs={startedTs} />
        {offer && (
          <>
            <Btn
              className="px-2 py-0.5 text-[12px]"
              title={i18nT('pages.chat.turnClock.notify_offer_hint')}
              onClick={() => saveChatCompleteNotify(true)}
              data-testid="turn-clock-notify"
            >
              <Bell size={12} aria-hidden="true" />
              {i18nT('pages.chat.turnClock.notify_offer')}
            </Btn>
            <Btn
              className="px-1 py-0.5 border-transparent"
              aria-label={i18nT('pages.chat.turnClock.dismiss_offer')}
              title={i18nT('pages.chat.turnClock.dismiss_offer')}
              onClick={dismissChatCompleteOffer}
              data-testid="turn-clock-dismiss"
            >
              <X size={12} aria-hidden="true" />
            </Btn>
          </>
        )}
      </Glass>
    </div>
  )
})

export default TurnClock
