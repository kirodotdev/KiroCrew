/**
 * The KAS runs a chat parents, for that chat's side-panel Workflows tab,
 * shaped like the Kiro IDE's session overview card:
 *
 *   ◌ Implement feature   2m   [⏸] [■]
 *   ⚠ Review loop         5m   [▶] [■]
 *   ✔ Docs pass           1h
 *
 * Every live run, then the 10 most recent finished ones. Clicking a row opens
 * the run's detail (STEPS tree + the selected step's own chat) in the panel;
 * the in-chat run card opens the same detail through `kasSelection`.
 *
 * With no KAS runs it renders only the Crew content it wraps, so a chat
 * without them reads exactly as before.
 */
import { useState, type ReactNode } from 'react'
import { ChevronDown, ChevronRight } from 'lucide-react'

import type { KasRunRow } from '../../../api/client'
import { i18nT } from '../../../i18n/t'
import { timeAgo } from '../../../utils/timeAgo'
import { approvalsFor, isTerminalRun, needsAttention, RECENT_FINISHED_RUNS, runTitle, splitRuns } from './kasModel'
import { KasEngineGate } from './KasEngineGate'
import { KasRunIconControls, KasRunStatusIcon } from './KasRunBits'
import { KasRunRecoveryButtons } from './KasParityControls'
import { KasRunDetail, KasStepsTree } from './KasRunDetail'
import { useKasSelection } from './kasSelection'
import { mergeRuns, useKasChatRuns, useKasState } from './useKasWorkflows'

export function runsOfChat(runs: readonly KasRunRow[], slotKey: string): KasRunRow[] {
  return runs.filter(r => !!slotKey && r.slotKey === slotKey)
}

function ageOf(row: KasRunRow): string {
  const ms = Date.parse(row.createdAt ?? row.startedAt ?? '')
  return Number.isFinite(ms) ? timeAgo(ms / 1000) : ''
}

export function KasRunRowCompact({
  row,
  onOpen,
  onOpenStep,
  defaultExpanded,
}: {
  row: KasRunRow
  onOpen: () => void
  /** Opens one step's own chat; the row's step tree is shown only when given. */
  onOpenStep?: (nodeId: string, path: string) => void
  /** Whether the step tree starts open. Defaults to "open while live" when
   *  omitted; the list passes `false` for all but a sole live run (gap 22). */
  defaultExpanded?: boolean
}) {
  const title = runTitle(row)
  // Like the IDE's Workflows rail: only the sole unfinished run starts open.
  const [expanded, setExpanded] = useState(() => defaultExpanded ?? !isTerminalRun(row))
  const canExpand = !!onOpenStep && row.nodes.length > 0
  return (
    <li className="text-text" data-testid="kas-panel-row" data-workflow-id={row.workflowId}>
      <div className="group flex items-center gap-1 rounded hover:bg-bg-hover">
        {canExpand ? (
          <button
            type="button"
            className="shrink-0 w-5 h-6 flex items-center justify-center bg-transparent border-none text-muted cursor-pointer rounded focus-visible:outline focus-visible:outline-2 focus-visible:outline-accent"
            onClick={() => setExpanded(v => !v)}
            aria-expanded={expanded}
            aria-label={i18nT(expanded ? 'apps.workflows.kas.hide_steps' : 'apps.workflows.kas.show_steps', { name: title })}
            data-testid="kas-panel-row-toggle"
          >
            {expanded ? <ChevronDown size={13} aria-hidden="true" /> : <ChevronRight size={13} aria-hidden="true" />}
          </button>
        ) : (
          <span className="shrink-0 w-5" aria-hidden="true" />
        )}
        <button
          type="button"
          className="flex-1 min-w-0 flex items-center gap-2 pr-2 py-1.5 bg-transparent border-none text-left text-[12.5px] text-text cursor-pointer rounded focus-visible:outline focus-visible:outline-2 focus-visible:outline-accent"
          onClick={onOpen}
          aria-label={i18nT('apps.workflows.kas.open_run', { name: title })}
        >
          <KasRunStatusIcon display={row.controls.display} attention={needsAttention(row)} />
          <span className="truncate min-w-0">{title}</span>
          <span className="ml-auto text-[11px] text-muted tabular-nums shrink-0">{ageOf(row)}</span>
        </button>
        <span className="pr-1 inline-flex items-center">
          <KasRunRecoveryButtons row={row} compact />
          <KasRunIconControls row={row} />
        </span>
      </div>
      {canExpand && expanded ? (
        <div className="pl-5 pb-1">
          <KasStepsTree nodes={row.nodes} selectedPath={null} onSelect={(nodeId, path) => onOpenStep?.(nodeId, path)} row={row} />
        </div>
      ) : null}
    </li>
  )
}

