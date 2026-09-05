// SpecRail — the specs column. Expanded: New-spec button, filter, and
// ACTIVE / PLAN READY groups with pulsing running dots, app identity + settings
// pinned at the bottom. Collapsed: a narrow icon strip with expand + new + a
// running indicator.
//
// Geometry is owned by Workspace (Issue Radar's LeftRail convention): the width
// arrives as a prop, the drag handle lives on the column's right edge, and
// dragging past the minimum collapses the column to the strip. The rail is a
// flush panel separated by a border rather than a floating block, so its edge
// lines up with the columns beside it.
import { useState } from 'react'
import { motion } from 'framer-motion'
import { ChevronsRight, Plus, FileText, Settings } from 'lucide-react'
import type { SpecSummary } from '../api'
import { phaseLabel as phaseLabelFor, PHASE_BUILDING_KEY, PHASE_READY_KEY, APP_VERSION } from '../api'
import { ACCENT, SEL_BG, SEL_BORDER, PULSE_MOTION, Btn } from './shared'
import { SearchInput } from '../../../components/ui'
import { SpecListSkeleton } from './Shimmer'
import Clickable from '../../../components/Clickable'

import { i18nT } from '../../../i18n/t'
export interface SpecRailProps {
  specs: SpecSummary[]
  sel: string | null
  setSel: (name: string) => void
  onNew: () => void
  /** True while the first specs fetch is in flight — drives the skeleton. */
  loading?: boolean
  /** Opens the settings modal from the rail footer. */
  onSettings?: () => void
  /** Current column width in px (or the collapsed strip width). A CSS length
   * string is used for the full-width narrow-viewport rail. */
  width: number | string
  /** True when the column is showing its icon strip. */
  collapsed?: boolean
  /** Re-open a collapsed rail at its last dragged width. */
  onExpand?: () => void
  /** Only meaningful with `collapsed`: lay the strip across the TOP instead of
   * down the left edge, so the pane below owns the full viewport width. Set
   * while narrow, where horizontal is the one axis with nothing to spare. */
  horizontal?: boolean
}

