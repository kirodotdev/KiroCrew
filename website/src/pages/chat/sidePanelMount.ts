import type { TargetAndTransition, Transition } from 'framer-motion'

/**
 * When the tabbed side panel must stay MOUNTED.
 *
 * An MCP App tab hosts a null-origin iframe (`sandbox="allow-scripts
 * allow-forms"`, no `allow-same-origin`) with no storage. Unmounting it reloads
 * the app and destroys whatever the user has drawn — there is nothing to restore
 * from. See `docs/architecture/dashboard-iframe-hosts.md`.
 *
 * An app-contributed tab (`contributes.panelTabs`) is the same class of loss for
 * a different reason: it mounts the app's bundle in-process through `AppHost`, so
 * an unmount discards the app component's own in-body state — an unsaved form, a
 * scroll position, an editor buffer — which nothing outside the component holds.
 *
 * The panel is normally render-gated on `activityOpen`, so closing it unmounts
 * the whole subtree. While such a tab is live we keep the subtree mounted and
 * hide it instead, matching the hide-not-unmount rule SidePanel already applies
 * to its own tab bodies and `InstancesViewport` applies to instance frames.
 *
 * With no app tab the behaviour is unchanged, which preserves the existing
 * width exit animation on close.
 */
export interface SidePanelMountInput {
  /** User-facing open/closed state of the panel. */
  activityOpen: boolean
  /** True while at least one body-owning app tab exists in ANY slot's strip: an
   *  MCP `app` render, or an app-contributed tab (`app:<appName>:<id>`) whose
   *  `AppHost` holds the app component's own in-body state. */
  hasLiveAppTab: boolean
  /** True while a `browser` tab is live. Its Electron WebContentsView is
   *  destroyed on unmount, so — like an app tab — closing the panel must hide,
   *  not unmount, or the loaded page is lost. */
  hasBrowserTab: boolean
  /** The find pane takes the dock slot exclusively. */
  searchOpen: boolean
}

/** Whether to render the panel subtree at all.
 *
 *  A live app tab makes this UNCONDITIONAL. Everything else — the user closing
 *  the panel, the find pane claiming the dock — controls *visibility* only
 *  (`isSidePanelHidden`), never mounting. Deciding not to mount is deciding to
 *  destroy the drawing, and no transient UI state is worth that. A live
 *  `browser` tab is kept mounted for the same reason: its WebContentsView is
 *  destroyed on unmount, so a close would lose the loaded page.
 *
 *  With no app tab, behaviour is exactly as before: the find pane takes the dock
 *  exclusively and closing the panel unmounts it, preserving the exit animation. */
export function shouldMountSidePanel({ activityOpen, hasLiveAppTab, hasBrowserTab, searchOpen }: SidePanelMountInput): boolean {
  if (hasLiveAppTab || hasBrowserTab) return true
  if (searchOpen) return false
  return activityOpen
}

/** Whether the rendered subtree should be visually hidden (mounted, not shown).
 *
 *  Two reasons to hide: the user closed the panel, or the find pane owns the dock.
 *  Both are reachable only via the keep-mounted path — with no app tab those
 *  states unmount instead, so this never returns true for a panel the user has
 *  open and unobstructed. */
export function isSidePanelHidden(input: SidePanelMountInput): boolean {
  if (!shouldMountSidePanel(input)) return false
  return input.searchOpen || !input.activityOpen
}

/**
 * The panel's open/close timing, defined ONCE.
 *
 * Two different mechanisms animate the same edge and a person reads them as one
 * motion: the dock wrapper grows `width: 0 -> auto` with framer
 * (`SIDE_PANEL_DOCK_TRANSITION`), while the panel itself eases between two
 * remembered per-group widths with a CSS transition (`sidePanelDimTransition`).
 * A framer tween and a CSS transition cannot share a code path, so they share
 * these constants instead — otherwise the two curves drift apart in two files
 * and opening the panel stops matching moving between tabs inside it.
 */
export const SIDE_PANEL_MOTION_MS = 180
/** House easing, the same curve the shell uses for its own column and rail. */
export const SIDE_PANEL_MOTION_EASE: [number, number, number, number] = [0.2, 0, 0, 1]

/** The tween as framer's own transition, for the dock wrapper's grow/collapse. */
export const SIDE_PANEL_DOCK_TRANSITION: Transition = {
  duration: SIDE_PANEL_MOTION_MS / 1000,
  ease: SIDE_PANEL_MOTION_EASE,
}

/**
 * The same tween as a CSS `transition` declaration for one property.
 *
 * Derived from the constants rather than written out, so the CSS curve is the
 * framer curve by construction and a reviewer does not have to compare two
 * bezier literals by eye.
 *
 * Returns the declaration rather than a bare string so the duration stays in a
 * `transition` position: `180ms` is a stylesheet token the browser's CSS parser
 * reads, never copy a person reads, and the i18n unit-literal check recognises
 * that only from the key the value sits under. Spread it into a style object.
 */
export function sidePanelDimTransition(prop: 'width' | 'height'): { transition: string } {
  return { transition: `${prop} ${SIDE_PANEL_MOTION_MS}ms cubic-bezier(${SIDE_PANEL_MOTION_EASE.join(', ')})` }
}

/** One axis of the open/close animation, plus the cross axis held at its
 *  full-bleed size. Typed as framer-motion's own target so the three props can
 *  be spread onto `motion.div` without a cast — the animated axis travels
 *  between `0` and `'auto'`, which no narrower shape admits. */
export interface SidePanelDockMotion {
  initial: TargetAndTransition
  animate: TargetAndTransition
  exit: TargetAndTransition
}

/**
 * Motion targets for the side panel's dock wrapper: it grows along ONE axis —
 * width in the right column, height in the bottom row — while the other axis
 * stays full-bleed.
 *
 * Both axes appear in every target even though only one of them moves, and that
 * redundancy is the whole point. The wrapper has a STABLE React key, so flipping
 * the dock re-renders it rather than remounting it, and framer-motion does not
 * release a key that disappears from `animate`: it keeps owning the inline style
 * and holds the value it last resolved. Targeting one axis per dock therefore
 * left the flipped-away axis frozen at the size the OTHER dock gave it — a panel
 * sent to the bottom and brought back came home with the bottom row's height
 * (measured 850px -> 352px at a 900px viewport) pinned inline, and an inline
 * style outranks the `h-full` class that is supposed to size it. Naming the
 * cross axis keeps it under the animation's control at exactly the 100% its
 * class already asks for, so no axis is ever un-targeted and none can be frozen.
 */
export function sidePanelDockMotion(dock: 'right' | 'bottom'): SidePanelDockMotion {
  return dock === 'bottom'
    ? {
      initial: { height: 0, width: '100%', opacity: 0 },
      animate: { height: 'auto', width: '100%', opacity: 1 },
      exit: { height: 0, width: '100%', opacity: 0 },
    }
    : {
      initial: { width: 0, height: '100%', opacity: 0 },
      animate: { width: 'auto', height: '100%', opacity: 1 },
      exit: { width: 0, height: '100%', opacity: 0 },
    }
}
