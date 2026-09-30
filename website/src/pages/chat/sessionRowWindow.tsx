import { createContext, useCallback, useContext, useLayoutEffect, useMemo, useRef, useState } from 'react'
import type { CSSProperties, ReactNode } from 'react'
import { ROW_META_CLS } from '../../components/listShell'

/**
 * Row windowing for the session sidebar lanes.
 *
 * WHY: the cost of the sidebar grows with the number of MOUNTED rows, not the
 * number of VISIBLE ones. Every SessionRow carries a react-query observer, half a
 * dozen store subscriptions, a dnd-kit draggable + droppable, a Framer motion
 * node and two Radix menus. Measured against the production bundle at 4x CPU
 * throttle, five session switches cost 2.4s of long tasks with 50 rows, 7.6s with
 * 300 and 18s with 600; capping the lane at 40 mounted rows brought 600 back to
 * the 50-row cost. `content-visibility: auto` on `.session-row` already skips
 * paint and layout for off-screen rows, but not the React and subscription work,
 * which is where the time goes.
 *
 * WHY NOT A LIST VIRTUALIZER (react-virtuoso, as LogsPage uses): the lanes are a
 * nested tree -- folders with sticky headers, collapsed folders that stay
 * mounted inert, stale sections, board columns -- with dnd-kit sortables across
 * it and several features that read row order and geometry back from the DOM
 * (keyboard nav, digit shortcuts, hover hold, scroll memory, reveal). A
 * virtualizer needs one flat item list and owns the DOM, so adopting one means
 * rebuilding all of that. This keeps every row's slot in the DOM, in order, and
 * swaps only its CONTENTS: a row far from the viewport renders a cheap stub of
 * the same height that carries the same `data-session-row` / scope / container
 * attributes, so everything that walks rows in DOM order keeps working.
 *
 * One IntersectionObserver per lane scroller, rooted on that scroller, with a
 * margin of MARGIN_PX on both edges: rows mount well before they scroll in and
 * unmount once they are that far out. Without IntersectionObserver (jsdom, old
 * engines) there is no window and rows render exactly as before, with no wrapper.
 */

/** How far outside the lane's visible box a row stays mounted, in px. About two
 *  viewports of 64px rows on each side: fast wheel scrolling stays ahead of it,
 *  and the live set is roughly 60 rows whatever the list size. */
export const SESSION_ROW_WINDOW_MARGIN_PX = 1200

/** Stub height before a row has been measured. Equal to what a never-painted
 *  real row measures today: `.session-row` is `content-visibility: auto` with
 *  `contain-intrinsic-size: auto 60px` (index.css), plus its 16px vertical
 *  padding. Matching it keeps the lane's scroll geometry what it was before
 *  windowing, which useLaneScrollMemory's settle loop is already built around.
 *  Once a row has painted, `auto` remembers its real size and the stub takes
 *  that instead (64px for a three-line row). Change the two together. */
export const SESSION_ROW_STUB_DEFAULT_PX = 76

/** How many rows of each window ROOT mount on their first render, before the
 *  observer has reported anything, so the initial paint shows real rows. Per
 *  root, not per lane: every board column is its own root and scrolls on its
 *  own, so each column gets its own first two viewports of 64px rows. This is
 *  its own number: ChatSidebar's SIDEBAR_DISPLACEMENT_WINDOW bounds which rows
 *  animate when the lane reorders, a separate concern that can change on its
 *  own without affecting this budget. */
export const SESSION_ROW_INITIAL_MOUNT_BUDGET = 48

type Listener = (near: boolean, height: number) => void

export interface SessionRowWindow {
  observe: (el: Element, listener: Listener) => () => void
  /** Claim an initial-mount position for a row mounting under this root. True
   *  while the root's budget for the current render pass is not spent. Keyed by
   *  row, so a StrictMode double-invoked initializer gets the same answer twice
   *  instead of spending two positions. */
  claimInitialMount: (key: string) => boolean
}

/** Measured heights, so a row that goes back to a stub keeps its box and the
 *  lane does not jump. Keyed by row identity AND nav scope: one session can
 *  render in two lanes (tree and conductor) at different heights. */
const heights = new Map<string, number>()

/** Test-only: forget measured heights between cases. */
export function _resetSessionRowWindowHeights(): void {
  heights.clear()
}

export const SessionRowWindowContext = createContext<SessionRowWindow | null>(null)

