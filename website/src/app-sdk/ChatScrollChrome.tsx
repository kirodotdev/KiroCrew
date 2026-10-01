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
 *     except for the pill itself. `placement="inline"` is for a host that
 *     reserves the pill's space instead: a 36px row in normal flow, which the
 *     main chat puts at the top of its composer dock while its scroller ends
 *     above the dock, so the pill never sits over transcript text.
 *
 */
import { ArrowDown } from 'lucide-react'
import { i18nT } from '../i18n/t'
import { Glass } from '../components/Glass'

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

export function JumpToBottomButton({ visible, onClick, placement = 'floating' }: {
  visible: boolean
  onClick: () => void
  /** `floating` (default): hangs 40px above the positioned wrapper it is mounted
   *  in, over whatever scrolls there. `inline`: a 36px row in normal flow, for a
   *  host that reserves the pill's space instead (the main chat's composer
   *  dock, whose scroller ends above the dock while the pill shows, so the
   *  pill never sits over transcript text). */
  placement?: 'floating' | 'inline'
}) {
  if (!visible) return null
  const pill = (
    /* A glass pill (components/Glass.tsx, chip variant) rendered as the
       button: the same material as the composer it sits above. */
    <Glass
      as="button"
      variant="chip"
      radius={16}
      className="w-8 h-8 rounded-full flex items-center justify-center cursor-pointer pointer-events-auto transition-all duration-200 glass-hover text-text active:scale-95 active:duration-75 shadow-md"
      onClick={onClick}
      aria-label={i18nT('pages.chatPage.scroll_to_bottom')}
    ><ArrowDown size={14} strokeWidth={2.5} /></Glass>
  )
  if (placement === 'inline') {
    return (
      <div className="flex h-9 items-center justify-center pointer-events-none" data-testid="jump-to-bottom-row">
        {pill}
      </div>
    )
  }
  return (
    <div className="absolute -top-10 inset-x-0 z-10 pointer-events-none flex justify-center">
      {pill}
    </div>
  )
}
