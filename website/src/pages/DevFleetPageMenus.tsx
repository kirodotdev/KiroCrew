/**
 * The row controls the Dev Fleet page shares: the portaled row-actions menu
 * (`MenuBtn`) and the confirm-on-click button with its popover (`ConfirmBtn`).
 */
import { useState, useRef, useCallback, useEffect, type CSSProperties, type ReactNode, type RefObject } from 'react'
import { OverlayPortal, useCloseWhenCrewHidden } from '../components/overlayRoot'
import { Btn } from '../components/ui'
import Clickable from '../components/Clickable'
import { useDocumentImeLatch } from '../hooks/useImeGuard'
import { useDialogFocusTrap } from '../hooks/useDialogFocusTrap'
import { handleMenuKeydown } from '../hooks/useMenuKeyboard'
import { Ellipsis } from 'lucide-react'
import { i18nT } from '../i18n/t'

/* ─── Sub-components ─── */
interface MenuItemDef { label: string; icon?: ReactNode; onClick: () => void; disabled?: boolean; danger?: boolean; title?: string }
// Row-actions dropdown geometry. The menu is portaled to the body-level overlay root so a row's
// <Card overflow> can't clip it; these drive fixed positioning.
const MENU_GAP = 6        // gap between trigger and menu
const MENU_MARGIN = 8     // min gap from the viewport edge
const MENU_ITEM_H = 32    // estimated per-item height for the flip decision
const MENU_PAD = 8        // container vertical padding (4px top + 4px bottom)
export function MenuBtn({ items }: { items: (MenuItemDef | null)[] }) {
  const [open, setOpen] = useState(false)
  // Holds no input, so a Crew switch closes it like a click outside (components/overlayRoot).
  useCloseWhenCrewHidden(open, useCallback(() => setOpen(false), []))
  // Trigger rect captured on open; drives the portaled menu's fixed position.
  const [rect, setRect] = useState<DOMRect | null>(null)
  const triggerRef = useRef<HTMLButtonElement>(null)
  const menuRef = useRef<HTMLDivElement>(null)
  // One ref per VISIBLE item (index-aligned with `visible`, not `items`), so
  // focus-entry and Tab containment below can skip disabled rows without
  // reasoning about the gaps a filtered/disabled mix would otherwise leave.
  const itemRefs = useRef<(HTMLDivElement | null)[]>([])
  const visible = items.filter(Boolean) as MenuItemDef[]
  // Composition latch for the Escape branch and the shared menu contract
  // below: a Tab or Escape the IME owns is choosing/cancelling a candidate,
  // not navigating the menu (native-event contract in useImeGuard.ts). Menu
  // items are non-editable today, so no composition can start on them — the
  // latch pins that this stays safe if the menu ever grows a focusable text
  // field. (The sibling ConfirmBtn popover reaches the same guard through
  // `useDialogFocusTrap`, which carries its own latch.)
  const imeLatch = useDocumentImeLatch(open)

  // Explicit dismissal (Escape, an item click) restores focus to the trigger.
  // Outside-click closes WITHOUT moving focus, deliberately: the browser
  // routes focus per the click target after the handler, so the user's focus
  // is already elsewhere by their own action (#2533). Scroll/resize closes
  // restore focus only when it would otherwise be orphaned — see
  // onScrollOrResize below.
  const close = useCallback(() => { setOpen(false); triggerRef.current?.focus() }, [])

  // Enabled item elements, index-aligned filter over itemRefs/visible.
  const focusableItems = useCallback(
    () => itemRefs.current.filter((el, i) => !visible[i]?.disabled && el) as HTMLDivElement[],
    [visible],
  )

  useEffect(() => {
    if (!open) return
    // role="menu" tells assistive technology that focus is managed here —
    // move it onto the first enabled item so a keyboard user lands inside
    // the menu they were just told is open, not still on the trigger.
    focusableItems()[0]?.focus()
    // The menu is portaled to the body-level overlay root, so it is not a DOM descendant of
    // the trigger — the outside-click guard must exclude BOTH the trigger and
    // the menu (a plain trigger.contains() check would close on every menu
    // click).
    const onDown = (e: MouseEvent) => {
      const t = e.target as Node
      if (!triggerRef.current?.contains(t) && !menuRef.current?.contains(t)) setOpen(false)
    }
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') {
        // An Escape the IME owns is cancelling a candidate, not the menu —
        // and close() also yanks focus back to the trigger. Same claim, same
        // reason as the dialog contract's own Escape branch.
        if (!imeLatch.claimKey(e)) return
        close()
        return
      }
      // Arrow/Home/End navigation and Tab containment live in the shared
      // menu contract (useMenuKeyboard.ts) — extracted from here (#6231) so
      // the sibling role="menu" surfaces honour the same keyboard promise
      // without another inline spelling. Escape stays local: what "close"
      // means (restore focus to the trigger) is this host's own semantics.
      handleMenuKeydown(e, focusableItems, imeLatch)
    }
    // position:fixed desyncs from any scrolling ancestor — close on scroll
    // (capture phase catches nested scrollers) and on resize. A wheel scroll
    // moves no DOM focus, so with focus-entry above the unmount would orphan
    // focus to <body> — restore it to the trigger, but only when focus is
    // still inside the menu, and without scrolling: a default focus() would
    // yank the viewport back to the trigger, hijacking the very scroll that
    // dismissed the menu.
    const onScrollOrResize = () => {
      if (menuRef.current?.contains(document.activeElement)) triggerRef.current?.focus({ preventScroll: true })
      setOpen(false)
    }
    document.addEventListener('mousedown', onDown)
    document.addEventListener('keydown', onKey)
    window.addEventListener('scroll', onScrollOrResize, true)
    window.addEventListener('resize', onScrollOrResize)
    return () => {
      document.removeEventListener('mousedown', onDown)
      document.removeEventListener('keydown', onKey)
      window.removeEventListener('scroll', onScrollOrResize, true)
      window.removeEventListener('resize', onScrollOrResize)
    }
    // `focusableItems` is a new function identity every render (it closes
    // over `visible`, itself a new array every render); including it would
    // re-run this effect (and re-steal focus onto the first item) on every
    // unrelated parent re-render while the menu is open, not just on
    // open/close.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open, close, imeLatch])

  const toggle = () => {
    if (!open && triggerRef.current) setRect(triggerRef.current.getBoundingClientRect())
    setOpen((o) => !o)
  }

  // Right-align the menu's right edge to the trigger (as before), clamped so it
  // never sits flush against the viewport edge. Open downward by default; flip
  // up when there isn't room below for the estimated height and there's more
  // room above. Either `top` or `bottom` is set (never both) + maxHeight so the
  // menu is always clamped inside the viewport.
  const estH = visible.length * MENU_ITEM_H + MENU_PAD
  const spaceBelow = rect ? window.innerHeight - rect.bottom - MENU_GAP : 0
  const spaceAbove = rect ? rect.top - MENU_GAP : 0
  const openUp = !!rect && spaceBelow < estH + MENU_MARGIN && spaceAbove > spaceBelow
  const avail = Math.max(80, (openUp ? spaceAbove : spaceBelow) - MENU_MARGIN)
  const posStyle: CSSProperties = rect
    ? {
        position: 'fixed',
        right: Math.max(MENU_MARGIN, window.innerWidth - rect.right),
        ...(openUp
          ? { bottom: window.innerHeight - rect.top + MENU_GAP }
          : { top: rect.bottom + MENU_GAP }),
        maxHeight: avail,
      }
    : { position: 'fixed' }

  return (
    <span style={{ display: 'inline-flex' } as CSSProperties}>
      <Btn ref={triggerRef} onClick={toggle} title={i18nT('pages.devFleetPage.more_actions')} aria-label={i18nT('pages.devFleetPage.more_actions')} aria-haspopup="menu" aria-expanded={open}>
        <Ellipsis size={15} className="lucide-inline" />
      </Btn>
      {open && rect && <OverlayPortal>
        <div
          ref={menuRef}
          role="menu"
          aria-label={i18nT('pages.devFleetPage.more_actions')}
          data-placement={openUp ? 'up' : 'down'}
          style={{ ...posStyle, zIndex: 4000, overflowY: 'auto', background: 'var(--card, #16161a)', border: '1px solid var(--border)', borderRadius: 10, padding: 4, minWidth: 168, boxShadow: '0 8px 24px rgba(0,0,0,0.45)' } as CSSProperties}
        >
          {visible.map((item, i) => (
            <Clickable
              key={'mi' + i}
              ref={(el) => { itemRefs.current[i] = el }}
              onClick={() => { close(); item.onClick() }}
              disabled={!!item.disabled}
              style={{ display: 'flex', alignItems: 'center', gap: 8, width: '100%', textAlign: 'left' as const, background: 'none', border: 'none', borderRadius: 7, padding: '7px 10px', fontSize: 12, color: item.danger ? 'var(--danger)' : 'var(--text)', cursor: item.disabled ? 'default' : 'pointer', opacity: item.disabled ? 0.5 : 1 } as CSSProperties}
            >
              {item.icon || null}{item.label}
            </Clickable>
          ))}
        </div>
      </OverlayPortal>}
    </span>
  )
}

