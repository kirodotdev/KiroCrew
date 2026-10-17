import { useQuery } from '@tanstack/react-query'
import type { ComponentType, ReactNode } from 'react'
import { ChevronRight, Gauge, Hourglass } from 'lucide-react'
import { Trans } from 'react-i18next'
import { Link, useNavigate } from 'react-router-dom'
import { api } from '../api/client'
import type { ThrottleView } from '../api/tasks'
import { fmtNumber } from '../i18n/format'
import { i18nT } from '../i18n/t'
import { isMemoryWait } from '../pages/chat/subagentQueuedReason'
import { cn } from '../lib/utils'
import { useAppSelector } from '../store'
import { findReport } from '../utils/errorReport'
import ErrorNotice, { ErrorNoticeMenuItem } from './ErrorNotice'

/**
 * Chat-header chip for a concurrency cut the adaptive controller holds.
 *
 * The controller lowers the execution cap (and the spawn gate for agent
 * starts) under pressure, and while it does, work queues for a slot. The Tasks
 * & capacity card shows that, but the chat tab is where the operator waits, and
 * a turn whose work is queued there reads exactly like a dead session. So the
 * header carries one chip while a cut is in force, naming the reduced cap and
 * how much is queued, and linking to the card that explains it.
 *
 * The gateway decides what counts as a cut (`throttle_view`): a cap below the
 * value a fresh process starts it at. The chip renders that verdict and nothing
 * of its own. The Tasks & capacity card counts a spawn gate as lowered by the
 * same start value (the gate lane's `start`), because the gate starts below its
 * ceiling and grows, so a gate between its start value and its ceiling is cut
 * on neither surface.
 *
 * Beside it, {@link SessionQueuedMarker} marks THIS session when its own
 * subagent starts are among the queued work, so the open tab says whether the
 * cut is what it is waiting on.
 *
 * Width: both read the header row as a container (`@container/chathead` on the
 * row in `ChatPage`), so the session title keeps its room on a narrow pane at
 * any window size. As the row narrows the chip's sentence gives way to its lead
 * and the reduced cap ("Under load: 2/8"), then to the cap alone ("2/8"), and the
 * marker folds away (the subagent progress bar and the sidebar row still count
 * the queue). A row that also holds the channel-link chip is `crowded` and
 * steps down sooner. On a phone the row is not rendered at all, because the
 * shell's top bar holds the title; there {@link ConcurrencyCutMenuItem} carries
 * the sentence in the bar's overflow menu and {@link ConcurrencyCutDot} marks
 * the menu's trigger.
 */

/** How often the header re-reads the controller. A cut lasts minutes, not
 *  seconds, and the chip is mounted on every chat page, so this stays slower
 *  than the capacity card's 5 s. */
export const THROTTLE_REFETCH_MS = 15_000

/** The Tasks & capacity card: System > Services on the Developer page. */
export const CAPACITY_CARD_PATH = '/developer?tab=system&plane=services'

/** How long a failed read waits before its one retry. Only a read that fails
 *  twice is reported: a single miss (a gateway restarting) would otherwise
 *  flash an error in every chat header. */
export const THROTTLE_RETRY_MS = 5_000

/** The controller's cut reading, shared by every consumer (one query). A read
 *  that fails once keeps the last good reading through its retry. Once the
 *  retry fails too, the error is reported and the last good reading is
 *  withheld with it, so no surface shows numbers it can no longer confirm
 *  next to, or instead of, the notice. While the dashboard is disconnected the
 *  error is withheld: the offline banner already says why nothing can be read,
 *  and a second notice in every chat header would only repeat it. */
export function useThrottleView(): { view: ThrottleView | undefined; error: unknown } {
  const connected = useAppSelector(s => s.dashboard.connected)
  const { data, error, isError } = useQuery({
    queryKey: ['spawnAdaptive'],
    queryFn: () => api.spawnAdaptive(),
    refetchInterval: THROTTLE_REFETCH_MS,
    retry: 1,
    retryDelay: THROTTLE_RETRY_MS,
  })
  const reported = isError && connected ? error : null
  return { view: reported != null ? undefined : data?.throttle, error: reported }
}

