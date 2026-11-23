/**
 * A Side Chat answer draws its lock tag and its Blocked link card's Allow (#37380).
 *
 * The gateway stores a side answer with the credential records its placeholders
 * need and sends them as `redactions` on the final `chat.side_result` frame and in
 * `GET .../side`, beside `link_blocks`. The panel hands both to the shared bubble
 * in `meta`, on the local-slot rule the chat page uses, so the owner can reveal the
 * removed value and the reader can allow the link's host. After an Allow or Undo
 * on this slot the panel reads the conversation again, so the link shows.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { act, cleanup, fireEvent, screen, waitFor } from '@testing-library/react'
import { sseSideResult } from '../store/chatSlice'
import type { SideSnapshot } from '../types'
import { api } from '../api/client'
import SideChat from '../pages/chat/SideChat'
import { renderWithProviders, createConnectedTestStore } from './helpers'

vi.mock('../hooks/useSmoothStream', () => ({ useSmoothStream: (content: string) => content }))
vi.mock('../api/client', () => ({
  api: {
    sideState: vi.fn(),
    sideTools: vi.fn().mockResolvedValue({ read_only_tools: true, claude_adapter_outdated: false, config_unavailable: false }),
    sideOpen: vi.fn().mockResolvedValue({ ok: true, open: true, messages: 0, last_run_id: '', created_at: '' }),
    sideTurn: vi.fn().mockResolvedValue({ ok: true, run_id: 'r9', messages: 1 }),
    sideClose: vi.fn().mockResolvedValue({ ok: true, was_open: true }),
  },
}))

const SLOT = 'slot-1'
const HOST = 'collect.example.com'
const URL = `https://${HOST}/x?d=` + 'A'.repeat(250)
const CRED_PH = '[REDACTED: credential]'
const ANSWER = `See ${URL} and use the token ${CRED_PH} for the upload.`
const LINK_RECORD = { url: URL, domain: HOST, rule: 'exfil_query_length', query_chars: 252, withheld: null }
const CRED_RECORD = {
  ordinal: 0, rule: 'github_token', label: '', source: null, view_command: null, profile_command: null,
  reveal_id: 'rv_' + 'a'.repeat(22),
}

function snapshot(answer: Record<string, unknown> = {}): SideSnapshot {
  return {
    open: true,
    created_at: '2026-11-16T06:00:00.000Z',
    last_run_id: 'r1',
    running: false,
    messages: [
      { id: 'u1', run_id: 'r1', role: 'user', content: 'where?', ts: '2026-11-16T06:00:01.000Z' },
      { id: 'a1', run_id: 'r1', role: 'assistant', content: ANSWER, ts: '2026-11-16T06:00:02.000Z', ...answer },
    ],
    queue: [],
  }
}

/** The read once the host is allowed: the same text, with no record for its link. */
const allowedSnapshot = () => snapshot({ redactions: [CRED_RECORD] })

/** The footer's tool note is the panel's last asynchronous update on mount. */
const footerSettled = async () => { expect(await screen.findByRole('note')).toBeInTheDocument() }

const hostsChanged = (slot: string) => act(() => {
  window.dispatchEvent(new CustomEvent('mc:redaction-hosts-changed', { detail: { slot, messageTs: '2026-11-16T06:00:02.000Z' } }))
})

beforeEach(() => {
  vi.mocked(api.sideState).mockReset()
})

afterEach(() => {
  cleanup()
})