interface ConfirmBtnProps { title: string; desc: string; confirmLabel?: string; onConfirm: () => void; btn?: Record<string, unknown>; children: ReactNode }
// Confirm popover width, and the height estimate that drives the flip
// decision. The estimate only picks a side; `maxHeight` + `overflowY` below
// keep the popover inside the viewport even when a locale's `desc` wraps to
// more lines than assumed here.
const CONFIRM_W = 264
const CONFIRM_EST_H = 180

interface ConfirmPopoverProps {
  popRef: RefObject<HTMLDivElement>
  title: string
  desc: string
  confirmLabel?: string
  posStyle: CSSProperties
  openUp: boolean
  onCancel: () => void
  onConfirm: () => void
}
/**
 * The popover half of ConfirmBtn, split into a component of its own so it
 * MOUNTS when the popover opens. That is what makes the shared dialog contract
 * usable here: `useDialogFocusTrap`'s focus effects key on MOUNT, and
 * ConfirmBtn itself is a persistent component — a hook call up there would run
 * the focus effect once at page load against a null container.
 *
 * The trap owns Escape, the boundary-Tab ring, and the IME latch that guards
 * both, so this surface no longer spells its own (#5542). Two pieces stay with
 * the host because they are the host's own semantics: WHERE the popover sits
 * (portal + flip geometry) and WHAT dismissal means (`onCancel` returns focus
 * to the trigger).
 */
