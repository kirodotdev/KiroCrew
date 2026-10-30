import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { screen, fireEvent, act } from '@testing-library/react'
import { renderWithProviders } from './helpers'
import ChatInput from '../components/ChatInput'

/**
 * While "Optimize prompt" runs, the composer is read-only and Send is disabled
 * until the request settles. A mis-tap on the optimize button (it sits next to
 * Send on a phone) used to leave no way out short of waiting: the overlay gave
 * no control, so a slow or hung optimizer held the draft hostage. The overlay
 * now carries a Cancel button that aborts the request and hands the draft back
 * untouched.
 */

const ORIGINAL = 'my prompt'

// The Lexical composer is a React.lazy chunk (chat-input/engine.tsx), so its
// first mount can outlast findBy's 1000 ms default on a loaded runner.
const LAZY_COMPOSER_MOUNT = { timeout: 5000 }

/** An optimizer request that never answers on its own, but honours abort. */
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

const clickOptimize = () =>
  fireEvent.click(screen.getByRole('button', { name: 'Optimize prompt' }))

describe('ChatInput optimize: the in-flight overlay can be cancelled', () => {
  beforeEach(() => {
    vi.stubGlobal('requestAnimationFrame', (cb: FrameRequestCallback) => {
      cb(0)
      return 0
    })
  })
  afterEach(() => {
    vi.unstubAllGlobals()
  })

  it('aborts the request and returns the draft, editable, with no error', async () => {
    const signals: AbortSignal[] = []
    const onChange = vi.fn()
    vi.stubGlobal('fetch', hangingOptimizer(signals))

    renderWithProviders(
      <ChatInput value={ORIGINAL} onChange={onChange} onSend={vi.fn()} connected={true} />,
    )
    clickOptimize()

    const textbox = screen.getByRole('textbox') as HTMLTextAreaElement
    await vi.waitFor(() => expect(textbox.readOnly).toBe(true))

    const cancel = await screen.findByRole('button', { name: 'Cancel optimizing' })
    await act(async () => { fireEvent.click(cancel) })

    expect(signals).toHaveLength(1)
    expect(signals[0].aborted).toBe(true)
    await vi.waitFor(() => expect(screen.queryByRole('button', { name: 'Cancel optimizing' })).toBeNull())
    expect(textbox.readOnly).toBe(false)
    expect(screen.queryByTestId('optimize-error')).toBeNull()
    // The draft is the user's own text: a cancel writes nothing back.
    expect(onChange).not.toHaveBeenCalled()
    expect(screen.getByRole('button', { name: 'Optimize prompt' })).not.toBeDisabled()
  })

  it('puts focus back in the composer so the keyboard returns without a second tap', async () => {
    vi.stubGlobal('fetch', hangingOptimizer([]))
    renderWithProviders(
      <ChatInput value={ORIGINAL} onChange={vi.fn()} onSend={vi.fn()} connected={true} />,
    )
    clickOptimize()
    const cancel = await screen.findByRole('button', { name: 'Cancel optimizing' })
    cancel.focus()
    await act(async () => { fireEvent.click(cancel) })

    const textbox = screen.getByRole('textbox') as HTMLTextAreaElement
    expect(document.activeElement).toBe(textbox)
    expect(textbox.selectionStart).toBe(ORIGINAL.length)
  })

  it('makes the Lexical composer editable inside the tap, so a phone raises its keyboard', async () => {
    vi.stubGlobal('fetch', hangingOptimizer([]))
    renderWithProviders(
      <ChatInput value={ORIGINAL} onChange={vi.fn()} onSend={vi.fn()} connected={true} lexicalComposer />,
    )
    const root = await screen.findByRole('textbox', undefined, LAZY_COMPOSER_MOUNT)
    clickOptimize()
    await vi.waitFor(() => expect(root.getAttribute('contenteditable')).toBe('false'))
    const cancel = await screen.findByRole('button', { name: 'Cancel optimizing' })

    // Record the attribute at the moment focus() is called: a mobile browser
    // only raises the keyboard for a focus() on an element that is already
    // editable while the user's gesture is still running, before React
    // re-renders the composer out of read-only.
    let editableAtFocus: string | null = null
    const realFocus = root.focus.bind(root)
    vi.spyOn(root, 'focus').mockImplementation((opts?: FocusOptions) => {
      editableAtFocus ??= root.getAttribute('contenteditable')
      realFocus(opts)
    })
    await act(async () => { fireEvent.click(cancel) })

    expect(editableAtFocus).toBe('true')
    expect(document.activeElement).toBe(root)
  })

  it('leaves the Lexical composer non-editable when the host disabled it during the optimize', async () => {
    vi.stubGlobal('fetch', hangingOptimizer([]))
    const props = { value: ORIGINAL, onChange: vi.fn(), onSend: vi.fn(), connected: true, lexicalComposer: true }
    const { rerender } = renderWithProviders(<ChatInput {...props} />)
    const root = await screen.findByRole('textbox', undefined, LAZY_COMPOSER_MOUNT)
    clickOptimize()
    await screen.findByRole('button', { name: 'Cancel optimizing' })
    rerender(<ChatInput {...props} disabled />)
    await act(async () => { fireEvent.click(screen.getByRole('button', { name: 'Cancel optimizing' })) })

    // Lexical's setEditable(false) is a no-op on an editor that is already
    // non-editable, so a DOM write here would never be undone.
    expect(root.getAttribute('contenteditable')).toBe('false')
  })

  it('leaves the textarea read-only when the host disabled it during the optimize', async () => {
    vi.stubGlobal('fetch', hangingOptimizer([]))
    const props = { value: ORIGINAL, onChange: vi.fn(), onSend: vi.fn(), connected: true }
    const { rerender } = renderWithProviders(<ChatInput {...props} />)
    clickOptimize()
    await screen.findByRole('button', { name: 'Cancel optimizing' })
    rerender(<ChatInput {...props} disabled />)
    await act(async () => { fireEvent.click(screen.getByRole('button', { name: 'Cancel optimizing' })) })

    // readOnly={optimizing || disabled} stays true across the settle, so React
    // never rewrites the attribute and a DOM write here would never be undone.
    expect((screen.getByRole('textbox') as HTMLTextAreaElement).readOnly).toBe(true)
  })

  it('cancels on Escape in the prompt box, and that Escape does not stop the running turn', async () => {
    const signals: AbortSignal[] = []
    const onStop = vi.fn()
    vi.stubGlobal('fetch', hangingOptimizer(signals))
    const props = { value: ORIGINAL, onChange: vi.fn(), onSend: vi.fn(), connected: true, onStop }
    const { rerender } = renderWithProviders(<ChatInput {...props} />)
    clickOptimize()
    await screen.findByRole('button', { name: 'Cancel optimizing' })
    // A turn starts on the slot (a queued send) while the optimize is in flight.
    rerender(<ChatInput {...props} isRunning />)
    const textbox = screen.getByRole('textbox') as HTMLTextAreaElement
    await act(async () => { fireEvent.keyDown(textbox, { key: 'Escape' }) })

    expect(signals).toHaveLength(1)
    expect(signals[0].aborted).toBe(true)
    expect(onStop).not.toHaveBeenCalled()
    await vi.waitFor(() => expect(screen.queryByRole('button', { name: 'Cancel optimizing' })).toBeNull())
    expect(textbox.readOnly).toBe(false)
  })

  it('drops a rewrite whose body was already read when Cancel landed', async () => {
    const onChange = vi.fn()
    let answer: (v: unknown) => void = () => {}
    vi.stubGlobal('fetch', vi.fn((url: string) => {
      if (typeof url === 'string' && url.includes('/api/optimizer/optimize')) {
        // The response is in hand; only its body read is still pending, and
        // that read ignores the abort signal.
        return Promise.resolve({
          ok: true,
          json: () => new Promise((resolve) => { answer = resolve }),
        })
      }
      return Promise.resolve({ ok: true, json: async () => [] })
    }))
    renderWithProviders(
      <ChatInput value={ORIGINAL} onChange={onChange} onSend={vi.fn()} connected={true} />,
    )
    clickOptimize()
    const cancel = await screen.findByRole('button', { name: 'Cancel optimizing' })
    await act(async () => { fireEvent.click(cancel) })
    await act(async () => { answer({ changed: true, optimized: 'a rewrite' }) })

    await vi.waitFor(() => expect(screen.queryByRole('button', { name: 'Cancel optimizing' })).toBeNull())
    expect(onChange).not.toHaveBeenCalled()
  })
})
