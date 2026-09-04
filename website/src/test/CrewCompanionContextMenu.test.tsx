/**
 * Can the pet overlay's right-click menu be dismissed by clicking away?
 *
 * The overlay window is click-through except over the rects the renderer reports to
 * the main process. The menu used to report only its OWN box, so a click just
 * outside it was forwarded to the desktop and never reached this page — the
 * close-on-outside listener could never fire and the menu was stuck open ("I can no
 * longer click anywhere to dismiss it"). On top of that, `pet.html` sets
 * `pointer-events: none` on html AND body, so an empty-area click hits no DOM
 * element and dispatches no event at all.
 *
 * The fix, pinned here: while the menu is open in the overlay it reports the WHOLE
 * viewport as its interactive region (so the overlay accepts clicks everywhere), and
 * it renders a transparent full-viewport backdrop (a real element the outside click
 * can land on). The rect is cleared the instant the menu closes, so no stale
 * full-screen hitbox is left capturing the user's screen.
 *
 * These are DOM-level assertions — they bypass the OS hit-test entirely, so a pass
 * here means the renderer half is correct; the Electron half is pinned separately in
 * electron/crew-companion/test/petHitbox.test.js.
 */
import { useState } from 'react'
import { describe, it, expect, vi, afterEach } from 'vitest'
import { render, fireEvent, cleanup, act } from '@testing-library/react'

import { ContextMenu, type ContextMenuEntry } from '../apps/crew-companion/ContextMenu'
import { petBridge } from '../apps/crew-companion/petBridge'
import {
  ContextMenu as MochiContextMenu,
  type ContextMenuEntry as MochiContextMenuEntry,
} from '../apps/mochi/src/renderer/ContextMenu'

/*
 * The mochi copy reaches the main process through `api` (its ONE seam, see
 * mochiApi.ts) — stub it so importing the component does not drag the real
 * preload surface into this suite. Every method it touches is optional and
 * guarded with `?.` there, so an empty object is a faithful "no host" stand-in;
 * these keyboard tests never open in overlay mode (`reportHitbox`) anyway. The
 * crew-companion blocks above go through `petBridge` instead and are untouched
 * by this mock.
 */
vi.mock('../apps/mochi/src/mochiApi', () => ({ api: {} }))

const items: ContextMenuEntry[] = [
  { label: 'Change avatar', action: 'gallery' },
  { separator: true },
  { label: 'Turn off companion', action: 'quit', danger: true },
]

/*
 * Three rows with a separator in the middle, mirroring `mochiItems` below, so
 * the companion keyboard-contract block can assert the same walk/skip/wrap
 * shape the mochi block pins (#6266 ports #6231's contract onto this copy).
 */
const companionKbItems: ContextMenuEntry[] = [
  { label: 'Change avatar', action: 'gallery' },
  { label: 'Hide', action: 'hide' },
  { separator: true },
  { label: 'Turn off companion', action: 'quit', danger: true },
]

afterEach(() => {
  cleanup()
  vi.restoreAllMocks()
})

