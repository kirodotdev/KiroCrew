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
 * Select-to-Ask with Style Markdown While Typing on: the side chat's composer
 * is the lazily loaded Lexical editor, so it is not in the DOM on the first
 * frame after a seed. The caret nudge must wait for it, not consume the seed
 * against an empty wrapper and leave the quote unfocused.
 */
describe('SideChat seed focus with the Lexical composer', () => {
  beforeEach(() => {
    saveChatConfig({ ...loadChatConfig(), inlineMarkdown: true })
  })
  afterEach(() => {
    localStorage.removeItem('mc-chat-config')
  })

  it('focuses the Lexical composer once it mounts, instead of giving up on the first frame', async () => {
    const SLOT = 'seed-lexical-slot'
    seedSideChatDraft(SLOT, 'asked before the editor loaded')
    const store = createTestStore({
      dashboard: dashInitial,
      chat: { ...chatInitial, activeSlot: SLOT, slotHistory: [SLOT], activityOpen: true, activityTab: 'side' } as unknown as RootState['chat'],
    })
    renderWithProviders(<SideChat slot={SLOT} />, { store })
    const input = await screen.findByRole('textbox', { name: 'Ask a side question' }, LAZY_COMPOSER_MOUNT)
    expect(input).toHaveAttribute('data-lexical-composer')
    await waitFor(() => expect(document.activeElement).toBe(input))
  })
})
