/**
 * Artifact companion chat tests.
 *
 * Covers the frontend flows on ArtifactDetailPage:
 * - panel state machine (comments <-> chat mutual exclusivity, auto-reveal guard)
 * - bound-session resolution (none / one / many -> most recent activity)
 * - sparkle flow: create with the artifact binding + OPTIMISTIC bind (the panel
 *   must become interactive off the create response alone — no fetchSlots on the
 *   critical path) + background context injection; resume without create
 * - session activation: the panel embeds ChatPage in single-session chrome with
 *   URL sync off, so it can never navigate the host /artifacts route away
 * - "Ask agent to address" staging via the writePrefill sessionStorage channel
 *   (bound vs fresh sessions)
 * - "New chat" archive-then-create ordering + optimistic prune
 * - red-X prune (bound slot vanishing resets the panel to its empty state)
 * - deleted-artifact WS relay navigates the page away
 *
 * ChatPage is stubbed: its own rendering has dedicated suites; these assert the
 * wiring (that it mounts, with which props, and which slot the panel activates).
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { screen, waitFor, fireEvent, act } from '@testing-library/react'
import { Routes, Route } from 'react-router-dom'
import ArtifactDetailPage from '../pages/ArtifactDetailPage'
import { renderWithProviders, createTestStore } from './helpers'
import { api } from '../api/client'
import { sseSlots } from '../store/dashboardSlice'
import { PREFILL_STORAGE_KEY } from '../utils/navIntent'
import type { Artifact, ChatSlot } from '../types'

vi.mock('../api/client')
// Stub the embedded chat page — these tests assert the panel wiring, not
// ChatPage internals. Keep the named PREFILL_STORAGE_KEY re-export so unrelated
// importers don't break.
vi.mock('../pages/ChatPage', () => ({
  default: (props: { embedded?: boolean; embedMode?: string; noUrlSync?: boolean }) => (
    <div
      data-testid="chat-page"
      data-embedded={String(!!props.embedded)}
      data-embed-mode={props.embedMode ?? ''}
      data-no-url-sync={String(!!props.noUrlSync)}
    />
  ),
  PREFILL_STORAGE_KEY: 'kirocrew_prefill',
}))

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

const mkSlot = (overrides: Partial<ChatSlot> = {}): ChatSlot => ({
  key: 'chat-1',
  title: 'Artifact: CR Queue',
  messages: 0,
  running: false,
  ...overrides,
} as ChatSlot)

function renderPage(popout = false, store = createTestStore()) {
  return renderWithProviders(
    <Routes>
      <Route path="/artifacts/:slug" element={<ArtifactDetailPage popout={popout} />} />
      <Route path="/artifacts" element={<div>library page target</div>} />
      <Route path="/chat" element={<div>chat page target</div>} />
    </Routes>,
    { route: '/artifacts/cr-queue', store },
  )
}

/** Seed the Redux slots snapshot the bound-session resolver reads. */
function seedSlots(store: ReturnType<typeof createTestStore>, slots: ChatSlot[]) {
  act(() => { store.dispatch(sseSlots(slots)) })
}

/** The toolbar toggle only mounts once the artifact query resolves, and unlike
 *  the artifact NAME it cannot collide with the rendered markdown body ("# CR
 *  Queue" also renders the text "CR Queue"). */
async function waitForLoaded() {
  await waitFor(() => expect(screen.getByLabelText('Toggle agent chat')).toBeInTheDocument())
}