describe('a Side Chat answer with credential records', () => {
  it('draws the lock tag from a final frame that repeats the streamed text', async () => {
    vi.mocked(api.sideState).mockResolvedValue({ open: false })
    const store = createConnectedTestStore()
    renderWithProviders(<SideChat slot={SLOT} />, { store })
    await footerSettled()

    act(() => {
      store.dispatch(sseSideResult({ slot: SLOT, run_id: 'r1', role: 'user', content: 'where?', message_id: 'u1' }))
      store.dispatch(sseSideResult({ slot: SLOT, run_id: 'r1', role: 'assistant', content: ANSWER }))
      store.dispatch(sseSideResult({
        slot: SLOT, run_id: 'r1', role: 'assistant', content: ANSWER, final: true, message_id: 'a1',
        link_blocks: [LINK_RECORD], redactions: [CRED_RECORD],
      }))
    })

    expect(await screen.findByTestId('credential-tag')).toBeInTheDocument()
    expect(screen.getByTestId('blocked-link-target').textContent).toBe(HOST)
    expect(document.body.textContent).not.toContain(CRED_PH)
  })

  it('draws the lock tag for an answer restored from the read', async () => {
    vi.mocked(api.sideState).mockResolvedValue(snapshot({ link_blocks: [LINK_RECORD], redactions: [CRED_RECORD] }))
    renderWithProviders(<SideChat slot={SLOT} />, { store: createConnectedTestStore() })

    expect(await screen.findByTestId('credential-tag')).toBeInTheDocument()
    expect(document.body.textContent).not.toContain(CRED_PH)
    await footerSettled()
  })

  it("offers the Blocked link card's Allow, the side routes being this gateway's own slot", async () => {
    vi.mocked(api.sideState).mockResolvedValue(snapshot({ link_blocks: [LINK_RECORD] }))
    renderWithProviders(<SideChat slot={SLOT} />, { store: createConnectedTestStore() })
    fireEvent.click(await screen.findByTestId('blocked-link-inspect'))

    expect(await screen.findByTestId('blocked-link-allow')).toBeInTheDocument()
    await footerSettled()
  })

  it('draws an answer without records as before', async () => {
    vi.mocked(api.sideState).mockResolvedValue(snapshot())
    renderWithProviders(<SideChat slot={SLOT} />, { store: createConnectedTestStore() })

    expect(await screen.findByText(CRED_PH, { exact: false })).toBeInTheDocument()
    expect(screen.queryByTestId('credential-tag')).toBeNull()
    expect(screen.queryByTestId('blocked-link-target')).toBeNull()
    await footerSettled()
  })
})

describe('a streaming Side Chat answer', () => {
  it('keeps its local image URL while deltas with new stamps arrive', async () => {
    vi.mocked(api.sideState).mockResolvedValue({ open: false })
    const store = createConnectedTestStore()
    renderWithProviders(<SideChat slot={SLOT} />, { store })
    await footerSettled()
    const at = (iso: string) => Date.parse(iso) / 1000
    const imageSrc = () => document.querySelector('img[src^="/api/file-raw"]')?.getAttribute('src')

    act(() => {
      store.dispatch(sseSideResult({ slot: SLOT, run_id: 'r1', role: 'user', content: 'show it', ts: at('2026-11-16T06:00:01.000Z'), message_id: 'u1' }))
      store.dispatch(sseSideResult({ slot: SLOT, run_id: 'r1', role: 'assistant', content: '![shot](/Users/me/work/shot.png)\n\nHere it is', ts: at('2026-11-16T06:00:02.000Z') }))
    })
    await waitFor(() => expect(imageSrc()).toBeTruthy())
    const first = imageSrc()

    act(() => {
      store.dispatch(sseSideResult({ slot: SLOT, run_id: 'r1', role: 'assistant', content: ' and more.', ts: at('2026-11-16T06:00:03.000Z') }))
    })
    await waitFor(() => expect(document.body.textContent).toContain('and more.'))
    expect(imageSrc()).toBe(first)
  })
})

describe("after an Allow or Undo on a Blocked link card", () => {
  it('reads its own slot again and shows the answer as the gateway now serves it', async () => {
    vi.mocked(api.sideState).mockResolvedValueOnce(snapshot({ link_blocks: [LINK_RECORD], redactions: [CRED_RECORD] }))
    renderWithProviders(<SideChat slot={SLOT} />, { store: createConnectedTestStore() })
    await screen.findByTestId('blocked-link-target')
    await footerSettled()
    expect(api.sideState).toHaveBeenCalledTimes(1)

    vi.mocked(api.sideState).mockResolvedValueOnce(allowedSnapshot())
    hostsChanged(SLOT)

    await waitFor(() => expect(screen.queryByTestId('blocked-link-target')).toBeNull())
    expect(api.sideState).toHaveBeenCalledTimes(2)
    expect(api.sideState).toHaveBeenLastCalledWith(SLOT)
    expect(document.querySelector(`a[href="${URL}"]`)).toBeTruthy()
    expect(screen.getByTestId('credential-tag')).toBeInTheDocument()
  })

  it('reads nothing for a change on another slot', async () => {
    vi.mocked(api.sideState)
      .mockResolvedValueOnce(snapshot({ link_blocks: [LINK_RECORD] }))
      .mockResolvedValue(allowedSnapshot())
    renderWithProviders(<SideChat slot={SLOT} />, { store: createConnectedTestStore() })
    await screen.findByTestId('blocked-link-target')
    await footerSettled()
    expect(api.sideState).toHaveBeenCalledTimes(1)

    // Both reads would be sent before either lands, so once the link shows the count is final.
    hostsChanged('slot-2')
    hostsChanged(SLOT)

    await waitFor(() => expect(screen.queryByTestId('blocked-link-target')).toBeNull())
    // The mount's read and this slot's change only.
    expect(api.sideState).toHaveBeenCalledTimes(2)
  })
})
