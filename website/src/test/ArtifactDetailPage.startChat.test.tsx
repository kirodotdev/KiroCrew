/**
 * "Start a new chat from this artifact" (#13904).
 *
 * The toolbar's More-menu action hands the artifact off to a FRESH, ordinary session on the
 * full /chat page. What these tests pin:
 * - the session is created UNBOUND (no artifact binding, no pinned title), so
 *   the companion chat's one-active-bound-session-per-slug invariant is untouched;
 * - no companion context entry is injected (that is the bound panel's job);
 * - the handoff prompt naming the slug is STAGED through the writePrefill
 *   channel for the new slot, never auto-sent, and the page navigates to /chat;
 * - inside a popout the intent is forwarded to a main window, never routed in place.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { screen, waitFor, fireEvent, within } from '@testing-library/react'
import { Routes, Route } from 'react-router-dom'
import ArtifactDetailPage from '../pages/ArtifactDetailPage'
import { renderWithProviders, createTestStore } from './helpers'
import { api } from '../api/client'
import { forwardToMain } from '../utils/artifactPopout'
import { PREFILL_STORAGE_KEY } from '../utils/navIntent'
import type { Artifact } from '../types'

vi.mock('../api/client')
vi.mock('../pages/ChatPage', () => ({
  default: () => <div data-testid="chat-page" />,
  PREFILL_STORAGE_KEY: 'kirocrew_prefill',
}))
vi.mock('../utils/artifactPopout', async (importOriginal) => {
  const mod = await importOriginal<typeof import('../utils/artifactPopout')>()
  return { ...mod, forwardToMain: vi.fn() }
})

const LABEL = 'Start a new chat from this artifact'

/** The action lives in the toolbar's labelled "More" overflow menu (the row is
 *  capped at its existing buttons). Radix opens the trigger on Enter in jsdom. */
async function pickStartChat() {
  fireEvent.keyDown(await screen.findByLabelText('More actions'), { key: 'Enter' })
  fireEvent.click(await screen.findByRole('menuitem', { name: LABEL }))
}

const mkArtifact = (overrides: Partial<Artifact> = {}): Artifact => ({
  slug: 'cr-queue',
  name: 'CR Queue',
  kind: 'markdown',
  source: 'chat',
  description: '',
  tags: [],
  version: 2,
  created_at: '2026-05-21T22:00:00.000000+00:00',
  updated_at: '2026-05-21T22:30:00.000000+00:00',
  content: '# CR Queue',
  ...overrides,
})

function renderPage(popout = false, store = createTestStore()) {
  return renderWithProviders(
    <Routes>
      <Route path="/artifacts/:slug" element={<ArtifactDetailPage popout={popout} />} />
      <Route path="/chat" element={<div>chat page target</div>} />
      <Route path="/artifacts" element={<div>library page target</div>} />
    </Routes>,
    { route: '/artifacts/cr-queue', store },
  )
}

