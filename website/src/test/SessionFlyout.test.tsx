/**
 * Collapsed-sidebar session flyout.
 *
 * Locks the contract:
 *  (1) Rows are ordered most-recent-first ALWAYS, ignoring the sidebar's saved
 *      sort — the surface answers "what was I just doing".
 *  (2) Pin priority still applies, so a row does not jump position between the
 *      flyout and the sidebar.
 *  (3The list caps at FLYOUT_MAX_ROWS and defers the remainder to an expand
 *      affordance rather than scrolling forever.
 *  (4) Clicking a row switches; clicking New creates; both are inert offline.
 *  (5) The status marker follows the sidebar's precedence (approval > running >
 *      unread) and reserves its column when quiet, so titles never shift.
 *  (6) Up/Down rove over every menuitem and wrap at both ends.
 *  (7) The flyout occupies the panel rect's origin and width, so expanding
 *      moves only its bottom edge and the corner stays pinned.
 */
import { describe, it, expect, vi } from 'vitest'
import { render, fireEvent, within } from '@testing-library/react'
import type { ChatSlot } from '../types'
import SessionFlyout, { FLYOUT_MAX_ROWS, TOGGLE_RECT, toggleClip, FULL_CLIP } from '../pages/chat/SessionFlyout'

// Render framer-motion elements as plain DOM (jsdom can't run projection).
vi.mock('framer-motion', async () => {
  const React = await import('react')
  const FRAMER_PROPS = new Set(['layout', 'layoutId', 'initial', 'animate', 'exit', 'transition', 'variants'])
  const make = (tag: string) =>
    React.forwardRef((props: any, ref: any) => {
      const clean: any = {}
      for (const k of Object.keys(props)) {
        if (k === 'children' || FRAMER_PROPS.has(k)) continue
        clean[k] = props[k]
      }
      // Surface what framer is ASKED to animate — the real library cannot run
      // in jsdom, so the props are the only observable.
      const clip = props.animate?.clipPath
      if (Array.isArray(clip)) {
        clean['data-clip-from'] = clip[0]
        clean['data-clip-to'] = clip[clip.length - 1]
      }
      if (props.transition?.duration != null) clean['data-anim-dur'] = String(props.transition.duration)
      return React.createElement(tag, { ...clean, ref }, props.children)
    })
  return {
    motion: new Proxy({}, { get: (_t, tag: string) => make(tag) }),
    AnimatePresence: ({ children }: any) => React.createElement(React.Fragment, null, children),
    useReducedMotion: () => false,
  }
})

const slot = (over: Partial<ChatSlot> & { key: string }): ChatSlot => ({
  messages: 1, running: false, ...over,
} as ChatSlot)

const SLOTS: ChatSlot[] = [
  slot({ key: 'k-old', title: 'Oldest', last_ts: '2026-08-01T10:00:00Z' }),
  slot({ key: 'k-new', title: 'Newest', last_ts: '2026-08-05T10:00:00Z' }),
  slot({ key: 'k-mid', title: 'Middle', last_ts: '2026-08-03T10:00:00Z' }),
]

function mount(over: Partial<React.ComponentProps<typeof SessionFlyout>> = {}) {
  const props = {
    slots: SLOTS,
    activeSlot: null,
    unreadSlots: [] as string[],
    panelWidth: 260,
    maxHeight: 600,
    connected: true,
    onSwitch: vi.fn(),
    onNew: vi.fn(),
    onExpand: vi.fn(),
    onDismiss: vi.fn(),
    ...over,
  }
  const utils = render(<SessionFlyout {...props} />)
  return { ...utils, props }
}

const rowKeys = (c: HTMLElement) =>
  Array.from(c.querySelectorAll('[data-slot-key]')).map(el => el.getAttribute('data-slot-key'))

describe('SessionFlyout ordering', () => {
  it('lists sessions most-recent-first', () => {
    const { container } = mount()
    expect(rowKeys(container)).toEqual(['k-new', 'k-mid', 'k-old'])
  })

  it('puts a pinned session first even when it is the least recent', () => {
    const { container } = mount({
      slots: [...SLOTS.slice(0, 2), slot({ ...SLOTS[2], pinned: true } as any)],
    })
    expect(rowKeys(container)).toEqual(['k-mid', 'k-new', 'k-old'])
  })

  it('ranks a slot with only `created` behind slots with real activity', () => {
    const { container } = mount({
      slots: [
        slot({ key: 'k-created', title: 'Created only', created: '2026-08-04T10:00:00Z' }),
        SLOTS[1],
      ],
    })
    expect(rowKeys(container)).toEqual(['k-new', 'k-created'])
  })
})