describe('overlay context menu — click-outside dismissal', () => {
  it('reports the WHOLE viewport as its hitbox while open, not just the menu box', () => {
    const setMenuHitbox = vi.spyOn(petBridge, 'setMenuHitbox').mockImplementation(() => {})
    render(
      <ContextMenu x={10} y={10} items={items} reportHitbox onAction={() => {}} onClose={() => {}} />,
    )
    // Full viewport, so the overlay captures a click anywhere while the menu is open.
    // Reporting only the small menu box is exactly what let outside clicks fall
    // through to the desktop and left the menu undismissable.
    expect(setMenuHitbox).toHaveBeenCalledWith({
      x: 0,
      y: 0,
      w: window.innerWidth,
      h: window.innerHeight,
    })
  })

  it('renders a transparent full-viewport backdrop, and clicking it dismisses the menu', () => {
    const onClose = vi.fn()
    const { container } = render(
      <ContextMenu x={10} y={10} items={items} reportHitbox onAction={() => {}} onClose={onClose} />,
    )
    const backdrop = container.querySelector('.cc-menu-backdrop') as HTMLElement | null
    expect(backdrop).not.toBeNull()
    // The backdrop is the real DOM element the outside click lands on — without it the
    // overlay's pointer-events:none body would swallow the event before any handler.
    expect(backdrop!.style.pointerEvents).toBe('auto')
    expect(backdrop!.style.background).toBe('transparent')
    fireEvent.mouseDown(backdrop!)
    expect(onClose).toHaveBeenCalledTimes(1)
  })

  it('clears its hitbox on close, so no stale full-screen rect keeps capturing the screen', () => {
    const setMenuHitbox = vi.spyOn(petBridge, 'setMenuHitbox').mockImplementation(() => {})
    const { unmount } = render(
      <ContextMenu x={10} y={10} items={items} reportHitbox onAction={() => {}} onClose={() => {}} />,
    )
    setMenuHitbox.mockClear()
    unmount()
    expect(setMenuHitbox).toHaveBeenCalledWith(null)
  })

  it('selecting an item still closes the menu and fires its action', () => {
    const onClose = vi.fn()
    const onAction = vi.fn()
    const { getByText } = render(
      <ContextMenu x={10} y={10} items={items} reportHitbox onAction={onAction} onClose={onClose} />,
    )
    fireEvent.click(getByText('Change avatar'))
    expect(onClose).toHaveBeenCalledTimes(1)
    expect(onAction).toHaveBeenCalledWith('gallery')
  })

  it('Escape still closes the menu', () => {
    vi.useFakeTimers()
    try {
      const onClose = vi.fn()
      render(
        <ContextMenu x={10} y={10} items={items} reportHitbox onAction={() => {}} onClose={onClose} />,
      )
      // The key listener is attached one tick after open (so the opening click does
      // not self-close it); step past that guard before dispatching.
      vi.advanceTimersByTime(60)
      window.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape' }))
      expect(onClose).toHaveBeenCalledTimes(1)
    } finally {
      vi.useRealTimers()
    }
  })
})

describe('chat context menu — unchanged by the overlay fix', () => {
  it('does not render the overlay backdrop or report a hitbox when reportHitbox is off', () => {
    const setMenuHitbox = vi.spyOn(petBridge, 'setMenuHitbox').mockImplementation(() => {})
    const { container } = render(
      <ContextMenu x={10} y={10} items={items} onAction={() => {}} onClose={() => {}} />,
    )
    expect(container.querySelector('.cc-menu-backdrop')).toBeNull()
    expect(setMenuHitbox).not.toHaveBeenCalled()
  })
})

/*
 * ───── mochi's ContextMenu: the role="menu" keyboard contract (#6231) ─────
 *
 * A second, independently-vendored copy of this component lives at
 * `src/apps/mochi/src/renderer/ContextMenu.tsx`, and it is the copy that
 * actually puts `role="menu"` on its container. That role is a PROMISE to
 * assistive technology — "focus is managed here, use the arrow keys" — and
 * before #6231 the menu made it and then ignored every arrow: the rows were
 * `tabIndex={0}` divs with an Enter/Space handler and nothing else, so a screen
 * reader announced an arrow-navigable menu that only responded to Tab. The
 * tests below pin the shared contract (`useMenuKeyboard`) onto that surface,
 * and pin the behaviour it must NOT disturb.
 *
 * They live in this file because it is the repo's only ContextMenu test file;
 * the crew-companion describes above are untouched.
 */

const mochiItems: MochiContextMenuEntry[] = [
  { label: 'Change avatar', action: 'gallery' },
  { label: 'Hide', action: 'hide' },
  // A separator between the middle and last rows: it is a `role="separator"`
  // div, NOT a menuitem, so it must never receive focus — arrow navigation has
  // to step straight over it rather than parking a keyboard user on a divider.
  { separator: true },
  { label: 'Turn off companion', action: 'quit', danger: true },
]

