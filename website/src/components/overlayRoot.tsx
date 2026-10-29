// Where the dashboard's floating layers (popovers, menus, tooltips, dialogs,
// full-screen views) are portalled, and which of them a Crew switch hides.
//
// Switching to a remote Crew HIDES the local dashboard (`display:none` on its
// pane in App) rather than unmounting it, so local state survives the switch.
// A layer portalled straight to `document.body` escapes that pane and would
// stay on screen over the remote Crew, still anchored to content nobody can
// see. So every layer the LOCAL pane renders portals into one body-level
// container instead, and that container is hidden with the pane.
//
// Ownership comes from the React tree, not from a per-layer flag: React context
// crosses portals, so a layer opened anywhere inside the local pane resolves to
// the local container, and a layer opened from chrome outside it (the remote
// pane's Crew tabs, the command palette, the shortcuts sheet) resolves to
// `document.body` and stays visible over the Crew that opened it.
//
// The container is a plain static `<div>` directly under `<body>`: it creates no
// stacking context and no containing block, so `position:fixed` and `z-index`
// on the layers inside it resolve against the page as they did on `<body>`. One
// thing differs: it is attached once, at boot, so at EQUAL z-index a local layer
// paints under any body-level layer, whichever opened first: across the two,
// document order is not opening order. Pick z-indexes that do not rely on ties;
// one tie exists today: `Modal` and a Radix dialog share z-[100]/z-[101], so a
// Modal opened from outside a dialog's subtree while it is open paints under it.
//
// A switch to a remote Crew is treated as a click outside the local Crew:
// - The shared Radix menus, context menus and selects, and the notification
//   sheet, close (`useCloseWhenCrewHidden`), as a click on the Crew switcher
//   would. They hold no input.
// - Radix popovers stay open and mounted (they hold input and work in flight:
//   the goal editor, the add-registry form and its pending save), and refuse
//   Escape and outside-click dismissal while hidden (`standDownWhileHidden`).
//   A popover is modal only inside a Radix dialog (`useInsideDialog`), which is
//   never hidden, so a hidden one never holds the page. While one is open and
//   hidden, an Escape aimed at the Crew in front is still claimed by it, as an
//   open popover claims Escape on screen. A new shared Radix wrapper does the same: pass
//   `container={useOverlayRoot()}` inside `MountAfterCommit` and call
//   `useCloseWhenCrewHidden`. Lint keeps Radix imports inside components/ui/.
// - Every other layer stays mounted in the hidden root and returns as it was:
//   the comment box and its draft, the full-screen views, the image and diagram
//   viewers, `Modal`, and the hand-rolled pickers and menus. Hiding stops
//   painting, not listening, so a key handler of theirs that commits, discards
//   or claims a key must read `useLocalCrewHidden` and stand down (as Modal,
//   useDialogFocusTrap, useListKeyboardNav and the viewers do).
// - Two exceptions stay on `document.body`, visible across the switch: a Radix
//   dialog and everything opened inside it (`GlobalOverlayScope`), because its
//   modal hold on the page cannot be lifted without remounting what the user
//   entered; and the notification banner, which is meant for every Crew.
import {
  createContext, useContext, useEffect, useInsertionEffect, useLayoutEffect, useMemo, useState,
  type ReactNode,
} from 'react'
import { createPortal } from 'react-dom'

interface OverlayRootValue {
  container: HTMLElement
  hidden: boolean
  /** Inside a Radix dialog's content (`GlobalOverlayScope`). */
  inDialog?: boolean
}

const OverlayRootContext = createContext<OverlayRootValue | null>(null)

/** The element a floating layer should portal into: the local container inside
 *  the local pane, `document.body` anywhere else. */
export function useOverlayRoot(): HTMLElement {
  return useContext(OverlayRootContext)?.container ?? document.body
}

/** Whether the pane that owns this component is hidden behind another Crew.
 *
 *  Hiding stops a layer painting, but not its document- and window-level key
 *  listeners: a handler that dismisses, saves or claims a key must read this and
 *  stand down, or Escape aimed at the Crew in front would close a dialog in the
 *  hidden one. Always false outside the local pane. */
