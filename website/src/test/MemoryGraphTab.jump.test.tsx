/**
 * The memory graph hands memories to the Memories tab: a memory node's click opens
 * that memory, and a box drag selects the memory nodes inside it. The canvas fills
 * its box and re-fits sigma when that box changes size. sigma is a recorder.
 */
import { act, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { beforeEach, describe, expect, it, vi } from 'vitest'

const sigmaState = vi.hoisted(() => ({ instances: [] as { handlers: Record<string, (event: unknown) => void>; resize: ReturnType<typeof vi.fn> }[] }))

vi.mock('sigma', () => ({
  default: class RecordingSigma {
    handlers: Record<string, (event: unknown) => void> = {}
    camera = { x: 0.5, y: 0.5, ratio: 1, angle: 0 }
    kill = vi.fn()
    refresh = vi.fn()
    resize = vi.fn()
    setSetting = vi.fn()
    constructor() { sigmaState.instances.push(this as never) }
    on(event: string, handler: (event: unknown) => void) { this.handlers[event] = handler }
    getCamera() { return { on() {}, getState: () => ({ ...this.camera }), setState: (s: object) => Object.assign(this.camera, s) } }
    viewportToFramedGraph(p: { x: number; y: number }) { return { x: p.x / 100, y: p.y / 100 } }
    // Every node is drawn at (50, 50) on screen.
    graphToViewport() { return { x: 50, y: 50 } }
  },
}))
vi.mock('../api/client', () => ({ api: { memoryGraph: vi.fn() } }))

import { api } from '../api/client'
import MemoryGraphTab from '../pages/overview/MemoryGraphTab'
import { MemoryJumpContext } from '../pages/overview/memoryPage/memoryJump'

const GRAPH = {
  nodes: [
    { id: 'n1', label: 'project.alpha', group: 'semantic', title: 'project.alpha = 1', record: { kind: 'fact', id: 'project.alpha', ref: 'aaaaaaaaaa' } },
    { id: 'n2', label: 'lesson one', group: 'lesson', title: 'Use tabs', record: { kind: 'directive', id: 'lesson.one', ref: 'bbbbbbbbbb' } },
    { id: 'n3', label: 'pref one', group: 'preference', title: 'Prefers dark mode' },
  ],
  edges: [],
}

function renderTab(jump = vi.fn()) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  render(<QueryClientProvider client={client}><MemoryJumpContext.Provider value={jump}><MemoryGraphTab /></MemoryJumpContext.Provider></QueryClientProvider>)
  return jump
}
async function mounted() {
  await waitFor(() => expect(sigmaState.instances.length).toBeGreaterThan(0), { timeout: 5000 })
  return sigmaState.instances.at(-1)!
}
const pointer = (type: string, target: EventTarget, init: Record<string, unknown>) => {
  const event = new MouseEvent(type, { bubbles: true, cancelable: true, ...init })
  Object.assign(event, { pointerId: 1, pointerType: 'mouse' })
  act(() => { target.dispatchEvent(event) })
}

beforeEach(() => {
  sigmaState.instances.length = 0
  vi.mocked(api.memoryGraph).mockResolvedValue(GRAPH as never)
})

describe('the memory graph and the Memories tab', () => {
  it('opens a memory node in the Memories tab, and shows any other node in place', async () => {
    const jump = renderTab()
    const sigma = await mounted()
    act(() => sigma.handlers.clickNode({ node: 'n2' }))
    expect(jump).toHaveBeenCalledWith(['bbbbbbbbbb'], { kind: 'directive', id: 'lesson.one' })
    act(() => sigma.handlers.clickNode({ node: 'n3' }))
    expect(await screen.findByText('Prefers dark mode')).toBeInTheDocument()
    expect(jump).toHaveBeenCalledTimes(1)
  })

  it('selects the memory nodes inside a dragged box and opens exactly those', async () => {
    const jump = renderTab()
    await mounted()
    const canvas = screen.getByTestId('memory-graph-canvas')
    pointer('pointerdown', canvas, { button: 0, clientX: 0, clientY: 0 })
    pointer('pointermove', window, { clientX: 100, clientY: 100 })
    expect(screen.getByTestId('memory-graph-box')).toBeInTheDocument()
    pointer('pointerup', window, { clientX: 100, clientY: 100 })
    // n3 is not a memory, so two of the three nodes in the box are selected.
    expect(await screen.findByText('2 selected')).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: 'Show in Memories' }))
    expect(jump).toHaveBeenCalledWith(['aaaaaaaaaa', 'bbbbbbbbbb'])
    act(() => { document.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape', bubbles: true })) })
    expect(screen.queryByText('2 selected')).toBeNull()
  })

  it('fills its box and re-fits sigma when the box changes size', async () => {
    let observed: ResizeObserverCallback | undefined
    const original = globalThis.ResizeObserver
    globalThis.ResizeObserver = class { constructor(cb: ResizeObserverCallback) { observed = cb } observe() {} unobserve() {} disconnect() {} } as unknown as typeof ResizeObserver
    try {
      renderTab()
      const sigma = await mounted()
      const canvas = screen.getByTestId('memory-graph-canvas')
      expect(canvas).toHaveClass('w-full')
      expect(canvas.closest('.max-w-none')).not.toBeNull()
      expect(parseInt(canvas.style.height)).toBeGreaterThanOrEqual(420)
      act(() => observed?.([], {} as ResizeObserver))
      expect(sigma.resize).toHaveBeenCalled()
    } finally {
      globalThis.ResizeObserver = original
    }
  })
})