function renderMochiMenu(handlers: { onAction?: (a: string) => void; onClose?: () => void } = {}) {
  return render(
    <MochiContextMenu
      x={10}
      y={10}
      items={mochiItems}
      onAction={handlers.onAction ?? (() => {})}
      onClose={handlers.onClose ?? (() => {})}
    />,
  )
}

/**
 * The menu's focusable rows in document order. Queried from the DOM rather than
 * derived from `mochiItems` so the separator's absence from this list is a fact
 * about the rendered markup, which is what the arrow keys actually walk.
 */
function menuRows(container: HTMLElement): HTMLElement[] {
  return Array.from(container.querySelectorAll<HTMLElement>('[role="menuitem"]'))
}

describe('mochi context menu — role="menu" keyboard contract', () => {
  it('moves DOM focus onto the first menuitem when the menu opens', () => {
    const { container } = renderMochiMenu()
    const rows = menuRows(container)
    expect(rows).toHaveLength(3)
    // role="menu" says focus is managed here, so a keyboard user has to LAND
    // inside the menu they were just told is open — otherwise the first arrow
    // is spent entering the list instead of choosing a row.
    expect(rows[0]).toHaveFocus()
  })

  it('ArrowDown walks the menuitems in order, skips the separator, and wraps last → first', () => {
    const { container } = renderMochiMenu()
    const rows = menuRows(container)
    fireEvent.keyDown(document.body, { key: 'ArrowDown' })
    expect(rows[1]).toHaveFocus()
    // The separator sits between rows[1] and rows[2] in the markup; one
    // ArrowDown crosses it.
    fireEvent.keyDown(document.body, { key: 'ArrowDown' })
    expect(rows[2]).toHaveFocus()
    // Wrap, not clamp: this is the menu contract, not the listbox one.
    fireEvent.keyDown(document.body, { key: 'ArrowDown' })
    expect(rows[0]).toHaveFocus()
  })

  it('ArrowUp wraps first → last and then walks back up, skipping the separator', () => {
    const { container } = renderMochiMenu()
    const rows = menuRows(container)
    fireEvent.keyDown(document.body, { key: 'ArrowUp' })
    expect(rows[2]).toHaveFocus()
    fireEvent.keyDown(document.body, { key: 'ArrowUp' })
    expect(rows[1]).toHaveFocus()
  })

  it('Home and End jump to the boundary menuitems', () => {
    const { container } = renderMochiMenu()
    const rows = menuRows(container)
    fireEvent.keyDown(document.body, { key: 'End' })
    expect(rows[2]).toHaveFocus()
    fireEvent.keyDown(document.body, { key: 'Home' })
    expect(rows[0]).toHaveFocus()
  })

  it('contains Tab inside the menu: last → first, and Shift-Tab first → last', () => {
    const { container } = renderMochiMenu()
    const rows = menuRows(container)
    // Focused directly rather than arrowed to, so this test fails only on the
    // Tab containment it is about (#2533) and not on the arrow keys.
    rows[2].focus()
    // A Tab out of a still-open menu drops a keyboard user behind it with no
    // obvious way back — the menu keeps the cycle.
    fireEvent.keyDown(document.body, { key: 'Tab' })
    expect(rows[0]).toHaveFocus()
    fireEvent.keyDown(document.body, { key: 'Tab', shiftKey: true })
    expect(rows[2]).toHaveFocus()
  })

  it('PIN: Enter on the focused menuitem still fires onAction and closes', () => {
    const onAction = vi.fn()
    const onClose = vi.fn()
    const { container } = renderMochiMenu({ onAction, onClose })
    const rows = menuRows(container)
    rows[1].focus()
    // The per-row Enter/Space handler is the menu's activation path and is NOT
    // moved into the shared hook (the hook deliberately owns navigation only).
    fireEvent.keyDown(rows[1], { key: 'Enter' })
    expect(onAction).toHaveBeenCalledWith('hide')
    expect(onClose).toHaveBeenCalledTimes(1)
  })

  it('PIN: Escape still closes the menu', () => {
    vi.useFakeTimers()
    try {
      const onClose = vi.fn()
      renderMochiMenu({ onClose })
      // Same 50ms guard as the crew-companion copy: the closing listeners are
      // attached one tick late so the click that OPENED the menu cannot
      // immediately dismiss it. Step past it before dispatching.
      vi.advanceTimersByTime(60)
      window.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape' }))
      expect(onClose).toHaveBeenCalledTimes(1)
    } finally {
      vi.useRealTimers()
    }
  })

  it('PIN: entering and moving focus does not self-close the menu', () => {
    vi.useFakeTimers()
    try {
      const onClose = vi.fn()
      const { container } = renderMochiMenu({ onClose })
      // Past the 50ms guard, so the close-on-window-blur listener is live for
      // the focus move below — the case worth pinning, because focus entry
      // blurs whatever held focus before and an arrow blurs a row on every
      // step. Those are ELEMENT blurs, which do not bubble, and the closer
      // listens for WINDOW blur; if that ever changed, this menu would close
      // itself the moment it opened.
      vi.advanceTimersByTime(60)
      const rows = menuRows(container)
      expect(rows[0]).toHaveFocus()
      expect(onClose).not.toHaveBeenCalled()
      fireEvent.keyDown(document.body, { key: 'ArrowDown' })
      expect(rows[1]).toHaveFocus()
      expect(onClose).not.toHaveBeenCalled()
    } finally {
      vi.useRealTimers()
    }
  })
})

