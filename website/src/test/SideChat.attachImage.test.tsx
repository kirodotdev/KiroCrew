/**
 * A Side Chat question can carry an image.
 *
 * The side composer is the shared `ChatInput`, which shows its attach control,
 * Sketch and the paste-upload path only when the host hands it `onUploadFiles`.
 * These cases drive the real panel: the attach control is there, a picked
 * or pasted image is uploaded and staged as a chip, and the `/side/turn`
 * question carries its `![image](path)` line, which the side turn's prompt
 * builder attaches as an image block. The side upload holds only the side
 * composer's Send, never the main composer's of the same slot.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { screen, waitFor, fireEvent } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import reducer from '../store/chatSlice'
import { clearSideChatDrafts, readSideChatDraft, useSideChatDraft } from '../chat-core/composer/sideChatDrafts'
import { __resetComposerSendHoldsForTests, isComposerSendHeld } from '../utils/composerSendHolds'
import { renderWithProviders, createTestStore } from './helpers'
import dashboardReducer from '../store/dashboardSlice'
import { ApiError } from '../api/apiError'

vi.mock('../api/client', () => ({
  api: {
    sideState: vi.fn().mockResolvedValue({ open: false }),
    sideOpen: vi.fn().mockResolvedValue({ ok: true, open: true, messages: 0, last_run_id: '', created_at: '2026-01-01T00:00:00.000Z' }),
    sideTurn: vi.fn().mockResolvedValue({ ok: true, run_id: 'r1', messages: 1 }),
    sideClose: vi.fn().mockResolvedValue({ ok: true, was_open: true }),
    sideTools: vi.fn().mockResolvedValue({ read_only_tools: true, claude_adapter_outdated: false, config_unavailable: false }),
    dashboardConfig: vi.fn().mockResolvedValue({ upload_max_mb: 25 }),
    uploadFiles: vi.fn(),
  },
  SEARCH_MIN_CHARS: 2,
}))

import SideChat from '../pages/chat/SideChat'
import { sideUploadKey } from '../pages/chat/useSideChatUploads'
import { api } from '../api/client'

const dashInitial = { ...dashboardReducer(undefined, { type: '@@INIT' }), connected: true }
const SLOT = 'side-attach-slot'
const UPLOADED = '/home/user/.kiro/crew/uploads/abc_red.png'
const initial = reducer(undefined, { type: '@@INIT' })

function idleSide() {
  return createTestStore({
    dashboard: dashInitial,
    chat: {
      ...initial,
      activeSlot: SLOT,
      slotSide: { [SLOT]: { messages: [], lastRunId: '', pending: false, streaming: false, openedAtTurnCount: 0, createdAt: '2026-05-20T00:00:00Z' } },
    },
  })
}

const png = () => new File([new Uint8Array([0x89, 0x50, 0x4e, 0x47])], 'red.png', { type: 'image/png' })
const box = () => screen.getByLabelText('Ask a side question') as HTMLTextAreaElement
const fileInput = () => screen.getByLabelText('Attach files', { selector: 'input[type="file"]' })
const sideFiles = () => {
  let files: string[] = []
  function Probe() { files = useSideChatDraft(SLOT).files; return null }
  return { Probe, read: () => files }
}

beforeEach(() => {
  localStorage.clear()
  clearSideChatDrafts()
  __resetComposerSendHoldsForTests()
  vi.clearAllMocks()
  vi.mocked(api.uploadFiles).mockResolvedValue({ paths: [UPLOADED] })
})

describe('SideChat attachments', () => {
  it('offers the attach control in the side composer', async () => {
    renderWithProviders(<SideChat slot={SLOT} />, { store: idleSide() })
    expect(await screen.findByRole('button', { name: 'Add files & options' })).toBeInTheDocument()
  })

  it('a picked image is uploaded, staged, and sent as an image line of the side question', async () => {
    const user = userEvent.setup()
    const probe = sideFiles()
    renderWithProviders(<><SideChat slot={SLOT} /><probe.Probe /></>, { store: idleSide() })
    await user.type(box(), 'what colour is this?')
    fireEvent.change(fileInput(), { target: { files: [png()] } })
    await waitFor(() => expect(probe.read()).toEqual([UPLOADED]))
    expect(api.uploadFiles).toHaveBeenCalledTimes(1)
    expect(vi.mocked(api.uploadFiles).mock.calls[0][0].map(f => f.name)).toEqual(['red.png'])
    await user.click(screen.getByRole('button', { name: 'Send' }))
    await waitFor(() => expect(api.sideTurn).toHaveBeenCalledTimes(1))
    const [slot, question] = vi.mocked(api.sideTurn).mock.calls[0]
    expect(slot).toBe(SLOT)
    expect(question).toContain(`![image](${UPLOADED})`)
    expect(question).toContain('what colour is this?')
    // The submit consumed the staged image with the text.
    await waitFor(() => expect(probe.read()).toEqual([]))
    expect(readSideChatDraft(SLOT)).toBe('')
  })

  it('a pasted image takes the same upload path', async () => {
    const probe = sideFiles()
    renderWithProviders(<><SideChat slot={SLOT} /><probe.Probe /></>, { store: idleSide() })
    const file = png()
    fireEvent.paste(box(), {
      clipboardData: { items: [{ kind: 'file', type: 'image/png', getAsFile: () => file }], types: ['Files'], getData: () => '', files: [file] },
    })
    await waitFor(() => expect(probe.read()).toEqual([UPLOADED]))
    expect(api.uploadFiles).toHaveBeenCalledTimes(1)
  })

  it('an upload in flight holds only the side Send, and an image-only question sends once it lands', async () => {
    const user = userEvent.setup()
    let finish: (v: { paths: string[] }) => void = () => {}
    vi.mocked(api.uploadFiles).mockReturnValueOnce(new Promise(res => { finish = res }))
    const probe = sideFiles()
    renderWithProviders(<><SideChat slot={SLOT} /><probe.Probe /></>, { store: idleSide() })
    // Text in the box, so only the upload hold can disable Send.
    await user.type(box(), 'q')
    fireEvent.change(fileInput(), { target: { files: [png()] } })
    await waitFor(() => expect(api.uploadFiles).toHaveBeenCalledTimes(1))
    // The side Send waits; the main composer of the same slot is not held.
    expect(isComposerSendHeld(sideUploadKey(SLOT))).toBe(true)
    expect(screen.getByRole('button', { name: 'Send' })).toBeDisabled()
    expect(isComposerSendHeld(SLOT)).toBe(false)
    finish({ paths: [UPLOADED] })
    await waitFor(() => expect(probe.read()).toEqual([UPLOADED]))
    await user.clear(box())
    await user.click(screen.getByRole('button', { name: 'Send' }))
    await waitFor(() => expect(api.sideTurn).toHaveBeenCalledTimes(1))
    expect(vi.mocked(api.sideTurn).mock.calls[0][1]).toBe(`![image](${UPLOADED})`)
  })

  it('a refused send hands the staged image back as a chip, not as text', async () => {
    const user = userEvent.setup()
    vi.mocked(api.sideTurn).mockRejectedValueOnce(new ApiError(429, 'side queue is full (max 20)', ''))
    const probe = sideFiles()
    renderWithProviders(<><SideChat slot={SLOT} /><probe.Probe /></>, { store: idleSide() })
    await user.type(box(), 'look')
    fireEvent.change(fileInput(), { target: { files: [png()] } })
    await waitFor(() => expect(probe.read()).toEqual([UPLOADED]))
    await user.click(screen.getByRole('button', { name: 'Send' }))
    // The send ran (it cleared the composer first), then the refusal handed it back.
    await waitFor(() => expect(api.sideTurn).toHaveBeenCalledTimes(1))
    await waitFor(() => expect(box()).toHaveValue('look'))
    expect(probe.read()).toEqual([UPLOADED])
  })
})
