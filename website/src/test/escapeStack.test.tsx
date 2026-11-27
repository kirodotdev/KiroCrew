import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { useState } from 'react'
import { screen, fireEvent, act, render } from '@testing-library/react'
import { renderWithProviders } from './helpers'
import ChatInput from '../components/ChatInput'
import { LocalOverlayRoot } from '../components/overlayRoot'
import { useEscapeLayer, type EscapeVerdict } from '../hooks/useEscapeLayer'
import { useHoverIntent } from '../hooks/useHoverIntent'
import { useListKeyboardNav } from '../hooks/useListKeyboardNav'
import Modal from '../components/Modal'
import { noteEscapeStoppedSpeech } from '../utils/speechStopKey'
import { clickOptimize } from './optimizeRow'

/**
 * The page-wide Escape stack (issue #25202). The first block pins the stack's
 * own ordering; the second replays the four collisions PR #25185 hit while the
 * rich-text composer is locked for an optimize (issue #22470), each of which
 * fails on a tree without the stack.
 */

const ORIGINAL = 'my prompt'
// The Lexical composer is a React.lazy chunk; its first mount can be slow.
const LAZY_COMPOSER_MOUNT = { timeout: 5000 }

const hangingOptimizer = (signals: AbortSignal[]) =>
  vi.fn((url: string, init?: RequestInit) => {
    if (typeof url === 'string' && url.includes('/api/optimizer/optimize')) {
      return new Promise((_resolve, reject) => {
        const signal = init?.signal
        if (signal) {
          signals.push(signal)
          signal.addEventListener('abort', () => reject(new DOMException('Aborted', 'AbortError')))
        }
      })
    }
    return Promise.resolve({ ok: true, json: async () => [] })
  })

const escapeOn = (el: EventTarget) => act(async () => { fireEvent.keyDown(el, { key: 'Escape' }) })

function Layer({ name, priority, verdict, log }: { name: string; priority?: number; verdict: EscapeVerdict; log: string[] }) {
  useEscapeLayer(true, () => { log.push(name); return verdict }, priority)
  return null
}

describe('useEscapeLayer: one owner for page-wide Escape', () => {
  it('asks the newest layer first within a priority, higher priorities before it, and consumes a take', () => {
    const log: string[] = []
    const later = vi.fn()
    window.addEventListener('keydown', later)
    try {
      const view = render(<><Layer name="old" verdict="take" log={log} /><Layer name="high" priority={5} verdict="pass" log={log} /></>)
      view.rerender(<><Layer name="old" verdict="take" log={log} /><Layer name="high" priority={5} verdict="pass" log={log} /><Layer name="new" verdict="pass" log={log} /></>)
      fireEvent.keyDown(document.body, { key: 'Escape' })
      expect(log).toEqual(['high', 'new', 'old'])
      expect(later).not.toHaveBeenCalled()
      // A popped layer is never asked again.
      log.length = 0
      view.rerender(<Layer name="old" verdict="take" log={log} />)
      fireEvent.keyDown(document.body, { key: 'Escape' })
      expect(log).toEqual(['old'])
    } finally {
      window.removeEventListener('keydown', later)
    }
  })

  it('leaves a yielded press untouched for owners outside the stack, and asks no lower layer', () => {
    const log: string[] = []
    const seen: boolean[] = []
    const outside = (e: KeyboardEvent) => { seen.push(e.defaultPrevented) }
    window.addEventListener('keydown', outside)
    try {
      render(<><Layer name="low" verdict="take" log={log} /><Layer name="top" priority={5} verdict="yield" log={log} /></>)
      fireEvent.keyDown(document.body, { key: 'Escape' })
    } finally {
      window.removeEventListener('keydown', outside)
    }
    expect(log).toEqual(['top'])
    expect(seen).toEqual([false])
  })
})

