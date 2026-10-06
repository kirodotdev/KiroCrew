import { describe, it, expect, vi, beforeEach } from 'vitest'
import { screen, fireEvent, waitFor, act } from '@testing-library/react'
import { useState } from 'react'
import { ArtifactSendToSessionSubmenu, useArtifactSendToSession } from '../components/ArtifactSendToSession'
import { DropdownMenu, DropdownMenuContent, DropdownMenuTrigger } from '../components/ui/dropdown-menu'
import type { NavIntent } from '../utils/popoutController'
import { artifactReferencePrompt } from '../components/artifactReference.prompt'
import { renderWithProviders, createTestStore } from './helpers'
import { fetchSlots } from '../store/dashboardSlice'
import { api } from '../api/client'
import { DRAFTS_KEY, saveDrafts } from '../utils/chatDrafts'
import type { ChatSlot } from '../types'

vi.mock('../api/client')

const slot = (key: string, title: string, extra: Partial<ChatSlot> = {}): ChatSlot => ({
  key, title, messages: 3, running: false, last_activity_ts: '2026-10-05T12:00:00Z', ...extra,
})

type BeforeSend = (proceed: (recheck: (go: () => void) => void) => void | Promise<void>) => void

/** The page side of the control: owns the hook, hosts the submenu in a menu. */
function Harness({ onSend, onError, beforeSend, active = true }: {
  onSend: (intent: NavIntent) => void
  onError: (message: string | null) => void
  beforeSend: BeforeSend
  active?: boolean
}) {
  const state = useArtifactSendToSession({ name: 'CR Queue', slug: 'cr-queue', active, onSend, beforeSend, onError })
  return (
    <DropdownMenu>
      <DropdownMenuTrigger>More actions</DropdownMenuTrigger>
      <DropdownMenuContent>
        <ArtifactSendToSessionSubmenu state={state} />
      </DropdownMenuContent>
    </DropdownMenu>
  )
}

function setup(slots: ChatSlot[], beforeSend: BeforeSend = (p) => { void p((go) => go()) }) {
  const store = createTestStore()
  store.dispatch(fetchSlots.fulfilled(slots, 'req'))
  const onSend = vi.fn()
  const onError = vi.fn()
  renderWithProviders(<Harness onSend={onSend} onError={onError} beforeSend={beforeSend} />, { store })
  return { onSend, onError }
}

const openMenu = async () => {
  fireEvent.pointerDown(screen.getByRole('button', { name: 'More actions' }), { button: 0, ctrlKey: false, pointerType: 'mouse' })
  fireEvent.click(await screen.findByRole('menuitem', { name: 'Send to a session' }))
}

