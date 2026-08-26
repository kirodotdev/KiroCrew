// PipelineFlow — L0 of the global view: the pipeline itself, left to right.
//
// One card per step, with the three numbers an operator actually asks for: how
// many items are sitting in it now, how many it moved in the recent window, and
// how many it has moved in total. Between the cards, a connector whose thickness
// tracks recent flow, so a stalled hand-off reads as a thin line rather than as
// a number the eye has to compare.
//
// DRAWN IN HTML, NOT SVG, on purpose. A repo rule rejects inline vector markup in
// added lines (use lucide icons instead), and a six-step flow is a row of cards
// with connectors -- flexbox and borders express it directly, which also keeps it
// screen-reader legible and testable by role and text instead of by path geometry.
//
// COLOUR comes from theme CSS variables, never hardcoded hex (there is a
// lint:theme-colors gate).
//
// A step's `unit` is authoritative and differs per step: the early steps are
// batch jobs that open no session, so their unit is issues, while the working
// steps are counted in sessions. The card never guesses the noun.
import { Activity, AlertTriangle } from 'lucide-react'
import { Card, StatCard } from '../../../components/ui'
import { i18nT } from '../../../i18n/t'
import type { OverviewResponse, OverviewStep } from '../api'
import { formatRelativeTime } from '../lib/format'

const C = {
  card: 'var(--card)',
  cardHl: 'var(--card-hl)',
  border: 'var(--border)',
  text: 'var(--text)',
  dim: 'var(--text-dim)',
  accent: 'var(--accent)',
  warn: 'var(--warn)',
} as const

/** Localized label per step, as an EXPLICIT map rather than a computed key.
 *
 * Building the key by concatenation would make it invisible to the key-reference
 * gate -- a dynamic key cannot be proven to exist, which is why the repo reports
 * them separately -- so a typo or a removed catalog entry would ship as the raw
 * dotted key rendered into the UI. Spelling the keys out makes them checkable.
 *
 * A step the server adds later is not in this map, and falls back to the label the
 * server sent. That is deliberate: the step set is defined by the pipeline, not by
 * this file, and an unknown step should still render with a name.
 */
const STEP_LABEL_KEYS: Record<string, string> = {
  scan: 'apps.autoTriagePipeline.global.step.scan',
  triage: 'apps.autoTriagePipeline.global.step.triage',
  dispatch: 'apps.autoTriagePipeline.global.step.dispatch',
  implement: 'apps.autoTriagePipeline.global.step.implement',
  verify: 'apps.autoTriagePipeline.global.step.verify',
  cleanup: 'apps.autoTriagePipeline.global.step.cleanup',
}

export function stepLabel(step: Pick<OverviewStep, 'key' | 'label'>): string {
  const key = STEP_LABEL_KEYS[step.key]
  if (!key) return step.label || step.key
  const translated = i18nT(key)
  return translated === key ? step.label || step.key : translated
}

/** The noun for a step's counts, from the server's own unit field. */
export function unitLabel(step: OverviewStep): string {
  return step.unit === 'sessions'
    ? i18nT('apps.autoTriagePipeline.global.unit_sessions')
    : i18nT('apps.autoTriagePipeline.global.unit_issues')
}

/** Connector weight, 1-4, from a step's recent delivery relative to the busiest.
 *
 * Relative rather than absolute because the steps differ by orders of magnitude
 * (a scanner examines thousands while the implement step delivers tens); an
 * absolute scale would flatten every working step to the same hairline.
 */
export function flowWeight(recentDone: number, busiest: number): number {
  if (!Number.isFinite(recentDone) || recentDone <= 0) return 1
  if (!Number.isFinite(busiest) || busiest <= 0) return 1
  const share = recentDone / busiest
  if (share >= 0.66) return 4
  if (share >= 0.33) return 3
  // Both guards above already established a positive share, so this is the
  // remaining case rather than a fallback.
  return 2
}