describe('Escape while the rich-text composer is locked for an optimize', () => {
  beforeEach(() => {
    vi.stubGlobal('requestAnimationFrame', (cb: FrameRequestCallback) => { cb(0); return 0 })
  })
  afterEach(() => { vi.unstubAllGlobals() })

  const props = { value: ORIGINAL, onChange: vi.fn(), onSend: vi.fn(), connected: true, lexicalComposer: true }

  /** Focus a composer's editor, then start its optimize. */
  const startOptimize = async (index: number) => {
    const roots = await screen.findAllByRole('textbox', undefined, LAZY_COMPOSER_MOUNT)
    act(() => { roots[index].focus() })
    clickOptimize(index)
    await vi.waitFor(() => expect(roots[index].getAttribute('contenteditable')).toBe('false'))
  }

  it('cancels only the split pane that last held focus when the press lands on the page body', async () => {
    const signals: AbortSignal[] = []
    vi.stubGlobal('fetch', hangingOptimizer(signals))
    renderWithProviders(<><ChatInput {...props} /><ChatInput {...props} /></>)
    await startOptimize(0)
    await startOptimize(1)
    await escapeOn(document.body)
    expect(signals).toHaveLength(2)
    expect(signals[0].aborted).toBe(false)
    expect(signals[1].aborted).toBe(true)
  })

  it('cancels the optimize and leaves the notification banner card alone', async () => {
    const signals: AbortSignal[] = []
    vi.stubGlobal('fetch', hangingOptimizer(signals))
    // Registered before the optimize, on window like NotificationBanner.
    const seenByBanner: boolean[] = []
    const banner = (e: KeyboardEvent) => { seenByBanner.push(e.defaultPrevented) }
    window.addEventListener('keydown', banner)
    try {
      renderWithProviders(<ChatInput {...props} />)
      await startOptimize(0)
      await escapeOn(screen.getByRole('button', { name: 'Cancel optimizing' }))
    } finally {
      window.removeEventListener('keydown', banner)
    }
    expect(signals[0].aborted).toBe(true)
    expect(seenByBanner.every(spent => spent)).toBe(true)
  })

  it('lets a flyout opened mid-optimize take the first Escape, then cancels on the next', async () => {
    const signals: AbortSignal[] = []
    vi.stubGlobal('fetch', hangingOptimizer(signals))
    function Flyout() {
      const hover = useHoverIntent()
      return <button onClick={hover.openNow}>{hover.open ? 'flyout open' : 'flyout closed'}</button>
    }
    renderWithProviders(<><ChatInput {...props} /><Flyout /></>)
    await startOptimize(0)
    fireEvent.click(screen.getByRole('button', { name: 'flyout closed' }))
    await escapeOn(document.body)
    expect(screen.getByRole('button', { name: 'flyout closed' })).toBeInTheDocument()
    expect(signals[0].aborted).toBe(false)
    // Other document-capture listeners (the speech stop, list menus) still
    // hear a press the flyout took.
    let heardByDocumentCapture = 0
    const other = () => { heardByDocumentCapture += 1 }
    fireEvent.click(screen.getByRole('button', { name: 'flyout closed' }))
    document.addEventListener('keydown', other, true)
    try {
      await escapeOn(document.body)
    } finally {
      document.removeEventListener('keydown', other, true)
    }
    expect(heardByDocumentCapture).toBe(1)
    expect(signals[0].aborted).toBe(false)
    await escapeOn(document.body)
    expect(signals[0].aborted).toBe(true)
  })

  it('lets the speech read-back stop take the Escape first, so it does not also cancel the optimize', async () => {
    const signals: AbortSignal[] = []
    vi.stubGlobal('fetch', hangingOptimizer(signals))
    // The stop-speaking listener mounts at the App root, before any layer, on
    // document capture, and marks the press without preventing it.
    let speechStopped = 0
    const speech = (e: KeyboardEvent) => { if (e.key === 'Escape') { speechStopped += 1; noteEscapeStoppedSpeech(e) } }
    document.addEventListener('keydown', speech, true)
    try {
      renderWithProviders(<ChatInput {...props} />)
      await startOptimize(0)
      await escapeOn(screen.getByRole('button', { name: 'Cancel optimizing' }))
    } finally {
      document.removeEventListener('keydown', speech, true)
    }
    expect(speechStopped).toBe(1)
    expect(signals[0].aborted).toBe(false)
  })

  it('stands down while a remote Crew is in front, and answers again once this Crew returns', async () => {
    const signals: AbortSignal[] = []
    vi.stubGlobal('fetch', hangingOptimizer(signals))
    const view = renderWithProviders(<LocalOverlayRoot hidden={false}><ChatInput {...props} /></LocalOverlayRoot>)
    await startOptimize(0)
    view.rerender(<LocalOverlayRoot hidden><ChatInput {...props} /></LocalOverlayRoot>)
    await escapeOn(document.body)
    expect(signals[0].aborted).toBe(false)
    view.rerender(<LocalOverlayRoot hidden={false}><ChatInput {...props} /></LocalOverlayRoot>)
    await escapeOn(document.body)
    expect(signals[0].aborted).toBe(true)
  })
})

