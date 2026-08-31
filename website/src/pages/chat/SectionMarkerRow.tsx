/**
 * A labelled chapter break in the transcript — the `section_marker` row.
 *
 * ONE component for BOTH transcript surfaces, which is load-bearing: their
 * unknown-role fallbacks differ (one draws an assistant bubble, the other draws
 * nothing), so sharing this keeps them from drifting once both claim the role.
 *
 * Prefers `meta.label`; `content` is only the older-client fallback. Every break also
 * draws the row's own time: a label repeats when one unit of work is marked twice.
 *
 * No dismiss affordance, deliberately: nothing in this transcript can be hidden or
 * deleted — there is no per-row delete route and no client action for one — so a
 * local-only hide would return on the next reload rather than dismissing anything.
 */
import { memo } from 'react'
import { i18nT } from '../../i18n/t'
import { useLanguageGeneration } from '../../i18n/useLanguageGeneration'

export default memo(function SectionMarkerRow({
  label,
  fallback = '',
  time,
  timeTitle,
}: {
  // `unknown`, not `string`: both call sites CAST these off a PERSISTED row, and a
  // cast erases nothing, so a number, object or array really can arrive here.
  label?: unknown
  fallback?: unknown
  time?: unknown
  timeTitle?: unknown
}) {
  useLanguageGeneration() // memo() bails out of the provider-level repaint; subscribe directly
  const raw = typeof label === 'string' ? label.trim() : ''
  const spare = typeof fallback === 'string' ? fallback.trim() : ''
  // `label === undefined` means the row carries no `meta` at all (an older
  // client), which is the ONLY case the applier's raw English text should draw.
  // A row with neither reads as a rendering artifact and is nameless to a screen
  // reader, so the generic caption is the floor rather than an empty one.
  const caption = raw
    ? i18nT('pages.chat.sectionMarkerRow.end_of', { label: raw })
    : label === undefined && spare
      ? spare
      : i18nT('pages.chat.sectionMarkerRow.end_of_section')
  // Every break carries the time, labelled or not: a label repeats whenever the same
  // unit of work is marked twice, so the stamp alone separates them — hence equal weight.
  const stamp = typeof time === 'string' ? time.trim() : ''
  const stampTitle = typeof timeTitle === 'string' ? timeTitle.trim() : ''
  const name = [caption, stamp].filter(Boolean).join(' · ')
  return (
    <div
      className="self-center w-full max-w-full min-w-0 flex items-center gap-2 py-1 animate-scale-in"
      role="separator"
      // Named separator: without this the label is a floating text node beside
      // an unnamed rule. Absent when empty, so no nameless accessible name.
      aria-label={name || undefined}
      data-testid="section-marker-row"
    >
      <span className="flex-1 h-px bg-border" aria-hidden="true" />
      {caption && (
        // Not uppercased: text-transform is a no-op on CJK, so it would style
        // some languages and not others.
        // No `shrink-0`: a flex item that cannot shrink sizes to max-content, so
        // `break-words` would never get a constrained box and a long label overflows.
        <span className="min-w-0 break-words text-[11.5px] font-semibold text-muted">
          {caption}
        </span>
      )}
      {stamp && (
        <span
          className="shrink-0 text-[11.5px] font-semibold text-muted tabular-nums"
          data-testid="section-marker-time"
          title={stampTitle || undefined}
        >
          {stamp}
        </span>
      )}
      <span className="flex-1 h-px bg-border" aria-hidden="true" />
    </div>
  )
})
