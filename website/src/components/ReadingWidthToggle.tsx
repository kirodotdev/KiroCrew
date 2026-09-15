import type { ReadingWidth } from '../hooks/useReadingWidth'

import { i18nT } from '../i18n/t'

/**
 * Catalog KEY for the button's glyph and its tooltip.
 *
 * Keys, not strings: these tables are evaluated at module load, so an `i18nT()`
 * call here would freeze the boot language and never re-resolve on a language
 * switch. Both are indexed inline at the `i18nT()` call in the component, which
 * runs per render — and a flat `Record` of full literal keys is the form
 * `scripts/check-i18n-keys.mjs` can resolve statically.
 *
 * The GLYPH is translatable, not decorative: it is the initial of the width's
 * name ('M' for Medium, 'F' for Full), so a locale whose names start elsewhere
 * needs its own character. It is one glyph wide in a 26px square button, so a
 * translation must stay a single character.
 */
const LABEL_KEY: Record<ReadingWidth, string> = {
  md: 'components.readingWidthToggle.label_md',
  full: 'components.readingWidthToggle.label_full',
}
const TITLE_KEY: Record<ReadingWidth, string> = {
  md: 'components.readingWidthToggle.title_md',
  full: 'components.readingWidthToggle.title_full',
}

export default function ReadingWidthToggle({ value, onToggle }: { value: ReadingWidth; onToggle: () => void }) {
  // One lookup for both the tooltip and the accessible name — they are the same
  // string by design, so a translator can never make them disagree.
  const title = i18nT(TITLE_KEY[value])
  return (
    <button type="button"
      className={`w-[26px] h-[26px] flex items-center justify-center rounded-md text-[11px] font-medium cursor-pointer border transition-all ${value === 'full' ? 'border-accent bg-accent-subtle text-accent' : 'border-border text-muted hover:text-text hover:border-border-strong'}`}
      onClick={onToggle}
      title={title}
      aria-label={title}
      aria-pressed={value === 'full'}
    >{i18nT(LABEL_KEY[value])}</button>
  )
}