/*
 * ───── mochi's ContextMenu: focus RESTORE on close (#6267 review) ─────
 *
 * The block above pins focus ENTRY: the menu now moves DOM focus onto its first
 * row when it opens. That half alone is a regression, because the row holding
 * focus is about to be DESTROYED — every real host (PetContextMenu, ChatPanel)
 * renders this component conditionally, so "close" means unmount, and unmounting
 * the focused element drops focus to `<body>`. A keyboard user who pressed
 * Escape, or picked a row, would land nowhere: the next Tab restarts from the
 * top of the document instead of resuming beside the thing they right-clicked.
 * Before #6231 nothing focused the rows at all, so nothing was lost on close —
 * the entry is what created the debt.
 *
 * The tests in the block above cannot see any of this: they pass a `vi.fn()`
 * `onClose` that records the call and leaves the menu mounted, which is the one
 * arrangement in which the bug is invisible. So these tests render the menu
 * inside a MINIMAL CONTROLLED HOST that behaves like the real ones — `onClose`
 * flips a `useState` flag and the menu genuinely unmounts — with a real
 * focusable opener button standing in for the surface that was right-clicked.
 */

/**
 * A stand-in for PetContextMenu/ChatPanel: holds the open flag, unmounts the
 * menu when `onClose` fires, and owns two focusable buttons — the OPENER (focus
 * should come back here) and an unrelated one, used by the two PIN cases below
 * to play the part of "focus moved somewhere else on purpose".
 */
function MochiMenuHost({ onAction }: { onAction?: (action: string) => void }) {
  const [open, setOpen] = useState(false)
  return (
    <div>
      <button data-testid="opener" onClick={() => setOpen(true)}>Open menu</button>
      <button data-testid="elsewhere">Somewhere else</button>
      {open ? (
        <MochiContextMenu
          x={10}
          y={10}
          items={mochiItems}
          onAction={(action) => onAction?.(action)}
          onClose={() => setOpen(false)}
        />
      ) : null}
    </div>
  )
}