function ConfirmPopover({ popRef, title, desc, confirmLabel, posStyle, openUp, onCancel, onConfirm }: ConfirmPopoverProps) {
  // `restoreFocus: false` — the host's `close()` already returns focus to the
  // trigger, and for a trigger-anchored popover that is the more correct of the
  // two: the hook captures `document.activeElement`, which on Safari is NOT the
  // clicked trigger, and its restore is unconditional where dismissal by an
  // outside click must leave focus where the click put it (#2533). The hook's
  // own note carries the full reasoning.
  useDialogFocusTrap(popRef, onCancel, { restoreFocus: false })
  return (
    <div
      ref={popRef}
      role="dialog"
      aria-modal="true"
      aria-label={title}
      data-placement={openUp ? 'up' : 'down'}
      style={{ ...posStyle, zIndex: 4000, overflowY: 'auto', background: 'var(--card, #16161a)', border: '1px solid var(--border)', borderRadius: 10, padding: '10px 12px', width: CONFIRM_W, boxShadow: '0 8px 24px rgba(0,0,0,0.45)', textAlign: 'left' as const } as CSSProperties}
    >
      <div style={{ fontSize: 12.5, fontWeight: 600, marginBottom: 4 }}>{title}</div>
      <div style={{ fontSize: 11.5, color: 'var(--muted)', lineHeight: 1.5, marginBottom: 9 }}>{desc}</div>
      <div style={{ display: 'flex', gap: 8, justifyContent: 'flex-end' } as CSSProperties}>
        <Btn onClick={onCancel}>{i18nT('pages.devFleetPage.cancel')}</Btn>
        <Btn primary onClick={onConfirm}>{confirmLabel || i18nT('pages.devFleetPage.start')}</Btn>
      </div>
    </div>
  )
}