describe('ArtifactSendToSession', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    localStorage.removeItem(DRAFTS_KEY)
  })

  it('names the slug the agent loads it by', () => {
    expect(artifactReferencePrompt('CR Queue', 'cr-queue')).toContain('`cr-queue`')
    expect(artifactReferencePrompt('CR Queue', 'cr-queue')).toContain('artifact_get')
  })

  it('lists live sessions but not artifact-bound companion chats', async () => {
    setup([slot('chat-1', 'Release prep'), slot('chat-2', 'Companion', { artifact: 'cr-queue' })])
    await openMenu()
    expect(await screen.findByRole('menuitem', { name: 'Release prep' })).toBeInTheDocument()
    expect(screen.queryByRole('menuitem', { name: 'Companion' })).toBeNull()
    expect(screen.getByRole('menuitem', { name: 'New session' })).toBeInTheDocument()
  })

  it('seeds the chosen session with the reference appended to its stored draft', async () => {
    saveDrafts({ 'chat-1': 'half-typed thought' })
    const { onSend } = setup([slot('chat-1', 'Release prep')])
    await openMenu()
    fireEvent.click(await screen.findByRole('menuitem', { name: 'Release prep' }))
    expect(onSend).toHaveBeenCalledTimes(1)
    const intent = onSend.mock.calls[0][0]
    expect(intent.path).toBe('/chat')
    expect(intent.slotKey).toBe('chat-1')
    expect(intent.prefill.slotKey).toBe('chat-1')
    // The draft the user left there survives; the reference follows it.
    expect(intent.prefill.prompt).toBe(`half-typed thought\n\n${artifactReferencePrompt('CR Queue', 'cr-queue')}`)
  })

  it('creates a new session and hands off to it', async () => {
    vi.mocked(api).createChatSlot = vi.fn().mockResolvedValue({ key: 'chat-new' })
    const { onSend } = setup([])
    await openMenu()
    fireEvent.click(await screen.findByRole('menuitem', { name: 'New session' }))
    await waitFor(() => expect(onSend).toHaveBeenCalledTimes(1))
    expect(onSend.mock.calls[0][0]).toMatchObject({
      path: '/chat',
      slotKey: 'chat-new',
      prefill: { slotKey: 'chat-new', prompt: artifactReferencePrompt('CR Queue', 'cr-queue') },
    })
  })

  it('reports a failed create to the page instead of handing off', async () => {
    vi.mocked(api).createChatSlot = vi.fn().mockRejectedValue(new Error('boom'))
    const { onSend, onError } = setup([])
    await openMenu()
    fireEvent.click(await screen.findByRole('menuitem', { name: 'New session' }))
    await waitFor(() => expect(onError).toHaveBeenLastCalledWith('boom'))
    expect(onSend).not.toHaveBeenCalled()
  })

  it('abandons the hand-off when the user starts editing while the session is being created', async () => {
    let resolve!: (v: { key: string }) => void
    vi.mocked(api).createChatSlot = vi.fn().mockReturnValue(new Promise((r) => { resolve = r }))
    const store = createTestStore()
    store.dispatch(fetchSlots.fulfilled([], 'req'))
    const onSend = vi.fn()
    let setActive!: (v: boolean) => void
    function Toggle() {
      const [active, set] = useState(true)
      setActive = set
      return <Harness onSend={onSend} onError={vi.fn()} beforeSend={(p) => { void p((go) => go()) }} active={active} />
    }
    renderWithProviders(<Toggle />, { store })
    await openMenu()
    fireEvent.click(await screen.findByRole('menuitem', { name: 'New session' }))
    // The menu has closed and the create is still in flight; the user opens the editor.
    act(() => setActive(false))
    await act(async () => { resolve({ key: 'chat-new' }) })
    expect(onSend).not.toHaveBeenCalled()
  })

  it('finishes a New session hand-off after the menu has closed', async () => {
    let resolve!: (v: { key: string }) => void
    vi.mocked(api).createChatSlot = vi.fn().mockReturnValue(new Promise((r) => { resolve = r }))
    const { onSend } = setup([])
    await openMenu()
    fireEvent.click(await screen.findByRole('menuitem', { name: 'New session' }))
    await waitFor(() => expect(screen.queryByRole('menu')).toBeNull())
    await act(async () => { resolve({ key: 'chat-new' }) })
    await waitFor(() => expect(onSend).toHaveBeenCalledTimes(1))
    expect(onSend.mock.calls[0][0]).toMatchObject({ path: '/chat', slotKey: 'chat-new' })
  })

  it('asks the page once per hand-off, not again after the session is created', async () => {
    vi.mocked(api).createChatSlot = vi.fn().mockResolvedValue({ key: 'chat-new' })
    const beforeSend = vi.fn((p: (recheck: (go: () => void) => void) => void | Promise<void>) => { void p((go) => go()) })
    const { onSend } = setup([], beforeSend)
    await openMenu()
    fireEvent.click(await screen.findByRole('menuitem', { name: 'New session' }))
    await waitFor(() => expect(onSend).toHaveBeenCalledTimes(1))
    expect(beforeSend).toHaveBeenCalledTimes(1)
  })

  it('creates no session when the page declines to leave', async () => {
    const create = vi.fn().mockResolvedValue({ key: 'chat-new' })
    vi.mocked(api).createChatSlot = create
    // The comment-draft prompt answered "keep": proceed is never run.
    const { onSend } = setup([], () => {})
    await openMenu()
    fireEvent.click(await screen.findByRole('menuitem', { name: 'New session' }))
    await new Promise((r) => setTimeout(r, 0))
    expect(create).not.toHaveBeenCalled()
    expect(onSend).not.toHaveBeenCalled()
  })
})
