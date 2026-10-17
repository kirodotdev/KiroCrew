import type { Mock } from 'vitest'
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { fireEvent, screen, waitFor } from '@testing-library/react'
import type { RootState } from '../store'
import { __resetPathKindCache } from '../hooks/usePathKind'
import { stubPathKinds } from './pathKindStub'
import { renderWithProviders, createTestStore } from './helpers'

/* A side reply names files and artifacts the way a main reply does, and the
 * side transcript is drawn by the same `ChatMessageList`. Until SideChat handed
 * the host's openers down, a path chip in a side reply fell back to
 * `MarkdownRenderer`'s OS reveal (a clipboard copy on a remote dashboard) and an
 * artifact link navigated away from the chat, while the same chip and link in
 * the main transcript open a tab of the panel the side chat sits in. */

vi.mock('../api/client', () => ({
  api: new Proxy({} as Record<PropertyKey, unknown>, {
    get: (_t, prop) => {
      if (Object.prototype.hasOwnProperty.call(_t, prop)) return _t[prop]
      const fn = vi.fn().mockResolvedValue(
        prop === 'sideOpen' ? { ok: true, open: true, messages: 0, last_run_id: '', created_at: '' }
          : prop === 'sideTurn' ? { ok: true, run_id: 'r1', messages: 1 }
            : prop === 'sideClose' ? { ok: true, was_open: true }
              : prop === 'chatSlotDetail' ? { messages: [], has_more: false, total: 0 }
                : prop === 'acpBackends' ? { backends: [{ id: '', capabilities: [{ id: 'side_chat_tools', available: true }] }] }
                  : prop === 'revealPath' ? { ok: true }
                    : {},
      )
      Object.defineProperty(_t, prop, { value: fn, writable: true, configurable: true })
      return fn
    },
  }),
  SEARCH_MIN_CHARS: 2,
}))

import SideChat from '../pages/chat/SideChat'
import { api } from '../api/client'

const SLOT = 'parent-slot-1'
const FILE = '/home/user/notes/a.md'

function storeWithReply(content: string) {
  return createTestStore({
    chat: {
      activeSlot: SLOT,
      messages: [],
      slotRunning: false,
      slotStopping: false,
      slotState: 'idle',
      slotStatusDetail: {},
      slotHasMore: false,
      slotOldestIndex: 0,
      loadingOlder: false,
      lastChunkSeq: undefined,
      _wsChunkedDuringFetch: false,
      history: [],
      historyHasMore: false,
      historyOffset: 0,
      pendingInput: null,
      slotContextPct: {},
      voicePlaying: false,
      voiceAudio: null,
      subagents: {},
      toolLog: [],
      activityOpen: true,
      activityTab: 'side',
      focusToolCallId: null,
      slotActivity: {},
      slotSide: {
        [SLOT]: {
          messages: [
            { role: 'user', content: 'where are the notes?', ts: '2026-05-19T10:00:00Z', run_id: 'r1' },
            { role: 'assistant', content, ts: '2026-05-19T10:00:01Z', run_id: 'r1' },
          ],
          lastRunId: 'r1',
        },
      },
      slotHistory: [SLOT],
      stopPressedAt: {},
    } as unknown as RootState['chat'],
  })
}

/** The confirmed-file chip for FILE, once the kind probe has answered. */
async function fileChip(container: HTMLElement): Promise<HTMLElement> {
  let chip: HTMLElement | null = null
  await waitFor(() => {
    chip = container.querySelector(`code[data-path="${FILE}"][data-path-kind="file"]`)
    expect(chip).not.toBeNull()
  })
  return chip!
}

describe('SideChat opens paths and artifacts through the host', () => {
  const realFetch = globalThis.fetch

  beforeEach(() => {
    __resetPathKindCache()
    stubPathKinds(() => 'file')
  })
  afterEach(() => {
    globalThis.fetch = realFetch
    vi.restoreAllMocks()
  })

  it('a path chip in a side reply opens the file through onFileOpen', async () => {
    const onFileOpen = vi.fn()
    const { container } = renderWithProviders(
      <SideChat slot={SLOT} onFileOpen={onFileOpen} />,
      { store: storeWithReply(`They are in \`${FILE}\`.`) },
    )
    fireEvent.click(await fileChip(container))
    expect(onFileOpen).toHaveBeenCalledWith(FILE)
    // The host opener took the click; nothing was handed to the OS.
    expect(api.revealPath as unknown as Mock).not.toHaveBeenCalled()
  })

  it('without a host opener the chip keeps its OS-reveal fallback', async () => {
    const { container } = renderWithProviders(
      <SideChat slot={SLOT} />,
      { store: storeWithReply(`They are in \`${FILE}\`.`) },
    )
    fireEvent.click(await fileChip(container))
    await waitFor(() => expect(api.revealPath as unknown as Mock).toHaveBeenCalledWith(FILE, 'reveal'))
  })

  it('an artifact link in a side reply opens the artifact through onArtifactOpen', async () => {
    const onArtifactOpen = vi.fn()
    renderWithProviders(
      <SideChat slot={SLOT} onArtifactOpen={onArtifactOpen} />,
      { store: storeWithReply('See [the report](/artifacts/weekly-report).') },
    )
    // The footer's backend read settles the panel; await it so no provider
    // update lands after the test's last barrier.
    await screen.findByRole('note')
    fireEvent.click(await screen.findByRole('link', { name: 'the report' }))
    await waitFor(() => expect(onArtifactOpen).toHaveBeenCalledWith('weekly-report'))
  })
})
