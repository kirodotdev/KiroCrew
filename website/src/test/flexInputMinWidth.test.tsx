// Regression tests for issue #3789: three flex-1 inputs without min-w-0.
//
// A flex item's default `min-width: auto` resolves to the input's intrinsic
// width (~20ch), so under space pressure the input refuses to shrink and the
// row overflows instead. Inside an `overflow-hidden` container the overflow is
// silently clipped, cutting off the control that trails the input (same defect
// class as the command palette close button, PR #3663). jsdom performs no real
// layout, so these tests pin the class contract; the real narrow-width
// measurement lives in scripts/capture-flex-input-min-w-0.mjs (real browser).
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, screen, waitFor } from '@testing-library/react'
import React from 'react'
import { MemoryRouter } from 'react-router-dom'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { Input } from '../components/ui'
import SessionArchive from '../pages/SessionArchive'

const mockGet = vi.fn()
const mockPost = vi.fn()

vi.mock('../app-sdk/index', () => ({
  useAppApi: () => ({ get: mockGet, post: mockPost }),
}))

vi.mock('../app-sdk/ChatMessageList', () => ({
  default: () => <div data-testid="chat-message-list" />,
}))

import ChatEmbed from '../app-sdk/ChatEmbed'

function renderWithClient(ui: React.ReactElement) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <QueryClientProvider client={qc}>
      <MemoryRouter>{ui}</MemoryRouter>
    </QueryClientProvider>,
  )
}

beforeEach(() => {
  vi.clearAllMocks()
  Element.prototype.scrollIntoView = vi.fn()
  mockGet.mockResolvedValue({ messages: [], running: false, title: '' })
  mockPost.mockResolvedValue({})
})

afterEach(() => {
  vi.unstubAllGlobals()
  vi.restoreAllMocks()
})

describe('flex-1 inputs shrink instead of clipping trailing controls (#3789)', () => {
  it('shared Input carries min-w-0 alongside flex-1 in its base class', () => {
    render(<Input placeholder="p" />)
    const input = screen.getByPlaceholderText('p')
    expect(input.className).toContain('flex-1')
    expect(input.className).toContain('min-w-0')
  })

  it('shared Input still lets a consumer override the minimum via className', () => {
    render(<Input placeholder="p" className="min-w-[8rem]" />)
    const input = screen.getByPlaceholderText('p')
    // twMerge resolves the conflict in favour of the consumer's class.
    expect(input.className).toContain('min-w-[8rem]')
    expect(input.className).not.toMatch(/\bmin-w-0\b/)
  })

  it('SessionArchive fuzzy-filter input carries min-w-0 so Reload is never clipped', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => ({
      ok: true,
      status: 200,
      json: async () => ({ archives: [] }),
    })) as unknown as typeof fetch)
    renderWithClient(<SessionArchive />)
    const input = await screen.findByLabelText('Fuzzy filter (substring match)')
    expect(input.className).toContain('flex-1')
    expect(input.className).toContain('min-w-0')
  })

  it('ChatEmbed composer input carries min-w-0 so the send button is never clipped', async () => {
    renderWithClient(<ChatEmbed slotKey="slot-1" />)
    await waitFor(() => expect(screen.getByLabelText('Chat message')).toBeInTheDocument())
    const input = screen.getByLabelText('Chat message')
    expect(input.className).toContain('flex-1')
    expect(input.className).toContain('min-w-0')
  })
})
