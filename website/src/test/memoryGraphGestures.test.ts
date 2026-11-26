import { afterEach, describe, expect, it, vi } from 'vitest'
import { attachGraphGestures, insideBox, type GestureSigma } from '../pages/overview/memoryGraphGestures'

/** A camera whose framed-graph coordinates are screen pixels divided by 100. */
function fakeSigma() {
  const state = { x: 0.5, y: 0.5, ratio: 1, angle: 0 }
  const sigma: GestureSigma = {
    getCamera: () => ({ getState: () => ({ ...state }), setState: next => Object.assign(state, next) }),
    viewportToFramedGraph: p => ({ x: p.x / 100, y: p.y / 100 }),
  }
  return { sigma, state }
}

function mount(overNode = false) {
  const container = document.createElement('div')
  document.body.appendChild(container)
  const { sigma, state } = fakeSigma()
  const handlers = { overNode: () => overNode, onBox: vi.fn(), onSelect: vi.fn(), onClear: vi.fn() }
  const detach = attachGraphGestures(container, sigma, handlers)
  return { container, state, handlers, detach }
}
const pointer = (type: string, target: EventTarget, init: Record<string, unknown>) => {
  const event = new MouseEvent(type, { bubbles: true, cancelable: true, ...init })
  Object.assign(event, { pointerId: 1, pointerType: 'mouse' })
  target.dispatchEvent(event)
  return event
}

// jsdom's WheelEvent drops the modifier keys from its init, so they are set on the event.
const wheel = (init: WheelEventInit, keys: { ctrlKey?: boolean; metaKey?: boolean } = {}) => {
  const event = new WheelEvent('wheel', { bubbles: true, cancelable: true, ...init })
  for (const [key, value] of Object.entries(keys)) Object.defineProperty(event, key, { value })
  return event
}

afterEach(() => { document.body.replaceChildren() })

describe('the memory graph gestures (Figma model)', () => {
  it('pans on a wheel without ctrl, leaving the zoom alone, and keeps the page still', () => {
    const { container, state } = mount()
    const event = wheel({ deltaX: 30, deltaY: 50 })
    container.dispatchEvent(event)
    expect(event.defaultPrevented).toBe(true)
    expect(state.ratio).toBe(1)
    expect(state.x).toBeCloseTo(0.8)
    expect(state.y).toBeCloseTo(1)
  })

  it('zooms on a pinch (a wheel with ctrl) or Cmd + wheel, around the pointer', () => {
    const { container, state } = mount()
    container.dispatchEvent(wheel({ deltaY: -100 }, { ctrlKey: true }))
    expect(state.ratio).toBeLessThan(1)
    const zoomed = state.ratio
    container.dispatchEvent(wheel({ deltaY: 100 }, { metaKey: true }))
    expect(state.ratio).toBeGreaterThan(zoomed)
  })

  it('draws a box on an empty-canvas drag and selects what is inside it', () => {
    const { container, state, handlers } = mount()
    pointer('pointerdown', container, { button: 0, clientX: 10, clientY: 10 })
    pointer('pointermove', window, { clientX: 120, clientY: 90 })
    expect(handlers.onBox).toHaveBeenLastCalledWith({ x: 10, y: 10, width: 110, height: 80 })
    pointer('pointerup', window, { clientX: 120, clientY: 90 })
    expect(handlers.onSelect).toHaveBeenCalledWith({ x: 10, y: 10, width: 110, height: 80 })
    expect(state.x).toBe(0.5)
    expect(insideBox([{ id: 'in', at: { x: 50, y: 50 } }, { id: 'out', at: { x: 200, y: 50 } }], { x: 10, y: 10, width: 110, height: 80 })).toEqual(['in'])
  })

  it('pans on Space + drag, with the grab cursor, and leaves node presses to the click', () => {
    const { container, state, handlers } = mount()
    window.dispatchEvent(new KeyboardEvent('keydown', { code: 'Space', key: ' ', cancelable: true }))
    expect(container.style.cursor).toBe('grab')
    pointer('pointerdown', container, { button: 0, clientX: 100, clientY: 100 })
    pointer('pointermove', window, { clientX: 60, clientY: 100 })
    pointer('pointerup', window, { clientX: 60, clientY: 100 })
    window.dispatchEvent(new KeyboardEvent('keyup', { code: 'Space', key: ' ' }))
    expect(state.x).toBeCloseTo(0.9)
    expect(handlers.onSelect).not.toHaveBeenCalled()
    const node = mount(true)
    pointer('pointerdown', node.container, { button: 0, clientX: 10, clientY: 10 })
    pointer('pointermove', window, { clientX: 90, clientY: 90 })
    expect(node.handlers.onBox).not.toHaveBeenCalled()
  })

  it('leaves Space to a focused button outside the graph', () => {
    const { container } = mount()
    const button = document.createElement('button')
    document.body.appendChild(button)
    const event = new KeyboardEvent('keydown', { code: 'Space', key: ' ', bubbles: true, cancelable: true })
    button.dispatchEvent(event)
    expect(event.defaultPrevented).toBe(false)
    expect(container.style.cursor).toBe('')
  })

  it('pans on a middle-button drag too', () => {
    const { container, state } = mount()
    pointer('pointerdown', container, { button: 1, clientX: 100, clientY: 100 })
    pointer('pointermove', window, { clientX: 100, clientY: 50 })
    pointer('pointerup', window, { clientX: 100, clientY: 50 })
    expect(state.y).toBeCloseTo(1)
  })
})