describe('ArtifactDetailPage companion chat', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    sessionStorage.clear()
    if (!URL.createObjectURL) {
      // @ts-expect-error stub
      URL.createObjectURL = vi.fn().mockReturnValue('blob:test')
      // @ts-expect-error stub
      URL.revokeObjectURL = vi.fn()
    }
    vi.mocked(api).artifact = vi.fn().mockResolvedValue(mkArtifact())
    vi.mocked(api).artifactVersions = vi.fn().mockResolvedValue({ slug: 'cr-queue', versions: [1, 2] })
    vi.mocked(api).artifactEvents = vi.fn().mockResolvedValue({ slug: 'cr-queue', events: [] })
    vi.mocked(api).artifactComments = vi.fn().mockResolvedValue({ comments: [] })
    vi.mocked(api).createChatSlot = vi.fn().mockResolvedValue({ key: 'slot-new', title: 'Artifact: CR Queue' })
    vi.mocked(api).chatSlotContext = vi.fn().mockResolvedValue({ ok: true })
    vi.mocked(api).deleteChatSlot = vi.fn().mockResolvedValue({ ok: true })
    vi.mocked(api).chatSlots = vi.fn().mockResolvedValue([])
  })

  // ── create + optimistic bind ────────────────────────────────────────────────

  it('creates a bound session carrying the artifact slug and a pinned title', async () => {
    renderPage()
    await waitForLoaded()
    fireEvent.click(screen.getByLabelText('Toggle agent chat'))
    await waitFor(() => expect(vi.mocked(api).createChatSlot).toHaveBeenCalledTimes(1))
    const call = vi.mocked(api).createChatSlot.mock.calls[0]
    // No `name` — the backend must mint a unique key, or a name-derived key
    // would append onto an archived session's history file.
    expect(call[0]).toBeUndefined()
    expect(call[5]).toBe('Artifact: CR Queue')
    expect(call[7]).toBe('cr-queue')
  })

  it('becomes interactive off the create response alone (optimistic bind)', async () => {
    // The critical path is ONE round trip: the create response carries the
    // binding, so the panel must mount ChatPage without waiting for fetchSlots.
    renderPage()
    await waitForLoaded()
    fireEvent.click(screen.getByLabelText('Toggle agent chat'))
    await waitFor(() => expect(screen.getByTestId('chat-page')).toBeInTheDocument())
  })

  it('injects the artifact context ephemerally in the background', async () => {
    renderPage()
    await waitForLoaded()
    fireEvent.click(screen.getByLabelText('Toggle agent chat'))
    await waitFor(() => expect(vi.mocked(api).chatSlotContext).toHaveBeenCalledTimes(1))
    const [slot, content, opts] = vi.mocked(api).chatSlotContext.mock.calls[0]
    expect(slot).toBe('slot-new')
    expect(content).toContain('cr-queue')
    expect(content).toContain('artifact_get_comments')
    // Ephemeral: consumed on the NEXT user message, never persisted as a turn.
    expect(opts).toEqual({ source: 'artifact-companion', ephemeral: true })
  })

  it('embeds ChatPage in single-session chrome with URL sync off', async () => {
    // noUrlSync is what stops the embedded page writing ?sid= and navigating the
    // host /artifacts/:slug route out from under the panel.
    const store = createTestStore()
    seedSlots(store, [mkSlot({ key: 'chat-bound', artifact: 'cr-queue' })])
    renderPage(false, store)
    await waitForLoaded()
    fireEvent.click(screen.getByLabelText('Toggle agent chat'))
    const chat = await screen.findByTestId('chat-page')
    expect(chat).toHaveAttribute('data-embedded', 'true')
    expect(chat).toHaveAttribute('data-embed-mode', 'chat')
    expect(chat).toHaveAttribute('data-no-url-sync', 'true')
  })

  // ── resume vs create ────────────────────────────────────────────────────────

  it('resumes an existing bound session instead of creating one', async () => {
    const store = createTestStore()
    seedSlots(store, [mkSlot({ key: 'chat-bound', artifact: 'cr-queue' })])
    renderPage(false, store)
    await waitForLoaded()
    fireEvent.click(screen.getByLabelText('Toggle agent chat'))
    await waitFor(() => expect(screen.getByTestId('chat-page')).toBeInTheDocument())
    expect(vi.mocked(api).createChatSlot).not.toHaveBeenCalled()
    expect(store.getState().chat.activeSlot).toBe('chat-bound')
  })

  it('ignores sessions bound to a different artifact', async () => {
    const store = createTestStore()
    seedSlots(store, [mkSlot({ key: 'chat-other', artifact: 'some-other-slug' })])
    renderPage(false, store)
    await waitForLoaded()
    fireEvent.click(screen.getByLabelText('Toggle agent chat'))
    await waitFor(() => expect(vi.mocked(api).createChatSlot).toHaveBeenCalledTimes(1))
  })

  it('picks the most recently active session when several are bound', async () => {
    // The frontend flow keeps it to <=1 active bound session, but a race or a
    // History-page resume can produce more — degrade gracefully, never crash.
    const store = createTestStore()
    seedSlots(store, [
      mkSlot({ key: 'chat-old', artifact: 'cr-queue', last_activity_ts: '2026-05-01T00:00:00Z' }),
      mkSlot({ key: 'chat-new', artifact: 'cr-queue', last_activity_ts: '2026-06-01T00:00:00Z' }),
    ])
    renderPage(false, store)
    await waitForLoaded()
    fireEvent.click(screen.getByLabelText('Toggle agent chat'))
    await waitFor(() => expect(store.getState().chat.activeSlot).toBe('chat-new'))
  })

  // ── panel state machine ─────────────────────────────────────────────────────

  it('toggles the chat panel closed on a second click, keeping the session', async () => {
    const store = createTestStore()
    seedSlots(store, [mkSlot({ key: 'chat-bound', artifact: 'cr-queue' })])
    renderPage(false, store)
    await waitForLoaded()
    const toggle = screen.getByLabelText('Toggle agent chat')
    fireEvent.click(toggle)
    await waitFor(() => expect(toggle).toHaveAttribute('aria-pressed', 'true'))
    fireEvent.click(toggle)
    await waitFor(() => expect(screen.queryByTestId('chat-page')).toBeNull())
    // Closing the panel must not archive the session.
    expect(vi.mocked(api).deleteChatSlot).not.toHaveBeenCalled()
  })

  it('is mutually exclusive with the comments sidebar', async () => {
    const store = createTestStore()
    seedSlots(store, [mkSlot({ key: 'chat-bound', artifact: 'cr-queue' })])
    renderPage(false, store)
    await waitForLoaded()
    const chatToggle = screen.getByLabelText('Toggle agent chat')
    const commentsToggle = screen.getByLabelText('Toggle comments')
    fireEvent.click(chatToggle)
    await waitFor(() => expect(chatToggle).toHaveAttribute('aria-pressed', 'true'))
    fireEvent.click(commentsToggle)
    await waitFor(() => expect(commentsToggle).toHaveAttribute('aria-pressed', 'true'))
    expect(chatToggle).toHaveAttribute('aria-pressed', 'false')
    expect(screen.queryByTestId('chat-page')).toBeNull()
  })

  it('does not let the comment auto-reveal yank an open chat panel', async () => {
    // The chat panel only opens on explicit action, so a comment-count-driven
    // default must never discard that intent.
    const store = createTestStore()
    seedSlots(store, [mkSlot({ key: 'chat-bound', artifact: 'cr-queue' })])
    renderPage(false, store)
    await waitForLoaded()
    fireEvent.click(screen.getByLabelText('Toggle agent chat'))
    await waitFor(() => expect(screen.getByTestId('chat-page')).toBeInTheDocument())
    // A comment arriving now would flip the panel to 'comments' without the guard.
    vi.mocked(api).artifactComments = vi.fn().mockResolvedValue({
      comments: [{
        id: 'c1', origin: 'local', scope: 'private', author: 'nrb', body: 'note',
        thread_id: 't1', status: 'open', sync_state: 'local',
        created_at: '2026-05-21T22:00:00Z', updated_at: '2026-05-21T22:00:00Z',
      }],
    })
    await new Promise(r => setTimeout(r, 20))
    expect(screen.getByTestId('chat-page')).toBeInTheDocument()
  })

  it('falls back to the empty state when the bound slot is pruned', async () => {
    // The red-X delete anywhere removes the slot from the snapshot; the resolver
    // then yields null and the panel shows its start-chat empty state.
    const store = createTestStore()
    seedSlots(store, [mkSlot({ key: 'chat-bound', artifact: 'cr-queue' })])
    renderPage(false, store)
    await waitForLoaded()
    fireEvent.click(screen.getByLabelText('Toggle agent chat'))
    await waitFor(() => expect(screen.getByTestId('chat-page')).toBeInTheDocument())
    seedSlots(store, [])
    await waitFor(() => expect(screen.queryByTestId('chat-page')).toBeNull())
    expect(screen.getByText(/no active session for this artifact/i)).toBeInTheDocument()
  })

  // ── "Ask agent to address" staging ──────────────────────────────────────────

  it('stages (never auto-sends) the address message into a bound session', async () => {
    const store = createTestStore()
    seedSlots(store, [mkSlot({ key: 'chat-bound', artifact: 'cr-queue' })])
    vi.mocked(api).artifactComments = vi.fn().mockResolvedValue({
      comments: [{
        id: 'c1', origin: 'local', scope: 'private', author: 'nrb', body: 'tighten this',
        thread_id: 't1', status: 'open', sync_state: 'local',
        anchor: { quote: 'CR Queue' },
        created_at: '2026-05-21T22:00:00Z', updated_at: '2026-05-21T22:00:00Z',
      }],
    })
    renderPage(false, store)
    await waitForLoaded()
    // Comments auto-reveal, exposing the sidebar's "Ask agent" action.
    const ask = await screen.findByRole('button', { name: /ask agent/i })
    fireEvent.click(ask)
    await waitFor(() => expect(screen.getByTestId('chat-page')).toBeInTheDocument())
    const staged = sessionStorage.getItem(PREFILL_STORAGE_KEY) ?? ''
    expect(staged).toContain('chat-bound')
    expect(staged).toContain('1 open comment')
  })

  // ── "New chat" ──────────────────────────────────────────────────────────────

  it('archives the bound session BEFORE creating the replacement', async () => {
    const order: string[] = []
    vi.mocked(api).deleteChatSlot = vi.fn().mockImplementation(async () => { order.push('delete'); return { ok: true } })
    vi.mocked(api).createChatSlot = vi.fn().mockImplementation(async () => {
      order.push('create'); return { key: 'slot-new2', title: 'Artifact: CR Queue' }
    })
    const store = createTestStore()
    seedSlots(store, [mkSlot({ key: 'chat-bound', artifact: 'cr-queue' })])
    renderPage(false, store)
    await waitForLoaded()
    fireEvent.click(screen.getByLabelText('Toggle agent chat'))
    await waitFor(() => expect(screen.getByTestId('chat-page')).toBeInTheDocument())
    fireEvent.click(screen.getByLabelText('New chat'))
    await waitFor(() => expect(order).toEqual(['delete', 'create']))
    // Optimistic prune, or the resolver keeps picking the archived slot.
    expect(store.getState().dashboard.slots.some(s => s.key === 'chat-bound')).toBe(false)
  })

  it('archives EVERY slot bound to the slug, not just the resolved winner', async () => {
    // A two-window creation race can leave two slots bound to one slug. Archiving
    // only the winner leaves the other behind, and pickBoundSlot then reopens that
    // stale conversation — so "New chat" appears to resurrect an old session.
    const store = createTestStore()
    seedSlots(store, [
      mkSlot({ key: 'chat-old', artifact: 'cr-queue', last_activity_ts: '2026-05-01T00:00:00Z' }),
      mkSlot({ key: 'chat-new', artifact: 'cr-queue', last_activity_ts: '2026-06-01T00:00:00Z' }),
      mkSlot({ key: 'chat-other', artifact: 'some-other-slug' }),
    ])
    // Server truth after the archives: only the unrelated slot survives. Without
    // this the background fetchSlots() (mocked to []) would overwrite the store and
    // the post-state assertions below would be measuring the mock, not the code.
    vi.mocked(api).chatSlots = vi.fn().mockResolvedValue([
      mkSlot({ key: 'chat-other', artifact: 'some-other-slug' }),
    ])
    renderPage(false, store)
    await waitForLoaded()
    fireEvent.click(screen.getByLabelText('Toggle agent chat'))
    await waitFor(() => expect(screen.getByTestId('chat-page')).toBeInTheDocument())
    fireEvent.click(screen.getByLabelText('New chat'))

    await waitFor(() => expect(vi.mocked(api).createChatSlot).toHaveBeenCalledTimes(1))
    const archived = vi.mocked(api).deleteChatSlot.mock.calls.map(c => c[0]).sort()
    expect(archived).toEqual(['chat-new', 'chat-old'])
    // Another artifact's session must be untouched.
    expect(archived).not.toContain('chat-other')
    expect(store.getState().dashboard.slots.map(s => s.key)).toEqual(['chat-other'])
  })

  it('still creates a replacement when the old session is already gone (404)', async () => {
    vi.mocked(api).deleteChatSlot = vi.fn().mockRejectedValue(
      Object.assign(new Error('Not Found'), { status: 404 }),
    )
    const store = createTestStore()
    seedSlots(store, [mkSlot({ key: 'chat-bound', artifact: 'cr-queue' })])
    renderPage(false, store)
    await waitForLoaded()
    fireEvent.click(screen.getByLabelText('Toggle agent chat'))
    await waitFor(() => expect(screen.getByTestId('chat-page')).toBeInTheDocument())
    fireEvent.click(screen.getByLabelText('New chat'))
    await waitFor(() => expect(vi.mocked(api).createChatSlot).toHaveBeenCalledTimes(1))
    expect(store.getState().dashboard.slots.some(s => s.key === 'chat-bound')).toBe(false)
  })

  it('aborts "New chat" when archiving fails for any reason other than 404', async () => {
    // A 500 leaves the old session live server-side. Creating anyway would give
    // the slug TWO bound sessions, and since the resolver breaks ties on
    // last_activity_ts the OLD one keeps winning — "New chat" would look like a
    // no-op forever. Abort and surface the error instead.
    vi.mocked(api).deleteChatSlot = vi.fn().mockRejectedValue(
      Object.assign(new Error('Internal Server Error'), { status: 500 }),
    )
    const store = createTestStore()
    seedSlots(store, [mkSlot({ key: 'chat-bound', artifact: 'cr-queue' })])
    renderPage(false, store)
    await waitForLoaded()
    fireEvent.click(screen.getByLabelText('Toggle agent chat'))
    await waitFor(() => expect(screen.getByTestId('chat-page')).toBeInTheDocument())
    fireEvent.click(screen.getByLabelText('New chat'))
    await waitFor(() => expect(screen.getByText(/internal server error/i)).toBeInTheDocument())
    expect(vi.mocked(api).createChatSlot).not.toHaveBeenCalled()
    // The old session must stay bound, so the panel keeps working.
    expect(store.getState().dashboard.slots.some(s => s.key === 'chat-bound')).toBe(true)
  })

  // ── re-entrancy: at most one active bound session per slug ──────────────────
  // The archive-then-create ordering protects the "<=1 active bound session"
  // invariant against FAILURE. These protect it against CONCURRENCY: both entry
  // points are async, so a rapid double-click otherwise starts two flows that
  // resolve the same boundSlot, both archive it (the second 404s and proceeds by
  // design), and both create a replacement.

  it('creates ONE replacement when "New chat" is double-clicked', async () => {
    let releaseDelete: () => void = () => {}
    vi.mocked(api).deleteChatSlot = vi.fn().mockImplementation(
      () => new Promise<{ ok: boolean }>(res => { releaseDelete = () => res({ ok: true }) }),
    )
    const store = createTestStore()
    seedSlots(store, [mkSlot({ key: 'chat-bound', artifact: 'cr-queue' })])
    renderPage(false, store)
    await waitForLoaded()
    fireEvent.click(screen.getByLabelText('Toggle agent chat'))
    await waitFor(() => expect(screen.getByTestId('chat-page')).toBeInTheDocument())

    const newChat = screen.getByLabelText('New chat')
    fireEvent.click(newChat)
    fireEvent.click(newChat)   // second click lands while the archive is in flight
    await act(async () => { releaseDelete(); await Promise.resolve() })

    await waitFor(() => expect(vi.mocked(api).createChatSlot).toHaveBeenCalledTimes(1))
    expect(vi.mocked(api).deleteChatSlot).toHaveBeenCalledTimes(1)
  })

  it('creates ONE session when the sparkle is double-clicked with none bound', async () => {
    let releaseCreate: () => void = () => {}
    vi.mocked(api).createChatSlot = vi.fn().mockImplementation(
      () => new Promise(res => {
        releaseCreate = () => res({ key: 'slot-new', title: 'Artifact: CR Queue' })
      }),
    )
    renderPage()
    await waitForLoaded()
    const toggle = screen.getByLabelText('Toggle agent chat')
    // BOTH clicks inside ONE act() block. Dispatching them as two separate
    // fireEvent calls lets React re-render in between, so the second click sees
    // panel==='chat' and merely toggles the panel shut — which passes even with
    // no guard and therefore proves nothing. Batching them reproduces the real
    // double-click: the second handler runs against the pre-render state, so only
    // the in-flight ref can stop it.
    await act(async () => {
      fireEvent.click(toggle)
      fireEvent.click(toggle)
    })
    await act(async () => { releaseCreate(); await Promise.resolve() })

    expect(vi.mocked(api).createChatSlot).toHaveBeenCalledTimes(1)
  })

  it('releases the guard so a later "New chat" still works', async () => {
    // A guard that leaks would wedge the button permanently after one use.
    const store = createTestStore()
    seedSlots(store, [mkSlot({ key: 'chat-bound', artifact: 'cr-queue' })])
    renderPage(false, store)
    await waitForLoaded()
    fireEvent.click(screen.getByLabelText('Toggle agent chat'))
    await waitFor(() => expect(screen.getByTestId('chat-page')).toBeInTheDocument())
    fireEvent.click(screen.getByLabelText('New chat'))
    await waitFor(() => expect(vi.mocked(api).createChatSlot).toHaveBeenCalledTimes(1))
    seedSlots(store, [mkSlot({ key: 'slot-new', artifact: 'cr-queue' })])
    fireEvent.click(screen.getByLabelText('New chat'))
    await waitFor(() => expect(vi.mocked(api).createChatSlot).toHaveBeenCalledTimes(2))
  })

  it('releases the guard after a failed archive', async () => {
    vi.mocked(api).deleteChatSlot = vi.fn()
      .mockRejectedValueOnce(Object.assign(new Error('Internal Server Error'), { status: 500 }))
      .mockResolvedValue({ ok: true })
    const store = createTestStore()
    seedSlots(store, [mkSlot({ key: 'chat-bound', artifact: 'cr-queue' })])
    renderPage(false, store)
    await waitForLoaded()
    fireEvent.click(screen.getByLabelText('Toggle agent chat'))
    await waitFor(() => expect(screen.getByTestId('chat-page')).toBeInTheDocument())
    fireEvent.click(screen.getByLabelText('New chat'))
    await waitFor(() => expect(screen.getByText(/internal server error/i)).toBeInTheDocument())
    // The abort path returns from inside the try — `finally` must still clear it.
    fireEvent.click(screen.getByLabelText('New chat'))
    await waitFor(() => expect(vi.mocked(api).createChatSlot).toHaveBeenCalledTimes(1))
  })

  it('keeps the chat panel open when an anchored comment is added', async () => {
    // An anchored add is reachable while chatting (the body stays visible); the
    // guard keeps it from yanking the conversation over to 'comments'.
    const store = createTestStore()
    seedSlots(store, [mkSlot({ key: 'chat-bound', artifact: 'cr-queue' })])
    vi.mocked(api).postArtifactComment = vi.fn().mockResolvedValue({ ok: true })
    renderPage(false, store)
    await waitForLoaded()
    fireEvent.click(screen.getByLabelText('Toggle agent chat'))
    await waitFor(() => expect(screen.getByTestId('chat-page')).toBeInTheDocument())
    // Drive the doc-level add path directly via the sidebar-less route: reuse the
    // comments query to simulate the count changing while chat is open.
    vi.mocked(api).artifactComments = vi.fn().mockResolvedValue({
      comments: [{
        id: 'c1', origin: 'local', scope: 'private', author: 'nrb', body: 'note',
        thread_id: 't1', status: 'open', sync_state: 'local',
        anchor: { quote: 'Rollout plan' },
        created_at: '2026-05-21T22:00:00Z', updated_at: '2026-05-21T22:00:00Z',
      }],
    })
    await new Promise(r => setTimeout(r, 50))
    expect(screen.getByTestId('chat-page')).toBeInTheDocument()
    expect(screen.getByLabelText('Toggle agent chat')).toHaveAttribute('aria-pressed', 'true')
  })

  // ── cold load: an unloaded snapshot is not "nothing bound" ──────────────────

  it('resolves the slots snapshot before creating on a cold load', async () => {
    // `dashboard.slots` is [] both before the first fetch AND when nothing is
    // bound. Clicking the sparkle before the fetch lands must not create a second
    // session for a slug that already has one.
    const store = createTestStore()          // slotsLoaded === false, slots === []
    vi.mocked(api).chatSlots = vi.fn().mockResolvedValue([
      mkSlot({ key: 'chat-bound', artifact: 'cr-queue' }),
    ])
    renderPage(false, store)
    await waitForLoaded()
    fireEvent.click(screen.getByLabelText('Toggle agent chat'))
    await waitFor(() => expect(screen.getByTestId('chat-page')).toBeInTheDocument())
    expect(vi.mocked(api).createChatSlot).not.toHaveBeenCalled()
    expect(store.getState().chat.activeSlot).toBe('chat-bound')
  })

  it('creates on a cold load when the resolved snapshot really has none', async () => {
    const store = createTestStore()
    vi.mocked(api).chatSlots = vi.fn().mockResolvedValue([])
    renderPage(false, store)
    await waitForLoaded()
    fireEvent.click(screen.getByLabelText('Toggle agent chat'))
    await waitFor(() => expect(vi.mocked(api).createChatSlot).toHaveBeenCalledTimes(1))
  })

  it('still creates when the cold-load resolve fails', async () => {
    // A failed fetch must not wedge the button — fall through and create.
    const store = createTestStore()
    vi.mocked(api).chatSlots = vi.fn().mockRejectedValue(new Error('offline'))
    renderPage(false, store)
    await waitForLoaded()
    fireEvent.click(screen.getByLabelText('Toggle agent chat'))
    await waitFor(() => expect(vi.mocked(api).createChatSlot).toHaveBeenCalledTimes(1))
  })

  // ── webapp kind ─────────────────────────────────────────────────────────────

  it('renders the chat panel for a webapp artifact', async () => {
    // The toolbar toggles render for every kind, so the panels must too —
    // otherwise a click on a webapp artifact creates a session with nowhere to go.
    vi.mocked(api).artifact = vi.fn().mockResolvedValue(mkArtifact({ kind: 'webapp' }))
    const store = createTestStore()
    seedSlots(store, [mkSlot({ key: 'chat-bound', artifact: 'cr-queue' })])
    renderPage(false, store)
    await waitForLoaded()
    fireEvent.click(screen.getByLabelText('Toggle agent chat'))
    await waitFor(() => expect(screen.getByTestId('chat-page')).toBeInTheDocument())
  })

  it('renders the comments sidebar for a webapp artifact', async () => {
    vi.mocked(api).artifact = vi.fn().mockResolvedValue(mkArtifact({ kind: 'webapp' }))
    renderPage()
    await waitForLoaded()
    fireEvent.click(screen.getByLabelText('Toggle comments'))
    await waitFor(() =>
      expect(screen.getByLabelText('Toggle comments')).toHaveAttribute('aria-pressed', 'true'))
    expect(screen.getByRole('button', { name: /add comment/i })).toBeInTheDocument()
  })

  // ── deleted-artifact relay ──────────────────────────────────────────────────

  it('navigates away when the artifact is deleted elsewhere', async () => {
    renderPage()
    await waitForLoaded()
    act(() => {
      window.dispatchEvent(new CustomEvent('kirocrew:artifact-deleted', { detail: { slug: 'cr-queue' } }))
    })
    await waitFor(() => expect(screen.getByText('library page target')).toBeInTheDocument())
  })

  it('ignores a delete event for a different artifact', async () => {
    renderPage()
    await waitForLoaded()
    act(() => {
      window.dispatchEvent(new CustomEvent('kirocrew:artifact-deleted', { detail: { slug: 'other' } }))
    })
    expect(screen.queryByText('library page target')).toBeNull()
  })
})