export default function SpecRail({
  specs, sel, setSel, onNew, loading = false, onSettings, width, collapsed = false, onExpand,
  horizontal = false,
}: SpecRailProps) {
  const [filter, setFilter] = useState('')

  if (collapsed) {
    const anyRunning = specs.some((s) => s.running)
    if (horizontal) {
      return (
        <aside className="w-full shrink-0 flex items-center gap-2 px-2 h-[46px] border-b border-border bg-bg-elevated/30">
          {/* Identity doubles as the expand control, the way the vertical card
              does — the actions keep their own hit targets on the right. */}
          <button
            type="button"
            onClick={onExpand}
            title={i18nT('apps.specBuilder.components.specRail.show_specs')}
            aria-label={i18nT('apps.specBuilder.components.specRail.show_spec_list')}
            className="min-w-0 flex-1 flex items-center gap-2 h-9 px-1.5 rounded-md cursor-pointer bg-transparent border-none text-muted hover:text-text hover:bg-bg-hover transition-colors focus-ring"
          >
            <FileText className="lucide-inline text-accent shrink-0" />
            <span className="min-w-0 truncate text-[13px] font-semibold text-text">
              {i18nT('apps.specBuilder.components.specRail.spec_builder')}
            </span>
            <ChevronsRight className="lucide-inline ml-auto shrink-0" />
          </button>
          <span aria-live="polite" className="shrink-0 flex items-center">
            {anyRunning && (
              <motion.span
                title={i18nT('apps.specBuilder.components.specRail.agent_working')}
                aria-label={i18nT('apps.specBuilder.components.specRail.an_agent_is_working')}
                className="w-2 h-2 rounded-full block"
                style={{ background: ACCENT }}
                {...PULSE_MOTION}
              />
            )}
          </span>
          <Btn
            onClick={onNew}
            primary
            title={i18nT('apps.specBuilder.components.specRail.new_spec')}
            ariaLabel={i18nT('apps.specBuilder.components.specRail.new_spec')}
            label={<Plus className="lucide-inline" />}
          />
          {/* Settings is NOT here. AUTOSDE's max-two-buttons-per-row is blocking,
              and the bar already spends its two on expand and new — the vertical
              strip stacks the same three, which the rule does not govern. Settings
              lives in the expanded rail's footer, one tap away through expand. */}
        </aside>
      )
    }
    return (
      <aside
        style={{ width }}
        className="shrink-0 flex flex-col items-center gap-2 pt-2.5 border-r border-border bg-bg-elevated/30"
      >
        <Btn
          onClick={onExpand}
          title={i18nT('apps.specBuilder.components.specRail.show_specs')}
          ariaLabel={i18nT('apps.specBuilder.components.specRail.show_spec_list')}
          label={<ChevronsRight className="lucide-inline" />}
        />
        <Btn
          onClick={onNew}
          primary
          title={i18nT('apps.specBuilder.components.specRail.new_spec')}
          ariaLabel={i18nT('apps.specBuilder.components.specRail.new_spec')}
          label={<Plus className="lucide-inline" />}
        />
        <span aria-live="polite">
          {anyRunning && (
            <motion.span
              title={i18nT('apps.specBuilder.components.specRail.agent_working')}
              aria-label={i18nT('apps.specBuilder.components.specRail.an_agent_is_working')}
              className="w-2 h-2 rounded-full block"
              style={{ background: ACCENT }}
              {...PULSE_MOTION}
            />
          )}
        </span>
        {/* Identity stays reachable collapsed, as it is expanded. */}
        <span className="mt-auto pb-3 flex flex-col items-center gap-2">
          {onSettings && (
            <Btn
              onClick={onSettings}
              title={i18nT('apps.specBuilder.components.specRail.settings')}
              ariaLabel={i18nT('apps.specBuilder.components.specRail.spec_builder_settings')}
              label={<Settings className="lucide-inline" />}
            />
          )}
          <FileText className="lucide-inline text-accent shrink-0" />
        </span>
      </aside>
    )
  }

  const needle = filter.toLowerCase()
  const match = (s: SpecSummary) => (
    s.name.toLowerCase().includes(needle)
    || (s.title ?? '').toLowerCase().includes(needle)
  )
  const working = specs.filter((s) => match(s) && !s.archived)
  const active = working.filter((s) => s.phase !== 'tasks')
  const ready = working.filter((s) => s.phase === 'tasks')
  const archived = specs.filter((s) => match(s) && s.archived)

  const groupHeader = (label: string, n: number) => (
    <div
      key={label}
      className="flex items-center gap-2 mx-1 mt-3.5 mb-1.5 text-[11px] font-bold text-muted"
      style={{ letterSpacing: '.08em' }}
    >
      <span>{label}</span>
      <span className="flex-1 h-px bg-border" />
      <span>{String(n)}</span>
    </div>
  )

  const row = (s: SpecSummary) => {
    const selected = sel === s.name
    const phaseLabel = s.status === 'executing'
      ? i18nT(PHASE_BUILDING_KEY)
      : s.phase === 'tasks' ? i18nT(PHASE_READY_KEY) : phaseLabelFor(s.phase)
    const dotStyle = {
      background: s.phase === 'tasks' && !s.running ? 'var(--ok)' : ACCENT,
      opacity: s.phase === 'tasks' || s.running ? 1 : 0.55,
    }
    return (
      <Clickable
        key={s.name}
        onClick={() => setSel(s.name)}
        aria-current={selected || undefined}
        aria-label={(s.title || s.name) + ' — ' + phaseLabel}
        className="flex items-center gap-2 px-2.5 py-2 rounded-lg cursor-pointer mb-0.5 focus-ring"
        style={{
          background: selected ? SEL_BG : 'transparent',
          border: '1px solid ' + (selected ? SEL_BORDER : 'transparent'),
        }}
      >
        {s.running
          ? <motion.span className="w-[7px] h-[7px] rounded-full shrink-0" style={dotStyle} {...PULSE_MOTION} />
          : <span className="w-[7px] h-[7px] rounded-full shrink-0" style={dotStyle} />}
        <span className="text-[13px] font-medium overflow-hidden text-ellipsis whitespace-nowrap text-text flex-1 min-w-0">
          {s.title || s.name}
        </span>
        <span
          className="text-[11px] font-mono whitespace-nowrap"
          style={{ color: s.status === 'executing' ? 'var(--ok)' : 'var(--muted)' }}
        >
          {phaseLabel}
        </span>
      </Clickable>
    )
  }

  return (
    <aside
      style={{ width }}
      className="shrink-0 min-h-0 flex flex-col border-r border-border bg-bg-elevated/30"
    >
      {/* Header band: same height and bottom border as the columns beside it, so
          all three column headers sit on one line. */}
      <div className="shrink-0 px-2.5 py-2 border-b border-border">
        <Btn label={<><Plus className="lucide-inline" /> {i18nT('apps.specBuilder.components.specRail.new_spec')}</>} primary onClick={onNew} />
      </div>

      {/* Scroll region: only the list scrolls, so the header and the identity
          footer stay put (Issue Radar's list column does the same). */}
      <div className="flex-1 min-h-0 overflow-y-auto px-2 pt-2">
        <div className="flex items-center gap-2 mx-1 mb-1 text-[11px] font-bold text-muted" style={{ letterSpacing: '.08em' }}>
          <span>{i18nT('apps.specBuilder.components.specRail.specs')}</span>
          <span className="flex-1" />
          <span>{String(specs.length)}</span>
        </div>
        <SearchInput
          value={filter}
          onChange={(e) => setFilter(e.target.value)}
          placeholder={i18nT('apps.specBuilder.components.specRail.filter_specs')}
          aria-label={i18nT('apps.specBuilder.components.specRail.filter_specs_by_name')}
          className="mb-1"
        />
        {/* First load: skeleton rows that hold the list's shape (Issue Radar's
            layout-continuity pattern) rather than an empty rail or a spinner. */}
        {loading && specs.length === 0 ? (
          <SpecListSkeleton />
        ) : (
          <>
            {active.length > 0 && groupHeader(i18nT('apps.specBuilder.components.specRail.active'), active.length)}
            {active.map(row)}
            {ready.length > 0 && groupHeader(i18nT('apps.specBuilder.components.specRail.plan_ready'), ready.length)}
            {ready.map(row)}
            {archived.length > 0 && groupHeader(i18nT('apps.meetings.review.archivedSection'), archived.length)}
            {archived.map(row)}
            {specs.length > 0 && active.length === 0 && ready.length === 0 && archived.length === 0 && (
              <div className="px-2.5 py-6 text-[13px] text-muted text-center leading-relaxed">
                {i18nT('apps.specBuilder.components.specRail.no_matching_specs')}
              </div>
            )}
          </>
        )}
      </div>

      {/* App identity pinned at the bottom, with Settings reachable from the
          rail — both conventions taken from Issue Radar's LeftRail, which ends
          in an icon + name + version line and keeps Settings in the rail rather
          than hiding it behind a one-off entry point. */}
      <div className="shrink-0 border-t border-border px-3 py-2.5 flex items-center gap-2">
        <FileText className="lucide-inline text-accent shrink-0" />
        <span className="text-[13px] font-medium text-text">{i18nT('apps.specBuilder.components.specRail.spec_builder')}</span>
        <span className="text-[12px] text-muted opacity-70">{i18nT('apps.specBuilder.components.specRail.v')}{APP_VERSION}</span>
        <span className="flex-1" />
        {onSettings && (
          <Btn
            onClick={onSettings}
            title={i18nT('apps.specBuilder.components.specRail.settings')}
            ariaLabel={i18nT('apps.specBuilder.components.specRail.spec_builder_settings')}
            label={<Settings className="lucide-inline" />}
          />
        )}
      </div>
    </aside>
  )
}
