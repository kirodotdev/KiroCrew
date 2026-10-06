import { describe, it, expect, vi, beforeEach } from 'vitest'
import { screen, fireEvent, waitFor } from '@testing-library/react'
import { ArtifactSendToSession, artifactReferencePrompt } from '../components/ArtifactSendToSession'
import { renderWithProviders, createTestStore } from './helpers'
import { fetchSlots } from '../store/dashboardSlice'
import { api } from '../api/client'
import { DRAFTS_KEY, saveDrafts } from '../utils/chatDrafts'
import type { ChatSlot } from '../types'

vi.mock('../api/client')

const slot = (key: string, title: string, extra: Partial<ChatSlot> = {}): ChatSlot => ({
  key, title, messages: 3, running: false, last_activity_ts: '2026-10-05T12:00:00Z', ...extra,
})

function setup(slots: ChatSlot[]) {
  const store = createTestStore()
  store.dispatch(fetchSlots.fulfilled(slots, 'req'))
  const onSend = vi.fn()
  renderWithProviders(<ArtifactSendToSession name="CR Queue" slug="cr-queue" onSend={onSend} />, { store })
  return { onSend }
}

const openMenu = () => fireEvent.pointerDown(
  screen.getByRole('button', { name: 'Send to a session' }),
  { button: 0, ctrlKey: false, pointerType: 'mouse' },
)

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
    openMenu()
    expect(await screen.findByRole('menuitem', { name: 'Release prep' })).toBeInTheDocument()
    expect(screen.queryByRole('menuitem', { name: 'Companion' })).toBeNull()
    expect(screen.getByRole('menuitem', { name: 'New session' })).toBeInTheDocument()
  })

  it('seeds the chosen session with the reference appended to its stored draft', async () => {
    saveDrafts({ 'chat-1': 'half-typed thought' })
    const { onSend } = setup([slot('chat-1', 'Release prep')])
    openMenu()
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
    openMenu()
    fireEvent.click(await screen.findByRole('menuitem', { name: 'New session' }))
    await waitFor(() => expect(onSend).toHaveBeenCalledTimes(1))
    expect(onSend.mock.calls[0][0]).toMatchObject({
      path: '/chat',
      slotKey: 'chat-new',
      prefill: { slotKey: 'chat-new', prompt: artifactReferencePrompt('CR Queue', 'cr-queue') },
    })
  })

  it('says so on the control when the new session cannot be created', async () => {
    vi.mocked(api).createChatSlot = vi.fn().mockRejectedValue(new Error('boom'))
    const { onSend } = setup([])
    openMenu()
    fireEvent.click(await screen.findByRole('menuitem', { name: 'New session' }))
    expect(await screen.findByRole('button', { name: /Could not start a session/ })).toBeInTheDocument()
    expect(onSend).not.toHaveBeenCalled()
  })
})
