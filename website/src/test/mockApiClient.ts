import { vi } from 'vitest'

/** Shared vi.mock for api/client — import this file to apply the mock */
vi.mock('../api/client', () => ({
  api: {
    sessions: vi.fn(),
    chatSlotDetail: vi.fn(),
    createChatSlot: vi.fn(),
    deleteChatSlot: vi.fn(),
    resumeChatSlot: vi.fn(),
    deleteSession: vi.fn(),
    appContributors: vi.fn(() => Promise.resolve({ contributors: [] })),
  },
}))
