import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { screen, fireEvent } from '@testing-library/react'
import { renderWithProviders } from './helpers'
import ChatInput from '../components/ChatInput'

/**
 * The optimizer's write-back (`setTextUndoable`) is the sibling of the paste
 * path fixed in #6657, and it fails by the identical route: it drives the
 * textarea through `document.execCommand('insertText')` and trusts the call.
 *
 * That call is not trustworthy. It is missing on some engines, and iOS
 * Safari's native handling reports success on a <textarea> while leaving the
 * field untouched. Here the whole field has already been `select()`ed and the
 * readOnly overlay has already cleared, so a failure that goes unverified puts
 * the user's ORIGINAL prompt back on screen with the optimizer's result
 * discarded and nothing logged — visually identical to a successful optimize
 * that changed nothing.
 */

const OPTIMIZED = 'a much better prompt'
const ORIGINAL = 'my prompt'

const stubOptimizer = (optimized: string | null = OPTIMIZED) =>
  vi.fn((url: string) => {
    if (typeof url === 'string' && url.includes('/api/optimizer/optimize')) {
      return Promise.resolve({
        ok: true,
        json: async () => ({ changed: optimized !== null, optimized }),
      })
    }
    // Any other app fetch (e.g. the slash-command list) gets a benign shape.
    return Promise.resolve({ ok: true, json: async () => [] })
  })

const setExecCommand = (impl: (...a: unknown[]) => boolean) => {
  ;(document as unknown as { execCommand: (...a: unknown[]) => boolean }).execCommand = impl
}

const clickOptimize = () =>
  fireEvent.click(screen.getByRole('button', { name: 'Optimize prompt' }))

describe('ChatInput optimize: the write-back is verified against the DOM', () => {
  beforeEach(() => {
    vi.stubGlobal('requestAnimationFrame', (cb: FrameRequestCallback) => {
      cb(0)
      return 0
    })
  })

  afterEach(() => {
    vi.unstubAllGlobals()
    delete (document as unknown as { execCommand?: unknown }).execCommand
  })

  it('lands the optimized prompt when execCommand reports success but inserts NOTHING', async () => {
    // The regression: `optimizing` has already cleared and the textarea is
    // writable again, so trusting the boolean loses the optimizer's result
    // with no error and no visible trace.
    const onChange = vi.fn()
    vi.stubGlobal('fetch', stubOptimizer())
    setExecCommand(vi.fn(() => true)) // claims success, leaves the field alone

    renderWithProviders(
      <ChatInput value={ORIGINAL} onChange={onChange} onSend={vi.fn()} connected={true} />,
    )
    clickOptimize()

    await vi.waitFor(() => expect(onChange).toHaveBeenCalledWith(OPTIMIZED))
  })

  it('lands the optimized prompt when execCommand is absent entirely', async () => {
    const onChange = vi.fn()
    vi.stubGlobal('fetch', stubOptimizer())
    delete (document as unknown as { execCommand?: unknown }).execCommand

    renderWithProviders(
      <ChatInput value={ORIGINAL} onChange={onChange} onSend={vi.fn()} connected={true} />,
    )
    clickOptimize()

    await vi.waitFor(() => expect(onChange).toHaveBeenCalledWith(OPTIMIZED))
  })

  it('lands the optimized prompt when execCommand throws', async () => {
    const onChange = vi.fn()
    vi.stubGlobal('fetch', stubOptimizer())
    setExecCommand(
      vi.fn(() => {
        throw new Error('execCommand is not supported in this context')
      }),
    )

    renderWithProviders(
      <ChatInput value={ORIGINAL} onChange={onChange} onSend={vi.fn()} connected={true} />,
    )
    clickOptimize()

    await vi.waitFor(() => expect(onChange).toHaveBeenCalledWith(OPTIMIZED))
  })

  it('keeps the native input pipeline when execCommand verifiably inserted', async () => {
    // The DOM read-back must not turn every optimize into a controlled splice:
    // when the native insert really happened, the textarea's own onChange has
    // already reported that string and the component leaves the caret alone.
    const onChange = vi.fn()
    vi.stubGlobal('fetch', stubOptimizer())
    const setSelectionRange = vi.fn()
    const exec = vi.fn((_cmd: unknown, _ui: unknown, text: unknown) => {
      const ta = screen.getByRole('textbox') as HTMLTextAreaElement
      ta.value = String(text)
      ta.setSelectionRange = setSelectionRange
      return true
    })
    setExecCommand(exec)

    renderWithProviders(
      <ChatInput value={ORIGINAL} onChange={onChange} onSend={vi.fn()} connected={true} />,
    )
    clickOptimize()

    await vi.waitFor(() => expect(exec).toHaveBeenCalledWith('insertText', false, OPTIMIZED))
    // The native path owns the caret; the fallback's repositioning must not run.
    expect(setSelectionRange).not.toHaveBeenCalled()
  })

  it('lands the optimized prompt when the native insert emits no input event', async () => {
    // The narrow arm between the two above: `execCommand` really did mutate the
    // textarea, so the DOM read-back verifies, but the engine emitted no
    // `input` event — React's synthetic onChange never ran and the controlled
    // value is still the ORIGINAL. Returning early on the strength of the DOM
    // alone leaves the next render free to restore that stale prop over the
    // optimizer's result. Reconciling unconditionally, the way the paste
    // sibling already does, is what makes the DOM read-back load-bearing.
    const onChange = vi.fn()
    vi.stubGlobal('fetch', stubOptimizer())
    setExecCommand(
      vi.fn((_cmd: unknown, _ui: unknown, text: unknown) => {
        const ta = screen.getByRole('textbox') as HTMLTextAreaElement
        ta.value = String(text) // mutates the field, dispatches nothing
        return true
      }),
    )

    renderWithProviders(
      <ChatInput value={ORIGINAL} onChange={onChange} onSend={vi.fn()} connected={true} />,
    )
    clickOptimize()

    await vi.waitFor(() => expect(onChange).toHaveBeenCalledWith(OPTIMIZED))
  })

  it('restores the trimmed prompt when the optimizer request fails', async () => {
    // Same silent-loss shape on the error path: onError writes the original
    // back through setTextUndoable, and it writes the TRIMMED prompt — the one
    // that was actually sent — so this is a real change to reconcile, not a
    // no-op. (When the draft needs no trimming the DOM already holds the target
    // text and the read-back correctly reports nothing to do.)
    const onChange = vi.fn()
    vi.stubGlobal(
      'fetch',
      vi.fn((url: string) => {
        if (typeof url === 'string' && url.includes('/api/optimizer/optimize')) {
          return Promise.resolve({ ok: false, json: async () => ({}) })
        }
        return Promise.resolve({ ok: true, json: async () => [] })
      }),
    )
    setExecCommand(vi.fn(() => true)) // claims success, leaves the field alone
    const warn = vi.spyOn(console, 'warn').mockImplementation(() => {})

    renderWithProviders(
      <ChatInput value={`${ORIGINAL}   `} onChange={onChange} onSend={vi.fn()} connected={true} />,
    )
    clickOptimize()

    await vi.waitFor(() => expect(onChange).toHaveBeenCalledWith(ORIGINAL))
    warn.mockRestore()
  })
})
