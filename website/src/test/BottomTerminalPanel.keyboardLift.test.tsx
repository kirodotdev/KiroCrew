/**
 * The bottom-docked terminal rises above the iPhone keyboard while it is
 * being typed into.
 *
 * iPhone Safari shrinks the visual viewport AND window.innerHeight for the
 * keyboard, while the layout viewport (documentElement.clientHeight, which the
 * height-locked shell is laid out in) keeps its full height. Without its own
 * lift the panel relies on Safari panning the page far enough on focus.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { act } from '@testing-library/react'
import { renderWithProviders } from './helpers'
import BottomTerminalPanel from '../components/BottomTerminalPanel'
import { __resetBottomTerminal, openBottomTerminal } from '../hooks/useBottomTerminal'

vi.mock('../components/CliPanel', () => ({
  default: ({ sessionId }: { sessionId: string }) => <textarea aria-label="terminal input" data-testid={`cli-${sessionId}`} />,
  disposeTerminalSession: vi.fn(),
  useDeleteTerminalSession: () => ({ mutate: vi.fn() }),
}))
vi.mock('../utils/terminalRegistry', () => ({
  getTerminalTitle: () => '',
  useTerminalTitle: () => '',
  disposeTerminalConnection: vi.fn(),
}))
vi.mock('../utils/terminalPopout', () => ({
  openPopout: vi.fn(),
  isPopoutOpen: vi.fn(() => false),
  focusPopout: vi.fn(),
  bringBack: vi.fn(),
  returnSelfToMain: vi.fn(),
}))
vi.mock('../hooks/usePanelTabs', () => ({ usePanelTabs: () => ({}) }))

type Listener = () => void
const LAYOUT_HEIGHT = 800

function installIphoneViewport() {
  const listeners: Record<string, Listener[]> = {}
  const vv = {
    width: 390,
    height: LAYOUT_HEIGHT,
    offsetTop: 0,
    scale: 1,
    addEventListener: (t: string, l: Listener) => { (listeners[t] ||= []).push(l) },
    removeEventListener: (t: string, l: Listener) => { listeners[t] = (listeners[t] || []).filter(x => x !== l) },
  }
  Object.defineProperty(window, 'visualViewport', { configurable: true, value: vv })
  Object.defineProperty(document.documentElement, 'clientHeight', { configurable: true, value: LAYOUT_HEIGHT })
  const keyboard = async (height: number, pan = 0) => {
    vv.height = LAYOUT_HEIGHT - height
    vv.offsetTop = pan
    // iPhone Safari moves innerHeight with the visual viewport.
    Object.defineProperty(window, 'innerHeight', { configurable: true, value: vv.height })
    act(() => { for (const t of ['resize', 'scroll']) (listeners[t] || []).forEach(l => l()) })
    await nextFrame()
  }
  return { keyboard, vv }
}

/** The lift is measured once per animation frame. */
const nextFrame = () => act(() => new Promise<void>(r => requestAnimationFrame(() => r())))

const lift = () => panel().parentElement!.style.paddingBottom

const panel = () => document.querySelector<HTMLElement>('[data-testid="bottom-terminal-panel"]')!