describe('ArtifactDetailPage start a new chat from this artifact', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    sessionStorage.clear()
    vi.mocked(api).artifact = vi.fn().mockResolvedValue(mkArtifact())
    vi.mocked(api).artifactVersions = vi.fn().mockResolvedValue({ slug: 'cr-queue', versions: [1, 2] })
    vi.mocked(api).artifactEvents = vi.fn().mockResolvedValue({ slug: 'cr-queue', events: [] })
    vi.mocked(api).artifactComments = vi.fn().mockResolvedValue({ comments: [] })
    vi.mocked(api).createChatSlot = vi.fn().mockResolvedValue({ key: 'slot-fresh', title: '' })
    vi.mocked(api).chatSlotContext = vi.fn().mockResolvedValue({ ok: true })
    vi.mocked(api).chatSlots = vi.fn().mockResolvedValue([])
    vi.mocked(api).dashboardConfig = vi.fn().mockResolvedValue({})
  })

  it('creates an UNBOUND session and injects no companion context', async () => {
    renderPage()
    await pickStartChat()
    await waitFor(() => expect(vi.mocked(api).createChatSlot).toHaveBeenCalledTimes(1))
    const call = vi.mocked(api).createChatSlot.mock.calls[0]
    // No pinned title and — crucially — no artifact binding, so this never
    // becomes a second companion session for the slug.
    expect(call[5]).toBeUndefined()
    expect(call[6]).toBeUndefined()
    expect(vi.mocked(api).chatSlotContext).not.toHaveBeenCalled()
  })

  it('creates through the new-chat path: default memory mode applied, row registered', async () => {
    // A user whose default is Incognito gets an Incognito session here too, and
    // the new row is in the slots list before ChatPage mounts, so the session
    // controller cannot discard the selection of an unknown slot.
    vi.mocked(api).dashboardConfig = vi.fn().mockResolvedValue({ default_memory_mode: 'incognito' })
    const store = createTestStore()
    renderPage(false, store)
    await pickStartChat()
    await waitFor(() => expect(screen.getByText('chat page target')).toBeInTheDocument())
    expect(vi.mocked(api).createChatSlot.mock.calls[0][4]).toBe('incognito')
    expect(store.getState().dashboard.slots.map((x: { key: string }) => x.key)).toContain('slot-fresh')
  })

  it('stages the handoff prompt for the new slot and navigates to /chat', async () => {
    renderPage()
    await pickStartChat()
    await waitFor(() => expect(screen.getByText('chat page target')).toBeInTheDocument())
    const staged = JSON.parse(sessionStorage.getItem(PREFILL_STORAGE_KEY) || '{}')
    expect(staged.slotKey).toBe('slot-fresh')
    expect(staged.prompt).toBe(
      'Use the artifact `cr-queue` ("CR Queue") as the brief for this chat.',
    )
  })

  it('does not navigate or stage anything when the create fails', async () => {
    vi.mocked(api).createChatSlot = vi.fn().mockRejectedValue(new Error('gateway down'))
    renderPage()
    await pickStartChat()
    await waitFor(() => expect(screen.getByText('gateway down')).toBeInTheDocument())
    expect(screen.queryByText('chat page target')).not.toBeInTheDocument()
    expect(sessionStorage.getItem(PREFILL_STORAGE_KEY)).toBeNull()
  })

  it('asks before discarding an unsent comment in the sidebar, and leaves it on Cancel', async () => {
    // The sidebar's add box is component-local state: leaving for /chat would
    // drop it with no trace, so the hand-off must confirm first.
    renderPage()
    fireEvent.click(await screen.findByLabelText('Toggle comments'))
    fireEvent.click(await screen.findByRole('button', { name: /add comment/i }))
    const box = await screen.findByPlaceholderText('Add a comment on the whole artifact…')
    fireEvent.change(box, { target: { value: 'not posted yet' } })

    await pickStartChat()
    const dialog = await screen.findByRole('dialog')
    expect(within(dialog).getByText('Discard your unsaved comment?')).toBeInTheDocument()
    fireEvent.click(within(dialog).getByRole('button', { name: 'Cancel' }))
    await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull())
    expect(vi.mocked(api).createChatSlot).not.toHaveBeenCalled()
    expect(screen.getByPlaceholderText('Add a comment on the whole artifact…')).toHaveValue('not posted yet')

    await pickStartChat()
    fireEvent.click(within(await screen.findByRole('dialog')).getByRole('button', { name: 'Discard comment' }))
    await waitFor(() => expect(screen.getByText('chat page target')).toBeInTheDocument())
    expect(vi.mocked(api).createChatSlot).toHaveBeenCalledTimes(1)
  })

  it('does not ask when the sidebar box is open but empty', async () => {
    renderPage()
    fireEvent.click(await screen.findByLabelText('Toggle comments'))
    fireEvent.click(await screen.findByRole('button', { name: /add comment/i }))
    await screen.findByPlaceholderText('Add a comment on the whole artifact…')
    await pickStartChat()
    await waitFor(() => expect(screen.getByText('chat page target')).toBeInTheDocument())
    expect(screen.queryByText('Discard your unsaved comment?')).toBeNull()
  })

  it('re-asks when a sidebar draft is started while the create is in flight', async () => {
    // Backstop behind the lock: the first check passes (box empty), then a
    // draft is reported during the round-trip (programmatic change -- a user
    // can no longer type into the locked box). Leaving must ask again;
    // declining stays here and removes the session nobody will use.
    let resolveCreate: (v: unknown) => void = () => {}
    vi.mocked(api).createChatSlot = vi.fn().mockReturnValue(new Promise((r) => { resolveCreate = r }))
    vi.mocked(api).deleteChatSlot = vi.fn().mockResolvedValue({ ok: true })
    renderPage()
    fireEvent.click(await screen.findByLabelText('Toggle comments'))
    fireEvent.click(await screen.findByRole('button', { name: /add comment/i }))
    const box = await screen.findByPlaceholderText('Add a comment on the whole artifact…')
    await pickStartChat()
    await waitFor(() => expect(vi.mocked(api).createChatSlot).toHaveBeenCalledTimes(1))
    fireEvent.change(box, { target: { value: 'typed while waiting' } })
    resolveCreate({ key: 'slot-fresh', title: '' })

    const dialog = await screen.findByRole('dialog')
    fireEvent.click(within(dialog).getByRole('button', { name: 'Cancel' }))
    await waitFor(() => expect(vi.mocked(api).deleteChatSlot).toHaveBeenCalledWith('slot-fresh'))
    expect(screen.queryByText('chat page target')).not.toBeInTheDocument()
    expect(sessionStorage.getItem(PREFILL_STORAGE_KEY)).toBeNull()
    expect(screen.getByPlaceholderText('Add a comment on the whole artifact…')).toHaveValue('typed while waiting')
  })

  it('locks Edit and the sidebar composer while the create is in flight', async () => {
    // Anything typed in that window would be lost on navigation, so nothing
    // that starts unsaved text is usable until the create settles.
    let resolveCreate: (v: unknown) => void = () => {}
    vi.mocked(api).createChatSlot = vi.fn().mockReturnValue(new Promise((r) => { resolveCreate = r }))
    renderPage()
    fireEvent.click(await screen.findByLabelText('Toggle comments'))
    fireEvent.click(await screen.findByRole('button', { name: /add comment/i }))
    const box = await screen.findByPlaceholderText('Add a comment on the whole artifact…')
    expect(screen.getByLabelText('Edit content')).toBeEnabled()
    await pickStartChat()
    await waitFor(() => expect(vi.mocked(api).createChatSlot).toHaveBeenCalledTimes(1))
    expect(screen.getByLabelText('Edit content')).toBeDisabled()
    expect(box).toBeDisabled()
    resolveCreate({ key: 'slot-fresh', title: '' })
    await waitFor(() => expect(screen.getByText('chat page target')).toBeInTheDocument())
  })

  it('surfaces a failed delete of the declined session through the error notice', async () => {
    // The late re-check can still find a draft (the selection composer's draft
    // survives its suspension, and a draft can be reported by a box that was
    // already open). Simulate that with a programmatic change: jsdom delivers a
    // change event to a disabled textarea, a real user cannot type into it.
    let resolveCreate: (v: unknown) => void = () => {}
    vi.mocked(api).createChatSlot = vi.fn().mockReturnValue(new Promise((r) => { resolveCreate = r }))
    vi.mocked(api).deleteChatSlot = vi.fn().mockRejectedValue(new Error('delete failed: offline'))
    renderPage()
    fireEvent.click(await screen.findByLabelText('Toggle comments'))
    fireEvent.click(await screen.findByRole('button', { name: /add comment/i }))
    const box = await screen.findByPlaceholderText('Add a comment on the whole artifact…')
    await pickStartChat()
    await waitFor(() => expect(vi.mocked(api).createChatSlot).toHaveBeenCalledTimes(1))
    fireEvent.change(box, { target: { value: 'late draft' } })
    resolveCreate({ key: 'slot-fresh', title: '' })
    fireEvent.click(within(await screen.findByRole('dialog')).getByRole('button', { name: 'Cancel' }))
    await waitFor(() => expect(screen.getByText('delete failed: offline')).toBeInTheDocument())
    expect(screen.queryByText('chat page target')).not.toBeInTheDocument()
  })

  it('drops the hand-off when the user leaves the page before the create resolves', async () => {
    // A late create must not navigate away from wherever the user went, nor
    // judge "no draft" from the departed page's refs; the orphan session goes.
    let resolveCreate: (v: unknown) => void = () => {}
    vi.mocked(api).createChatSlot = vi.fn().mockReturnValue(new Promise((r) => { resolveCreate = r }))
    vi.mocked(api).deleteChatSlot = vi.fn().mockResolvedValue({ ok: true })
    renderPage()
    await pickStartChat()
    await waitFor(() => expect(vi.mocked(api).createChatSlot).toHaveBeenCalledTimes(1))
    fireEvent.click(screen.getByRole('button', { name: /back/i }))
    await waitFor(() => expect(screen.getByText('library page target')).toBeInTheDocument())
    resolveCreate({ key: 'slot-fresh', title: '' })
    await waitFor(() => expect(vi.mocked(api).deleteChatSlot).toHaveBeenCalledWith('slot-fresh'))
    expect(screen.getByText('library page target')).toBeInTheDocument()
    expect(screen.queryByText('chat page target')).not.toBeInTheDocument()
    expect(sessionStorage.getItem(PREFILL_STORAGE_KEY)).toBeNull()
  })

  it('popout: forwards the intent (slot + prefill) to a main window instead of routing', async () => {
    renderPage(true)
    await pickStartChat()
    await waitFor(() => expect(vi.mocked(forwardToMain)).toHaveBeenCalledTimes(1))
    expect(vi.mocked(forwardToMain).mock.calls[0][0]).toEqual({
      path: '/chat',
      slotKey: 'slot-fresh',
      prefill: {
        slotKey: 'slot-fresh',
        prompt: 'Use the artifact `cr-queue` ("CR Queue") as the brief for this chat.',
      },
    })
    expect(screen.queryByText('chat page target')).not.toBeInTheDocument()
  })
})
