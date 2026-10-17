import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { screen, waitFor } from '@testing-library/react'
import type { RootState } from '../store'
import { renderWithProviders, createTestStore } from './helpers'
import dashboardReducer from '../store/dashboardSlice'
import chatReducer from '../store/chatSlice'

// SideChat pulls the api client — stub the side-* calls it may touch. The
// two submit calls are STABLE fns (the Proxy mints a fresh fn per access for
// everything else) so a test can script a rejection and assert the call.
const sideTurn = vi.fn(() => Promise.resolve({}))
const sideOpen = vi.fn(() => Promise.resolve({}))
vi.mock('../api/client', () => ({
  api: new Proxy({}, {
    get: (_t, prop) => {
      const fn = prop === 'sideTurn'
        ? sideTurn
        : prop === 'sideOpen'
          ? sideOpen
          : vi.fn().mockResolvedValue({})
      Object.defineProperty(_t, prop, { value: fn, writable: true, configurable: true })
      return fn
    },
  }),
  SEARCH_MIN_CHARS: 2,
}))

// Spy on the seed consumer: SideChat calls it right after deciding whether to
// focus, so waiting on it waits for the late-focus decision, not a guessed delay.
const consumeSeed = vi.hoisted(() => vi.fn())
vi.mock('../chat-core/composer/sideChatDrafts', async importOriginal => {
  const actual = await importOriginal<typeof import('../chat-core/composer/sideChatDrafts')>()
  return {
    ...actual,
    consumeSideChatSeed: (slot: string) => { consumeSeed(slot); actual.consumeSideChatSeed(slot) },
  }
})

import SideChat from '../pages/chat/SideChat'
import { seedSideChatDraft } from '../chat-core/composer/sideChatDrafts'
import { loadChatConfig, saveChatConfig } from '../pages/chat/ChatSettings'

// The Lexical composer sits behind ChatInput's React.lazy boundary
// (components/chat-input/engine.tsx). A cold chunk import on a loaded CI
// shard can outlast findBy*'s 1 s default, so every wait for the composer
// to mount names this timeout.
const LAZY_COMPOSER_MOUNT = { timeout: 5000 }

// The composer blocks sends while the gateway reads as offline, so scenes run
// against a connected dashboard.
const dashInitial = { ...dashboardReducer(undefined, { type: '@@INIT' }), connected: true }
const chatInitial = chatReducer(undefined, { type: '@@INIT' })


/**
 * Select-to-Ask with Style Markdown While Typing on, when the lazy editor chunk
 * resolves late. A user who clicks into another field in the meantime keeps it:
 * the late caret nudge must not pull focus back into the side chat. Its own file
 * so the editor chunk is not already loaded by an earlier test, which would take
 * the first-frame path instead of the late one this locks.
 */
describe('SideChat seed focus when the Lexical composer loads late', () => {
  beforeEach(() => {
    saveChatConfig({ ...loadChatConfig(), inlineMarkdown: true })
  })
  afterEach(() => {
    localStorage.removeItem('mc-chat-config')
  })

  it('leaves focus alone when the user moved to another field before the editor loaded', async () => {
    const SLOT = 'seed-lexical-moved-slot'
    seedSideChatDraft(SLOT, 'asked, then typed elsewhere')
    const store = createTestStore({
      dashboard: dashInitial,
      chat: { ...chatInitial, activeSlot: SLOT, slotHistory: [SLOT], activityOpen: true, activityTab: 'side' } as unknown as RootState['chat'],
    })
    const elsewhere = document.createElement('input')
    document.body.appendChild(elsewhere)
    try {
      renderWithProviders(<SideChat slot={SLOT} />, { store })
      // The user clicks into another field while the lazy editor chunk loads.
      elsewhere.focus()
      const input = await screen.findByRole('textbox', { name: 'Ask a side question' }, LAZY_COMPOSER_MOUNT)
      expect(input).toHaveAttribute('data-lexical-composer')
      await waitFor(() => expect(consumeSeed).toHaveBeenCalledWith(SLOT))
      expect(document.activeElement).toBe(elsewhere)
    } finally {
      elsewhere.remove()
    }
  })
})
