import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { renderHook, act } from '@testing-library/react'
import { useSidePanelHealth, SIDE_PANEL_HEALTH_DELAY_MS, SLIDE_WAITS, type SidePanelHeal } from './useSidePanelHealth'
import { findSidePanelFaults } from '../utils/sidePanelHealth'

/**
 * One case per injected fault: the panel the store calls open is put in a
 * broken DOM state, and the check must heal it once or close the panel so the
 * header's open button comes back. jsdom lays nothing out, so every box is
 * given its size by hand.
 */

const sized = (el: Element, w: number, h: number) => {
  el.getBoundingClientRect = () => ({ x: 0, y: 0, width: w, height: h, top: 0, left: 0, right: w, bottom: h, toJSON: () => ({}) }) as DOMRect
}

let slot: HTMLElement
let warn: ReturnType<typeof vi.spyOn>
let chatPane: HTMLElement

/** A shell slot holding a panel column of the given size. */
function mountPanel({ w = 460, h = 850 }: { w?: number; h?: number } = {}) {
  slot.replaceChildren()
  const col = document.createElement('div')
  col.setAttribute('data-testid', 'side-panel-column')
  const panel = document.createElement('div')
  col.append(panel)
  slot.append(col)
  sized(col, w, h)
  sized(panel, w, h)
  return col
}

function heals(over: Partial<SidePanelHeal> = {}): SidePanelHeal {
  return { refreshSlot: vi.fn(), retireSlide: vi.fn(), hideNative: vi.fn(), close: vi.fn(), ...over }
}

function run(opts: { heal: SidePanelHeal; deskSlide?: boolean; nativeSuspect?: boolean; slotEl?: HTMLElement | null; enabled?: boolean }) {
  return renderHook(() => useSidePanelHealth({
    enabled: opts.enabled ?? true,
    activeSlot: 'chat-a',
    dock: 'right',
    slot: opts.slotEl === undefined ? slot : opts.slotEl,
    chatPane: () => chatPane,
    deskSlide: opts.deskSlide ?? false,
    nativeSuspect: () => opts.nativeSuspect ?? false,
    heal: opts.heal,
  }))
}

const tick = () => act(() => { vi.advanceTimersByTime(SIDE_PANEL_HEALTH_DELAY_MS + 1) })

beforeEach(() => {
  vi.useFakeTimers()
  warn = vi.spyOn(console, 'warn').mockImplementation(() => {})
  chatPane = document.createElement('div')
  slot = document.createElement('div')
  slot.id = 'activity-bar-slot'
  document.body.append(chatPane, slot)
})
afterEach(() => {
  vi.useRealTimers()
  vi.restoreAllMocks()
  document.body.replaceChildren()
})

