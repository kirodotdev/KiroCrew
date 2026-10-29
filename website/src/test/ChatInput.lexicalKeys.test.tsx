import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { act, screen, fireEvent, waitFor } from '@testing-library/react'
import { useState } from 'react'
import { renderWithProviders } from './helpers'
import ChatInput from '../components/ChatInput'
import { noteEscapeStoppedSpeech } from '../utils/speechStopKey'

// The Lexical composer sits behind ChatInput's React.lazy boundary
// (components/chat-input/engine.tsx). A cold chunk import on a loaded CI
// shard can outlast findBy*'s 1 s default, so every wait for the composer
// to mount names this timeout.
const LAZY_COMPOSER_MOUNT = { timeout: 5000 }

/**
 * Keyboard chords on the Lexical composer must match the textarea's.
 *
 * Style Markdown While Typing moves users onto the Lexical composer, so
 * whatever the textarea's `useComposerKeyDown` binds has to work there too:
 * Cmd/Ctrl+Shift+Enter optimizes the prompt, and Cmd/Ctrl+Enter while a turn
 * runs performs the OTHER busy action (steer <-> queue) for that one send.
 */

const ORIGINAL = 'my prompt'
const OPTIMIZED = 'a much better prompt'

function Host(props: Partial<React.ComponentProps<typeof ChatInput>>) {
  const [value, setValue] = useState(ORIGINAL)
  return (
    <>
      <ChatInput value={value} onChange={setValue} onSend={vi.fn()} connected={true} lexicalComposer {...props} />
      <output data-testid="value">{value}</output>
    </>
  )
}

const shown = () => screen.getByTestId('value').textContent
const optimizeCalls = () => (fetch as unknown as { mock: { calls: unknown[][] } }).mock.calls
  .filter(([url]) => typeof url === 'string' && url.includes('/api/optimizer/optimize')).length

describe('ChatInput Lexical composer: keyboard chords match the textarea', () => {
  beforeEach(() => {
    vi.stubGlobal('fetch', vi.fn((url: string) => {
      if (typeof url === 'string' && url.includes('/api/optimizer/optimize')) {
        return Promise.resolve({ ok: true, json: async () => ({ changed: true, optimized: OPTIMIZED }) })
      }
      return Promise.resolve({ ok: true, json: async () => [] })
    }))
  })
  afterEach(() => { vi.unstubAllGlobals() })

  it('Ctrl+Shift+Enter optimizes the prompt instead of sending it', async () => {
    const onSend = vi.fn()
    renderWithProviders(<Host onSend={onSend} />)
    const input = await screen.findByRole('textbox', undefined, LAZY_COMPOSER_MOUNT)
    expect(input).toHaveAttribute('data-lexical-composer')
    // Flush the useEffect that registers the Lexical key-down handler so it is
    // installed before the synthetic event fires.
    await act(async () => {})
    fireEvent.keyDown(input, { key: 'Enter', code: 'Enter', ctrlKey: true, shiftKey: true })
    // The chord starts the optimize request, and the send path is never taken.
    // The generous timeouts cover a loaded CI coverage shard, where the mocked
    // request and the controlled re-render can outlast waitFor's 1 s default.
    await waitFor(() => expect(optimizeCalls()).toBe(1), { timeout: 5000 })
    await waitFor(() => expect(shown()).toBe(OPTIMIZED), { timeout: 5000 })
    expect(onSend).not.toHaveBeenCalled()
  })

  it('Ctrl+Enter while running in steer mode queues (onSend) for that one send', async () => {
    const onSend = vi.fn()
    const onSteer = vi.fn()
    renderWithProviders(<Host onSend={onSend} onSteer={onSteer} isRunning canSteer onStop={vi.fn()} />)
    const input = await screen.findByRole('textbox', undefined, LAZY_COMPOSER_MOUNT)
    await act(async () => {})
    fireEvent.keyDown(input, { key: 'Enter', code: 'Enter', ctrlKey: true })
    await waitFor(() => expect(onSend).toHaveBeenCalledTimes(1))
    expect(onSteer).not.toHaveBeenCalled()
    // One-shot: the next plain Enter is back to the split's own mode.
    fireEvent.keyDown(input, { key: 'Enter', code: 'Enter' })
    await waitFor(() => expect(onSteer).toHaveBeenCalledTimes(1))
  })

  it('a bare Escape while a turn runs stops it, as in the textarea (#22315)', async () => {
    const onStop = vi.fn()
    renderWithProviders(<Host isRunning onStop={onStop} />)
    const input = await screen.findByRole('textbox', undefined, LAZY_COMPOSER_MOUNT)
    expect(input).toHaveAttribute('data-lexical-composer')
    await act(async () => {})
    fireEvent.keyDown(input, { key: 'Escape', code: 'Escape' })
    await waitFor(() => expect(onStop).toHaveBeenCalledTimes(1))
    // A modified Escape is not the binding.
    fireEvent.keyDown(input, { key: 'Escape', code: 'Escape', shiftKey: true })
    expect(onStop).toHaveBeenCalledTimes(1)
  })

  it('an Escape that stopped voice read-back leaves the turn running', async () => {
    // Stand-in for the capture-phase stop-speaking shortcut, which marks the
    // keydown it spent before the editor sees it.
    const markSpeech = (e: Event) => { if ((e as KeyboardEvent).key === 'Escape') noteEscapeStoppedSpeech(e) }
    document.addEventListener('keydown', markSpeech, true)
    try {
      const onStop = vi.fn()
      renderWithProviders(<Host isRunning onStop={onStop} />)
      const input = await screen.findByRole('textbox', undefined, LAZY_COMPOSER_MOUNT)
      fireEvent.keyDown(input, { key: 'Escape', code: 'Escape' })
      expect(onStop).not.toHaveBeenCalled()
    } finally {
      document.removeEventListener('keydown', markSpeech, true)
    }
  })

  it('Escape leaves the turn alone when none is running, and during IME composition', async () => {
    const onStop = vi.fn()
    const { rerender } = renderWithProviders(<Host onStop={onStop} />)
    const input = await screen.findByRole('textbox', undefined, LAZY_COMPOSER_MOUNT)
    fireEvent.keyDown(input, { key: 'Escape', code: 'Escape' })
    expect(onStop).not.toHaveBeenCalled()
    rerender(<Host isRunning onStop={onStop} />)
    // keyCode 229 is the WebKit post-composition keydown the latch catches.
    fireEvent.keyDown(input, { key: 'Escape', code: 'Escape', keyCode: 229 })
    expect(onStop).not.toHaveBeenCalled()
  })
})
