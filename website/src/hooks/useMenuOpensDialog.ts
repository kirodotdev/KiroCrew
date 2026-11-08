// Focus hand-off for a Radix menu item that opens another layer: a dialog, a
// picker, or an inline editor that takes focus. Lives beside
// `useDialogFocusTrap` because it is the other half of the same contract: the
// trap moves focus into a dialog, and this hook keeps the closing menu from
// pulling focus back out of it.
import { useCallback, useRef, type SyntheticEvent } from 'react'

/**
 * Hand focus from a closing Radix menu (DropdownMenu / ContextMenu) to the
 * layer one of its items opened, and back to the menu's opener afterwards.
 *
 * Two Radix behaviours meet here, one macrotask apart:
 *
 *   1. Item select is dispatched with `flushSync`, so a layer set from
 *      `onSelect` mounts in a commit where the menu's FocusScope is STILL
 *      trapping focus. The layer focuses itself, the trap yanks focus straight
 *      back into a menu that then unmounts, and focus is stranded on `body`.
 *      `openLayer` therefore defers the open by one macrotask
 *      (`setTimeout(…, 0)`), past the menu's own close commit.
 *   2. When the menu content unmounts, its FocusScope schedules the restore to
 *      the trigger one macrotask later and fires `onCloseAutoFocus` from that
 *      task. By then the layer owns focus, so restoring the trigger would steal
 *      it (a dialog loses its focus; an inline input blurs and cancels the
 *      edit). An item that opened a layer arms a one-shot flag, and the menu's
 *      `onCloseAutoFocus` consumes it and calls `preventDefault()`.
 *
 * Because the trigger restore was suppressed, a dialog hands focus back itself
 * on close: call `returnFocus()` from its close path. An inline editor that
 * owns focus while mounted (a rename field) has no close hand-back and only
 * needs `armSkipRestore()`.
 *
 * Usage:
 *   - `rememberOpener` on the trigger's `onPointerDown` / `onKeyDown`. It reads
 *     `currentTarget` from the trigger's own event rather than
 *     `document.activeElement`: Safari does not focus a button on pointer
 *     click, so the active element would still be whatever had focus before.
 *   - `onCloseAutoFocus` on the menu content. It returns whether it consumed
 *     an armed flag, so a site with its own fallback (e.g. skip the restore
 *     after a pointer dismissal) can run that only when it returns false.
 *   - `openLayer(() => setDialog(…))` from the item's `onSelect` for a dialog
 *     or picker; `armSkipRestore()` for a layer that mounts in the same commit
 *     and grabs focus on its own next frame.
 *   - `disarm()` when the caller knows no suppression can be pending, as a
 *     guard against a menu dismissed without firing `onCloseAutoFocus`.
 *
 * Every returned function has a stable identity, so passing them to memoized
 * rows does not invalidate those rows.
 */
export function useMenuOpensDialog() {
  const openerRef = useRef<HTMLElement | null>(null)
  const armedRef = useRef(false)

  const rememberOpener = useCallback((e: SyntheticEvent<HTMLElement>) => {
    openerRef.current = e.currentTarget
  }, [])

  const armSkipRestore = useCallback(() => {
    armedRef.current = true
  }, [])

  const openLayer = useCallback((open: () => void) => {
    armedRef.current = true
    setTimeout(open, 0)
  }, [])

  const onCloseAutoFocus = useCallback((e: Event): boolean => {
    if (!armedRef.current) return false
    armedRef.current = false
    e.preventDefault()
    return true
  }, [])

  const disarm = useCallback(() => {
    armedRef.current = false
  }, [])

  const returnFocus = useCallback(() => {
    openerRef.current?.focus({ preventScroll: true })
  }, [])

  return { rememberOpener, armSkipRestore, openLayer, onCloseAutoFocus, disarm, returnFocus }
}
