import { describe, it, expect, vi, beforeEach } from 'vitest'
import { act, screen } from '@testing-library/react'
import { useRef } from 'react'
import type { RootState } from '../store'
import { renderWithProviders, createTestStore } from './helpers'
import SelectionToolbar, { useSelectionActions } from '../components/SelectionToolbar'

// SideChat pulls the api client — stub the side-* calls it may touch.
vi.mock('../api/client', () => ({
  api: new Proxy({}, {
    get: (_t, prop) => {
      const fn = vi.fn().mockResolvedValue({})
      Object.defineProperty(_t, prop, { value: fn, writable: true, configurable: true })
      return fn
    },
  }),
  SEARCH_MIN_CHARS: 2,
}))

import SideChat from '../pages/chat/SideChat'

// Harness that mounts the toolbar from an external selection so the actions
// render deterministically without simulating a real DOM range.
function ToolbarHarness({ onAsk }: { onAsk?: (t: string, r: DOMRect) => void }) {
  const ref = useRef<HTMLDivElement>(null)
  const actions = useSelectionActions(undefined, onAsk)
  return (
    <div ref={ref}>
      <SelectionToolbar containerRef={ref} actions={actions} externalSelection={{ text: 'hi', x: 10, y: 10 }} />
    </div>
  )
}

describe('Select-to-Ask', () => {
  beforeEach(() => { vi.restoreAllMocks() })

  it('useSelectionActions exposes an Ask action only when onAsk is provided', () => {
    renderWithProviders(<ToolbarHarness onAsk={() => {}} />)
    expect(screen.getByRole('button', { name: 'Ask in Side' })).toBeInTheDocument()
  })

  it('omits the Ask action when onAsk is absent', () => {
    renderWithProviders(<ToolbarHarness />)
    expect(screen.queryByRole('button', { name: 'Ask in Side' })).not.toBeInTheDocument()
  })

  it('clicking Ask invokes the handler with the selected text', () => {
    const onAsk = vi.fn()
    renderWithProviders(<ToolbarHarness onAsk={onAsk} />)
    act(() => { screen.getByRole('button', { name: 'Ask in Side' }).click() })
    expect(onAsk).toHaveBeenCalledWith('hi', expect.anything())
  })

  it('SideChat seeds the draft as a grounding quote on the side-seed event', () => {
    const SLOT = 'seed-slot'
    const store = createTestStore({
      chat: {
        activeSlot: SLOT,
        messages: [],
        slotSide: {},
        slotHistory: [SLOT],
        activityOpen: true,
        activityTab: 'side',
      } as unknown as RootState['chat'],
    })
    renderWithProviders(<SideChat slot={SLOT} />, { store })
    const ta = screen.getByLabelText('Ask a side question') as HTMLTextAreaElement
    expect(ta.value).toBe('')
    act(() => {
      window.dispatchEvent(new CustomEvent('side-seed', { detail: { text: 'line one\nline two' } }))
    })
    expect(ta.value).toBe('> line one\n> line two\n\n')
  })
})
