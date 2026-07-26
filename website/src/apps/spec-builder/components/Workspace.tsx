// Workspace — the non-creating view: specs rail (column 1) + selected spec
// detail (chat + docs, columns 2 & 3). With no specs yet, the main area carries
// the first-run empty state.
//
// Layout follows Issue Radar's shell: full-height flush columns separated by
// borders and drag handles, no page gutters and no floating cards, so every
// column header sits on the same line. The rail's width is owned here (the
// shared useColumnResize hook, same as Issue Radar's rail) and dragging past the
// minimum collapses it to an icon strip.
//
// The RAIL STAYS MOUNTED IN EVERY STATE (Issue Radar's LeftRail convention).
// It previously unmounted on the empty state, which took the app-identity
// footer and the Settings entry point with it — so a first-run user had no way
// to reach settings, and the layout jumped the moment the first spec appeared.
//
// Detail is mounted only for a spec that is actually in the list: a selection
// restored from localStorage for a spec that no longer exists used to make
// SpecDetail fetch it and surface a raw "not found" error banner before the
// list reconciled.
import { FileText, ArrowLeft } from 'lucide-react'
import type { SpecSummary } from '../api'
import {
  LS, loadRailWidth, loadRailCollapsed,
  MIN_RAIL_WIDTH, MAX_RAIL_WIDTH, COLLAPSED_RAIL_WIDTH,
} from '../api'
import { useColumnResize, type CollapseConfig } from '../../../hooks/useColumnResize'
import { Btn } from './shared'
import { EmptyState } from '../../../components/ui'
import SpecRail from './SpecRail'
import SpecDetail from './SpecDetail'
import ColumnSplitter from './ColumnSplitter'

import { i18nT } from '../../../i18n/t'
// Module-level so the hook's memoised resolver isn't invalidated every render.
const RAIL_COLLAPSE: CollapseConfig = {
  width: COLLAPSED_RAIL_WIDTH,
  storageKey: LS.railCollapsed,
}

export interface WorkspaceProps {
  specs: SpecSummary[]
  /** First-load flag, forwarded to the rail's skeleton. */
  loading?: boolean
  /** Opens settings from the rail footer. */
  onSettings?: () => void
  sel: string | null
  setSel: (name: string | null) => void
  setErr: (msg: string) => void
  onNew: () => void
}

export default function Workspace({ specs, sel, setSel, setErr, onNew, loading = false, onSettings }: WorkspaceProps) {
  const firstRun = specs.length === 0 && !loading
  const rail = useColumnResize(
    LS.railWidth, loadRailWidth, MIN_RAIL_WIDTH, MAX_RAIL_WIDTH, RAIL_COLLAPSE, loadRailCollapsed,
  )

  return (
    <div className="flex flex-1 min-h-0">
      <SpecRail
        specs={specs}
        sel={sel}
        setSel={setSel}
        onNew={onNew}
        loading={loading}
        onSettings={onSettings}
        width={rail.width}
        collapsed={rail.collapsed}
        onExpand={rail.expand}
      />

      {/* Drag handle on the rail's right edge. Present in every state, since the
          rail is; dragging well past the minimum collapses it. */}
      <ColumnSplitter
        handleProps={rail.handleProps}
        label={i18nT('apps.specBuilder.components.workspace.resize_spec_list')}
        valueNow={rail.width}
        valueMin={COLLAPSED_RAIL_WIDTH}
        valueMax={MAX_RAIL_WIDTH}
        onNudge={(d) => rail.nudge(d * 16)}
      />

      {firstRun ? (
        <div className="flex-1 min-w-0 flex flex-col items-center justify-center">
          <EmptyState
            icon={<FileText className="lucide-inline text-accent opacity-50" />}
            title={i18nT('apps.specBuilder.components.workspace.plan_your_next_feature_with_a_spec')}
            subtitle={i18nT('apps.specBuilder.components.workspace.describe_what_you_want_to_build_answer_a_few_que')}
          />
          <Btn label={i18nT('apps.specBuilder.components.workspace.start_your_first_spec')} primary big onClick={onNew} />
        </div>
      ) : sel && specs.some((s) => s.name === sel) ? (
        <SpecDetail key={sel} name={sel} setErr={setErr} />
      ) : (
        <div className="flex-1 min-w-0 flex items-center justify-center text-muted text-[13px] gap-1.5">
          <ArrowLeft className="lucide-inline" /> {i18nT('apps.specBuilder.components.workspace.pick_a_spec_to_continue_where_you_left_off')}
        </div>
      )}
    </div>
  )
}