export function useLocalCrewHidden(): boolean {
  return useContext(OverlayRootContext)?.hidden ?? false
}

/** `createPortal` into the owning pane's overlay root. Use this instead of
 *  portalling to `document.body` (lint refuses the latter). */
export function OverlayPortal({ children }: { children: ReactNode }) {
  return createPortal(children, useOverlayRoot())
}

/** Whether this component renders inside a Radix dialog's content. A modal
 *  popover is only meaningful there (it takes over the dialog's scroll lock);
 *  anywhere else its hold on the page would outlive a Crew switch unseen. */
export function useInsideDialog(): boolean {
  return useContext(OverlayRootContext)?.inDialog ?? false
}

/** For a Radix dismissal handler (`onEscapeKeyDown`, `onInteractOutside`) of a
 *  layer that survives a switch: while its Crew is hidden, refuse the dismissal,
 *  so a key or click aimed at the Crew in front cannot close it unseen. */
export function standDownWhileHidden<E extends { preventDefault(): void }>(
  hidden: boolean,
  handler?: (event: E) => void,
): ((event: E) => void) | undefined {
  return hidden ? event => event.preventDefault() : handler
}

/** Close a transient layer when its Crew is hidden behind a remote one, the
 *  way a click outside it would. `close` must be the layer's own close path, so
 *  a controlled caller hears `onOpenChange(false)`. */
export function useCloseWhenCrewHidden(open: boolean, close: () => void) {
  const hidden = useLocalCrewHidden()
  useEffect(() => { if (hidden && open) close() }, [hidden, open, close])
}

/** Puts a subtree back on `document.body` and visible, for content that stays
 *  on screen across a Crew switch (a Radix dialog's), so what it opens stacks
 *  above it as it does outside the local pane. */
export function GlobalOverlayScope({ children }: { children: ReactNode }) {
  const value = useMemo(() => ({ container: document.body, hidden: false, inDialog: true }), [])
  return <OverlayRootContext.Provider value={value}>{children}</OverlayRootContext.Provider>
}

/** Renders its children from the commit after it mounts.
 *
 *  For the shared Radix wrappers (`components/ui/*`), which pass `container`.
 *  A Radix portal given no container defers its content by one commit (it waits
 *  for `document.body` to exist), and passing one removes that deferral. The
 *  deferral is load-bearing for nested modal layers opened together: each layer
 *  marks everything outside itself `aria-hidden` when it mounts, so the outer
 *  layer must mount a commit before the inner one or it hides the inner one.
 *  Wrapping the portal in this restores the same order. It wraps the portal,
 *  never the portal's child: Radix hands that child the ref its exit animation
 *  watches, and a function component would drop it. */
export function MountAfterCommit({ children }: { children: ReactNode }) {
  const [mounted, setMounted] = useState(false)
  useLayoutEffect(() => setMounted(true), [])
  return mounted ? children : null
}

/** The local pane's overlay container, for a layer that renders OUTSIDE the
 *  local pane's React tree yet belongs to the local Crew (the enterprise relock
 *  notice, drawn by the gate that wraps App). Null before App mounts it, and in
 *  an embedded or test tree that has none. */
export function localOverlayContainer(): HTMLElement | null {
  return document.querySelector<HTMLElement>('[data-local-overlay-root]')
}

/** Owns the local pane's overlay container and hides it while `hidden`. */
export function LocalOverlayRoot({ hidden, children }: { hidden: boolean; children: ReactNode }) {
  const [container] = useState(() => {
    const el = document.createElement('div')
    el.dataset.localOverlayRoot = ''
    return el
  })
  // Attached in the insertion phase, which completes before any layout effect
  // runs, so a layer that measures itself on mount is already in the document.
  useInsertionEffect(() => {
    document.body.appendChild(container)
    return () => container.remove()
  }, [container])
  useLayoutEffect(() => {
    container.style.display = hidden ? 'none' : ''
  }, [container, hidden])
  const value = useMemo(() => ({ container, hidden }), [container, hidden])
  return <OverlayRootContext.Provider value={value}>{children}</OverlayRootContext.Provider>
}
