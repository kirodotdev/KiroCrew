/**
 * "Before I Forget" topbar scratchpad.
 *
 * Covers persistence (mount load, debounced write, coalescing, flush on
 * teardown, failure surfacing), the dismissal paths (Escape, click-outside,
 * close control, toggle), and the timer-ordering regressions: Clear must beat
 * a queued debounced write, and a queued write must land — not fire setState —
 * when the component goes away.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, screen, fireEvent, act, cleanup } from '@testing-library/react'

// Render framer-motion elements as plain DOM. Without this the AnimatePresence
// exit animation keeps the panel mounted, so every close assertion reads as a
// failure to close.
vi.mock('framer-motion', async () => {
  const React = await import('react')
  const FRAMER_PROPS = new Set([
    'layout', 'layoutId', 'layoutScroll', 'initial', 'animate', 'exit',
    'transition', 'variants', 'whileHover', 'whileTap', 'whileInView',
    'drag', 'dragConstraints', 'dragElastic', 'onAnimationComplete',
  ])
  const make = (tag: string) =>
    React.forwardRef((props: Record<string, unknown>, ref: unknown) => {
      const clean: Record<string, unknown> = {}
      for (const k of Object.keys(props)) {
        if (k === 'children' || FRAMER_PROPS.has(k)) continue
        clean[k] = props[k]
      }
      return React.createElement(tag, { ...clean, ref }, props.children as React.ReactNode)
    })
  const motion = new Proxy({}, { get: (_t, tag: string) => make(tag) })
  return {
    motion,
    AnimatePresence: ({ children }: { children?: React.ReactNode }) =>
      React.createElement(React.Fragment, null, children),
    LayoutGroup: ({ children }: { children?: React.ReactNode }) =>
      React.createElement(React.Fragment, null, children),
  }
})

// Wrap safeSetItem in a spy that delegates to the real implementation, so a
// single test can inject a quota-style failure (mockReturnValueOnce(false))
// while every other test keeps real storage behaviour.
vi.mock('../utils/safeStorage', async importOriginal => {
  const actual = await importOriginal<typeof import('../utils/safeStorage')>()
  return { ...actual, safeSetItem: vi.fn(actual.safeSetItem) }
})

const BeforeIForget = (await import('../components/BeforeIForget')).default
const { safeGetItem, safeSetItem } = await import('../utils/safeStorage')

const STORAGE_KEY = 'kirocrew:before-i-forget'
const DEBOUNCE_MS = 400

function openPanel() {
  fireEvent.click(screen.getByRole('button', { name: 'Before I Forget' }))
}

function panelTextarea() {
  return screen.getByRole('textbox') as HTMLTextAreaElement
}

/** Advance past the debounce window inside act(), so the state update the timer
 *  performs is flushed before assertions run. */
function flushDebounce() {
  act(() => {
    vi.advanceTimersByTime(DEBOUNCE_MS + 1)
  })
}