describe('useSidePanelHealth', () => {
  it('leaves a panel that is on screen alone', () => {
    mountPanel()
    const heal = heals()
    run({ heal })
    tick(); tick()
    expect(Object.values(heal).every(fn => (fn as ReturnType<typeof vi.fn>).mock.calls.length === 0)).toBe(true)
    expect(warn).not.toHaveBeenCalled()
  })

  it('does nothing while the panel is not open (or on a phone / embed)', () => {
    const heal = heals()
    run({ heal, enabled: false })
    tick()
    expect(heal.close).not.toHaveBeenCalled()
  })

  it('a detached portal slot: looks the slot up again, and a healthy panel stays open', () => {
    const stale = document.createElement('div')
    let held: HTMLElement | null = stale
    const heal = heals({ refreshSlot: vi.fn(() => { held = slot; mountPanel() }) })
    const { rerender } = renderHook(() => useSidePanelHealth({
      enabled: true, activeSlot: 'chat-a', dock: 'right', slot: held, chatPane: () => chatPane, deskSlide: false, nativeSuspect: () => false, heal,
    }))
    tick()
    expect(heal.refreshSlot).toHaveBeenCalledTimes(1)
    rerender()
    tick()
    expect(heal.close).not.toHaveBeenCalled()
  })

  it('no panel column: closes, so the open button comes back (its next open mounts afresh)', () => {
    slot.replaceChildren()
    const heal = heals()
    run({ heal })
    tick()
    expect(heal.close).toHaveBeenCalledTimes(1)
  })

  it('a column stuck at zero width: closes, and never remounts a subtree kept mounted for its state', () => {
    mountPanel({ w: 0 })
    const heal = heals()
    run({ heal })
    tick()
    expect(heal.close).toHaveBeenCalledTimes(1)
  })

  it('a bottom-docked panel at zero height counts the same', () => {
    expect(findSidePanelFaults({ slot: (mountPanel({ h: 0 }), slot), chatPane, deskSlide: false, nativeSuspect: false })).toEqual(['zero-size'])
  })

  it('a hidden ancestor while the chat is on screen: closes at once, nothing to heal', () => {
    const col = mountPanel()
    const wrap = document.createElement('div')
    wrap.style.display = 'none'
    slot.replaceWith(wrap)
    wrap.append(slot)
    expect(col.isConnected).toBe(true)
    const heal = heals()
    run({ heal })
    tick()
    expect(heal.close).toHaveBeenCalledTimes(1)
  })

  it('the whole chat hidden (another crew pane in front): nothing is out of step', () => {
    mountPanel()
    const wrap = document.createElement('div')
    wrap.style.display = 'none'
    document.body.append(wrap)
    wrap.append(chatPane, slot)
    const heal = heals()
    run({ heal })
    tick()
    expect(heal.close).not.toHaveBeenCalled()
  })

  it('a slide that ends while it is waited out (a live drag) is left alone', () => {
    mountPanel()
    let sliding = true
    const heal = heals()
    const { rerender } = renderHook(() => useSidePanelHealth({
      enabled: true, activeSlot: 'chat-a', dock: 'right', slot, chatPane: () => chatPane, deskSlide: sliding, nativeSuspect: () => false, heal,
    }))
    tick(); tick()
    sliding = false
    rerender()
    for (let i = 0; i < SLIDE_WAITS + 2; i++) tick()
    expect(heal.retireSlide).not.toHaveBeenCalled()
    expect(heal.close).not.toHaveBeenCalled()
  })

  it('a trackpad slide left set: retires it once it outlasts any drag', () => {
    mountPanel()
    let sliding = true
    const heal = heals({ retireSlide: vi.fn(() => { sliding = false }) })
    const { rerender } = renderHook(() => useSidePanelHealth({
      enabled: true, activeSlot: 'chat-a', dock: 'right', slot, chatPane: () => chatPane, deskSlide: sliding, nativeSuspect: () => false, heal,
    }))
    for (let i = 0; i < SLIDE_WAITS; i++) tick()
    expect(heal.retireSlide).not.toHaveBeenCalled()
    tick()
    expect(heal.retireSlide).toHaveBeenCalledTimes(1)
    rerender()
    tick()
    expect(heal.close).not.toHaveBeenCalled()
  })

  it('a native browser view possibly left over the panel: hides it once, never closes for it', () => {
    mountPanel()
    const heal = heals()
    run({ heal, nativeSuspect: true })
    tick(); tick()
    expect(heal.hideNative).toHaveBeenCalledTimes(1)
    expect(heal.close).not.toHaveBeenCalled()
    // Inferred only: no console line for it.
    expect(warn).not.toHaveBeenCalled()
  })

  it('a window hidden when the timer fires reads again once it is shown', () => {
    slot.replaceChildren()
    const vis = vi.spyOn(document, 'visibilityState', 'get').mockReturnValue('hidden')
    const heal = heals()
    run({ heal })
    tick(); tick()
    expect(heal.close).not.toHaveBeenCalled()
    vis.mockReturnValue('visible')
    tick()
    expect(heal.close).toHaveBeenCalledTimes(1)
  })

  it('a new drag started right after a heal is waited out, never closed mid-drag', () => {
    const stale = document.createElement('div')
    let held: HTMLElement | null = stale
    let sliding = false
    const heal = heals({ refreshSlot: vi.fn(() => { held = slot; mountPanel(); sliding = true }) })
    const { rerender } = renderHook(() => useSidePanelHealth({
      enabled: true, activeSlot: 'chat-a', dock: 'right', slot: held, chatPane: () => chatPane, deskSlide: sliding, nativeSuspect: () => false, heal,
    }))
    tick()
    expect(heal.refreshSlot).toHaveBeenCalledTimes(1)
    rerender()
    tick(); tick()
    sliding = false
    rerender()
    for (let i = 0; i < SLIDE_WAITS + 2; i++) tick()
    expect(heal.close).not.toHaveBeenCalled()
  })


  it('records the diag fields on the console when it fires', () => {
    slot.replaceChildren()
    run({ heal: heals() })
    tick()
    const line = warn.mock.calls[0]
    expect(line[0]).toBe('[side-panel-health]')
    const diag = JSON.parse(line[1] as string)
    expect(diag.faults).toEqual(['no-column'])
    expect(diag.action).toEqual(['close'])
    expect(diag).toHaveProperty('slotInDoc', true)
    expect(diag).toHaveProperty('hiddenAncestor')
    expect(diag).toHaveProperty('storage')
  })

  it('records a panel tab strip as kinds only, never its titles or URLs', () => {
    localStorage.setItem('mc-panel-tabs:chat-a', JSON.stringify({ tabs: [{ id: 'b', kind: 'browser', title: 'secret', url: 'https://x.test/?q=1' }], activeId: 'b' }))
    slot.replaceChildren()
    run({ heal: heals() })
    tick()
    const line = warn.mock.calls[0][1] as string
    expect(line).toContain('{"kinds":["browser"],"active":"b"}')
    expect(line).not.toContain('secret')
    expect(line).not.toContain('x.test')
    localStorage.removeItem('mc-panel-tabs:chat-a')
  })
})