describe('Escape with a list menu (useListKeyboardNav) open', () => {
  function Menu({ open, onClose }: { open: boolean; onClose: () => void }) {
    useListKeyboardNav({ open, count: 2, onChoose: () => {}, onClose })
    return null
  }

  it('closes only the list menu while the flyout is open, then the flyout on the next press', () => {
    const closed: string[] = []
    function Surfaces() {
      const hover = useHoverIntent()
      const [menu, setMenu] = useState(false)
      return (
        <>
          <button onClick={hover.openNow}>{hover.open ? 'flyout open' : 'flyout closed'}</button>
          <button onClick={() => setMenu(true)}>open menu</button>
          <Menu open={hover.open && menu} onClose={() => { closed.push('menu'); setMenu(false) }} />
        </>
      )
    }
    render(<Surfaces />)
    // The flyout opens first, then a menu inside it: the order in which a
    // page-wide listener registered used to decide that one press closed both.
    fireEvent.click(screen.getByRole('button', { name: 'flyout closed' }))
    fireEvent.click(screen.getByRole('button', { name: 'open menu' }))
    act(() => { fireEvent.keyDown(document.body, { key: 'Escape' }) })
    expect(closed).toEqual(['menu'])
    expect(screen.getByRole('button', { name: 'flyout open' })).toBeInTheDocument()
    act(() => { fireEvent.keyDown(document.body, { key: 'Escape' }) })
    expect(closed).toEqual(['menu'])
    expect(screen.getByRole('button', { name: 'flyout closed' })).toBeInTheDocument()
  })

  it('still closes a list menu alone, and leaves a flyout alone when no menu is open', () => {
    const onClose = vi.fn()
    const view = render(<Menu open onClose={onClose} />)
    fireEvent.keyDown(document.body, { key: 'Escape' })
    expect(onClose).toHaveBeenCalledTimes(1)
    view.unmount()
    function Flyout() {
      const hover = useHoverIntent()
      return <button onClick={hover.openNow}>{hover.open ? 'flyout open' : 'flyout closed'}</button>
    }
    render(<Flyout />)
    fireEvent.click(screen.getByRole('button', { name: 'flyout closed' }))
    fireEvent.keyDown(document.body, { key: 'Escape' })
    expect(screen.getByRole('button', { name: 'flyout closed' })).toBeInTheDocument()
  })

  it('closes a list menu inside a dialog first, then the dialog on the next press', () => {
    const closed: string[] = []
    function Host() {
      const [dialog, setDialog] = useState(true)
      const [menu, setMenu] = useState(true)
      return (
        <Modal open={dialog} onClose={() => { closed.push('dialog'); setDialog(false) }} title="Dialog">
          <Menu open={menu} onClose={() => { closed.push('menu'); setMenu(false) }} />
        </Modal>
      )
    }
    renderWithProviders(<Host />)
    const target = screen.getByRole('dialog')
    act(() => { fireEvent.keyDown(target, { key: 'Escape' }) })
    expect(closed).toEqual(['menu'])
    act(() => { fireEvent.keyDown(target, { key: 'Escape' }) })
    expect(closed).toEqual(['menu', 'dialog'])
  })
})
