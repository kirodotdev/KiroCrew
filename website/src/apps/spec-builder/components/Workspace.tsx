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
import { FileText, ArrowLeft, ArrowUp } from 'lucide-react'
import type { SpecSummary } from '../api'
import {
  LS, loadRailWidth, loadRailCollapsed,
  MIN_RAIL_WIDTH, MAX_RAIL_WIDTH, COLLAPSED_RAIL_WIDTH,
} from '../api'
import { useColumnResize, type CollapseConfig } from '../../../hooks/useColumnResize'
import { useIsMobile } from '../../../hooks/useIsMobile'
import { Btn } from './shared'
import { EmptyState } from '../../../components/ui'
import SpecRail from './SpecRail'
import SpecDetail from './SpecDetail'
import ColumnSplitter from './ColumnSplitter'

import { i18nT } from '../../../i18n/t'
// Module-level so the hook's memoised resolver isn't invalidated every render.
// `whenNarrow`: rail + detail cannot share a phone — the rail's minimum is
// MIN_RAIL_WIDTH and the detail carries a chat column and a document, so the two
// become a drill-down instead. The contract requires the expanded rail to take
// the WHOLE viewport (see railFull below), or the strip's expand control just
// leads back into the squeeze it escaped.
const RAIL_COLLAPSE: CollapseConfig = {
  width: COLLAPSED_RAIL_WIDTH,
  storageKey: LS.railCollapsed,
  whenNarrow: true,
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
  const isMobile = useIsMobile()
  // Collapsed while narrow: the rail lies ACROSS THE TOP rather than down the
  // side, so the pane below owns the full viewport width. A left/right split has
  // to give the reading pane the whole width on a phone — horizontal is the only
  // axis with nothing to spare, and vertical room is what a phone can give.
  const railBar = isMobile && rail.collapsed
  // Expanded while narrow: the rail IS the page, so the detail steps aside.
  const railFull = isMobile && !rail.collapsed
  // Collapse on select — the third leg of the drill-down. Without it, picking a
  // spec from the full-width rail changes nothing visible: the rail keeps the
  // viewport and the detail stays hidden, while the drag handle that would
  // otherwise close it is gone on touch.
  const selectSpec = (name: string | null) => {
    setSel(name)
    if (isMobile) rail.collapse()
  }

  return (
    <div className={`flex flex-1 min-h-0 ${railBar ? 'flex-col' : ''}`}>
      <SpecRail
        specs={specs}
        sel={sel}
        setSel={selectSpec}
        onNew={onNew}
        loading={loading}
        onSettings={onSettings}
        width={railFull ? '100%' : rail.width}
        collapsed={rail.collapsed}
        horizontal={railBar}
        onExpand={rail.expand}
      />

      {/* Drag handle on the rail's right edge. Present in every state, since the
          rail is; dragging well past the minimum collapses it. Dropped on touch,
          where it costs width a phone has none of and does nothing. */}
      {!isMobile && (
      <ColumnSplitter
        handleProps={rail.handleProps}
        label={i18nT('apps.specBuilder.components.workspace.resize_spec_list')}
        valueNow={rail.width}
        valueMin={COLLAPSED_RAIL_WIDTH}
        valueMax={MAX_RAIL_WIDTH}
        onNudge={(d) => rail.nudge(d * 16)}
      />
      )}

      {/* HIDDEN, never unmounted, while the full-width rail is up. `null` here
          would discard a typed chat message and any staged review comments the
          moment the user opens the rail to look for another spec — SpecDetail
          owns both in local state, so unmounting is silent data loss. This is
          the same shape every other narrow shell in the repo uses. */}
      <div className={`flex-1 min-w-0 min-h-0 ${railFull ? 'hidden' : 'flex'}`}>
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
        <SpecDetail key={sel} name={sel} setErr={setErr} onDuplicated={selectSpec} />
      ) : (
        <div className="flex-1 min-w-0 flex items-center justify-center text-muted text-[13px] gap-1.5">
          {/* Points at where the list actually IS: beside this pane on a desktop,
              above it while narrow, where the rail is a bar. A left arrow there
              pointed at the screen edge. */}
          {isMobile
            ? <ArrowUp className="lucide-inline" />
            : <ArrowLeft className="lucide-inline" />}
          {' '}{i18nT('apps.specBuilder.components.workspace.pick_a_spec_to_continue_where_you_left_off')}
        </div>
      )}
      </div>
    </div>
  )
}
