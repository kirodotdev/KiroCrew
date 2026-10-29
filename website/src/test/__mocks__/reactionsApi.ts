/**
 * Stand-in for `api/reactions` in ChatPage-level tests that do not exercise
 * reactions: every call resolves empty, so the reactions GET succeeds.
 * Unmocked, its read fails and ChatPage's `reactions_load_failed` notice
 * renders an extra `role="alert"`, which breaks tests that count alerts or
 * look for the one notice they expect.
 *
 * Use: `vi.mock('../api/reactions', async () => await import('./__mocks__/reactionsApi'))`
 */
import { vi } from 'vitest'

const actual = await vi.importActual<typeof import('../../api/reactions')>('../../api/reactions')

export const REACTION_EMOJI = actual.REACTION_EMOJI

export const reactionsApi = {
  list: vi.fn().mockResolvedValue({ reactions: [] }),
  create: vi.fn().mockResolvedValue({}),
  remove: vi.fn().mockResolvedValue({ ok: true }),
}