describe('SessionFlyout row cap', () => {
  const many = Array.from({ length: FLYOUT_MAX_ROWS + 4 }, (_, i) =>
    slot({ key: `k${i}`, title: `S${i}`, last_ts: `2026-08-0${(i % 9) + 1}T10:00:00Z` }))

  it(`shows at most ${FLYOUT_MAX_ROWS} rows`, () => {
    const { container } = mount({ slots: many })
    expect(rowKeys(container)).toHaveLength(FLYOUT_MAX_ROWS)
  })

  it('offers an expand affordance only when sessions are hidden', () => {
    const withOverflow = mount({ slots: many })
    const showAll = withOverflow.getByText('Show all sessions')
    fireEvent.click(showAll)
    expect(withOverflow.props.onExpand).toHaveBeenCalledTimes(1)
    withOverflow.unmount()

    const noOverflow = mount()
    expect(noOverflow.queryByText('Show all sessions')).toBeNull()
  })
})

describe('SessionFlyout actions', () => {
  it('switches on row click', () => {
    const { container, props } = mount()
    fireEvent.click(container.querySelector('[data-slot-key="k-mid"]')!)
    expect(props.onSwitch).toHaveBeenCalledWith('k-mid')
  })

  it('creates on New', () => {
    const { getByText, props } = mount()
    fireEvent.click(getByText('New'))
    expect(props.onNew).toHaveBeenCalledTimes(1)
  })

  it('is inert while disconnected — switching offline would strand the user', () => {
    const { container, getByText, props } = mount({ connected: false })
    fireEvent.click(container.querySelector('[data-slot-key="k-new"]')!)
    expect(props.onSwitch).not.toHaveBeenCalled()
    expect(container.querySelector('[data-slot-key="k-new"]')).toHaveAttribute('aria-disabled', 'true')
    expect(getByText('New').closest('button')).toBeDisabled()
  })

  it('rows stay focusable while offline so the arrow ring keeps its length', () => {
    // `disabled` would drop them out of the roving ring entirely.
    const { container } = mount({ connected: false })
    expect(container.querySelector('[data-slot-key="k-new"]')).not.toBeDisabled()
  })

  it('disables New while a create is already in flight', () => {
    const { getByText } = mount({ creating: true })
    expect(getByText('New').closest('button')).toBeDisabled()
  })
})

describe('SessionFlyout row content', () => {
  it('shows only a marker and a title — no timestamp', () => {
    // The list is already ordered by recency, so a per-row time restates what
    // the position shows, and it was eating a third of the row's width.
    const { container } = mount()
    const row = container.querySelector('[data-slot-key="k-new"]')!
    expect(row.textContent).toBe('Newest')
    // Two children: the marker span and the title span.
    expect(row.children).toHaveLength(2)
  })

  it('does not render any clock or date text in the list', () => {
    const { container } = mount()
    const list = container.querySelector('[role="menu"]')!
    expect(list.textContent).not.toMatch(/\d{1,2}:\d{2}/)
    expect(list.textContent).not.toMatch(/Yesterday|Mon|Tue|Wed|Thu|Fri|Sat|Sun/)
  })
})