describe('BottomTerminalPanel keyboard lift', () => {
  let originalViewport: PropertyDescriptor | undefined
  let originalInnerHeight: PropertyDescriptor | undefined
  let originalClientHeight: PropertyDescriptor | undefined

  beforeEach(() => {
    originalViewport = Object.getOwnPropertyDescriptor(window, 'visualViewport')
    originalInnerHeight = Object.getOwnPropertyDescriptor(window, 'innerHeight')
    originalClientHeight = Object.getOwnPropertyDescriptor(document.documentElement, 'clientHeight')
    __resetBottomTerminal()
    // jsdom lays nothing out: dock the column at the layout bottom. The lift is
    // padding inside the measured wrapper, so its bottom edge stays there.
    vi.spyOn(HTMLElement.prototype, 'getBoundingClientRect').mockImplementation(
      () => DOMRect.fromRect({ x: 0, y: LAYOUT_HEIGHT - 300, width: 390, height: 300 }),
    )
  })
  afterEach(() => {
    vi.restoreAllMocks()
    __resetBottomTerminal()
    if (originalViewport) Object.defineProperty(window, 'visualViewport', originalViewport)
    else delete (window as { visualViewport?: VisualViewport }).visualViewport
    if (originalInnerHeight) Object.defineProperty(window, 'innerHeight', originalInnerHeight)
    if (originalClientHeight) Object.defineProperty(document.documentElement, 'clientHeight', originalClientHeight)
    else delete (document.documentElement as { clientHeight?: number }).clientHeight
  })

  it('rises by the part of the keyboard Safari did not pan away, only while typing in it', async () => {
    const { keyboard } = installIphoneViewport()
    act(() => openBottomTerminal())
    renderWithProviders(<BottomTerminalPanel />)
    const input = document.querySelector<HTMLTextAreaElement>('[data-testid^="cli-"]')!
    expect(lift()).toBe('')

    act(() => input.focus())
    await nextFrame()
    await keyboard(340) // keyboard up, no pan: the whole keyboard covers the panel
    expect(lift()).toBe('340px')

    await keyboard(340, 100) // Safari panned part of the way
    expect(lift()).toBe('240px')

    await keyboard(340, 340) // Safari panned all the way: nothing left to lift
    expect(lift()).toBe('')

    await keyboard(340)
    act(() => input.blur()) // typing elsewhere: the panel is not the keyboard's target
    await nextFrame()
    expect(lift()).toBe('')
  })

  it('announces its slide so the transcript keeps its bottom pin', () => {
    // The chat column shrinks every frame of the slide; unannounced, the
    // transcript caught up afterwards and the last lines crept up the screen.
    installIphoneViewport()
    renderWithProviders(<BottomTerminalPanel />)
    act(() => openBottomTerminal())
    expect(document.documentElement.hasAttribute('data-panel-sliding')).toBe(true)
  })

  it('grows its height so the chat above reflows, with the column held at a fixed height', () => {
    // A transform left the chat pane at its final size for the whole slide, so
    // it jumped on open and again on close; the column's own height stays put
    // so xterm does not refit mid-slide.
    installIphoneViewport()
    renderWithProviders(<BottomTerminalPanel />)
    act(() => openBottomTerminal())
    expect(panel().style.willChange).toBe('height')
    expect(panel().style.transform === '' || panel().style.transform === 'none').toBe(true)
    expect((panel().firstElementChild as HTMLElement).style.height).toMatch(/^\d+px$/)
  })

  it('lifts the whole column, not the panel alone, and lets the panel shrink to fit', async () => {
    // A transform on the panel slid it up under the chat pane's composer; the
    // padding makes the chat pane itself shorter, so its composer moves up too.
    const { keyboard } = installIphoneViewport()
    act(() => openBottomTerminal())
    renderWithProviders(<BottomTerminalPanel />)
    act(() => document.querySelector<HTMLTextAreaElement>('[data-testid^="cli-"]')!.focus())
    await nextFrame()
    await keyboard(340)
    expect(lift()).toBe('340px')
    expect(panel().parentElement!.style.transform).toBe('')
    expect(panel().style.transform === '' || panel().style.transform === 'none').toBe(true)
    expect(panel().className).toContain('min-h-0')
    expect(panel().className).not.toContain('shrink-0')
  })

  it('leaves the column alone when the chat composer has the keyboard', async () => {
    const { keyboard } = installIphoneViewport()
    act(() => openBottomTerminal())
    renderWithProviders(<><textarea aria-label="composer" data-testid="composer" /><BottomTerminalPanel /></>)
    act(() => document.querySelector<HTMLTextAreaElement>('[data-testid="composer"]')!.focus())
    await nextFrame()
    await keyboard(340)
    expect(lift()).toBe('')
  })

  it('does not read a pinch-zoom as a keyboard', async () => {
    const { keyboard, vv } = installIphoneViewport()
    act(() => openBottomTerminal())
    renderWithProviders(<BottomTerminalPanel />)
    act(() => document.querySelector<HTMLTextAreaElement>('[data-testid^="cli-"]')!.focus())
    await nextFrame()
    vv.scale = 2 // zoomed in: the visual viewport is half the layout height
    await keyboard(400)
    expect(lift()).toBe('')
  })

  it('stays put with no keyboard', async () => {
    installIphoneViewport()
    act(() => openBottomTerminal())
    renderWithProviders(<BottomTerminalPanel />)
    act(() => document.querySelector<HTMLTextAreaElement>('[data-testid^="cli-"]')!.focus())
    await nextFrame()
    expect(lift()).toBe('')
  })
})
