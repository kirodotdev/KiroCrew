import { useEffect } from 'react'
import { useDocumentImeLatch, type ImeLatch } from './useImeGuard'

/**
 * Shared keyboard contract for `role="menu"` surfaces (WAI-ARIA menu pattern):
 * ArrowDown/ArrowUp move real DOM focus between the enabled items and wrap at
 * both ends, Home/End jump to the boundary items, and Tab/Shift-Tab are
 * contained within the enabled items (#2533) — a Tab out of a still-open menu
 * would drop a keyboard user behind it with no obvious way back. Escape is
 * deliberately NOT handled here: every host already owns its own dismissal
 * (and what "close" means — restore focus, toggle state — differs per host).
 *
 * This is the extraction of `MenuBtn`'s roving-focus keydown logic
 * (DevFleetPage.tsx, landed via #6226) so the sibling `role="menu"` surfaces
 * can honour the same contract without a per-surface re-spelling (#6231).
 * `MenuBtn` itself consumes `handleMenuKeydown` from its existing
 * document-level listener; popover-style hosts use the `useMenuKeyboard` hook.
 *
 * Not `useListboxKeyboard`: that hook closes on Tab (`closeToTrigger`), clamps
 * instead of wrapping (`els[i+1] ?? els[els.length-1]`), and scopes its IME
 * guard to a filter input — all three are the LISTBOX/combobox side of the
 * pattern. The menu contract wraps, contains Tab, and needs a document-level
 * composition latch because a menu has no editable target to anchor one to.
 */

/**
 * The enabled, keyboard-reachable rows of a menu container. Menus in this
 * repo mark rows as `role="menuitem"` / `role="menuitemradio"` /
 * `role="menuitemcheckbox"`, native `<button>`s, or `Clickable`
 * (`role="button"`) — match all of them so a host's action rows are never
 * stranded outside the arrow/Tab cycle. Disabled rows (native `disabled` or
 * `aria-disabled`, the `Clickable` spelling) are skipped the same way
 * `MenuBtn` skips them.
 */
export function menuItemsOf(container: HTMLElement | null): HTMLElement[] {
  if (!container) return []
  const sel = '[role="menuitem"],[role="menuitemradio"],[role="menuitemcheckbox"],[role="button"],button'
  return Array.from(container.querySelectorAll<HTMLElement>(sel)).filter(
    el => !el.hasAttribute('disabled') && el.getAttribute('aria-disabled') !== 'true',
  )
}

/**
 * Handle one keydown against the menu contract. Returns true when the key was
 * consumed (or claimed by the IME latch), false when it is not the menu's key
 * and should fall through to the host's other handling.
 *
 * Framework-free on purpose (same reasoning as `createImeLatch`): document-
 * level native listeners (`MenuBtn`) and React `onKeyDown` handlers (via
 * `e.nativeEvent`) share ONE spelling of the contract.
 */
export function handleMenuKeydown(
  e: globalThis.KeyboardEvent,
  getItems: () => HTMLElement[],
  imeLatch: ImeLatch,
): boolean {
  if (e.key === 'ArrowDown' || e.key === 'ArrowUp' || e.key === 'Home' || e.key === 'End') {
    // role="menu" promises arrow-key item navigation (WAI-ARIA menu
    // pattern) — screen-reader users reach for arrows first (#5851).
    // Wrap at both ends; Home/End jump to the boundary items.
    // preventDefault stops the page scrolling behind the open menu.
    // Modified arrows (Cmd/Ctrl/Alt) are OS/browser shortcuts the menu
    // pattern assigns no behavior to — let them through untouched.
    if (e.altKey || e.ctrlKey || e.metaKey) return false
    // An arrow the IME owns is moving through composition candidates, not
    // menu items — the claim runs BEFORE the preventDefault() and focus
    // move. (useListKeyboardNav deliberately lets composition arrows
    // through because its list coexists with a text input; a menu holds no
    // editable target, so it takes the stricter side.)
    const focusable = getItems()
    if (focusable.length === 0) return false
    if (!imeLatch.claimKey(e)) return true
    e.preventDefault()
    if (e.key === 'Home') { focusable[0].focus(); return true }
    if (e.key === 'End') { focusable[focusable.length - 1].focus(); return true }
    const idx = focusable.indexOf(document.activeElement as HTMLElement)
    // Focus outside the item list (edge: it never entered) — treat the
    // arrow as entry: Down lands on the first item, Up on the last.
    const next = idx === -1
      ? (e.key === 'ArrowDown' ? 0 : focusable.length - 1)
      : (idx + (e.key === 'ArrowDown' ? 1 : -1) + focusable.length) % focusable.length
    focusable[next].focus()
    return true
  }
  if (e.key !== 'Tab') return false
  // Contain Tab/Shift-Tab within the menu's enabled items (#2533). A boundary
  // Tab the IME owns must not cycle focus: the claim runs BEFORE the
  // preventDefault() and focus move.
  const focusable = getItems()
  if (focusable.length === 0) return false
  const first = focusable[0]
  const last = focusable[focusable.length - 1]
  if (e.shiftKey && document.activeElement === first) {
    if (!imeLatch.claimKey(e)) return true
    e.preventDefault()
    last.focus()
    return true
  }
  if (!e.shiftKey && document.activeElement === last) {
    if (!imeLatch.claimKey(e)) return true
    e.preventDefault()
    first.focus()
    return true
  }
  return false
}