/**
 * Which sentence the chip says for a cut, with its values, or null when there
 * is none to show. Each sentence is one catalog key whose `<0>` part names the
 * outcome ("Under load:") and whose `<1>` part names the reduced cap, so a
 * translation can reorder them. The execution cap comes first because it is
 * the one queued work waits on; a gate-only cut names new agents starting,
 * waiting or reduced. `withQueue: false` leaves the queued count out.
 *
 * The values are already formatted in the active locale's numerals, the same
 * way {@link throttleShortCap} formats the narrow "2/8", so the sentence and
 * the narrow form never show two numeral systems side by side.
 */
export function throttleChipKey(
  view: ThrottleView | undefined,
  { withQueue = true } = {},
): { key: string; values: Record<string, string> } | null {
  if (!view?.cut) return null
  const queued = withQueue ? Math.max(0, Math.floor(Number(view.queued) || 0)) : 0
  const exec = view.exec
  if (exec?.cut && exec.cap != null && exec.configured != null) {
    const pair = { cap: fmtNumber(exec.cap), configured: fmtNumber(exec.configured) }
    return queued > 0
      ? { key: 'components.concurrencyCutChip.exec_reduced_queued', values: { ...pair, queued: fmtNumber(queued) } }
      : { key: 'components.concurrencyCutChip.exec_reduced', values: pair }
  }
  const gate = view.gate
  if (gate?.paused) return { key: 'components.concurrencyCutChip.gate_paused', values: {} }
  if (gate?.cut && gate.cap != null && gate.configured != null) {
    return { key: 'components.concurrencyCutChip.gate_reduced', values: { cap: fmtNumber(gate.cap), configured: fmtNumber(gate.configured) } }
  }
  return null
}

/** The chip's sentence as plain text, the way the live region announces it. */
export function throttleChipLabel(view: ThrottleView | undefined, opts?: { withQueue?: boolean }): string | null {
  const chosen = throttleChipKey(view, opts)
  return chosen ? i18nT(chosen.key, chosen.values).replace(/<\/?[01]>/g, '') : null
}

/** The reduced cap as "2/8", what a narrow row shows; null for a cut with no
 *  pair of numbers to show (a paused gate). */
export function throttleShortCap(view: ThrottleView | undefined): string | null {
  if (!view?.cut) return null
  const pair = view.exec?.cut ? view.exec : view.gate?.paused ? null : view.gate?.cut ? view.gate : null
  if (!pair || pair.cap == null || pair.configured == null) return null
  return `${fmtNumber(pair.cap)}/${fmtNumber(pair.configured)}`
}

function rawMessage(error: unknown): string {
  return error instanceof Error ? error.message : String(error ?? '')
}

/** The sr-only live region that announces a cut (without its moving count). */
export function ConcurrencyCutLiveRegion() {
  const { view } = useThrottleView()
  return (
    <span role="status" aria-live="polite" className="sr-only">
      {throttleChipLabel(view, { withQueue: false }) ?? ''}
    </span>
  )
}

/**
 * Width tiers, as literal class names so the build generates each one. From
 * `sentence` the chip shows its whole sentence; from `lead` the lead and the cap
 * ("Under load: 2/8"); below that the cap alone. A `crowded` row (one that also
 * holds the channel-link chip) moves each step up, and below its `cap` step
 * shows the gauge alone: that row is already full on its own there, and the
 * sentence stays the link's name.
 */
