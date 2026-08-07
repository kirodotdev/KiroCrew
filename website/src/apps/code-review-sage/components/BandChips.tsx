// The band FILTER row above a Focus Report's row list.
//
// Three severity bands (red → yellow → green) plus an "All" reset, rendered as
// toggle chips. Selecting a band narrows the list below; "All" clears the
// filter. Each chip is a real <button> carrying `aria-pressed` so assistive tech
// announces the current filter state (the visual tint alone is not enough).
//
// Wording mirrors the backend report (sage_lib/report.py): red = "needs review",
// yellow = "worth a glance", green = "clean" — so the chip labels read the same
// as the artifact page a shared copy links to.
import { Circle } from 'lucide-react'
import type { Band, ReportBands } from '../lib/types'

import { i18nT } from '../../../i18n/t'
/** Per-band chip metadata. Class strings are static (not interpolated) so
 * Tailwind's content scan keeps them. `on` = selected treatment, `off` = the
 * quiet resting treatment.
 *
 * The label is held as a KEY, not a translated string: this table is
 * module-level, so it evaluates once at import time — before any language
 * switch — and a resolved string here would freeze the first locale for the
 * process. The key is resolved per render instead. */
/** Label keys as full literals in one indexable map, so the key-resolution
 *  gate can verify each one exists. Indexed at the call site, not read off a
 *  local — that indirection is what made these sites unverifiable. */
const BAND_LABEL_KEY: Record<Band, string> = {
  red: 'apps.codeReviewSage.components.bandChips.needs_review',
  yellow: 'apps.codeReviewSage.components.bandChips.worth_a_glance',
  green: 'apps.codeReviewSage.components.bandChips.clean',
}

const BAND_META: Record<Band, { dot: string; on: string; off: string }> = {
  red: {
    dot: 'text-danger',
    on: 'bg-danger-subtle text-danger border-danger',
    off: 'bg-card text-muted border-border hover:text-text',
  },
  yellow: {
    dot: 'text-warn',
    on: 'bg-warn-subtle text-warn border-warn',
    off: 'bg-card text-muted border-border hover:text-text',
  },
  green: {
    dot: 'text-ok',
    on: 'bg-ok-subtle text-ok border-ok',
    off: 'bg-card text-muted border-border hover:text-text',
  },
}

const BAND_ORDER: Band[] = ['red', 'yellow', 'green']

/** A filter chip. The button is the interactive surface; `aria-pressed` carries
 * the selected state for screen readers. */
function Chip({
  label, count, dot, selected, on, off, onClick,
}: {
  label: string
  count: number
  dot?: string
  selected: boolean
  on: string
  off: string
  onClick: () => void
}) {
  return (
    <button
      type="button"
      aria-pressed={selected}
      onClick={onClick}
      className={
        'inline-flex items-center gap-1.5 rounded-full border px-3 py-1 text-[12px] '
        + 'font-medium cursor-pointer transition-colors focus:outline-none '
        + 'focus-visible:ring-1 focus-visible:ring-accent/40 '
        + (selected ? on : off)
      }
    >
      {dot && <Circle size={8} className={`${dot} fill-current`} aria-hidden="true" />}
      <span>{label}</span>
      <span className="tabular-nums opacity-70">{count}</span>
    </button>
  )
}

/** The band filter row. `active` defaults to 'all' (no narrowing). */
export default function BandChips({
  bands, active = 'all', onSelect,
}: {
  bands: ReportBands
  active?: Band | 'all'
  onSelect?: (b: Band | 'all') => void
}) {
  const total = bands.red + bands.yellow + bands.green
  const select = (b: Band | 'all') => onSelect?.(b)
  return (
    <div className="flex flex-wrap items-center gap-2" role="group" aria-label={i18nT('apps.codeReviewSage.components.bandChips.filter_by_band')}>
      <Chip
        label={i18nT('apps.codeReviewSage.components.bandChips.all')}
        count={total}
        selected={active === 'all'}
        on="bg-accent-subtle text-accent border-accent"
        off="bg-card text-muted border-border hover:text-text"
        onClick={() => select('all')}
      />
      {BAND_ORDER.map((band) => {
        const meta = BAND_META[band]
        return (
          <Chip
            key={band}
            label={i18nT(BAND_LABEL_KEY[band])}
            count={bands[band]}
            dot={meta.dot}
            selected={active === band}
            on={meta.on}
            off={meta.off}
            onClick={() => select(band)}
          />
        )
      })}
    </div>
  )
}