/**
 * The lane side: a stable window object plus the callback ref to hang on the
 * lane's scroll container. Returns `rowWindow: null` where IntersectionObserver is
 * unavailable, which turns windowing off for every row below it.
 *
 * The window object never changes identity for the life of the lane, including
 * while the root is not attached yet: rows decide once whether they are wrapped,
 * and a context value that flipped from null to an object would remount every
 * row in the lane.
 *
 * `initialMountBudget` is how many rows mount on their first render under this
 * root before the observer has spoken (default SESSION_ROW_INITIAL_MOUNT_BUDGET;
 * tests pass a small one). The budget belongs to one render pass of the root:
 * the root renders before its rows, so resetting the claims here hands each
 * pass a fresh first-N. Rows that mount in a later pass without the root
 * re-rendering claim from where the last pass left off and, past the budget,
 * start as stubs until the observer's first delivery, which is immediate.
 */
export function useSessionRowWindowRoot(initialMountBudget = SESSION_ROW_INITIAL_MOUNT_BUDGET): { rowWindow: SessionRowWindow | null; setRoot: (el: HTMLElement | null) => void } {
  const stateRef = useRef<{ root: HTMLElement | null; io: IntersectionObserver | null; targets: Map<Element, Listener>; claims: Map<string, number>; budget: number } | null>(null)
  if (stateRef.current == null) stateRef.current = { root: null, io: null, targets: new Map(), claims: new Map(), budget: initialMountBudget }
  // Each render of the root is a new pass over its rows: forget the last pass's
  // positions so the rows rendering under this pass count from zero again.
  stateRef.current.claims.clear()
  stateRef.current.budget = initialMountBudget
  const supported = typeof IntersectionObserver === 'function'

  const setRoot = useCallback((el: HTMLElement | null) => {
    const st = stateRef.current!
    if (!supported || el === st.root) return
    st.io?.disconnect()
    st.io = null
    st.root = el
    if (!el) return
    const io = new IntersectionObserver(entries => {
      for (const e of entries) st.targets.get(e.target)?.(e.isIntersecting, e.boundingClientRect.height)
    }, { root: el, rootMargin: `${SESSION_ROW_WINDOW_MARGIN_PX}px 0px` })
    st.io = io
    for (const t of st.targets.keys()) io.observe(t)
  }, [supported])

  const rowWindow = useMemo<SessionRowWindow | null>(() => supported ? {
    observe: (el, listener) => {
      const st = stateRef.current!
      st.targets.set(el, listener)
      st.io?.observe(el)
      return () => {
        st.targets.delete(el)
        st.io?.unobserve(el)
      }
    },
    claimInitialMount: key => {
      const st = stateRef.current!
      let pos = st.claims.get(key)
      if (pos == null) {
        pos = st.claims.size
        st.claims.set(key, pos)
      }
      return pos < st.budget
    },
  } : null, [supported])

  return { rowWindow, setRoot }
}

/** Does the row hold something a remount would destroy: keyboard focus, or an
 *  open menu or popover (Radix marks its trigger `data-state="open"`)? */
function rowHoldsState(el: HTMLElement): boolean {
  const active = el.ownerDocument.activeElement
  if (active && active !== el && el.contains(active)) return true
  return el.querySelector('[data-state="open"]') != null
}

export interface WindowedSessionRowProps {
  /** `data-session-row` identity, stamped on the stub so DOM walks see the row. */
  rowId: string
  /** The raw slot key the real row's outer box carries as `data-slot-key`. The
   *  stub repeats it in the same place, so `[data-slot-key=…]` and
   *  `[data-slot-key=…] [data-session-row]` find a stubbed row too. */
  slotKey: string
  navScope: string
  holdContainer: string
  /** Shown in the stub, and its accessible name (`title`), so a row caught
   *  mid-scroll reads as a session. The caller resolves the untitled fallback. */
  title: string
  /** Keep the real row mounted whatever the observer says: the active row, a
   *  row being renamed, dragged or flashed by a reveal. */
  keepMounted: boolean
  children: ReactNode
}

/**
 * The row side. Outside a windowed lane (no provider, or no IntersectionObserver)
 * this returns `children` unchanged -- no wrapper element -- so every other
 * surface and every jsdom test sees the DOM it always did.
 */
export function WindowedSessionRow(props: WindowedSessionRowProps) {
  const win = useContext(SessionRowWindowContext)
  if (!win) return <>{props.children}</>
  return <WindowedSlot win={win} {...props} />
}

