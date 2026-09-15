/**
 * The touch-device gate on the rich composer (Design Review on #11100).
 *
 * Every chat host mounts `<ChatInput lexicalComposer={!touchDevice}>`: on a
 * coarse-pointer device the classic `<textarea>` stays until a device pass has
 * shown that soft-keyboard composition (IME latch, Enter-to-send) and the
 * pointer-only pill reorder hold up on the Lexical composer. SideChat is the
 * smallest host, so it carries the behavioural proof; the source scan in
 * `chatInputHosts.lexicalComposer.test.ts` pins the same gate at all three
 * mounts.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { waitFor } from '@testing-library/react'
import reducer from '../store/chatSlice'
import dashboardReducer from '../store/dashboardSlice'
import { renderWithProviders, createTestStore } from './helpers'

const touchEnv = { touch: false }
// `useIsTouchDevice` reads its snapshot through this predicate, so mocking the
// util drives the hook the hosts call.
vi.mock('../utils/isTouchDevice', () => ({ isTouchDevice: () => touchEnv.touch }))

vi.mock('../api/client', () => ({
  api: {
    sideOpen: vi.fn().mockResolvedValue({ ok: true, open: true, messages: 0, last_run_id: '', created_at: new Date().toISOString() }),
    sideTurn: vi.fn().mockResolvedValue({ ok: true, run_id: 'r1', messages: 1 }),
    sideClose: vi.fn().mockResolvedValue({ ok: true, was_open: true }),
    sideQueueCancel: vi.fn().mockResolvedValue({ ok: true, content: '', depth: 0 }),
    sideQueueEdit: vi.fn().mockResolvedValue({ ok: true, depth: 1 }),
  },
  SEARCH_MIN_CHARS: 2,
}))

import SideChat from '../pages/chat/SideChat'

const SLOT = 'touch-guard-slot'

function store() {
  return createTestStore({
    dashboard: { ...dashboardReducer(undefined, { type: '@@INIT' }), connected: true },
    chat: { ...reducer(undefined, { type: '@@INIT' }), activeSlot: SLOT },
  })
}

async function composerElement(): Promise<HTMLElement> {
  let el: HTMLElement | null = null
  await waitFor(() => {
    el = document.querySelector('[data-composer-input]')
    expect(el).not.toBeNull()
  })
  return el!
}

describe('chat hosts — rich composer is gated on the touch-device signal', () => {
  beforeEach(() => {
    localStorage.clear()
  })

  it('a fine-pointer device gets the Lexical composer', async () => {
    touchEnv.touch = false
    renderWithProviders(<SideChat slot={SLOT} />, { store: store() })
    const el = await composerElement()
    expect(el.getAttribute('contenteditable')).toBe('true')
    expect(el.tagName).not.toBe('TEXTAREA')
  })

  it('a coarse-pointer (touch) device keeps the classic textarea', async () => {
    touchEnv.touch = true
    renderWithProviders(<SideChat slot={SLOT} />, { store: store() })
    const el = await composerElement()
    expect(el.tagName).toBe('TEXTAREA')
    expect(document.querySelector('[contenteditable="true"]')).toBeNull()
  })
})