describe('SessionFlyout status markers', () => {
  const marker = (c: HTMLElement, key: string) =>
    c.querySelector(`[data-slot-key="${key}"] span[aria-hidden]`)!.className

  it('ranks approval above activity above unread', () => {
    const { container } = mount({
      slots: [
        slot({ key: 'a', title: 'Approval', pending_approval: true, running: true, last_ts: '2026-08-05T04:00:00Z' }),
        slot({ key: 'r', title: 'Running', running: true, last_ts: '2026-08-05T03:00:00Z' }),
        slot({ key: 'u', title: 'Unread', last_ts: '2026-08-05T02:00:00Z' }),
        slot({ key: 'q', title: 'Quiet', last_ts: '2026-08-05T01:00:00Z' }),
      ],
      unreadSlots: ['a', 'r', 'u'],
    })
    expect(marker(container, 'a')).toContain('bg-warn')
    expect(marker(container, 'r')).toContain('animate-pulse')
    expect(marker(container, 'u')).toContain('bg-accent')
    expect(marker(container, 'u')).not.toContain('animate-pulse')
  })

  it('reserves the marker column when quiet so titles never shift', () => {
    const { container } = mount()
    expect(marker(container, 'k-new')).toContain('bg-transparent')
  })

  it('marks the active row with aria-current', () => {
    const { container } = mount({ activeSlot: 'k-mid' })
    expect(container.querySelector('[data-slot-key="k-mid"]')).toHaveAttribute('aria-current', 'true')
    expect(container.querySelector('[data-slot-key="k-new"]')).not.toHaveAttribute('aria-current')
  })
})

describe('SessionFlyout keyboard', () => {
  it('roves over every menuitem with Up/Down and wraps at both ends', () => {
    const { container } = mount()
    const menu = container.querySelector('[role="menu"]')! as HTMLElement
    const items = Array.from(menu.querySelectorAll<HTMLElement>('[role="menuitem"]'))
    // New + 3 rows (no overflow, so no Show all).
    expect(items).toHaveLength(4)

    items[0].focus()
    fireEvent.keyDown(menu, { key: 'ArrowDown' })
    expect(document.activeElement).toBe(items[1])
    fireEvent.keyDown(menu, { key: 'ArrowUp' })
    expect(document.activeElement).toBe(items[0])
    // Wrap backwards off the first item.
    fireEvent.keyDown(menu, { key: 'ArrowUp' })
    expect(document.activeElement).toBe(items[items.length - 1])
    // Wrap forwards off the last.
    fireEvent.keyDown(menu, { key: 'ArrowDown' })
    expect(document.activeElement).toBe(items[0])
    fireEvent.keyDown(menu, { key: 'End' })
    expect(document.activeElement).toBe(items[items.length - 1])
    fireEvent.keyDown(menu, { key: 'Home' })
    expect(document.activeElement).toBe(items[0])
  })

  it('focuses the ACTIVE row when the keyboard opened it, not the first', () => {
    const { container } = mount({ activeSlot: 'k-old', autoFocus: true })
    expect(document.activeElement).toBe(container.querySelector('[data-slot-key="k-old"]'))
  })

  it('focuses the first row when the keyboard opened it and nothing is active', () => {
    const { container } = mount({ autoFocus: true })
    expect(document.activeElement).toBe(container.querySelector('[data-slot-key="k-new"]'))
  })

  it('steals no focus on hover open — autoFocus is the caller\'s keyboard signal', () => {
    const { container } = mount({ activeSlot: 'k-old' })
    expect(container.contains(document.activeElement)).toBe(false)
  })

  it('adds no tab stops of its own — the trigger is the single stop', () => {
    const { container } = mount()
    const stops = Array.from(container.querySelectorAll<HTMLElement>('[role="menuitem"]'))
      .filter(el => el.tabIndex >= 0)
    expect(stops).toEqual([])
  })
})

