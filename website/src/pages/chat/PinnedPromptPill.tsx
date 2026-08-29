import { Pin } from 'lucide-react'
import { i18nT } from '../../i18n/t'

interface PinnedPromptPillProps {
  /** Restore the full card (`PinnedPrompt`). */
  onRestore: () => void
}

/**
 * The minimized form of the pinned-prompt banner: a chip at the transcript's
 * top-LEFT, which restores the card.
 *
 * A chip rather than a smaller card because ChatPage HIDES the pinned prompt's
 * transcript row while the card stands in for it — shrink the card and it stops
 * covering the row it hides, so the message disappears. Minimized therefore behaves
 * like `pinLastPrompt: false` (no band, no hand-off, row visible) plus this way
 * back, which is also why none of the card's geometry needed changing.
 * Icon PLUS text, matching the product's other minimize-to-chip
 * (`ComputerUseLiveView`, which pairs its glyph with a visible "Desktop"): the
 * minimized state persists across sessions, so a returning user meets this chip with
 * no other trace of the feature, and a tooltip cannot label it on touch.
 * Left rather than at the card's right edge, and the asymmetry is the reason: the
 * card covers a row ChatPage has HIDDEN, so overlap costs nothing, while the chip
 * covers a VISIBLE row whose action toolbar is right-aligned and, on touch, always
 * shown. Measured at 390px, a right-aligned chip covers that toolbar's timestamp
 * outright; left it clips only the leading button's padding.
 * `ComputerUseLiveView`'s chip is likewise left. Not hover-gated: a hover-only
 * control on a touch screen would make minimizing a one-way trip.
 */
export default function PinnedPromptPill({ onRestore }: PinnedPromptPillProps) {
  return (
    <div
      className="relative px-4 py-1 mx-auto w-full pointer-events-none flex items-start justify-start"
      style={{ maxWidth: 'var(--mc-content-width, 900px)' }}
    >
      <button
        type="button"
        data-testid="pinned-prompt-pill"
        onClick={onRestore}
        aria-label={i18nT('pages.chat.pinnedPrompt.restore_pinned_prompt')}
        title={i18nT('pages.chat.pinnedPrompt.restore_pinned_prompt')}
        className="pointer-events-auto flex items-center gap-2 px-3 py-2 rounded-full border border-border bg-card shadow-sm hover:bg-bg-hover transition-colors cursor-pointer"
      >
        <Pin className="lucide-inline text-muted" />
        <span className="text-[12px] font-medium text-text">{i18nT('pages.chat.pinnedPrompt.pinned_turn')}</span>
      </button>
    </div>
  )
}
