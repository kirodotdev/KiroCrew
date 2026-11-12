import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { useState } from 'react'
import { act, waitFor } from '@testing-library/react'
import { renderWithProviders } from './helpers'
import { SlotProvider } from '../providers/SlotContext'

/* On iPadOS the on-screen keyboard shrinks only the visual viewport and
 * Safari's focus pan does not clear the composer, so the composer
 * (`input-area`) must rise above the keyboard while it holds focus (#25240).
 * It grows by bottom padding, so the dock's measured height (and the
 * transcript's reserved space) grows with it instead of being painted over.
 * The lift comes from useKeyboardLift: the keyboard gap left after Safari's
 * pan (`offsetTop`), and 0 when the composer is not focused. */
const mockApi = vi.hoisted(() => ({
  skills: vi.fn(),
  skillTrust: vi.fn(),
  grantSkillTrust: vi.fn(),
  fileSearch: vi.fn(),
}))
vi.mock('../api/client', () => ({ api: mockApi }))

import ChatInput from '../components/ChatInput'

type Listener = () => void

function Host() {
  const [val, setVal] = useState('')
  return (
    <SlotProvider slotId="chat-1">
      <ChatInput value={val} onChange={setVal} onSend={vi.fn()} onUploadFiles={vi.fn()} />
    </SlotProvider>
  )
}

const LAYOUT_HEIGHT = 1024

describe('ChatInput lifts above the on-screen keyboard', () => {
  let originalViewport: PropertyDescriptor | undefined
  let originalClientHeight: PropertyDescriptor | undefined
  let listeners: Record<string, Listener[]>
  let vv: { height: number; offsetTop: number; scale: number; addEventListener: (t: string, l: Listener) => void; removeEventListener: (t: string, l: Listener) => void }

  beforeEach(() => {
    vi.clearAllMocks()
    localStorage.clear()
    mockApi.skills.mockResolvedValue([])
    mockApi.skillTrust.mockResolvedValue({ project: '/work/p', project_key: '/work/p' })
    mockApi.grantSkillTrust.mockResolvedValue({ trusted: true })
    mockApi.fileSearch.mockResolvedValue({ results: [] })
    originalViewport = Object.getOwnPropertyDescriptor(window, 'visualViewport')
    originalClientHeight = Object.getOwnPropertyDescriptor(document.documentElement, 'clientHeight')
    Object.defineProperty(document.documentElement, 'clientHeight', { configurable: true, value: LAYOUT_HEIGHT })
    listeners = {}
    vv = {
      height: LAYOUT_HEIGHT,
      offsetTop: 0,
      scale: 1,
      addEventListener: (t, l) => { (listeners[t] ||= []).push(l) },
      removeEventListener: (t, l) => { listeners[t] = (listeners[t] || []).filter(x => x !== l) },
    }
    Object.defineProperty(window, 'visualViewport', { configurable: true, value: vv })
  })
  afterEach(() => {
    if (originalViewport) Object.defineProperty(window, 'visualViewport', originalViewport)
    else delete (window as { visualViewport?: VisualViewport }).visualViewport
    if (originalClientHeight) Object.defineProperty(document.documentElement, 'clientHeight', originalClientHeight)
    else delete (document.documentElement as { clientHeight?: number }).clientHeight
  })

  function fire(t: string) { (listeners[t] || []).forEach(l => l()) }
  function composer(): HTMLElement {
    return document.querySelector('.input-area') as HTMLElement
  }
  /** Lay the composer out with its bottom edge at `bottom` (the dock is bottom-anchored, so the lift padding grows it upward). */
  function layOut(bottom: number) {
    const el = composer()
    el.getBoundingClientRect = () => DOMRect.fromRect({ x: 0, y: bottom - 120, width: 820, height: 120 })
  }
  const lift = () => composer().style.paddingBottom
  function focusComposer() {
    const field = composer().querySelector('textarea, [contenteditable="true"]') as HTMLElement
    expect(field).toBeInstanceOf(HTMLElement)
    act(() => { field.focus() })
  }
  function openKeyboard(height: number, offsetTop: number, scale = 1) {
    vv.height = height
    vv.offsetTop = offsetTop
    vv.scale = scale
    act(() => { fire('resize'); fire('scroll') })
  }

  it('stays put with no keyboard (desktop)', async () => {
    renderWithProviders(<Host />)
    layOut(LAYOUT_HEIGHT)
    focusComposer()
    await act(() => new Promise<void>(r => requestAnimationFrame(() => r())))
    expect(lift()).toBe('')
  })

  it('lifts by the keyboard height when Safari does not pan (iPad)', async () => {
    renderWithProviders(<Host />)
    layOut(LAYOUT_HEIGHT)
    focusComposer()
    openKeyboard(600, 0)
    await waitFor(() => expect(lift()).toBe('calc(0.25rem + 424px)'))
    // The shelf's fade is anchored to the dock wrapper's bottom; it rises by the same lift.
    expect(composer().style.getPropertyValue('--glass-shelf-lift')).toBe('424px')
  })

  it('lifts only by the part Safari\'s pan left covered, never twice', async () => {
    renderWithProviders(<Host />)
    layOut(LAYOUT_HEIGHT)
    focusComposer()
    openKeyboard(600, 324)
    await waitFor(() => expect(lift()).toBe('calc(0.25rem + 100px)'))
    // A pan that already clears the keyboard (iPhone) leaves no lift.
    openKeyboard(600, 424)
    await waitFor(() => expect(lift()).toBe(''))
  })

  it('lifts only past a docked terminal below it, and holds the lift on re-measure', async () => {
    renderWithProviders(<Host />)
    layOut(LAYOUT_HEIGHT - 300)
    focusComposer()
    openKeyboard(624, 0)
    await waitFor(() => expect(lift()).toBe('calc(0.25rem + 100px)'))
    act(() => { fire('resize') })
    await act(() => new Promise<void>(r => requestAnimationFrame(() => r())))
    expect(lift()).toBe('calc(0.25rem + 100px)')
  })

  it('does not lift for a pinch-zoom or when the composer is not focused', async () => {
    renderWithProviders(<Host />)
    layOut(LAYOUT_HEIGHT)
    openKeyboard(600, 0)
    await act(() => new Promise<void>(r => requestAnimationFrame(() => r())))
    expect(lift()).toBe('')
    focusComposer()
    openKeyboard(600, 0, 2)
    await act(() => new Promise<void>(r => requestAnimationFrame(() => r())))
    expect(lift()).toBe('')
  })
})