describe('BeforeIForget', () => {
  beforeEach(() => {
    vi.useFakeTimers()
    localStorage.clear()
  })

  afterEach(() => {
    cleanup()
    vi.useRealTimers()
    localStorage.clear()
  })

  it('renders a topbar toggle and no panel until it is clicked', () => {
    render(<BeforeIForget />)
    expect(screen.getByRole('button', { name: 'Before I Forget' })).toBeInTheDocument()
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument()

    openPanel()
    expect(screen.getByRole('dialog', { name: 'Before I Forget' })).toBeInTheDocument()
  })

  it('closes again on a second toggle click', () => {
    render(<BeforeIForget />)
    openPanel()
    expect(screen.getByRole('dialog')).toBeInTheDocument()
    openPanel()
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument()
  })

  // In a real browser a click on the trigger is PRECEDED by a mousedown. The
  // outside-click closer used to catch that mousedown (the trigger sits outside
  // panelRef), close the panel, and then the click's toggle flipped it straight
  // back open — toggle-to-close was dead. The closer must ignore the trigger.
  it('toggle-to-close survives the mousedown that precedes a real click', () => {
    render(<BeforeIForget />)
    openPanel()
    act(() => { vi.advanceTimersByTime(1) }) // arm the outside-click listener

    const trigger = screen.getByRole('button', { name: 'Before I Forget' })
    fireEvent.mouseDown(trigger)
    // The mousedown alone must not close it — otherwise the click reopens.
    expect(screen.getByRole('dialog')).toBeInTheDocument()

    fireEvent.click(trigger)
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument()
  })

  it('loads persisted content on mount', () => {
    safeSetItem(STORAGE_KEY, 'carried over')
    render(<BeforeIForget />)
    openPanel()
    expect(panelTextarea().value).toBe('carried over')
  })

  it('persists typed content after the debounce window, not before', () => {
    render(<BeforeIForget />)
    openPanel()
    fireEvent.change(panelTextarea(), { target: { value: 'ship the thing' } })

    // Still queued — nothing written yet.
    expect(safeGetItem(STORAGE_KEY)).toBeNull()

    flushDebounce()
    expect(safeGetItem(STORAGE_KEY)).toBe('ship the thing')
  })

  it('coalesces rapid keystrokes into a single write of the latest value', () => {
    render(<BeforeIForget />)
    openPanel()
    // Re-query between edits: the panel re-renders on each keystroke, so a
    // handle captured once goes stale and later events land on a detached node.
    fireEvent.change(panelTextarea(), { target: { value: 'a' } })
    act(() => { vi.advanceTimersByTime(100) })
    fireEvent.change(panelTextarea(), { target: { value: 'ab' } })
    act(() => { vi.advanceTimersByTime(100) })
    fireEvent.change(panelTextarea(), { target: { value: 'abc' } })

    expect(safeGetItem(STORAGE_KEY)).toBeNull()
    flushDebounce()
    expect(safeGetItem(STORAGE_KEY)).toBe('abc')
  })

  it('reports saving while a write is queued and saved once it lands', () => {
    render(<BeforeIForget />)
    openPanel()
    fireEvent.change(panelTextarea(), { target: { value: 'note' } })
    expect(screen.getByText('saving…')).toBeInTheDocument()

    flushDebounce()
    expect(screen.getByText('saved')).toBeInTheDocument()
    expect(screen.queryByText('saving…')).not.toBeInTheDocument()
  })

  it('clears the textarea and the stored value', () => {
    safeSetItem(STORAGE_KEY, 'old note')
    render(<BeforeIForget />)
    openPanel()
    expect(panelTextarea().value).toBe('old note')

    fireEvent.click(screen.getByRole('button', { name: 'Clear scratchpad' }))
    expect(panelTextarea().value).toBe('')
    expect(safeGetItem(STORAGE_KEY)).toBe('')
  })

  // Regression: handleClear used to leave the debounced write queued, so the
  // timer fired after the clear and restored the text the user had just wiped.
  it('clear cancels a still-queued save instead of letting it restore the text', () => {
    render(<BeforeIForget />)
    openPanel()
    fireEvent.change(panelTextarea(), { target: { value: 'typed then cleared' } })

    // Clear inside the debounce window, while the write is still pending.
    act(() => { vi.advanceTimersByTime(DEBOUNCE_MS - 100) })
    fireEvent.click(screen.getByRole('button', { name: 'Clear scratchpad' }))

    // Let the window the cancelled timer would have fired in elapse.
    flushDebounce()

    expect(safeGetItem(STORAGE_KEY)).toBe('')
    expect(panelTextarea().value).toBe('')
  })

  // Regression: the debounce timer used to survive teardown and fire setState
  // against an unmounted tree; the first fix dropped the queued write, which
  // traded the crash for data loss. Teardown now FLUSHES the write — the text
  // lands in storage, and the flush performs no setState so nothing touches the
  // unmounted tree.
  it('flushes a queued save on unmount instead of dropping it', () => {
    const { unmount } = render(<BeforeIForget />)
    openPanel()
    fireEvent.change(panelTextarea(), { target: { value: 'must land' } })

    unmount()
    expect(safeGetItem(STORAGE_KEY)).toBe('must land')

    // And nothing fires later against the torn-down tree.
    act(() => { vi.advanceTimersByTime(DEBOUNCE_MS + 1) })
    expect(safeGetItem(STORAGE_KEY)).toBe('must land')
  })

  // Typing and refreshing within the debounce window used to discard the
  // newest text — the queued write died with the document.
  it('flushes a queued save on pagehide so a refresh cannot discard typed text', () => {
    render(<BeforeIForget />)
    openPanel()
    fireEvent.change(panelTextarea(), { target: { value: 'typed then refreshed' } })
    expect(safeGetItem(STORAGE_KEY)).toBeNull()

    act(() => { window.dispatchEvent(new Event('pagehide')) })
    expect(safeGetItem(STORAGE_KEY)).toBe('typed then refreshed')
  })

  it('reports a failed write as not saved, never as saved', () => {
    render(<BeforeIForget />)
    openPanel()
    fireEvent.change(panelTextarea(), { target: { value: 'doomed write' } })

    vi.mocked(safeSetItem).mockReturnValueOnce(false)
    flushDebounce()

    expect(screen.getByText('not saved')).toBeInTheDocument()
    expect(screen.queryByText('saved')).not.toBeInTheDocument()

    // The next successful write clears the failure state.
    fireEvent.change(panelTextarea(), { target: { value: 'doomed write, retried' } })
    flushDebounce()
    expect(screen.getByText('saved')).toBeInTheDocument()
    expect(screen.queryByText('not saved')).not.toBeInTheDocument()
  })

  // Another window writing the key must reach this one — otherwise this
  // window's next edit starts from its stale mount-time copy and overwrites
  // the newer note.
  it('adopts a write from another window via the storage event', () => {
    render(<BeforeIForget />)
    openPanel()
    expect(panelTextarea().value).toBe('')

    act(() => {
      window.dispatchEvent(new StorageEvent('storage', {
        key: STORAGE_KEY, newValue: 'written elsewhere',
      }))
    })
    expect(panelTextarea().value).toBe('written elsewhere')
  })

  it('does not let a remote write clobber an edit in flight here', () => {
    render(<BeforeIForget />)
    openPanel()
    fireEvent.change(panelTextarea(), { target: { value: 'local, mid-debounce' } })

    // Remote write lands while this window's save is still queued.
    act(() => {
      window.dispatchEvent(new StorageEvent('storage', {
        key: STORAGE_KEY, newValue: 'remote value',
      }))
    })
    expect(panelTextarea().value).toBe('local, mid-debounce')

    // The local edit then persists as normal (last writer wins).
    flushDebounce()
    expect(safeGetItem(STORAGE_KEY)).toBe('local, mid-debounce')
  })

  it('shows the unsaved-content dot only while the panel is closed', () => {
    safeSetItem(STORAGE_KEY, 'has content')
    const { container } = render(<BeforeIForget />)
    expect(container.querySelector('.rounded-full.bg-accent')).not.toBeNull()

    openPanel()
    expect(container.querySelector('.rounded-full.bg-accent')).toBeNull()
  })

  it('shows no dot when the scratchpad is empty', () => {
    const { container } = render(<BeforeIForget />)
    expect(container.querySelector('.rounded-full.bg-accent')).toBeNull()
  })

  it('renders a character count once there is content', () => {
    render(<BeforeIForget />)
    openPanel()
    expect(screen.queryByText(/chars/)).not.toBeInTheDocument()

    fireEvent.change(panelTextarea(), { target: { value: 'four' } })
    expect(screen.getByText('4 chars')).toBeInTheDocument()
  })

  it('uses the singular plural form at count 1', () => {
    render(<BeforeIForget />)
    openPanel()
    fireEvent.change(panelTextarea(), { target: { value: 'x' } })
    expect(screen.getByText('1 char')).toBeInTheDocument()
    expect(screen.queryByText('1 chars')).not.toBeInTheDocument()
  })

  it('dismisses on Escape', () => {
    render(<BeforeIForget />)
    openPanel()
    act(() => {
      document.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape', bubbles: true }))
    })
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument()
  })

  it('ignores other keys', () => {
    render(<BeforeIForget />)
    openPanel()
    act(() => {
      document.dispatchEvent(new KeyboardEvent('keydown', { key: 'a', bubbles: true }))
    })
    expect(screen.getByRole('dialog')).toBeInTheDocument()
  })

  it('dismisses on a click outside the panel', () => {
    render(<BeforeIForget />)
    openPanel()
    // The listener is attached on a zero-delay timeout so the opening click
    // itself cannot close the panel.
    act(() => { vi.advanceTimersByTime(1) })

    act(() => { document.body.dispatchEvent(new MouseEvent('mousedown', { bubbles: true })) })
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument()
  })

  it('keeps the panel open when the click lands inside it', () => {
    render(<BeforeIForget />)
    openPanel()
    act(() => { vi.advanceTimersByTime(1) })

    fireEvent.mouseDown(panelTextarea())
    expect(screen.getByRole('dialog')).toBeInTheDocument()
  })

  it('closes from the panel header close control', () => {
    render(<BeforeIForget />)
    openPanel()
    fireEvent.click(screen.getByRole('button', { name: 'Close' }))
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument()
  })

  it('focuses the textarea shortly after opening', () => {
    render(<BeforeIForget />)
    openPanel()
    act(() => { vi.advanceTimersByTime(150) })
    expect(document.activeElement).toBe(panelTextarea())
  })
})
