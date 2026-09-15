/**
 * `data-owns-swipe`: how a page keeps the app-wide nav drawer's gesture from
 * arming on top of its own drawer.
 *
 * The nav instance is bound on `<main>`, so it is an ANCESTOR of every page. The
 * asymmetry that makes one attribute serve both instances is the root boundary:
 * the claim sits on the page's own bound element, which is strictly below
 * `<main>` (so the nav instance yields) and IS the page instance's own root (so
 * the page instance proceeds). The self-suppression case is the one that would
 * silently disable the chat page's own drawer, so it is pinned first.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { renderHook, act } from '@testing-library/react'
import { motionValue } from 'framer-motion'
import { useDrawerSwipe } from '../hooks/useDrawerSwipe'

function touch(type: string, clientX: number, clientY = 0, timeStamp = 0): TouchEvent {
  const t = { clientX, clientY } as Touch
  const init: TouchEventInit = { bubbles: true }
  if (type === 'touchstart' || type === 'touchmove') init.touches = [t]
  if (type === 'touchend' || type === 'touchcancel') init.changedTouches = [t]
  const e = new TouchEvent(type, init)
  Object.defineProperty(e, 'timeStamp', { value: timeStamp })
  return e
}

describe('data-owns-swipe', () => {
  /** `<main>` stand-in: what the app-wide nav instance binds. */
  let outer: HTMLDivElement
  /** A page's own bound element, nested inside it. */
  let page: HTMLDivElement
  /** Ordinary page content the finger actually lands on. */
  let content: HTMLDivElement
  let x: ReturnType<typeof motionValue<number>>
  let onGestureOpen: ReturnType<typeof vi.fn>

  beforeEach(() => {
    outer = document.createElement('div')
    page = document.createElement('div')
    content = document.createElement('div')
    page.appendChild(content)
    outer.appendChild(page)
    document.body.appendChild(outer)
    x = motionValue(0)
    onGestureOpen = vi.fn()
    Object.defineProperty(window, 'innerWidth', { writable: true, value: 400 })
  })

  function bind(root: HTMLElement, side: 'left' | 'right' = 'left') {
    return renderHook(() => useDrawerSwipe({ current: root }, {
      enabled: true, side, open: false, x, onGestureOpen, onSettle: vi.fn(),
    }))
  }

  /** Drive a committed-direction drag from mid-screen off `content`. */
  function drag(side: 'left' | 'right' = 'left') {
    const dir = side === 'left' ? 1 : -1
    act(() => {
      content.dispatchEvent(touch('touchstart', 200, 0, 0))
      content.dispatchEvent(touch('touchmove', 200 + dir * 40, 0, 16))
    })
  }

  it('lets the claiming page keep its own gesture', () => {
    // The regression this whole rule is shaped around: a naive ancestor walk
    // finds the claim on the page's own root and disables the page's drawer.
    page.dataset.ownsSwipe = 'left right'
    bind(page)
    drag()
    expect(onGestureOpen).toHaveBeenCalledTimes(1)
    expect(x.get()).not.toBe(0)
  })

  it('suppresses an instance rooted ABOVE the claim', () => {
    page.dataset.ownsSwipe = 'left right'
    bind(outer)
    drag()
    expect(onGestureOpen).not.toHaveBeenCalled()
    expect(x.get()).toBe(0)
  })

  it('suppresses only the sides actually claimed', () => {
    page.dataset.ownsSwipe = 'left'
    bind(outer, 'right')
    drag('right')
    // The page took the left-anchored drawer's drag; the right one is still the
    // app-wide instance's to serve.
    expect(onGestureOpen).toHaveBeenCalledTimes(1)
  })

  it('reads the claim as a side list, not a substring', () => {
    // 'lefty' must not satisfy a claim on 'left' — a substring test would let an
    // unrelated attribute value silently disable a gesture app-wide.
    page.dataset.ownsSwipe = 'lefty'
    bind(outer)
    drag()
    expect(onGestureOpen).toHaveBeenCalledTimes(1)
  })

  it('works when the finger lands on the claiming element itself', () => {
    page.dataset.ownsSwipe = 'left'
    bind(outer)
    act(() => {
      page.dispatchEvent(touch('touchstart', 200, 0, 0))
      page.dispatchEvent(touch('touchmove', 240, 0, 16))
    })
    expect(onGestureOpen).not.toHaveBeenCalled()
  })

  it('ignores a claim on the instance\'s own root', () => {
    // An instance claiming its own element is stating what it already owns.
    outer.dataset.ownsSwipe = 'left'
    bind(outer)
    drag()
    expect(onGestureOpen).toHaveBeenCalledTimes(1)
  })

  it('is inert with no claim anywhere', () => {
    bind(outer)
    drag()
    expect(onGestureOpen).toHaveBeenCalledTimes(1)
  })

  it('yields to a widget that took touch handling from the browser', () => {
    // `touch-action: none` is what a drag widget sets — sliders, resize handles,
    // column splitters, pinch-zoom canvases. None of them is horizontally
    // SCROLLABLE, so the scroller deference does not cover them, and they run on
    // POINTER events whose preventDefault does not stop the touch stream from
    // reaching a listener on an ancestor. Without this, dragging the settings
    // volume slider rightward pulled the nav drawer out mid-adjustment.
    page.style.touchAction = 'none'
    bind(outer)
    drag()
    expect(onGestureOpen).not.toHaveBeenCalled()
    expect(x.get()).toBe(0)
  })

  it('yields on the widget itself, not only on an ancestor of it', () => {
    content.style.touchAction = 'none'
    bind(outer)
    drag()
    expect(onGestureOpen).not.toHaveBeenCalled()
  })

  it('does not yield to a merely pan-restricted ancestor', () => {
    // The root sets `touch-action: pan-x pan-y` under a coarse pointer to switch
    // page zoom off. Treating any touch-action as ownership would kill the
    // gesture everywhere — only a full `none` is the platform's "I own this".
    page.style.touchAction = 'pan-y'
    bind(outer)
    drag()
    expect(onGestureOpen).toHaveBeenCalledTimes(1)
  })

  it('ignores touch-action on the instance\'s own root', () => {
    // Same rule as a claim on the root: an instance does not suppress itself.
    outer.style.touchAction = 'none'
    bind(outer)
    drag()
    expect(onGestureOpen).toHaveBeenCalledTimes(1)
  })

  it('still arms on a sibling of the claiming page — the closing drag', () => {    // The nav drawer's scrim and panel are `fixed` siblings of the page, not
    // descendants of it, so an OPEN drawer is dismissed by a touch that lands
    // outside every claim. This is what a root at the page's own parent (an
    // element the scrim is not inside) cannot reach, and why the instance is
    // rooted at the shell.
    page.dataset.ownsSwipe = 'left right'
    const scrim = document.createElement('div')
    outer.appendChild(scrim)
    renderHook(() => useDrawerSwipe({ current: outer }, {
      enabled: true, side: 'left', open: true, x, onGestureOpen, onSettle: vi.fn(),
    }))
    act(() => {
      // Leftward: an open left-anchored drawer closes on a drag back toward its
      // own edge. Started mid-screen, which the edge dead zone only guards while
      // the panel is closed.
      scrim.dispatchEvent(touch('touchstart', 200, 0, 0))
      scrim.dispatchEvent(touch('touchmove', 160, 0, 16))
    })
    expect(x.get()).toBeLessThan(0)
  })

  it('stands down inside a modal dialog that is NOT portaled out of the shell', () => {
    // The changelog and update-error overlays are plain `fixed inset-0` JSX
    // inside the shell, so before this rule a drag across one pulled the nav
    // drawer out BEHIND the dialog. Read from the role attribute rather than a
    // list of overlays: `src/` declares dozens of dialogs, and a list means the
    // next one silently fights the drawer.
    const dialog = document.createElement('div')
    dialog.setAttribute('role', 'dialog')
    dialog.setAttribute('aria-modal', 'true')
    page.appendChild(dialog)
    renderHook(() => useDrawerSwipe({ current: outer }, {
      enabled: true, side: 'left', open: false, x, onGestureOpen, onSettle: vi.fn(),
    }))
    act(() => {
      dialog.dispatchEvent(touch('touchstart', 200, 0, 0))
      dialog.dispatchEvent(touch('touchmove', 260, 0, 16))
    })
    expect(onGestureOpen).not.toHaveBeenCalled()
    expect(x.get()).toBe(0)
  })

  it('an alertdialog owns the touch too', () => {
    const dialog = document.createElement('div')
    dialog.setAttribute('role', 'alertdialog')
    page.appendChild(dialog)
    renderHook(() => useDrawerSwipe({ current: outer }, {
      enabled: true, side: 'left', open: false, x, onGestureOpen, onSettle: vi.fn(),
    }))
    act(() => {
      dialog.dispatchEvent(touch('touchstart', 200, 0, 0))
      dialog.dispatchEvent(touch('touchmove', 260, 0, 16))
    })
    expect(onGestureOpen).not.toHaveBeenCalled()
  })

  it('a non-modal role does NOT own the touch', () => {
    // The control: the rule must not creep into every ARIA role that happens to
    // sit above the page. Only a modal layer takes the gesture.
    const region = document.createElement('div')
    region.setAttribute('role', 'region')
    page.appendChild(region)
    renderHook(() => useDrawerSwipe({ current: outer }, {
      enabled: true, side: 'left', open: false, x, onGestureOpen, onSettle: vi.fn(),
    }))
    act(() => {
      region.dispatchEvent(touch('touchstart', 200, 0, 0))
      region.dispatchEvent(touch('touchmove', 260, 0, 16))
    })
    expect(onGestureOpen).toHaveBeenCalledTimes(1)
  })
})