describe('SessionFlyout geometry', () => {
  it('occupies the panel rect origin and full width, so only the bottom edge grows', () => {
    // The whole "expands in place" claim rests on this: same origin, same width
    // as the expanded sidebar, only shorter. An inset card would move all four
    // edges on expand, which reads as a jump rather than a growth.
    const { container } = mount({ panelWidth: 260 })
    const surface = container.querySelector('[role="menu"]')! as HTMLElement
    expect(surface.className).toContain('top-0')
    expect(surface.className).toContain('left-0')
    expect(surface.style.width).toBe('260px')
    expect(surface.style.left).toBe('')
  })

  it('tracks a resized sidebar width', () => {
    const { container } = mount({ panelWidth: 420 })
    expect((container.querySelector('[role="menu"]') as HTMLElement).style.width).toBe('420px')
  })

  it('reserves the toggle button’s column in the header', () => {
    // The toggle paints ON TOP of this surface at z-[61]; without the pad the
    // caption would sit underneath it.
    const { container } = mount()
    const caption = container.querySelector('[role="menu"] .pl-9')
    expect(caption).not.toBeNull()
    expect(caption!.textContent).toContain('Sessions')
  })

  it('carries the expanded panel’s own title, not a flyout-local caption', () => {
    // Same visible string as the sidebar header, from the same catalog key, so
    // the morph never swaps text mid-flight and no locale can drift them apart.
    const { container } = mount()
    const title = container.querySelector('[role="menu"] .sessions-panel-title')!
    expect(title.textContent).toBe('Sessions')
    expect(container.querySelector('[role="menu"]')!.textContent).not.toContain('RECENT')
  })

  it('mirrors the expanded header’s row box so the header does not move on expand', () => {
    // ChatSidebar's header is `px-2 mt-0.5 h-10` with an h-7 action button —
    // that is what puts its New button at y 9..37, the toggle button's exact
    // rect. Matching it is what makes the morph a single-edge growth.
    const { container, getByText } = mount()
    const row = container.querySelector('[role="menu"] > div')! as HTMLElement
    for (const cls of ['h-10', 'px-2', 'mt-0.5']) expect(row.className).toContain(cls)
    expect(getByText('New').closest('button')!.className).toContain('h-7')
  })

  it('caps its height so a short window scrolls instead of clipping', () => {
    const { container } = mount({ maxHeight: 180 })
    const surface = container.querySelector('[role="menu"]')! as HTMLElement
    expect(surface.style.maxHeight).toBe('180px')
  })

  it('labels itself as a menu for screen readers', () => {
    const { container } = mount()
    const menu = container.querySelector('[role="menu"]')!
    // Visible title is "Sessions" (the panel's own); the accessible name says
    // "Recent sessions", which is what the surface actually offers.
    expect(menu).toHaveAttribute('aria-label', 'Recent sessions')
    expect(within(menu as HTMLElement).getByText('Sessions')).toBeTruthy()
  })
})


describe('SessionFlyout open clip', () => {
  // Hovering must GROW the surface out of the toggle button, not fade a
  // full-size panel in over it. Measured on the built app before this existed:
  // the entrance animated (13 frames) but `clip-path` was `none` and the box was
  // full size from frame one — so it read as "appeared", not "came out of".
  it('starts as a window covering exactly the toggle button', () => {
    // 260 wide, 379 tall: right = 260-8-28 = 224, bottom = 379-9-28 = 342.
    expect(toggleClip(260, 379)).toBe('inset(9px 224px 342px 8px round 6px)')
  })

  it('ends as the whole surface, with the panel’s corner radius', () => {
    // 12px is `rounded-xl` — the radius the expanded panel uses — so curvature
    // is continuous across hover-open, click-expand and collapse.
    expect(FULL_CLIP).toBe('inset(0px 0px 0px 0px round 12px)')
  })

  it('clamps every side instead of emitting a negative inset', () => {
    // A negative inset is invalid CSS and drops the clip entirely, flashing the
    // surface at full size. Both degenerate inputs are reachable: a sidebar
    // dragged narrower than the button's right offset, and the pre-layout frame.
    for (const [w, h] of [[20, 379], [260, 10], [0, 0], [-5, -5]]) {
      const clip = toggleClip(w, h)
      expect(clip, `${w}x${h}`).not.toContain('-')
    }
    expect(toggleClip(0, 0)).toBe('inset(9px 0px 0px 8px round 6px)')
  })

  it('tracks a resized sidebar', () => {
    expect(toggleClip(420, 379)).toContain('384px')   // 420-8-28
  })

  it('is wired to the surface as animated keyframes', () => {
    // happy-dom reports offsetHeight 0, so the component correctly skips the
    // clip until a real layout exists; stub it to cover the wiring. The REAL
    // verification is the capture harness, which measures a live clip ladder.
    const spy = vi.spyOn(HTMLElement.prototype, 'offsetHeight', 'get').mockReturnValue(379)
    try {
      const { container } = mount({ panelWidth: 260 })
      const surface = container.querySelector('[role="menu"]')!
      expect(surface.getAttribute('data-clip-from')).toBe(toggleClip(260, 379))
      expect(surface.getAttribute('data-clip-to')).toBe(FULL_CLIP)
      // OverlayDrawer's clip morph is 0.24s. A different number here would make
      // hover-open and click-expand feel like two unrelated animations.
      expect(surface).toHaveAttribute('data-anim-dur', '0.24')
    } finally {
      spy.mockRestore()
    }
  })
})