/**
 * Wire an open `role="menu"` container onto the shared menu keyboard
 * contract. While `enabled`:
 *
 *  - a DOCUMENT-level keydown listener drives `handleMenuKeydown` (same shape
 *    as `MenuBtn`): arrows work even while focus still sits on the trigger —
 *    the first arrow ENTERS the item list — and portaled menus need no React
 *    bubbling path;
 *  - focus moves onto the first enabled item when the menu opens
 *    (`focusFirstOnOpen`, default true): role="menu" tells assistive
 *    technology that focus is managed here, so a keyboard user lands inside
 *    the menu they were just told is open. Hosts that already own focus entry
 *    (Pierre's tree menu) pass false;
 *  - a shared document IME latch keeps composition keys out of the contract
 *    (see `useDocumentImeLatch`).
 *
 * One guard `MenuBtn` does not need but a document-level contract here does:
 * when focus sits in an EDITABLE element outside the menu (the composer
 * textarea under `MicSourceMenu`), the keys belong to the editor — an open
 * menu must not hijack a caret's arrows. `MenuBtn` closes on any outside
 * click, so its menu and an outside caret cannot coexist; these popovers can.
 *
 * PRECONDITION: at most one hook-driven menu open at a time. Each instance
 * installs its own document listener and treats focus-outside-the-list as
 * "enter the list", so two open menus would both act on the same arrow and
 * the unfocused one would steal focus. Every current host already guarantees
 * this (all dismiss on outside pointerdown/mousedown, and Tab containment
 * keeps the keyboard from reaching a second trigger while one is open) — a
 * new consumer must keep that dismissal shape or add arbitration here first.
 *
 * Focus RESTORE on close is the HOST's job, and there are two sanctioned
 * postures — pick one on purpose: restore-on-every-unmount, guarded by
 * "focus is still inside the menu" (mochi ContextMenu — covers host-driven
 * unmounts too), or restore-only-on-explicit-dismissal, leaving outside-
 * pointer dismissal alone (SlotPopover/MenuBtn — the browser routes focus
 * per the click target, #2533). Focus entry without a matching restore
 * strands a keyboard user on <body> when the focused item unmounts.
 *
 * Item discovery is `menuItemsOf(containerRef.current)`.
 */
export function useMenuKeyboard(opts: {
  /** The menu's open state; gates the listener, latch, and focus entry. */
  enabled: boolean
  containerRef: React.RefObject<HTMLElement | null>
  /** Move focus onto the first enabled item when `enabled` flips true. */
  focusFirstOnOpen?: boolean
}): void {
  const { enabled, containerRef, focusFirstOnOpen = true } = opts
  const imeLatch = useDocumentImeLatch(enabled)
  useEffect(() => {
    if (!enabled) return
    const items = () => menuItemsOf(containerRef.current)
    if (focusFirstOnOpen) items()[0]?.focus()
    const onKey = (e: KeyboardEvent) => {
      const active = document.activeElement as HTMLElement | null
      if (active && !containerRef.current?.contains(active)) {
        const tag = active.tagName
        if (tag === 'INPUT' || tag === 'TEXTAREA' || tag === 'SELECT' || active.isContentEditable) return
      }
      handleMenuKeydown(e, items, imeLatch)
    }
    document.addEventListener('keydown', onKey)
    return () => document.removeEventListener('keydown', onKey)
    // `imeLatch` is identity-stable (useDocumentImeLatch); `containerRef` is a
    // stable ref object. Only open/close (and the static focusFirstOnOpen)
    // should re-run this effect.
  }, [enabled, focusFirstOnOpen, containerRef, imeLatch])
}