const widthTiers = {
  roomy: {
    sentence: cn('hidden @[36rem]/chathead:inline'), short: cn('@[36rem]/chathead:hidden'),
    lead: cn('hidden @[28rem]/chathead:inline'), cap: '', chevron: '',
  },
  crowded: {
    sentence: cn('hidden @[48rem]/chathead:inline'), short: cn('@[48rem]/chathead:hidden'),
    lead: cn('hidden @[40rem]/chathead:inline'), cap: cn('hidden @[32rem]/chathead:inline'), chevron: cn('hidden @[32rem]/chathead:inline'),
  },
} as const

export default function ConcurrencyCutChip({ crowded = false }: { crowded?: boolean }) {
  const { view, error } = useThrottleView()
  const chosen = throttleChipKey(view)
  const shortCap = throttleShortCap(view)
  const label = throttleChipLabel(view)
  const hint = i18nT('components.concurrencyCutChip.hint')
  const tier = widthTiers[crowded ? 'crowded' : 'roomy']
  const lead = chosen ? /<0>(.*?)<\/0>/.exec(i18nT(chosen.key, chosen.values))?.[1] ?? '' : ''
  return (
    <>
      <ConcurrencyCutLiveRegion />
      {chosen && (
        <Link
          to={CAPACITY_CARD_PATH}
          className="pointer-events-auto inline-flex min-w-0 items-center gap-1.5 overflow-hidden rounded-md border border-warn/40 bg-warn/10 px-2 py-0.5 text-[11px] text-text no-underline hover:border-warn transition-colors"
          title={hint}
          data-testid="concurrency-cut-chip"
        >
          <Gauge size={11} className="shrink-0 text-warn" aria-hidden />
          <span className={`min-w-0 truncate ${shortCap ? tier.sentence : ''}`} data-chip-part="sentence">
            <Trans
              i18nKey={chosen.key}
              values={chosen.values}
              components={[
                <span key="lead" className="font-medium" data-chip-part="lead" />,
                <span key="detail" data-chip-part="detail" />,
              ]}
            />
          </span>
          {/* The narrow forms are drawn for the eye; the sentence stays for
              assistive technology. */}
          {shortCap && (
            <span className={`whitespace-nowrap ${tier.short}`} data-chip-part="short">
              <span aria-hidden>
                <span className={`font-medium ${tier.lead}`} data-chip-part="short-lead">{lead} </span>
                <span className={`font-medium ${tier.cap}`} data-chip-part="short-cap">{shortCap}</span>
              </span>
              <span className="sr-only">{label}</span>
            </span>
          )}
          {/* The hint as a description for touch, keyboard and AT readers, who
              never see a hover title; the label stays the link's name. */}
          <span className="sr-only">{hint}</span>
          {/* Says the chip opens something, for readers who never hover. */}
          <ChevronRight size={11} className={`shrink-0 text-muted ${tier.chevron}`} aria-hidden />
        </Link>
      )}
      {/* A reported read failure takes the chip's place: the hook withholds
          the last good reading with it, so the notice is never hidden behind
          a cut and stale numbers never stand in for "no cut". */}
      {error != null && (
        <ErrorNotice
          variant="inline"
          className="pointer-events-auto shrink-0"
          message={i18nT('components.concurrencyCutChip.load_failed')}
          report={findReport(rawMessage(error))}
          askAgent
          testId="concurrency-cut-error"
        />
      )}
    </>
  )
}

/**
 * Marks the open session when its own subagent starts are queued for an
 * execution slot while the controller holds the execution cap below its
 * configured value. A memory wait is left to the sidebar's own memory wording:
 * it waits on free memory, which a cap cut does not explain.
 */
