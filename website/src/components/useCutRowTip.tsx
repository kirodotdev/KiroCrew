import { useState, type ReactNode } from 'react'

import { InstantTip, keyboardFocused, useInstantTip } from './InstantTip'

/** Whether a one-line `truncate` text inside `row` is cut off right now. */
function hasCutText(row: Element): boolean {
  return Array.from(row.querySelectorAll<HTMLElement>('.truncate')).some(el => el.scrollWidth > el.clientWidth)
}

interface RowHandlers {
  onPointerEnter: React.PointerEventHandler<HTMLElement>
  onPointerDown: React.PointerEventHandler<HTMLElement>
  onPointerUp: React.PointerEventHandler<HTMLElement>
  onMouseEnter: React.MouseEventHandler<HTMLElement>
  onMouseLeave: React.MouseEventHandler<HTMLElement>
  onFocus: React.FocusEventHandler<HTMLElement>
  onBlur: React.FocusEventHandler<HTMLElement>
}

/**
 * One hover/focus bubble for a list whose rows hold one-line texts that can be
 * cut off. It opens only on a row where a `truncate` line is cut, so a row that
 * fits shows nothing as the pointer travels down the list. `full` renders a
 * row's texts in full. Spread `rowHandlers(row)` on each row element and render
 * `bubble` once. No `aria-describedby`: the cut is visual only, so the row's
 * accessible text is already complete.
 */
export function useCutRowTip<T>(full: (row: T) => ReactNode): {
  rowHandlers: (row: T) => RowHandlers
  bubble: ReactNode
} {
  const { tip, tipHandlers, tipId } = useInstantTip()
  const [shown, setShown] = useState<T | null>(null)
  return {
    rowHandlers: row => ({
      onPointerEnter: tipHandlers.onPointerEnter,
      onPointerDown: tipHandlers.onPointerDown,
      onPointerUp: tipHandlers.onPointerUp,
      onMouseEnter: e => {
        if (!hasCutText(e.currentTarget)) return
        setShown(row)
        tipHandlers.onMouseEnter(e)
      },
      onMouseLeave: tipHandlers.onMouseLeave,
      onFocus: e => {
        if (!keyboardFocused(e.currentTarget) || !hasCutText(e.currentTarget)) return
        setShown(row)
        tipHandlers.onFocus(e)
      },
      onBlur: tipHandlers.onBlur,
    }),
    bubble: (
      <InstantTip tip={tip} tipId={tipId} arrow className="w-max max-w-[min(26rem,calc(100vw-1rem))] whitespace-normal break-words">
        {shown !== null && full(shown)}
      </InstantTip>
    ),
  }
}
