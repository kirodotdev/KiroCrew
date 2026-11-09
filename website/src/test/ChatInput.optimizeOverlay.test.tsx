import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { screen, fireEvent } from '@testing-library/react'
import { renderWithProviders } from './helpers'
import ChatInput from '../components/ChatInput'

/**
 * While "Optimize prompt" runs, the overlay's pill must show that work is in
 * progress. The frosted scrim blurs the toolbar's own Optimize button, so the
 * pill carries the same spinner that button shows while busy; a still icon
 * reads as a frozen composer.
 */

const hangingOptimizer = () =>
  vi.fn((url: string) => {
    if (typeof url === 'string' && url.includes('/api/optimizer/optimize')) {
      return new Promise(() => { /* stays in flight */ })
    }
    return Promise.resolve({ ok: true, json: async () => [] })
  })

describe('ChatInput optimize: the in-flight overlay shows progress', () => {
  beforeEach(() => {
    vi.stubGlobal('requestAnimationFrame', (cb: FrameRequestCallback) => {
      cb(0)
      return 0
    })
  })
  afterEach(() => {
    vi.unstubAllGlobals()
  })

  it('spins a decorative loader beside the label while the optimize runs', async () => {
    vi.stubGlobal('fetch', hangingOptimizer())
    renderWithProviders(
      <ChatInput value="my prompt" onChange={vi.fn()} onSend={vi.fn()} connected={true} />,
    )
    fireEvent.click(screen.getByRole('button', { name: 'Optimize prompt' }))

    const label = await screen.findByText(/Optimizing prompt/)
    const pill = label.closest('.optimize-overlay-pill')
    expect(pill).not.toBeNull()
    // The pill's first child is its status icon; Cancel's X is inside the button.
    const icon = pill!.querySelector(':scope > svg')
    expect(icon).not.toBeNull()
    expect(icon!.classList.contains('animate-spin')).toBe(true)
    expect(icon!.getAttribute('aria-hidden')).toBe('true')
  })
})