describe('mochi context menu — focus returns to the opener on close (#6267 review)', () => {
  /**
   * Focus the opener the way a keyboard user leaves it (the context menu is
   * reached from the element that already had focus), then open the menu and
   * assert the entry landed — so a failure below is about RESTORE and not about
   * the harness never having opened the menu.
   */
  function openFromOpener(onAction?: (action: string) => void) {
    const utils = render(<MochiMenuHost onAction={onAction} />)
    const opener = utils.getByTestId('opener')
    opener.focus()
    expect(opener).toHaveFocus()
    fireEvent.click(opener)
    const rows = menuRows(utils.container)
    expect(rows).toHaveLength(3)
    expect(rows[0]).toHaveFocus()
    return { ...utils, opener, rows }
  }

  it('Escape unmounts the menu and returns focus to the opener', () => {
    vi.useFakeTimers()
    try {
      const { container, opener } = openFromOpener()
      // Same 50ms guard as everywhere else in this file: the closing listeners
      // are attached one tick late so the click that OPENED the menu cannot
      // dismiss it. Inside `act` because advancing timers runs React effects.
      act(() => { vi.advanceTimersByTime(60) })
      fireEvent.keyDown(window, { key: 'Escape' })
      // The host really unmounted it — this is the condition the mocked-onClose
      // tests above never reproduce.
      expect(menuRows(container)).toHaveLength(0)
      expect(opener).toHaveFocus()
    } finally {
      vi.useRealTimers()
    }
  })

  it('clicking a menuitem unmounts the menu and returns focus to the opener', () => {
    const onAction = vi.fn()
    const { container, opener, rows } = openFromOpener(onAction)
    fireEvent.click(rows[1])
    expect(onAction).toHaveBeenCalledWith('hide')
    expect(menuRows(container)).toHaveLength(0)
    expect(opener).toHaveFocus()
  })

  it('Enter on the focused menuitem unmounts the menu and returns focus to the opener', () => {
    const onAction = vi.fn()
    const { container, opener, rows } = openFromOpener(onAction)
    // Activation from the keyboard is the path that MATTERS for restore: this
    // user has no pointer to re-establish a focus position with.
    fireEvent.keyDown(rows[0], { key: 'Enter' })
    expect(onAction).toHaveBeenCalledWith('gallery')
    expect(menuRows(container)).toHaveLength(0)
    expect(opener).toHaveFocus()
  })

  it('PIN: an action that moves focus elsewhere ends with focus where the ACTION put it, not on the opener', () => {
    // Real actions open other surfaces (settings, the dashboard) and focus
    // something there. Whatever the close/restore ordering, the user must end up
    // where the action sent them.
    //
    // Labelled a PIN rather than a guard test on purpose: `handleAction` calls
    // `onClose()` before `onAction()`, and in this host the state flush unmounts
    // the menu inside that first call — so focus is still on a menu row when the
    // restore cleanup runs, the restore fires, and the action's own `focus()`
    // lands afterwards and wins on ordering. The `contains` guard is therefore
    // NOT what makes this pass; the test below is the one that exercises it.
    let elsewhere: HTMLElement | null = null
    const utils = render(<MochiMenuHost onAction={() => { elsewhere?.focus() }} />)
    elsewhere = utils.getByTestId('elsewhere')
    const opener = utils.getByTestId('opener')
    opener.focus()
    fireEvent.click(opener)
    const rows = menuRows(utils.container)
    expect(rows[0]).toHaveFocus()
    fireEvent.click(rows[1])
    expect(menuRows(utils.container)).toHaveLength(0)
    expect(elsewhere).toHaveFocus()
    expect(opener).not.toHaveFocus()
  })

  it('PIN: dismissing by an outside click leaves focus where the click put it — it is not yanked back to the opener', () => {
    // The everyday shape of "focus already left the menu on its own": the user
    // clicked another control, which both focused it and dismissed the menu.
    // Focus must stay on the control they chose.
    //
    // Labelled a PIN, not a guard test, and the reason is worth writing down:
    // this passes even with the `contains(...)` guard REMOVED, because react-dom
    // snapshots `document.activeElement` before the mutation phase and re-focuses
    // it after the commit whenever that node is still in the document. So an
    // unconditional restore is overwritten right back to `elsewhere` here. The
    // guard is therefore belt-and-braces in this path rather than the thing that
    // makes this test pass — no closed-box test through React's unmount can
    // separate the two. Kept because it states the intent at the site, and
    // because it is the only thing protecting the invariant if the restore is
    // ever moved off the commit path (e.g. into an explicit Escape handler, the
    // shape `SlotPopover` uses).
    vi.useFakeTimers()
    try {
      const utils = render(<MochiMenuHost />)
      const opener = utils.getByTestId('opener')
      const elsewhere = utils.getByTestId('elsewhere')
      opener.focus()
      fireEvent.click(opener)
      expect(menuRows(utils.container)[0]).toHaveFocus()
      // Past the 50ms guard so the close-on-outside-mousedown listener is live.
      act(() => { vi.advanceTimersByTime(60) })
      // A real click on another control focuses it and then dismisses the menu.
      elsewhere.focus()
      fireEvent.mouseDown(document.body)
      expect(menuRows(utils.container)).toHaveLength(0)
      expect(elsewhere).toHaveFocus()
      expect(opener).not.toHaveFocus()
    } finally {
      vi.useRealTimers()
    }
  })
})

