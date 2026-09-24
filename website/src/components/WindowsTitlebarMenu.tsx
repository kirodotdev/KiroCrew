import { useCallback, useEffect, useRef, useState, type KeyboardEvent } from 'react'
import { OverlayPortal } from './overlayRoot'
import { Check, Menu } from 'lucide-react'
import Clickable from './Clickable'
import ErrorNotice from './ErrorNotice'
import { i18nT } from '../i18n/t'

const WINDOWS_MENUS = [
  { id: 'file-menu', labelKey: 'components.windowsTitlebarMenu.file' },
  { id: 'edit-menu', labelKey: 'components.windowsTitlebarMenu.edit' },
  { id: 'view-menu', labelKey: 'components.windowsTitlebarMenu.view' },
  { id: 'connection-menu', labelKey: 'components.windowsTitlebarMenu.connection' },
  { id: 'window-menu', labelKey: 'components.windowsTitlebarMenu.window' },
  { id: 'help-menu', labelKey: 'components.windowsTitlebarMenu.help' },
] as const

const WINDOWS_MENU_POPUP_WIDTH = 224
const WINDOWS_MENU_VIEWPORT_GUTTER = 8

const formatWindowsAccelerator = (accelerator: string) => accelerator
  .replaceAll('CommandOrControl', 'Ctrl')
  .replaceAll('CmdOrCtrl', 'Ctrl')

type AppMenuItem = ElectronAppMenuItem

// `executeAppMenuItem` carries a verdict only on a shell built with the
// per-action gate. An older shell's bridge is still `ipcRenderer.send`: it
// forwards the command and answers `undefined` on the spot, so there is no
// verdict in that answer to read, and reading it as a refusal reported every
// click as failed on a shell older than the gateway serving this bundle.
//
// Duck-typed rather than `instanceof Promise` on purpose: `contextBridge`
// hands the renderer a proxy of a promise created in the PRELOAD context,
// which is thenable without necessarily being an instance of this realm's
// `Promise`, so an identity test could misread a modern bridge as a legacy one
// and wave through a refusal it was meant to report.
const acknowledgesDispatch = (answer: unknown): answer is Promise<boolean> =>
  typeof (answer as { then?: unknown } | null | undefined)?.then === 'function'

/**
 * Zed-style Windows application menu. It rests as a compact hamburger, expands
 * the top-level labels while a submenu is open, and switches submenus on hover.
 * The submenu is drawn here rather than by Menu.popup() because a native popup
 * captures window input on Windows, which kills hover switching; only the item
 * model and the command dispatch cross the IPC bridge, so Electron's roles,
 * enabled/checked state and accelerators stay authoritative. Escape, an outside
 * pointerdown, window blur, or picking a command ends the menu session.
 *
 * Expanding is deliberately NOT reported upward: the header measures this
 * cluster's real width (see calculateTopbarSearchLayout in App.tsx), so the
 * centered command palette reacts to the growth on its own.
 */