function WindowedSlot({ win, rowId, slotKey, navScope, holdContainer, title, keepMounted, children }: WindowedSessionRowProps & { win: SessionRowWindow }) {
  const ref = useRef<HTMLDivElement | null>(null)
  const heightKey = JSON.stringify([navScope, rowId])
  // Mount-only: the first rows of each root's render pass start live so the
  // initial paint shows real rows; everything after waits for the observer.
  const [near, setNear] = useState(() => win.claimInitialMount(heightKey))
  const nearRef = useRef(near)
  nearRef.current = near
  // True after an observer says a state-holding row is outside the window.
  const heldOffWindowRef = useRef(false)
  // Set when keyboard navigation lands on a stub, so focus follows onto the
  // real row once it mounts.
  const focusOnMount = useRef(false)
  // An activation can land on a visible stub before IntersectionObserver has
  // delivered. Mount the real row, then replay the event there so SessionRow
  // remains the single owner of click, aux-click, and keyboard semantics.
  const pendingActivation = useRef<
    | { kind: 'mouse'; type: 'click' | 'auxclick' | 'contextmenu'; init: MouseEventInit }
    | { kind: 'key'; init: KeyboardEventInit }
    | null
  >(null)

  useLayoutEffect(() => {
    const el = ref.current
    if (!el) return
    let releaseWatchCleanup: (() => void) | null = null
    let releaseCheckQueued = false

    const stopReleaseWatch = () => {
      releaseWatchCleanup?.()
      releaseWatchCleanup = null
      releaseCheckQueued = false
    }
    const releaseIfIdle = () => {
      releaseCheckQueued = false
      if (!heldOffWindowRef.current || rowHoldsState(el)) return
      heldOffWindowRef.current = false
      stopReleaseWatch()
      const height = el.getBoundingClientRect().height
      if (height > 0 && liveRef.current) heights.set(heightKey, height)
      if (nearRef.current) setNear(false)
    }
    const queueReleaseCheck = () => {
      if (releaseCheckQueued) return
      releaseCheckQueued = true
      queueMicrotask(releaseIfIdle)
    }
    const startReleaseWatch = () => {
      if (releaseWatchCleanup) return
      const onFocusOut = () => queueReleaseCheck()
      el.addEventListener('focusout', onFocusOut)
      const mutationObserver = new MutationObserver(queueReleaseCheck)
      mutationObserver.observe(el, { attributes: true, attributeFilter: ['data-state'], subtree: true })
      releaseWatchCleanup = () => {
        el.removeEventListener('focusout', onFocusOut)
        mutationObserver.disconnect()
      }
    }

    const unobserve = win.observe(el, (isNear, height) => {
      // A zero height is not a measurement: a row inside a collapsed folder
      // (FolderBody keeps it mounted but un-rendered) reports 0, and storing
      // that would stub it at the wrong size when the folder opens. Nor is a
      // zero-height "not intersecting" a position: every collapsed row reports
      // that wherever its folder sits, so acting on it would stub them all. A
      // live row keeps its state until the folder opens and a real entry comes.
      // A zero-height "intersecting" still mounts: a box the engine has not laid
      // out yet may well be on screen, and a spare mount is the cheap mistake.
      if (height <= 0 && !isNear) return
      // Record every measurement of the live row, arriving or leaving.
      if (height > 0 && liveRef.current) heights.set(heightKey, height)
      // A fresh near entry cancels any deferred off-window release. The row may
      // still hold focus/menu state, but it is no longer outside the window.
      if (isNear && heldOffWindowRef.current) {
        heldOffWindowRef.current = false
        stopReleaseWatch()
      }
      if (isNear === nearRef.current) return
      // Leaving: keep a row that holds focus or an open menu, which a remount
      // would drop. Remember that it is outside the window and reconsider it as
      // soon as focus leaves or a Radix trigger loses data-state="open".
      if (!isNear && rowHoldsState(el)) {
        heldOffWindowRef.current = true
        startReleaseWatch()
        return
      }
      setNear(isNear)
    })
    return () => {
      heldOffWindowRef.current = false
      stopReleaseWatch()
      unobserve()
    }
  }, [win, heightKey])

  const mounted = near || keepMounted
  // Read by the observer callback: only a LIVE row's box is a measurement; a
  // stub's box is the estimate itself.
  const liveRef = useRef(mounted)
  liveRef.current = mounted

  // Tab or keyboard navigation (sessionRowNav) focuses the stub, which is a
  // `data-session-row` element like any other; mount the real row and hand the
  // focus on. Native listeners avoid making the placeholder a JSX-owned
  // control. Activation is replayed after mount so the real SessionRow keeps
  // ownership of navigation semantics.
  useLayoutEffect(() => {
    const el = ref.current
    if (!el || mounted) return
    const stub = el.querySelector<HTMLElement>('[data-session-row]')
    if (!stub) return
    const onFocusIn = () => { focusOnMount.current = true; setNear(true) }
    // Every button: a focusable stub would otherwise take focus on press, the
    // focusin handler would swap in the real row before mouseup, and the
    // browser would send `click` to the common ancestor instead of the stub.
    // For the middle button this also suppresses autoscroll, as SessionRow does.
    const onMouseDown = (event: MouseEvent) => { event.preventDefault() }
    const onMouseActivation = (event: MouseEvent) => {
      if (event.type === 'auxclick' && event.button !== 1) return
      event.preventDefault()
      event.stopPropagation()
      // The press did not move focus (see onMouseDown); leave the real row
      // focused as a real click on it would. A context menu does not move focus.
      if (event.type !== 'contextmenu') focusOnMount.current = true
      pendingActivation.current = {
        kind: 'mouse',
        type: event.type as 'click' | 'auxclick' | 'contextmenu',
        init: {
          button: event.button,
          buttons: event.buttons,
          clientX: event.clientX,
          clientY: event.clientY,
          detail: event.detail,
          ctrlKey: event.ctrlKey,
          metaKey: event.metaKey,
          shiftKey: event.shiftKey,
          altKey: event.altKey,
        },
      }
      setNear(true)
    }
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key !== 'Enter' && event.key !== ' ') return
      event.preventDefault()
      event.stopPropagation()
      pendingActivation.current = {
        kind: 'key',
        init: {
          key: event.key,
          code: event.code,
          location: event.location,
          repeat: event.repeat,
          ctrlKey: event.ctrlKey,
          metaKey: event.metaKey,
          shiftKey: event.shiftKey,
          altKey: event.altKey,
        },
      }
      setNear(true)
    }
    stub.addEventListener('focusin', onFocusIn)
    stub.addEventListener('mousedown', onMouseDown)
    stub.addEventListener('click', onMouseActivation)
    stub.addEventListener('auxclick', onMouseActivation)
    stub.addEventListener('keydown', onKeyDown)
    stub.addEventListener('contextmenu', onMouseActivation)
    return () => {
      stub.removeEventListener('focusin', onFocusIn)
      stub.removeEventListener('mousedown', onMouseDown)
      stub.removeEventListener('click', onMouseActivation)
      stub.removeEventListener('auxclick', onMouseActivation)
      stub.removeEventListener('keydown', onKeyDown)
      stub.removeEventListener('contextmenu', onMouseActivation)
    }
  }, [mounted])

  useLayoutEffect(() => {
    if (!mounted) return
    const row = ref.current?.querySelector<HTMLElement>('[data-session-row]')
    if (!row) return
    if (focusOnMount.current) {
      focusOnMount.current = false
      row.focus()
    }
    const pending = pendingActivation.current
    if (!pending) return
    pendingActivation.current = null
    if (pending.kind === 'mouse') {
      row.dispatchEvent(new MouseEvent(pending.type, { ...pending.init, bubbles: true, cancelable: true }))
    } else {
      row.dispatchEvent(new KeyboardEvent('keydown', { ...pending.init, bubbles: true, cancelable: true }))
    }
  }, [mounted])

  if (mounted) return <div ref={ref} data-session-window="live">{children}</div>
  return (
    <div ref={ref} data-session-window="stub">
      <div data-slot-key={slotKey}>
        <div
          data-session-row={rowId}
          data-session-scope={navScope}
          data-session-container={holdContainer}
          // Same tab stop as the real row (role="button", tabIndex 0), so Tab
          // still walks every session. Focus lands here only for a moment: the
          // focusin handler above mounts the row and moves focus onto it.
          role="button"
          tabIndex={0}
          title={title}
          className="truncate pl-3.5 pr-3 py-2 text-sm"
          style={{ height: heights.get(heightKey) ?? SESSION_ROW_STUB_DEFAULT_PX }}
        >
          <div aria-hidden="true" className={ROW_META_CLS}>{'\u00A0'}</div>
          <div className="truncate">{title}</div>
        </div>
      </div>
    </div>
  )
}

/** A scroll container that is its own window root: the board lane's columns,
 *  which scroll independently of each other and of the main lane. */
export function SessionRowWindowScroller({ className, style, children }: { className?: string; style?: CSSProperties; children: ReactNode }) {
  const { rowWindow, setRoot } = useSessionRowWindowRoot()
  return (
    <div ref={setRoot} className={className} style={style}>
      <SessionRowWindowContext.Provider value={rowWindow}>{children}</SessionRowWindowContext.Provider>
    </div>
  )
}
