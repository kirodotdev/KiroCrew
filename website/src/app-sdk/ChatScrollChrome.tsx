/**
 * ChatScrollChrome — shared visual chrome for a chat transcript scroller:
 * the top/bottom edge fades and the jump-to-bottom pill. Extracted from the
 * main chat page so every chat surface (ChatPane split panes, the Crew
 * Members thread, embeds) wears the same edges instead of hand-rolling them.
 *
 * Layout contract (matches how the main chat mounts its own copies):
 *   - <EdgeFade side="top">  goes in a zero-height `relative` wrapper placed
 *     BETWEEN the header and the scroller; it overlays the scroller's first
 *     24px so content dissolves under the header edge instead of clipping.
 *   - <EdgeFade side="bottom"> goes directly AFTER the scroller; its in-flow
 *     height is cancelled with a negative top margin so it overlays the
 *     scroller's last 24px above the composer.
 *   - <JumpToBottomButton> goes inside a `relative` wrapper around the
 *     composer block; it floats 40px above it, centred, and is pointer-inert
 *     except for the pill itself.
 *
 */
import { ArrowDown } from 'lucide-react'
import { i18nT } from '../i18n/t'

export function EdgeFade({ side, anchor = 'overlay' }: {
  side: 'top' | 'bottom'
  /** Top-fade anchoring: 'overlay' hangs 24px into a following scroller from a
   *  zero-height `relative` wrapper placed before it; 'below' hangs off the
   *  BOTTOM edge of the positioned element it is mounted inside (the main
   *  chat's opaque header row). Same gradient, two real mounting sites. */
  anchor?: 'overlay' | 'below'
}) {
  if (side === 'top') {
    return (
      <div aria-hidden className={`absolute ${anchor === 'below' ? 'top-full' : 'top-0'} inset-x-0 h-6 bg-gradient-to-b from-bg to-transparent pointer-events-none`} />
    )
  }
  return (
    <div aria-hidden className="h-6 -mt-6 bg-gradient-to-t from-bg to-transparent pointer-events-none relative z-[1]" />
  )
}

export function JumpToBottomButton({ visible, onClick }: {
  visible: boolean
  onClick: () => void
}) {
  if (!visible) return null
  return (
    <div className="absolute -top-10 inset-x-0 z-10 pointer-events-none flex justify-center">
      <button
        className="w-8 h-8 rounded-full flex items-center justify-center cursor-pointer pointer-events-auto transition-all duration-200 bg-bg-elevated border border-border-strong text-text hover:bg-bg-hover hover:border-accent hover:scale-[1.06] active:scale-95 active:duration-75 shadow-md"
        onClick={onClick}
        aria-label={i18nT('pages.chatPage.scroll_to_bottom')}
      ><ArrowDown size={14} strokeWidth={2.5} /></button>
    </div>
  )
}
