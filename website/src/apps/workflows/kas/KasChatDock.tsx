/**
 * The chat's workflow dock: when one chat has several live KAS runs at once,
 * they gather above the composer, collapsed by default, in one of two shapes
 * (a device-local display preference, Settings > Developer > Feature Previews):
 *
 *   stack     the run that needs you most on top, the others peeking out
 *             beneath it as card edges ("+2 more"); a click spreads them out.
 *   combined  one card for all of them: "3 workflows · 1 needs you",
 *             summed steps, a shared progress bar; a click expands to rows.
 *
 * A single live run draws nothing here: its own card in the transcript is
 * already compact. Each expanded row opens that run in the side panel, where
 * its approvals, questions and step chats are answered.
 */
import { useState } from 'react'
import { ChevronDown, ChevronUp } from 'lucide-react'

import type { KasRunRow } from '../../../api/client'
import { useAppDispatch } from '../../../store'
import { openActivityToTab } from '../../../store/chatSlice'
import { fmtElapsed } from '../../../i18n/format'
import { i18nT } from '../../../i18n/t'
import { dockRuns, dockSummary, liveStatusLine, liveStepProgress, needsAttention, runBadge, runElapsedMs, runTitle } from './kasModel'
import { runsOfChat } from './KasChatRuns'
import { KasEngineGate } from './KasEngineGate'
import { KasRunIconControls, KasRunStatusIcon, useNow } from './KasRunBits'
import { setKasSelection } from './kasSelection'
import { mergeRuns, useKasChatRuns, useKasDockMode, useKasState, type KasDockMode } from './useKasWorkflows'

/** How many card edges peek out under the top card of a collapsed stack. */
const STACK_PEEK = 2

function DockRow({ row, slotKey, now }: { row: KasRunRow; slotKey: string; now: number }) {
  const dispatch = useAppDispatch()
  const title = runTitle(row)
  const badge = runBadge(row)
  const live = liveStatusLine(row)
  const progress = liveStepProgress(row)
  const elapsed = runElapsedMs(row, now)
  const open = () => {
    setKasSelection(slotKey, { workflowId: row.workflowId })
    dispatch(openActivityToTab('workflows'))
  }
  return (
    <div className="flex items-center gap-2 px-3 py-1.5 min-w-0" data-testid="kas-dock-row" data-workflow-id={row.workflowId}>
      <button
        type="button"
        className="flex-1 min-w-0 flex items-center gap-2 bg-transparent border-none p-0 text-left cursor-pointer text-text rounded focus-visible:outline focus-visible:outline-2 focus-visible:outline-accent"
        onClick={open}
        aria-label={i18nT('apps.workflows.kas.open_run_in_panel', { name: title })}
      >
        <KasRunStatusIcon display={row.controls.display} attention={needsAttention(row)} size={13} />
        <span className="font-medium text-[12.5px] text-text-strong truncate min-w-0 max-w-[40%]">{title}</span>
        <span className="sr-only">{badge.label}</span>
        {live ? (
          <span className={`text-[12px] truncate min-w-0 ${live.kind === 'blocking' ? 'text-warn' : 'text-muted'}`}>{live.text}</span>
        ) : null}
        <span className="ml-auto shrink-0 text-[11px] text-muted tabular-nums">
          {[
            progress ? i18nT('apps.workflows.kas.step_progress', { done: progress.done, total: progress.total }) : null,
            elapsed != null ? fmtElapsed(elapsed, { wholeSeconds: true }) : null,
          ]
            .filter(Boolean)
            .join(' · ')}
        </span>
      </button>
      <KasRunIconControls row={row} />
    </div>
  )
}

/** The combined card's summary line: "3 workflows · 1 needs you". */
export function dockHeadline(count: number, needsYou: number): string {
  const head = i18nT('apps.workflows.kas.dock_workflows', { count })
  return needsYou > 0 ? `${head} · ${i18nT('apps.workflows.kas.dock_needs_you', { count: needsYou })}` : head
}