export function ConfirmBtn({ title, desc, confirmLabel, onConfirm, btn, children }: ConfirmBtnProps) {
  const [open, setOpen] = useState(false)
  // Trigger rect captured on open; drives the portaled popover's fixed
  // position. Same approach as MenuBtn above: an absolutely positioned
  // popover is clipped by the row's `.card-glow { overflow: hidden }`
  // ancestor, so it must be portaled to the body-level overlay root instead.
  const [rect, setRect] = useState<DOMRect | null>(null)
  const triggerRef = useRef<HTMLButtonElement>(null)
  const popRef = useRef<HTMLDivElement>(null)

  const close = useCallback(() => {
    setOpen(false)
    triggerRef.current?.focus()
  }, [])

  useEffect(() => {
    if (!open) return
    // Portaled to the body-level overlay root, so the popover is not a DOM descendant of the
    // trigger — the outside-click guard must exclude BOTH, or every click
    // inside the popover (including Cancel/Start) would close it first.
    // Closing this way deliberately does NOT move focus: the browser routes
    // focus per the click target, so the user is already elsewhere by their
    // own action (#2533).
    const onDown = (e: MouseEvent) => {
      const t = e.target as Node
      if (!triggerRef.current?.contains(t) && !popRef.current?.contains(t)) setOpen(false)
    }
    // position:fixed desyncs from any scrolling ancestor — close on scroll
    // (capture phase catches nested scrollers) and on resize.
    const onScrollOrResize = () => setOpen(false)
    document.addEventListener('mousedown', onDown)
    window.addEventListener('scroll', onScrollOrResize, true)
    window.addEventListener('resize', onScrollOrResize)
    return () => {
      document.removeEventListener('mousedown', onDown)
      window.removeEventListener('scroll', onScrollOrResize, true)
      window.removeEventListener('resize', onScrollOrResize)
    }
  }, [open])

  const toggle = () => {
    if (!open && triggerRef.current) setRect(triggerRef.current.getBoundingClientRect())
    setOpen((o) => !o)
  }

  // Right-align to the trigger (as before), clamped so the popover never sits
  // flush against a viewport edge. Open downward by default; flip up when
  // there is no room below and more room above. Either `top` or `bottom` is
  // set, never both.
  const spaceBelow = rect ? window.innerHeight - rect.bottom - MENU_GAP : 0
  const spaceAbove = rect ? rect.top - MENU_GAP : 0
  const openUp = !!rect && spaceBelow < CONFIRM_EST_H + MENU_MARGIN && spaceAbove > spaceBelow
  const avail = Math.max(80, (openUp ? spaceAbove : spaceBelow) - MENU_MARGIN)
  const posStyle: CSSProperties = rect
    ? {
        position: 'fixed',
        right: Math.max(MENU_MARGIN, window.innerWidth - rect.right),
        ...(openUp
          ? { bottom: window.innerHeight - rect.top + MENU_GAP }
          : { top: rect.bottom + MENU_GAP }),
        maxHeight: avail,
      }
    : { position: 'fixed' }

  return (
    <span style={{ display: 'inline-flex' } as CSSProperties}>
      <Btn ref={triggerRef} {...(btn || {})} onClick={toggle} aria-haspopup="dialog" aria-expanded={open}>{children}</Btn>
      {open && rect && <OverlayPortal>
        <ConfirmPopover
          popRef={popRef}
          title={title}
          desc={desc}
          confirmLabel={confirmLabel}
          posStyle={posStyle}
          openUp={openUp}
          onCancel={close}
          onConfirm={() => { close(); onConfirm() }}
        />
      </OverlayPortal>}
    </span>
  )
}