/*
 * ───── crew-companion's ContextMenu: the role="menu" keyboard contract (#6266) ─────
 *
 * The crew-companion copy is the separately-vendored sibling of the mochi menu
 * above. Its rows carried `role="menuitem"` but its container declared no
 * `role="menu"` and its dividers no `role="separator"` — an ARIA structural
 * violation (menuitem requires a menu/menubar ancestor) — and, because #6231's
 * inventory enumerated `role="menu"` CONTAINERS, this file was invisible to it
 * and never received the shared `useMenuKeyboard` wiring. These tests mirror
 * the mochi block above onto the companion surface, plus the roles themselves.
 */

function renderCompanionKbMenu(
  handlers: { onAction?: (a: string) => void; onClose?: () => void; reportHitbox?: boolean } = {},
) {
  return render(
    <ContextMenu
      x={10}
      y={10}
      items={companionKbItems}
      reportHitbox={handlers.reportHitbox}
      onAction={handlers.onAction ?? (() => {})}
      onClose={handlers.onClose ?? (() => {})}
    />,
  )
}

describe('crew-companion context menu — role="menu" keyboard contract (#6266)', () => {
  it('declares role="menu" on the container and role="separator" on the divider', () => {
    // Rendered in OVERLAY mode (reportHitbox), the configuration the only
    // production host (PetContextMenu) actually uses — the backdrop is a
    // role="presentation" sibling outside the menu container and must not
    // perturb the roles or the item list.
    const setMenuHitbox = vi.spyOn(petBridge, 'setMenuHitbox').mockImplementation(() => {})
    const { container } = renderCompanionKbMenu({ reportHitbox: true })
    expect(setMenuHitbox).toHaveBeenCalled()
    // The container is the element that owns the menuitem rows; without
    // role="menu" they are orphaned and the ARIA structure is invalid.
    const menu = container.querySelector('[role="menu"]') as HTMLElement | null
    expect(menu).not.toBeNull()
    expect(menuRows(menu!)).toHaveLength(3)
    const separator = menu!.querySelector('[role="separator"]')
    expect(separator).not.toBeNull()
  })

  it('moves DOM focus onto the first menuitem when the menu opens', () => {
    const { container } = renderCompanionKbMenu()
    const rows = menuRows(container)
    expect(rows).toHaveLength(3)
    expect(rows[0]).toHaveFocus()
  })

  it('ArrowDown walks the menuitems in order, skips the separator, and wraps last → first', () => {
    const { container } = renderCompanionKbMenu()
    const rows = menuRows(container)
    fireEvent.keyDown(document.body, { key: 'ArrowDown' })
    expect(rows[1]).toHaveFocus()
    // The separator sits between rows[1] and rows[2] in the markup; one
    // ArrowDown crosses it.
    fireEvent.keyDown(document.body, { key: 'ArrowDown' })
    expect(rows[2]).toHaveFocus()
    // Wrap, not clamp: this is the menu contract, not the listbox one.
    fireEvent.keyDown(document.body, { key: 'ArrowDown' })
    expect(rows[0]).toHaveFocus()
  })

  it('ArrowUp wraps first → last and then walks back up, skipping the separator', () => {
    const { container } = renderCompanionKbMenu()
    const rows = menuRows(container)
    fireEvent.keyDown(document.body, { key: 'ArrowUp' })
    expect(rows[2]).toHaveFocus()
    fireEvent.keyDown(document.body, { key: 'ArrowUp' })
    expect(rows[1]).toHaveFocus()
  })

  it('Home and End jump to the boundary menuitems', () => {
    const { container } = renderCompanionKbMenu()
    const rows = menuRows(container)
    fireEvent.keyDown(document.body, { key: 'End' })
    expect(rows[2]).toHaveFocus()
    fireEvent.keyDown(document.body, { key: 'Home' })
    expect(rows[0]).toHaveFocus()
  })

  it('contains Tab inside the menu: last → first, and Shift-Tab first → last', () => {
    const { container } = renderCompanionKbMenu()
    const rows = menuRows(container)
    // Focused directly rather than arrowed to, so this test fails only on the
    // Tab containment it is about (#2533) and not on the arrow keys.
    rows[2].focus()
    fireEvent.keyDown(document.body, { key: 'Tab' })
    expect(rows[0]).toHaveFocus()
    fireEvent.keyDown(document.body, { key: 'Tab', shiftKey: true })
    expect(rows[2]).toHaveFocus()
  })

  it('PIN: Enter on the focused menuitem still fires onAction and closes', () => {
    const onAction = vi.fn()
    const onClose = vi.fn()
    const { container } = renderCompanionKbMenu({ onAction, onClose })
    const rows = menuRows(container)
    rows[1].focus()
    // The per-row Enter/Space handler is the menu's activation path and is NOT
    // moved into the shared hook (the hook deliberately owns navigation only).
    fireEvent.keyDown(rows[1], { key: 'Enter' })
    expect(onAction).toHaveBeenCalledWith('hide')
    expect(onClose).toHaveBeenCalledTimes(1)
  })

  it('PIN: entering and moving focus does not self-close the menu', () => {
    vi.useFakeTimers()
    try {
      const onClose = vi.fn()
      const { container } = renderCompanionKbMenu({ onClose })
      // Past the 50ms guard, so the close-on-window-blur listener is live for
      // the focus moves below — element blurs do not bubble and must not trip
      // the WINDOW blur closer.
      vi.advanceTimersByTime(60)
      const rows = menuRows(container)
      expect(rows[0]).toHaveFocus()
      expect(onClose).not.toHaveBeenCalled()
      fireEvent.keyDown(document.body, { key: 'ArrowDown' })
      expect(rows[1]).toHaveFocus()
      expect(onClose).not.toHaveBeenCalled()
    } finally {
      vi.useRealTimers()
    }
  })
})