export default function WindowsTitlebarMenu() {
  const [expanded, setMenuExpanded] = useState(false)
  const [activeMenuId, setActiveMenuId] = useState<string | null>(null)
  const [menuItems, setMenuItems] = useState<AppMenuItem[]>([])
  const [popupPosition, setPopupPosition] = useState({ left: 0, top: 0 })
  // The row a refused click asked for, or null. A refusal is the outcome of
  // something that FAILED, so `errors-use-error-notice` (blocking) wants it
  // rendered through `ErrorNotice` rather than left to the refetched row state
  // — which in the inconclusive paint grace repaints the SAME enabled rows and
  // explains nothing. Held outside the popup deliberately: the user has to be
  // able to read it after the menu is gone.
  const [refusedItemLabel, setRefusedItemLabel] = useState<string | null>(null)
  const menuItemRefs = useRef(new Map<string, HTMLDivElement>())
  const popupRef = useRef<HTMLDivElement>(null)
  const navRef = useRef<HTMLElement>(null)
  const hamburgerRef = useRef<HTMLDivElement | null>(null)
  const requestIdRef = useRef(0)
  // Holds the request generation whose activation is awaiting its dispatch
  // answer, or null. A ref, not state, because it gates an event handler rather
  // than rendering anything — a state flip would not be visible to a second
  // click dispatched in the same tick as the first.
  const clickInFlightRef = useRef<number | null>(null)

  // Collapsing unmounts both the expanded labels and the portal popup, so
  // whatever had focus is destroyed. Hand focus back to the hamburger that
  // replaces them, or a keyboard user is dropped to <body> on every Escape and
  // every command pick and has to Tab in from the top of the header again.
  //
  // `restoreFocus` is the CALLER's intent; the containment check below is the
  // correctness guard. Both are needed: an outside pointerdown or a window blur
  // passes false because the user is deliberately elsewhere, while an
  // IPC-failure collapse passes true yet may have been triggered by mere HOVER,
  // where focus is still in the chat input — moving it to the titlebar would
  // steal keystrokes. So restore only when the menu genuinely owns the focused
  // element, i.e. only when collapsing is what destroys it.
  const collapseMenu = useCallback((restoreFocus = false) => {
    requestIdRef.current += 1
    const ownsFocus = !!navRef.current?.contains(document.activeElement)
      || !!popupRef.current?.contains(document.activeElement)
    setActiveMenuId(null)
    setMenuItems([])
    setMenuExpanded(false)
    if (!restoreFocus || !ownsFocus) return
    // The hamburger only exists after this render drops `expanded`.
    requestAnimationFrame(() => hamburgerRef.current?.focus())
  }, [setMenuExpanded])

  useEffect(() => {
    if (!expanded) return
    const onPointerDown = (event: PointerEvent) => {
      const target = event.target
      if (!(target instanceof Node)) return
      if (popupRef.current?.contains(target)) return
      if (target instanceof Element && target.closest('.win-titlebar-menu')) return
      collapseMenu()
    }
    // Wrapped, NOT passed directly: a listener receives the Event as its first
    // argument, which would arrive as a truthy `restoreFocus` and yank focus
    // into the titlebar of a window the user just switched away from.
    const onBlur = () => collapseMenu()
    window.addEventListener('pointerdown', onPointerDown, true)
    window.addEventListener('blur', onBlur)
    return () => {
      window.removeEventListener('pointerdown', onPointerDown, true)
      window.removeEventListener('blur', onBlur)
    }
  }, [collapseMenu, expanded])

  const openMenu = useCallback(async (id: string, target: HTMLElement) => {
    const api = window.electronAPI
    if (!api?.getAppMenuItems) return
    const rect = target.getBoundingClientRect()
    const titlebarBottom = target.closest('header')?.getBoundingClientRect().bottom
    const requestId = ++requestIdRef.current
    // A new session supersedes the last one's refusal: left standing the notice
    // would account for a click the user has already moved past. Cleared here
    // rather than in `collapseMenu` because a refusal does NOT collapse, and
    // because the notice has to survive the menu closing to be readable at all.
    setRefusedItemLabel(null)
    setActiveMenuId(id)
    setMenuItems([])
    setMenuExpanded(true)
    setPopupPosition({
      left: Math.max(
        WINDOWS_MENU_VIEWPORT_GUTTER,
        Math.min(rect.left, window.innerWidth - WINDOWS_MENU_POPUP_WIDTH - WINDOWS_MENU_VIEWPORT_GUTTER),
      ),
      top: titlebarBottom ?? rect.bottom,
    })
    try {
      const items = await api.getAppMenuItems(id)
      if (requestIdRef.current === requestId) setMenuItems(items)
    } catch {
      // Restore intent is true here: a keyboard-initiated open that fails must
      // not strand the user on <body>. collapseMenu's containment check keeps a
      // hover-initiated failure from stealing focus out of the chat input.
      if (requestIdRef.current === requestId) collapseMenu(true)
    }
  }, [collapseMenu, setMenuExpanded])

  const handleMenuClick = useCallback((id: string, target: HTMLElement) => {
    if (activeMenuId === id) collapseMenu(true)
    else openMenu(id, target)
  }, [activeMenuId, collapseMenu, openMenu])

  // A click can be REFUSED after being painted enabled: `app-menu:items` paints
  // from the last positive port-ownership verdict without waiting on a
  // re-probe, so for one open after a foreign process takes the gateway port
  // the LOCAL_ONLY rows still look live. The refusal is the only moment this
  // component can learn that, which is why `executeAppMenuItem` answers rather
  // than being fire-and-forget.
  //
  // On a refusal we refetch instead of collapsing. The refused click's own
  // probe has by then proven the foreign holder, so the second answer comes
  // back with the gated rows `enabled: false` / `gated: true` — which greys
  // them and renders the footer that names the reason. Collapsing would throw
  // that explanation away in the frame it arrived, leaving a click that
  // silently did nothing with nothing on screen to account for it.
  const executeItem = useCallback(async (item: Exclude<AppMenuItem, { type: 'separator' }>) => {
    const api = window.electronAPI
    if (!activeMenuId || !item.enabled) {
      collapseMenu(true)
      return
    }
    const menuId = activeMenuId
    // Both `openMenu` and `collapseMenu` bump this, so it is the test for "the
    // user is still in the menu session that issued the click". Without it a
    // slow answer landing after an Escape would re-open the popup under the
    // cursor, and an answer from a menu the user has since switched away from
    // would overwrite — or collapse — the one now on screen.
    const requestId = requestIdRef.current
    // The row stays mounted and enabled for as long as the answer takes, so a
    // second activation can reach a command whose first one has not settled.
    // Each `invoke` joins the same port-owner probe and then dispatches
    // independently, running the command twice — and for a toggle like Keep on
    // Top that reads as the click having done nothing. The synchronous collapse
    // this await replaced unmounted the popup before a second click was
    // possible, so the latch is what carries that guarantee now.
    //
    // Keyed on the generation, not a bare boolean: the latch must bound ONE
    // menu session. A probe still running for a session the user has left would
    // otherwise swallow their next click outright — the same "an old session
    // decides for the current one" error as collapsing on a stale answer, so it
    // takes the same cure rather than a second kind of guard.
    if (clickInFlightRef.current === requestId) return
    clickInFlightRef.current = requestId
    let dispatched = false
    try {
      const execute = api?.executeAppMenuItem
      // A missing method also yields `undefined` through optional chaining,
      // but there nothing ran at all — so it keeps the refusal rather than
      // being classified as the legacy bridge. Only `getAppMenuItems` gates
      // whether rows paint, so a shell missing just this one can still put an
      // enabled row under the cursor.
      if (typeof execute !== 'function') dispatched = false
      else {
        const answer: unknown = execute(menuId, item.index)
        // Strict `true` is still required of a bridge that ACKNOWLEDGES, so a
        // real refusal is unchanged. A void answer is the legacy bridge, whose
        // main process has already taken the command.
        dispatched = acknowledgesDispatch(answer) ? (await answer) === true : true
      }
    } catch {
      // An `invoke` that rejects tells us only that the click did not land,
      // which is the same thing a `false` tells us. Treat it identically.
      dispatched = false
    } finally {
      // Only if this session still owns it: a later session's click may have
      // claimed the latch while this answer was outstanding, and clearing it
      // unconditionally would reopen the double-dispatch window for that one.
      if (clickInFlightRef.current === requestId) clickInFlightRef.current = null
    }
    // Checked BEFORE the dispatched branch, not alongside it: a stale
    // generation means this answer belongs to a menu session that is already
    // over, so neither outcome may act on the popup now on screen. Collapsing
    // on a late SUCCESS would destroy a menu the user opened afterwards and
    // never activated an item in.
    if (requestIdRef.current !== requestId) return
    if (dispatched) {
      // A dispatched click closes the menu the way a native one does.
      collapseMenu(true)
      return
    }
    // Set BEFORE the refetch, not after it: the refetch can throw, and a
    // refusal the user is never told about is the defect being fixed here. The
    // refetch below is still worth attempting — where the probe DID name a
    // foreign holder it greys the rows and the footer adds the standing reason
    // — but the notice is what guarantees the click is accounted for at all.
    setRefusedItemLabel(item.label)
    try {
      const items = await api?.getAppMenuItems?.(menuId)
      if (items && requestIdRef.current === requestId) setMenuItems(items)
    } catch {
      // Same contract as `openMenu`'s read failure: a popup we cannot repaint
      // is worse than no popup, and restore intent is true because the click
      // that got us here came from inside the menu.
      if (requestIdRef.current === requestId) collapseMenu(true)
    }
  }, [activeMenuId, collapseMenu])

  const handleKeyDown = useCallback((event: KeyboardEvent<HTMLElement>) => {
    if (!expanded) return
    if (event.key === 'Escape') {
      event.preventDefault()
      collapseMenu(true)
      return
    }
    if (event.key === 'ArrowDown' && !popupRef.current?.contains(event.target as Node)) {
      event.preventDefault()
      popupRef.current?.querySelector<HTMLButtonElement>('button:not(:disabled)')?.focus()
      return
    }
    if (event.key !== 'ArrowLeft' && event.key !== 'ArrowRight') return
    event.preventDefault()
    const activeIndex = WINDOWS_MENUS.findIndex(menu => menu.id === activeMenuId)
    const direction = event.key === 'ArrowRight' ? 1 : -1
    const nextIndex = (Math.max(0, activeIndex) + direction + WINDOWS_MENUS.length) % WINDOWS_MENUS.length
    const nextMenu = WINDOWS_MENUS[nextIndex]
    const target = menuItemRefs.current.get(nextMenu.id)
    if (!target) return
    target.focus()
    openMenu(nextMenu.id, target)
  }, [activeMenuId, collapseMenu, expanded, openMenu])

  return (
    // The nav's onKeyDown is the menu bar's key model (Escape, ArrowLeft/Right
    // between menus, ArrowDown into the popup), delegated here because focus
    // legitimately sits on any of the triggers or on an item inside the popup.
    // It has no pointer counterpart — every activatable affordance is a
    // <Clickable> child — so it adds a keyboard path rather than missing one.
    // eslint-disable-next-line jsx-a11y/no-noninteractive-element-interactions -- keyboard-only delegated handler for the descendant triggers, not an action on the nav itself
    <nav
      ref={navRef}
      className="win-titlebar-menu flex h-full shrink-0 items-center gap-0.5"
      aria-label={i18nT('app.open_menu')}
      onKeyDown={handleKeyDown}
    >
      {refusedItemLabel !== null && (
        // Through `OverlayPortal`, not inline in the nav: the titlebar is a
        // fixed-height strip that would clip a multi-line notice, and the
        // notice has to outlive the popup it came from. `OverlayPortal` is the
        // repo's own escape hatch (`useOverlayRoot()`), which eslint requires
        // over a `document.body` portal.
        //
        // `askAgent` is ON: the hand-off navigates to the chat and unmounts
        // this tree, and a titlebar menu holds no unsaved state for that to
        // destroy — which is the condition `errors-use-error-notice` names. A
        // Windows port-owner probe that cannot name the holder is also squarely
        // the class of failure a user cannot fix and the agent can.
        <OverlayPortal>
          <div className="pointer-events-auto fixed left-1/2 top-safe-offset-12 z-50 w-[min(28rem,calc(100vw-2rem))] -translate-x-1/2">
            <ErrorNotice
              variant="inline"
              message={i18nT('app.titlebar_menu_refused_notice', { item: refusedItemLabel })}
              askAgent
              onDismiss={() => setRefusedItemLabel(null)}
            />
          </div>
        </OverlayPortal>
      )}
      {!expanded && (
        <Clickable
          ref={hamburgerRef}
          className="win-titlebar-menu-item inline-flex size-7 items-center justify-center rounded-md text-muted hover:bg-bg-hover hover:text-text focus-visible:bg-bg-hover focus-visible:text-text focus-visible:outline-hidden"
          aria-haspopup="menu"
          aria-expanded="false"
          aria-label={i18nT('app.open_menu')}
          onClick={event => {
            const target = event?.currentTarget as HTMLElement | undefined
            if (!target) return
            const first = WINDOWS_MENUS[0].id
            openMenu(first, target)
            // Expanding REPLACES the hamburger with the label row, so the
            // element that was just activated is unmounted. Without moving
            // focus into that row, focus falls to <body> and the nav's
            // onKeyDown never fires again — Escape and ArrowLeft/Right stop
            // working and a keyboard user cannot drive the menu they just
            // opened. Deferred because the labels do not exist until the
            // render that `openMenu` triggers.
            //
            // Done for pointer opens too, not just keyboard ones: a native menu
            // bar also takes focus when opened, so there is no case that wants
            // the old behaviour and no activation-source sniffing needed.
            requestAnimationFrame(() => menuItemRefs.current.get(first)?.focus())
          }}
        >
          <Menu className="lucide-inline" size={16} aria-hidden="true" />
        </Clickable>
      )}
      {/* The expanded row is six sibling triggers, which a literal reading of
          website/AUTOSDE.yaml `max-two-buttons-per-row` matches. It is exempt
          for the reason that rule's own remedy names: an overflow menu trigger
          "counts as ONE regardless of how many items it holds", and that is
          exactly what this is — the RESTING state is a single hamburger, and
          these labels ARE that one control opened. They are also not N peer
          actions competing for a click (the harm the rule guards): File / Edit /
          View is the most conventionally ordered surface in desktop software,
          read by position rather than by scanning every label. Under width
          pressure the row collapses back to the hamburger and the centered
          command palette yields the region, so it degrades rather than clips. */}
      {expanded && WINDOWS_MENUS.map(menu => (
        <Clickable
          key={menu.id}
          ref={node => {
            if (node) menuItemRefs.current.set(menu.id, node)
            else menuItemRefs.current.delete(menu.id)
          }}
          className={`win-titlebar-menu-item inline-flex h-7 items-center justify-center rounded-md px-2 text-[12px] font-medium leading-none transition-colors focus-visible:outline-hidden ${activeMenuId === menu.id ? 'bg-bg-hover text-text' : 'text-muted hover:bg-bg-hover hover:text-text focus-visible:bg-bg-hover focus-visible:text-text'}`}
          aria-haspopup="menu"
          aria-expanded={activeMenuId === menu.id}
          onMouseEnter={event => {
            if (activeMenuId !== menu.id) openMenu(menu.id, event.currentTarget)
          }}
          onClick={event => event && handleMenuClick(menu.id, event.currentTarget as HTMLElement)}
        >
          {i18nT(menu.labelKey)}
        </Clickable>
      ))}
      {expanded && activeMenuId && <OverlayPortal>
        <div
          ref={popupRef}
          role="menu"
          tabIndex={-1}
          className="fixed z-[9999] max-h-[calc(100vh-50px)] min-w-56 overflow-y-auto rounded-lg border border-border bg-bg-elevated p-1 text-text shadow-lg"
          style={{ left: popupPosition.left, top: popupPosition.top }}
          onKeyDown={event => {
            if (event.key === 'Escape') {
              event.preventDefault()
              collapseMenu(true)
              return
            }
            if (!['ArrowDown', 'ArrowUp', 'Home', 'End'].includes(event.key)) return
            event.preventDefault()
            event.stopPropagation()
            const buttons = [...event.currentTarget.querySelectorAll<HTMLButtonElement>('button:not(:disabled)')]
            if (buttons.length === 0) return
            const currentIndex = buttons.indexOf(document.activeElement as HTMLButtonElement)
            const nextIndex = event.key === 'Home'
              ? 0
              : event.key === 'End'
                ? buttons.length - 1
                : (Math.max(0, currentIndex) + (event.key === 'ArrowDown' ? 1 : -1) + buttons.length) % buttons.length
            buttons[nextIndex]?.focus()
          }}
        >
          {/* Footer legend. Rendered ONCE, BELOW the mapped items, when this
              menu has any GATE-CAUSED disabled row (`item.gated === true`).
              A natively-disabled row (e.g. an `undo` role with an empty
              history) is NOT counted: the "unavailable from this window,
              shortcut still fires here" claim would be false for it. So
              `serializeMenuItems` exposes `gated` alongside `enabled` and
              this predicate reads the former.
              The footer is the ONLY reachable explainer for a sighted mouse
              user: `disabled:pointer-events-none` blocks hover on the greyed
              rows, so no tooltip on them can ever be opened. A single line at
              the bottom of the popup carries the counter-conventional message
              the greyed rows need — rendered
              AFTER items so menu-row geometry stays stable (first row's
              vertical position matches the same menu in the main window,
              not shifted down by the footer height). Rendered only when
              relevant so a fully-enabled menu (main window) stays clean. */}
          {menuItems.map(item => item.type === 'separator' ? (
            <div key={item.index} role="separator" className="mx-1 my-1 h-px bg-border" />
          ) : (
            <button
              key={item.index}
              type="button"
              role={item.type === 'normal' ? 'menuitem' : 'menuitemcheckbox'}
              aria-checked={item.type === 'normal' ? undefined : item.checked}
              disabled={!item.enabled}
              className="flex w-full cursor-pointer select-none items-center gap-2 rounded-md border-none bg-transparent px-3 py-1.5 text-left text-[13px] text-text outline-hidden transition-colors hover:bg-bg-hover focus:bg-bg-hover disabled:pointer-events-none disabled:opacity-50"
              onClick={() => { void executeItem(item) }}
            >
              <span className="flex size-3 shrink-0 items-center justify-center">
                {item.type !== 'normal' && item.checked && (
                  <Check className="lucide-inline" size={12} aria-hidden="true" />
                )}
              </span>
              <span className="flex-1 whitespace-nowrap">{item.label}</span>
              {item.accelerator && (
                // Plain caption on every row, gated or not — and on a gated
                // row the caption's presence is itself the signal, because
                // windows-menu-model blanks it when the chord cannot fire
                // from this window. So a caption that renders here is one
                // Electron's native dispatch (or Chromium's clipboard
                // handling) still delivers, which is what lets the footer
                // say the shortcuts shown still work.
                <span className="ml-6 flex items-center whitespace-nowrap text-[11px] text-muted">
                  {formatWindowsAccelerator(item.accelerator)}
                </span>
              )}
            </button>
          ))}
          {menuItems.some(item => item.type !== 'separator' && item.gated) && (
            <div
              // max-width matches the popup's ~min-w-56 (224px) plus its
              // outer padding, giving a wrap point roughly aligned with the
              // menu rows. Without it (and without `whitespace-normal`,
              // which is the default on divs but easy to lose to a parent
              // rule) the footer's single line stretches the popup to
              // several hundred pixels wider than a normal menu — UX
              // Review "popup width blowout" watch on c2d85562b.
              //
              // `border-t` draws the separator above the footer so it reads
              // as a legend appended AFTER the row list, not another item. The
              // border belongs to the full-width wrapper while `max-w-[240px]`
              // sits on the inner span: both on one element would clamp the
              // separator to the text's own width instead of the popup's.
              className="mx-2 mt-1 border-t border-border pb-1 pt-1 text-[11px] leading-snug text-muted"
              role="note"
            >
              <span className="block max-w-[240px] whitespace-normal">
                {i18nT('app.titlebar_menu_disabled_footer')}
              </span>
            </div>
          )}
        </div>
      </OverlayPortal>}
    </nav>
  )
}