function KasChatRunsInner({ slotKey, children }: KasChatRunsProps) {
  const state = useKasState(true)
  const ledger = useKasChatRuns(slotKey, true)
  const [selection, setSelection] = useKasSelection(slotKey)
  const runs = runsOfChat(mergeRuns(state.data?.runs ?? [], ledger.data?.runs), slotKey)
  const approvals = state.data?.approvals ?? []
  const selected = selection ? runs.find(r => r.workflowId === selection.workflowId) : undefined

  if (selected && selection) {
    return (
      <KasRunDetail
        row={selected}
        approvals={approvalsFor(approvals, selected.workflowId)}
        questions={state.data?.questions ?? []}
        slotKey={slotKey}
        selectedNodeId={selection.nodeId}
        selectedPath={selection.path}
        onSelectStep={(nodeId, path) => setSelection({ workflowId: selected.workflowId, nodeId, path })}
        onBack={() => setSelection(null)}
      />
    )
  }

  if (runs.length === 0) return <>{children(false)}</>
  return (
    <>
      <KasRunSections
        runs={runs}
        onOpen={workflowId => setSelection({ workflowId })}
        onOpenStep={(workflowId, nodeId, path) => setSelection({ workflowId, nodeId, path })}
      />
      {children(true)}
    </>
  )
}

/** In progress / Completed sections, each with a count, an empty sentence, and
 *  a "Load more" for finished runs past the window (gap 22). */
function KasRunSections({
  runs,
  onOpen,
  onOpenStep,
}: {
  runs: readonly KasRunRow[]
  onOpen: (workflowId: string) => void
  onOpenStep: (workflowId: string, nodeId: string, path: string) => void
}) {
  const { live, done } = splitRuns(runs)
  const [limit, setLimit] = useState(RECENT_FINISHED_RUNS)
  const doneShown = done.slice(0, limit)
  const remaining = done.length - doneShown.length
  const renderRow = (row: KasRunRow, soleLive: boolean) => (
    <KasRunRowCompact
      key={row.workflowId}
      row={row}
      defaultExpanded={soleLive}
      onOpen={() => onOpen(row.workflowId)}
      onOpenStep={(nodeId, path) => onOpenStep(row.workflowId, nodeId, path)}
    />
  )
  return (
    <section className="flex flex-col gap-2" data-testid="kas-chat-runs">
      <div data-testid="kas-section-live">
        <SectionHeading label={i18nT('apps.workflows.kas.section_in_progress')} count={live.length} />
        {live.length === 0 ? (
          <p className="m-0 px-1.5 py-1 text-[11.5px] text-muted">{i18nT('apps.workflows.kas.nothing_running')}</p>
        ) : (
          <ul className="m-0 p-0 list-none flex flex-col" aria-label={i18nT('apps.workflows.kas.section_in_progress')}>
            {live.map(row => renderRow(row, live.length === 1))}
          </ul>
        )}
      </div>
      <div data-testid="kas-section-done">
        <SectionHeading label={i18nT('apps.workflows.kas.section_completed')} count={done.length} />
        {done.length === 0 ? (
          <p className="m-0 px-1.5 py-1 text-[11.5px] text-muted">{i18nT('apps.workflows.kas.nothing_finished')}</p>
        ) : (
          <>
            <ul className="m-0 p-0 list-none flex flex-col" aria-label={i18nT('apps.workflows.kas.section_completed')}>
              {doneShown.map(row => renderRow(row, false))}
            </ul>
            {remaining > 0 ? (
              <button
                type="button"
                className="mt-1 ml-1.5 bg-transparent border-none p-0 text-[11.5px] text-accent cursor-pointer hover:underline focus-visible:outline focus-visible:outline-2 focus-visible:outline-accent"
                onClick={() => setLimit(l => l + RECENT_FINISHED_RUNS)}
                data-testid="kas-load-more"
              >
                {i18nT('apps.workflows.kas.load_more', { count: remaining })}
              </button>
            ) : null}
          </>
        )}
      </div>
    </section>
  )
}

function SectionHeading({ label, count }: { label: string; count: number }) {
  return (
    <p className="m-0 px-1.5 py-1 text-[10.5px] font-semibold uppercase tracking-wide text-muted" data-testid="kas-section-heading">
      {label} ({count})
    </p>
  )
}

interface KasChatRunsProps {
  slotKey: string
  /** The Crew runs below, told whether KAS runs were shown (so an empty state can stand down). */
  children: (hasKasRuns: boolean) => ReactNode
}

export function KasChatRuns({ slotKey, children }: KasChatRunsProps) {
  return (
    <KasEngineGate fallback={children(false)}>
      <KasChatRunsInner slotKey={slotKey}>{children}</KasChatRunsInner>
    </KasEngineGate>
  )
}