export function SessionQueuedMarker({ slotKey, crowded = false }: { slotKey?: string; crowded?: boolean }) {
  const { view } = useThrottleView()
  const queued = useAppSelector(st => (slotKey ? st.chat.subagentQueued?.[slotKey] ?? 0 : 0))
  const reason = useAppSelector(st => (slotKey ? st.chat.subagentQueuedReason?.[slotKey] : undefined))
  if (!slotKey || !view?.exec?.cut || queued <= 0 || isMemoryWait(reason)) return null
  const hint = i18nT('components.concurrencyCutChip.session_queued_hint')
  return (
    <span
      className={`pointer-events-auto hidden shrink-0 items-center gap-1 rounded-md border border-border bg-bg-hover px-2 py-0.5 text-[11px] text-muted ${crowded ? '@[64rem]/chathead:inline-flex' : '@[44rem]/chathead:inline-flex'}`}
      title={hint}
      data-testid="session-queued-marker"
    >
      <Hourglass size={11} className="shrink-0" aria-hidden />
      <span className="whitespace-nowrap">{i18nT('components.concurrencyCutChip.session_queued', { queued: fmtNumber(queued) })}</span>
      <span className="sr-only">{hint}</span>
    </span>
  )
}

/** A dot on the phone overflow menu's trigger, so the menu's cut or failed-read
 *  row is seen without opening the menu: warning-toned while a cut is in force,
 *  error-toned once a read is reported failed (the hook withholds the cut then,
 *  so the two never show together). Decorative: the live region names a cut,
 *  and the menu row names the failure. */
export function ConcurrencyCutDot() {
  const { view, error } = useThrottleView()
  const tone = error != null ? 'error' : throttleChipKey(view) ? 'cut' : null
  if (!tone) return null
  return (
    <span
      aria-hidden
      data-testid="concurrency-cut-dot"
      data-tone={tone}
      className={`absolute top-1 right-1 h-1.5 w-1.5 rounded-full ring-1 ring-bg ${tone === 'error' ? 'bg-danger' : 'bg-warn'}`}
    />
  )
}

/**
 * The cut as a row of the phone top bar's overflow menu, linking to the
 * capacity card. Renders nothing while no cut is in force and no read failed.
 * A failed read is a passive notice with the hand-off as a sibling menu item,
 * so the hand-off joins the menu's roving focus.
 */
/** A menu's own item component (the dropdown's `DropdownMenuItem`). */
export type CutMenuItemComponent = ComponentType<{
  title?: string
  disabled?: boolean
  className?: string
  'aria-describedby'?: string
  onSelect?: (event: Event) => void
  children?: ReactNode
}>

export function ConcurrencyCutMenuItem({ Item }: { Item: CutMenuItemComponent }) {
  const { view, error } = useThrottleView()
  const navigate = useNavigate()
  const label = throttleChipLabel(view)
  if (!label && error == null) return null
  const message = i18nT('components.concurrencyCutChip.load_failed')
  return (
    <>
      {label && (
        <Item className="[@media(hover:none)]:min-h-10" onSelect={() => navigate(CAPACITY_CARD_PATH)}>
          <span className="flex min-w-0 items-start gap-2" data-testid="concurrency-cut-menu-item">
            <Gauge size={14} className="mt-0.5 shrink-0 text-warn" aria-hidden />
            {/* Wraps rather than widening the menu past a 320 px screen. The
                second line is the chip's hover reassurance, shown here because
                a touch screen never shows a hover title. */}
            <span className="flex max-w-[14rem] flex-col gap-0.5 whitespace-normal leading-snug">
              <span data-testid="concurrency-cut-menu-label">{label}</span>
              <span className="text-[11px] text-muted" data-testid="concurrency-cut-menu-hint">
                {i18nT('components.concurrencyCutChip.queue_kept')}
              </span>
            </span>
          </span>
        </Item>
      )}
      {/* As in the header: a reported read failure replaces the cut row. */}
      {error != null && (
        <>
          <ErrorNotice id="concurrency-cut-menu-error" variant="inline" className="px-2 py-1.5" message={message} />
          <ErrorNoticeMenuItem Item={Item} message={message} describedBy="concurrency-cut-menu-error" report={findReport(rawMessage(error))} />
        </>
      )}
    </>
  )
}
