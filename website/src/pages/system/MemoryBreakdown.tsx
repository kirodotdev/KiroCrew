/**
 * The Sessions tab's whole-product memory total, and the breakdown behind it.
 *
 * The rows of the table measure agent runtimes. The total in the footer is
 * everything the product runs on this host (`totals.breakdown` from
 * `/api/sessions/memory`): the sessions, the warm pool, the gateway, the MCP
 * gateway and the desktop app. That is the figure an operator compares with the
 * OS's own number for the app, so it is measured the same way. Pressing it opens
 * the per-part breakdown, which is how a reader finds out where the gap between
 * the rows and the total went.
 */
import type { api } from '../../api/client'
import { Btn } from '../../components/ui'
import { Popover, PopoverContent, PopoverTrigger } from '../../components/ui/popover'
import InfoTip from '../../components/InfoTip'
import { fmtList, fmtNumber } from '../../i18n/format'
import { i18nT } from '../../i18n/t'
import { fmtGb, fmtMb } from './sessionRows'

type Payload = Awaited<ReturnType<typeof api.sessionsMemory>>
export type MemoryBreakdown = NonNullable<Payload['totals']['breakdown']>
export type MemoryBreakdownCategory = MemoryBreakdown['categories'][number]

/**
 * Each part's label and hint. A switch of literal keys rather than a lookup built
 * from the id, so every key is a literal the catalog extraction can see. An id
 * this page does not know (a newer gateway) is shown by its id.
 */
function categoryText(id: string): { label: string; hint: string | null } {
  switch (id) {
    case 'sessions':
      return { label: i18nT('pages.sessionsTab.breakdown_sessions'), hint: i18nT('pages.sessionsTab.breakdown_sessions_hint') }
    case 'warm_pool':
      return { label: i18nT('pages.sessionsTab.breakdown_warm_pool'), hint: i18nT('pages.sessionsTab.breakdown_warm_pool_hint') }
    case 'other_agents':
      return { label: i18nT('pages.sessionsTab.breakdown_other_agents'), hint: i18nT('pages.sessionsTab.breakdown_other_agents_hint') }
    case 'mcp_gateway':
      return { label: i18nT('pages.sessionsTab.breakdown_mcp_gateway'), hint: i18nT('pages.sessionsTab.breakdown_mcp_gateway_hint') }
    case 'gateway':
      return { label: i18nT('pages.sessionsTab.breakdown_gateway'), hint: i18nT('pages.sessionsTab.breakdown_gateway_hint') }
    case 'gateway_helpers':
      return { label: i18nT('pages.sessionsTab.breakdown_gateway_helpers'), hint: i18nT('pages.sessionsTab.breakdown_gateway_helpers_hint') }
    case 'desktop':
      return { label: i18nT('pages.sessionsTab.breakdown_desktop'), hint: i18nT('pages.sessionsTab.breakdown_desktop_hint') }
    default:
      return { label: id, hint: null }
  }
}

/** `node ×3, ssh ×7`: process names are data, so only the count is localised. */
function topNames(top: MemoryBreakdownCategory['top']): string {
  return fmtList(top.map(t => `${t.name} ×${fmtNumber(t.procs)}`), { type: 'unit', style: 'short' })
}

/** The footer total as a button that opens the breakdown. */
export function MemoryBreakdownButton({ breakdown }: { breakdown: MemoryBreakdown }) {
  const total = fmtGb(breakdown.total_mb)
  return (
    <Popover>
      <PopoverTrigger asChild>
        {/* Styled as the stat's own value: the number IS the control. The name
            keeps the number first, so a screen reader still hears the value. */}
        <Btn
          type="button"
          aria-label={i18nT('pages.sessionsTab.breakdown_open', { value: total })}
          className="font-mono tabular-nums text-[12px] font-medium text-text-strong underline decoration-dotted underline-offset-2 border-0 rounded-sm px-0 py-0 hover:bg-transparent hover:text-accent"
        >
          {total}
        </Btn>
      </PopoverTrigger>
      <PopoverContent
        align="start"
        side="top"
        sideOffset={6}
        aria-label={i18nT('pages.sessionsTab.breakdown_title')}
        className="w-auto min-w-72 max-w-[min(28rem,92vw)]"
      >
        <MemoryBreakdownPanel breakdown={breakdown} />
      </PopoverContent>
    </Popover>
  )
}

/** The breakdown itself: one line per part, largest bars against the total. */
export function MemoryBreakdownPanel({ breakdown }: { breakdown: MemoryBreakdown }) {
  const total = breakdown.total_mb
  return (
    <div className="flex flex-col gap-2 text-[12px]">
      <div className="flex items-baseline justify-between gap-4">
        <span className="font-semibold text-text-strong">{i18nT('pages.sessionsTab.breakdown_title')}</span>
        <span className="text-muted">
          {i18nT('pages.sessionsTab.breakdown_total', { value: fmtGb(total) })}
        </span>
      </div>
      <div className="grid grid-cols-[minmax(0,1fr)_auto_auto] gap-x-3 gap-y-1 items-baseline">
        <span className="text-[11px] text-muted">{i18nT('pages.sessionsTab.breakdown_part')}</span>
        <span className="text-[11px] text-muted text-right">{i18nT('pages.sessionsTab.procs')}</span>
        <span className="text-[11px] text-muted text-right">{i18nT('pages.sessionsTab.breakdown_memory')}</span>
        {breakdown.categories.map(c => {
          const { label, hint } = categoryText(c.id)
          const share = c.mb != null && total > 0 ? Math.min(100, (c.mb / total) * 100) : 0
          return (
            <div key={c.id} className="contents" data-testid={`memory-breakdown-${c.id}`}>
              <span className="inline-flex items-center gap-1 min-w-0">
                <span className="truncate text-text">{label}</span>
                {hint && <InfoTip text={hint} placement="top" />}
              </span>
              <span className="font-mono tabular-nums text-right text-muted">
                {c.procs != null ? fmtNumber(c.procs) : '—'}
              </span>
              <span className="font-mono tabular-nums text-right text-text-strong">{fmtMb(c.mb)}</span>
              <div className="col-span-3 flex flex-col gap-0.5 pb-1">
                <div className="h-1 rounded bg-bg-hover overflow-hidden" aria-hidden="true">
                  <div className="h-1 rounded bg-accent" style={{ width: `${share}%` }} />
                </div>
                {c.top.length > 0 && (
                  <span className="text-[10.5px] text-muted truncate">{topNames(c.top)}</span>
                )}
              </div>
            </div>
          )
        })}
      </div>
      <p className="text-[11px] text-muted leading-snug m-0">{i18nT('pages.sessionsTab.breakdown_method_note')}</p>
      {breakdown.partial && (
        <p className="text-[11px] text-warn leading-snug m-0">{i18nT('pages.sessionsTab.breakdown_partial_note')}</p>
      )}
    </div>
  )
}