export function KasChatDockView({
  runs,
  slotKey,
  mode,
  defaultExpanded = false,
}: {
  runs: readonly KasRunRow[]
  slotKey: string
  mode: KasDockMode
  defaultExpanded?: boolean
}) {
  const [expanded, setExpanded] = useState(defaultExpanded)
  const live = dockRuns(runs)
  // The clock ticks only while the dock is drawn.
  const now = useNow(live.length >= 2)
  if (live.length < 2) return null
  const summary = dockSummary(live)
  const headline = dockHeadline(summary.count, summary.needsYou)
  const start = live.reduce<number | null>((min, r) => {
    const ms = Date.parse(r.startedAt ?? r.createdAt ?? '')
    return Number.isFinite(ms) && (min == null || ms < min) ? ms : min
  }, null)
  const toggleLabel = expanded ? i18nT('apps.workflows.kas.dock_collapse') : i18nT('apps.workflows.kas.dock_expand')
  const toggle = (
    <button
      type="button"
      className="shrink-0 inline-flex items-center gap-1 bg-transparent border-none px-1.5 py-1 text-[11.5px] text-muted cursor-pointer rounded hover:text-text-strong focus-visible:outline focus-visible:outline-2 focus-visible:outline-accent"
      onClick={() => setExpanded(v => !v)}
      aria-expanded={expanded}
      aria-label={toggleLabel}
      title={toggleLabel}
      data-testid="kas-dock-toggle"
    >
      {expanded ? <ChevronUp size={13} aria-hidden="true" /> : <ChevronDown size={13} aria-hidden="true" />}
    </button>
  )

  if (mode === 'combined') {
    return (
      <section
        className="rounded-lg border border-border bg-bg-elevated text-text overflow-hidden"
        aria-label={i18nT('apps.workflows.kas.dock_label')}
        data-testid="kas-dock"
        data-mode="combined"
        data-expanded={expanded ? 'true' : 'false'}
      >
        <div className="flex items-center gap-2 px-3 py-1.5 min-w-0">
          <button
            type="button"
            className="flex-1 min-w-0 flex items-center gap-2 bg-transparent border-none p-0 text-left cursor-pointer text-text rounded focus-visible:outline focus-visible:outline-2 focus-visible:outline-accent"
            onClick={() => setExpanded(v => !v)}
            aria-expanded={expanded}
            data-testid="kas-dock-combined-head"
          >
            <KasRunStatusIcon display="running" attention={summary.needsYou > 0} size={13} />
            <span className={`font-semibold text-[12.5px] truncate min-w-0 ${summary.needsYou > 0 ? 'text-warn' : 'text-text-strong'}`}>{headline}</span>
            <span className="ml-auto shrink-0 text-[11px] text-muted tabular-nums">
              {[
                summary.stepsTotal > 0 ? i18nT('apps.workflows.kas.dock_steps', { done: summary.stepsDone, total: summary.stepsTotal }) : null,
                start != null ? fmtElapsed(Math.max(0, now - start), { wholeSeconds: true }) : null,
              ]
                .filter(Boolean)
                .join(' · ')}
            </span>
          </button>
          {toggle}
        </div>
        {summary.stepsTotal > 0 ? (
          <div
            className="h-[3px] bg-bg-hover"
            role="progressbar"
            aria-label={i18nT('apps.workflows.kas.dock_progress')}
            aria-valuemin={0}
            aria-valuemax={100}
            aria-valuenow={summary.progressPct}
          >
            <div className="h-full bg-accent" style={{ width: `${summary.progressPct}%` }} />
          </div>
        ) : null}
        {expanded ? (
          <div className="flex flex-col divide-y divide-border border-t border-border" data-testid="kas-dock-rows">
            {live.map(row => (
              <DockRow key={row.workflowId} row={row} slotKey={slotKey} now={now} />
            ))}
          </div>
        ) : null}
      </section>
    )
  }

  // Stack: expanded, the cards sit one under another; collapsed, the top card
  // shows and the next ones peek out beneath it as edges.
  if (expanded) {
    return (
      <section className="flex flex-col gap-1" aria-label={i18nT('apps.workflows.kas.dock_label')} data-testid="kas-dock" data-mode="stack" data-expanded="true">
        <div className="flex items-center gap-2 px-1 min-w-0">
          <span className="text-[11.5px] font-medium text-muted truncate min-w-0">{headline}</span>
          <span className="ml-auto">{toggle}</span>
        </div>
        {live.map(row => (
          <div key={row.workflowId} className="rounded-lg border border-border bg-bg-elevated">
            <DockRow row={row} slotKey={slotKey} now={now} />
          </div>
        ))}
      </section>
    )
  }
  const [top, ...rest] = live
  const peeks = Math.min(STACK_PEEK, rest.length)
  return (
    <section
      className="relative"
      style={{ paddingBottom: peeks * 5 }}
      aria-label={i18nT('apps.workflows.kas.dock_label')}
      data-testid="kas-dock"
      data-mode="stack"
      data-expanded="false"
    >
      {/* The card edges behind the top card, one per hidden run (up to two). */}
      {Array.from({ length: peeks }, (_, i) => (
        <div
          key={i}
          aria-hidden="true"
          className="absolute rounded-lg border border-border bg-bg-elevated"
          style={{ left: 8 * (i + 1), right: 8 * (i + 1), top: 5 * (i + 1), bottom: 0, zIndex: peeks - i, opacity: 1 - 0.25 * (i + 1) }}
          data-testid="kas-dock-peek"
        />
      ))}
      <div className="relative rounded-lg border border-border bg-bg-elevated shadow-sm flex items-center min-w-0" style={{ zIndex: peeks + 1 }}>
        <div className="flex-1 min-w-0">
          <DockRow row={top} slotKey={slotKey} now={now} />
        </div>
        <button
          type="button"
          className="shrink-0 mr-2 inline-flex items-center gap-1 rounded-full border border-border bg-bg px-2 py-0.5 text-[11px] text-muted cursor-pointer hover:text-text-strong focus-visible:outline focus-visible:outline-2 focus-visible:outline-accent"
          onClick={() => setExpanded(true)}
          aria-expanded={false}
          aria-label={toggleLabel}
          data-testid="kas-dock-toggle"
        >
          {i18nT('apps.workflows.kas.dock_more', { count: rest.length })}
          <ChevronDown size={12} aria-hidden="true" />
        </button>
      </div>
    </section>
  )
}

function KasChatDockInner({ slotKey }: { slotKey: string }) {
  const state = useKasState(true)
  const ledger = useKasChatRuns(slotKey, true)
  const [mode] = useKasDockMode()
  const runs = runsOfChat(mergeRuns(state.data?.runs ?? [], ledger.data?.runs), slotKey)
  // Keyed by chat: switching chats starts collapsed again.
  return <KasChatDockView key={slotKey} runs={runs} slotKey={slotKey} mode={mode} />
}

/** Above the composer of `slotKey`'s chat; draws nothing off KAS or with <2 live runs. */
export function KasChatDock({ slotKey }: { slotKey: string | null | undefined }) {
  if (!slotKey) return null
  return (
    <KasEngineGate fallback={null}>
      <KasChatDockInner slotKey={slotKey} />
    </KasEngineGate>
  )
}
