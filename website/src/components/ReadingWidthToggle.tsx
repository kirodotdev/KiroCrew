import { FoldHorizontal, UnfoldHorizontal } from 'lucide-react'

import type { ReadingWidth } from '../hooks/useReadingWidth'

import { i18nT } from '../i18n/t'
import HoverTip from './HoverTip'

/**
 * Catalog KEY for the button's tooltip and accessible name.
 *
 * Keys, not strings: this table is evaluated at module load, so an `i18nT()`
 * call here would freeze the boot language and never re-resolve on a language
 * switch. It is indexed inline at the `i18nT()` call in the component, which
 * runs per render — and a flat `Record` of full literal keys is the form
 * `scripts/check-i18n-keys.mjs` can resolve statically.
 */
const TITLE_KEY: Record<ReadingWidth, string> = {
  md: 'components.readingWidthToggle.title_md',
  full: 'components.readingWidthToggle.title_full',
}

export default function ReadingWidthToggle({ value, onToggle }: { value: ReadingWidth; onToggle: () => void }) {
  // One lookup for both the tooltip and the accessible name — they are the same
  // string by design, so a translator can never make them disagree.
  const title = i18nT(TITLE_KEY[value])
  // The icon shows what a click does: arrows out widens a medium column, arrows
  // in narrows a full one.
  const Icon = value === 'full' ? FoldHorizontal : UnfoldHorizontal
  return (
    <HoverTip label={title}>
      <button type="button"
        className={`w-[26px] h-[26px] flex items-center justify-center rounded-md cursor-pointer border transition-all ${value === 'full' ? 'border-accent bg-accent-subtle text-accent' : 'border-border text-muted hover:text-text hover:border-border-strong'}`}
        onClick={onToggle}
        aria-label={title}
        aria-pressed={value === 'full'}
      ><Icon size={13} aria-hidden="true" /></button>
    </HoverTip>
  )
}
