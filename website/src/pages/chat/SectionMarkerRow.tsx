/**
 * A labelled chapter break in the transcript — the `section_marker` row.
 *
 * ONE component for BOTH transcript surfaces, which is load-bearing: their
 * unknown-role fallbacks differ (one draws an assistant bubble, the other draws
 * nothing), so sharing this keeps them from drifting once both claim the role.
 *
 * Prefers `meta.label`; `content` is only the older-client fallback.
 */
import { memo } from 'react'
import { i18nT } from '../../i18n/t'
import { useLanguageGeneration } from '../../i18n/useLanguageGeneration'

export default memo(function SectionMarkerRow({
  label,
  fallback = '',
}: {
  // `unknown`, not `string`: both call sites CAST these off a PERSISTED row, and a
  // cast erases nothing, so a number, object or array really can arrive here.
  label?: unknown
  fallback?: unknown
}) {
  useLanguageGeneration() // memo() bails out of the provider-level repaint; subscribe directly
  const raw = typeof label === 'string' ? label.trim() : ''
  const spare = typeof fallback === 'string' ? fallback.trim() : ''
  // `label === undefined` means the row carries no `meta` at all (an older
  // client), which is the ONLY case the applier's raw English text should draw.
  const caption = raw
    ? i18nT('pages.chat.sectionMarkerRow.end_of', { label: raw })
    : label === undefined
      ? spare
      : i18nT('pages.chat.sectionMarkerRow.end_of_section')
  return (
    <div
      className="self-center w-full max-w-full min-w-0 flex items-center gap-2 py-1 animate-scale-in"
      role="separator"
      // Named separator: without this the label is a floating text node beside
      // an unnamed rule. Absent when empty, so no nameless accessible name.
      aria-label={caption || undefined}
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
      <span className="flex-1 h-px bg-border" aria-hidden="true" />
    </div>
  )
})