function StepCard({
  step,
  active,
  onSelect,
}: {
  step: OverviewStep
  active: boolean
  onSelect: (key: string) => void
}) {
  const live = step.inFlight > 0
  return (
    <button
      type="button"
      onClick={() => onSelect(step.key)}
      aria-pressed={active}
      data-testid={`atp-step-${step.key}`}
      className="group relative flex w-full flex-1 flex-col gap-2 rounded-lg border p-3 text-left transition-colors sm:min-w-[9.5rem]"
      style={{
        background: active ? C.cardHl : C.card,
        borderColor: active ? C.accent : C.border,
      }}
    >
      <div className="flex items-center justify-between gap-2">
        <span
          className="text-[11px] font-semibold uppercase tracking-wide"
          style={{ color: C.text }}
        >
          {stepLabel(step)}
        </span>
        {live ? (
          <span
            aria-hidden="true"
            className="h-1.5 w-1.5 shrink-0 rounded-full motion-safe:animate-pulse"
            style={{ background: C.accent }}
          />
        ) : null}
      </div>

      <div className="flex items-baseline gap-1">
        <span className="text-2xl font-semibold tabular-nums leading-none" style={{ color: C.text }}>
          {step.inFlight}
        </span>
        <span className="text-[11px]" style={{ color: C.dim }}>
          {i18nT('apps.autoTriagePipeline.global.in_step', { unit: unitLabel(step) })}
        </span>
      </div>

      <dl className="grid grid-cols-2 gap-x-2 gap-y-0.5 text-[11px]" style={{ color: C.dim }}>
        <dt>{i18nT('apps.autoTriagePipeline.global.recent')}</dt>
        <dd className="text-right tabular-nums" style={{ color: C.text }}>
          {step.recentDone}
        </dd>
        <dt>{i18nT('apps.autoTriagePipeline.global.total')}</dt>
        <dd className="text-right tabular-nums" style={{ color: C.text }}>
          {step.distinctDone}
        </dd>
      </dl>
    </button>
  )
}

function Connector({ weight }: { weight: number }) {
  return (
    <div
      aria-hidden="true"
      className="hidden w-4 shrink-0 items-center justify-center self-stretch sm:flex"
    >
      <div
        className="w-full rounded-full"
        style={{ height: `${weight}px`, background: weight > 1 ? C.accent : C.border }}
      />
    </div>
  )
}

export default function PipelineFlow({
  overview,
  selectedStep,
  onSelectStep,
  nowMs,
}: {
  overview: OverviewResponse
  selectedStep: string | null
  onSelectStep: (key: string) => void
  nowMs: number
}) {
  const busiest = overview.steps.reduce((max, s) => Math.max(max, s.recentDone), 0)
  const totalInFlight = overview.steps.reduce((sum, s) => sum + s.inFlight, 0)
  const lastSeen = overview.lastEventAt
    ? formatRelativeTime(overview.lastEventAt, nowMs)
    : i18nT('apps.autoTriagePipeline.global.never')

  return (
    <div className="flex flex-col gap-3">
      <div className="grid grid-cols-2 gap-2 md:grid-cols-4">
        <StatCard
          label={i18nT('apps.autoTriagePipeline.global.stat_in_flight')}
          value={totalInFlight}
          accent={totalInFlight > 0}
        />
        <StatCard
          label={i18nT('apps.autoTriagePipeline.global.stat_last_activity')}
          value={lastSeen}
        />
        <StatCard
          label={i18nT('apps.autoTriagePipeline.global.stat_events')}
          value={overview.totalEvents}
        />
        <StatCard
          label={i18nT('apps.autoTriagePipeline.global.stat_window', {
            hours: overview.recentHours,
          })}
          value={overview.steps.reduce((sum, s) => sum + s.recentDone, 0)}
        />
      </div>

      <Card className="p-3">
        {/* Narrow-first. Six cards with a fixed minimum width are ~57rem of content,
            so on a 320px viewport the horizontal strip could only be reached by
            sideways panning -- and horizontal is the one axis a phone cannot spare.
            Below `sm` the steps stack vertically (each card full width, the
            connectors dropped because a vertical list already reads in order);
            from `sm` up the left-to-right flow returns, which is the shape that
            carries the pipeline's meaning on a wide screen. */}
        <div className="flex flex-col items-stretch gap-2 sm:flex-row sm:gap-0 sm:overflow-x-auto sm:pb-1">
          {overview.steps.map((step, index) => (
            <div key={step.key} className="flex items-stretch sm:flex-1">
              {index > 0 ? (
                <Connector weight={flowWeight(overview.steps[index - 1].recentDone, busiest)} />
              ) : null}
              <StepCard
                step={step}
                active={selectedStep === step.key}
                onSelect={onSelectStep}
              />
            </div>
          ))}
        </div>

        <p className="mt-2 flex items-center gap-1.5 text-[11px]" style={{ color: C.dim }}>
          <Activity aria-hidden="true" className="h-3 w-3" />
          {i18nT('apps.autoTriagePipeline.global.flow_hint')}
        </p>

        {overview.unparseable > 0 ? (
          <p
            className="mt-1 flex items-center gap-1.5 text-[11px]"
            style={{ color: C.warn }}
            data-testid="atp-unparseable"
          >
            <AlertTriangle aria-hidden="true" className="h-3 w-3" />
            {i18nT('apps.autoTriagePipeline.global.unparseable', {
              count: overview.unparseable,
            })}
          </p>
        ) : null}
      </Card>
    </div>
  )
}
