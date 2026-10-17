import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { screen, waitFor } from '@testing-library/react'
import { useState } from 'react'
import { renderWithProviders } from './helpers'
import ChatInput from '../components/ChatInput'

/**
 * The Lexical composer's keyboard chords are bound in the SAME commit that
 * puts the editor in the DOM, so a key that arrives the instant the textbox
 * exists already finds them (#18193).
 *
 * The composer sits behind ChatInput's React.lazy boundary
 * (components/chat-input/engine.tsx). On the cold first mount React commits
 * the resolved chunk in one scheduler task and runs PASSIVE effects in the
 * next; a chord handler registered in a passive effect leaves a gap between
 * the two in which Enter reaches only Lexical's default handler and inserts a
 * line break instead of optimizing or sending. Testing Library's findBy*
 * resolves inside that gap on a loaded shard, which is how
 * ChatInput.lexicalKeys.test.tsx went red on CI with its diff untouched.
 *
 * This file holds ONE test on purpose: the cold mount exists only for the
 * first render of the lazy boundary in a worker, and vitest isolates module
 * state per file, so a file of its own is what makes the condition
 * independent of test order.
 */

const ORIGINAL = 'my prompt'
const OPTIMIZED = 'a much better prompt'
// Named like the sibling file's wait: a cold chunk import on a loaded shard
// outlasts waitFor's 1 s default.
const LAZY_COMPOSER_MOUNT = { timeout: 5000 }

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

describe('ChatInput Lexical composer: chords are bound when the editor mounts', () => {
  beforeEach(() => {
    vi.stubGlobal('fetch', vi.fn((url: string) => {
      if (typeof url === 'string' && url.includes('/api/optimizer/optimize')) {
        return Promise.resolve({ ok: true, json: async () => ({ changed: true, optimized: OPTIMIZED }) })
      }
      return Promise.resolve({ ok: true, json: async () => [] })
    }))
  })
  afterEach(() => { vi.unstubAllGlobals() })

  it('Ctrl+Shift+Enter sent the instant the textbox appears optimizes (cold chunk)', async () => {
    const onSend = vi.fn()
    renderWithProviders(<Host onSend={onSend} />)
    // The condition under test: the chunk is still loading, so this render is
    // the cold mount. Fails by name if the boundary ever stops being lazy.
    expect(document.querySelector('[data-lexical-composer]')).toBeNull()
    // Send the chord from the MutationObserver callback -- a microtask after
    // the commit that inserted the editor, before any later scheduler task --
    // as a raw event rather than fireEvent: the observer fires inside
    // waitFor's non-act window, where fireEvent's act() would warn.
    let sentAtMount = false
    const seen = new MutationObserver(() => {
      const input = document.querySelector('[data-lexical-composer]')
      if (!input || sentAtMount) return
      sentAtMount = true
      seen.disconnect()
      input.dispatchEvent(new KeyboardEvent('keydown', {
        key: 'Enter', code: 'Enter', ctrlKey: true, shiftKey: true, bubbles: true, cancelable: true,
      }))
    })
    seen.observe(document.body, { childList: true, subtree: true })
    try {
      await waitFor(() => expect(optimizeCalls()).toBe(1), LAZY_COMPOSER_MOUNT)
    } finally {
      seen.disconnect()
    }
    expect(sentAtMount).toBe(true)
    expect(onSend).not.toHaveBeenCalled()
    await waitFor(() => expect(shown()).toBe(OPTIMIZED), LAZY_COMPOSER_MOUNT)
  })
})