/*
 * ───── crew-companion's ContextMenu: focus RESTORE on close ─────
 *
 * Focus entry (pinned above) without a matching restore strands a keyboard
 * user on `<body>` when the focused row unmounts — the same debt #6267's
 * review called out on the mochi copy. Same minimal-controlled-host shape as
 * the mochi restore block above: `onClose` flips real state so the menu
 * genuinely unmounts, with a focusable opener standing in for the surface
 * that was right-clicked.
 */

function CompanionMenuHost({ onAction }: { onAction?: (action: string) => void }) {
  const [open, setOpen] = useState(false)
  return (
    <div>
      <button data-testid="cc-opener" onClick={() => setOpen(true)}>Open menu</button>
      <button data-testid="cc-elsewhere">Somewhere else</button>
      {open ? (
        <ContextMenu
          x={10}
          y={10}
          items={companionKbItems}
          onAction={(action) => onAction?.(action)}
          onClose={() => setOpen(false)}
        />
      ) : null}
    </div>
  )
}

describe('crew-companion context menu — focus returns to the opener on close (#6266)', () => {
  function openFromOpener(onAction?: (action: string) => void) {
    const utils = render(<CompanionMenuHost onAction={onAction} />)
    const opener = utils.getByTestId('cc-opener')
    opener.focus()
    expect(opener).toHaveFocus()
    fireEvent.click(opener)
    const rows = menuRows(utils.container)
    expect(rows).toHaveLength(3)
    expect(rows[0]).toHaveFocus()
    return { ...utils, opener, rows }
  }

  it('Escape unmounts the menu and returns focus to the opener', () => {
    vi.useFakeTimers()
    try {
      const { container, opener } = openFromOpener()
      // Past the 50ms listener guard; inside `act` because advancing timers
      // runs React effects.
      act(() => { vi.advanceTimersByTime(60) })
      fireEvent.keyDown(window, { key: 'Escape' })
      expect(menuRows(container)).toHaveLength(0)
      expect(opener).toHaveFocus()
    } finally {
      vi.useRealTimers()
    }
  })

  it('Enter on the focused menuitem unmounts the menu and returns focus to the opener', () => {
    const onAction = vi.fn()
    const { container, opener, rows } = openFromOpener(onAction)
    // Activation from the keyboard is the path that MATTERS for restore: this
    // user has no pointer to re-establish a focus position with.
    fireEvent.keyDown(rows[0], { key: 'Enter' })
    expect(onAction).toHaveBeenCalledWith('gallery')
    expect(menuRows(container)).toHaveLength(0)
    expect(opener).toHaveFocus()
  })

  it('clicking a menuitem unmounts the menu and returns focus to the opener', () => {
    const onAction = vi.fn()
    const { container, opener, rows } = openFromOpener(onAction)
    fireEvent.click(rows[1])
    expect(onAction).toHaveBeenCalledWith('hide')
    expect(menuRows(container)).toHaveLength(0)
    expect(opener).toHaveFocus()
  })

  it('PIN: an action that moves focus elsewhere ends with focus where the ACTION put it, not on the opener', () => {
    // Real actions open other surfaces and focus something there. Whatever the
    // close/restore ordering, the user must end up where the action sent them.
    // Same PIN as the mochi block above (and the same caveat: `handleAction`
    // calls onClose() before onAction(), so the action's own focus() lands
    // after the restore and wins on ordering — the contains guard is not what
    // makes this pass, but the invariant is worth stating on this copy too).
    let elsewhere: HTMLElement | null = null
    const utils = render(<CompanionMenuHost onAction={() => { elsewhere?.focus() }} />)
    elsewhere = utils.getByTestId('cc-elsewhere')
    const opener = utils.getByTestId('cc-opener')
    opener.focus()
    fireEvent.click(opener)
    const rows = menuRows(utils.container)
    expect(rows[0]).toHaveFocus()
    fireEvent.click(rows[1])
    expect(menuRows(utils.container)).toHaveLength(0)
    expect(elsewhere).toHaveFocus()
    expect(opener).not.toHaveFocus()
  })

  it('PIN: dismissing by an outside click leaves focus where the click put it — it is not yanked back to the opener', () => {
    // The everyday shape of "focus already left the menu on its own": the user
    // clicked another control, which both focused it and dismissed the menu.
    // Same belt-and-braces caveat as the mochi copy of this PIN: react-dom's
    // commit-phase focus snapshot also defends this, so the test protects the
    // invariant rather than discriminating the contains guard.
    vi.useFakeTimers()
    try {
      const utils = render(<CompanionMenuHost />)
      const opener = utils.getByTestId('cc-opener')
      const elsewhere = utils.getByTestId('cc-elsewhere')
      opener.focus()
      fireEvent.click(opener)
      expect(menuRows(utils.container)[0]).toHaveFocus()
      // Past the 50ms guard so the close-on-outside-mousedown listener is live.
      act(() => { vi.advanceTimersByTime(60) })
      // A real click on another control focuses it and then dismisses the menu.
      elsewhere.focus()
      fireEvent.mouseDown(document.body)
      expect(menuRows(utils.container)).toHaveLength(0)
      expect(elsewhere).toHaveFocus()
      expect(opener).not.toHaveFocus()
    } finally {
      vi.useRealTimers()
    }
  })
})
