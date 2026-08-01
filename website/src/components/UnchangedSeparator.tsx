import { i18nT } from '../i18n/t'

/** "N unchanged lines" divider shown where a diff skips an unmodified stretch
 *  between hunks (replaces the raw `@@` header — the gutter line numbers carry
 *  the position). Shared by the chat DiffBlock and the Changes panel DiffView.
 *  Callers decide WHEN to render it (first hunk / zero gaps render nothing).
 *
 *  The flanking zigzag rules are `.zigzag-rule` (index.css): a tiling SVG
 *  data-URI painted as a CSS mask over currentColor — the BrandGlyph pattern —
 *  so the color follows the theme's hunk tint and no SVG element lives in TSX. */
export default function UnchangedSeparator({ count }: { count: number }) {
  return (
    <div className="flex items-center gap-2 px-3 py-1.5 select-none">
      <span className="zigzag-rule text-diff-hunk-text/50" aria-hidden="true" />
      <span className="text-[11px] whitespace-nowrap px-2 rounded-full bg-diff-hunk text-diff-hunk-text">
        {i18nT('components.diffBlock.unchanged_lines', { count })}
      </span>
      <span className="zigzag-rule text-diff-hunk-text/50" aria-hidden="true" />
    </div>
  )
}
