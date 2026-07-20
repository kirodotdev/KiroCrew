import { describe, it, expect, vi, beforeEach, beforeAll } from 'vitest'
import { screen, waitFor, fireEvent } from '@testing-library/react'
import ChannelPage from '../pages/ChannelPage'
import { renderWithProviders } from './helpers'
import { api } from '../api/client'

vi.mock('../api/client')

beforeAll(() => {
  // jsdom doesn't implement scrollIntoView
  Element.prototype.scrollIntoView = vi.fn()
})

const mockChannel = {
  id: 'ch1',
  topic: 'Test Channel',
  members: {
    a1: { id: 'a1', role: 'Researcher', agent_name: 'kirocrew', state: 'listening', listen_mode: 'mention', approval_policy: 'writes', session_key: 'k1' },
  },
  messages: [],
}

const PLACEHOLDER = 'Message the channel... (type @ to mention)'

describe('ChannelPage — IME composition Enter guard (Mesh-2693)', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    vi.mocked(api).channelsList = vi.fn().mockResolvedValue({ channels: [mockChannel] })
    vi.mocked(api).channelGet = vi.fn().mockResolvedValue(mockChannel)
    vi.mocked(api).channelPresets = vi.fn().mockResolvedValue({ presets: [] })
    vi.mocked(api).channelPost = vi.fn().mockResolvedValue({ ok: true })
  })

  async function composer() {
    renderWithProviders(<ChannelPage />)
    await waitFor(() => expect(screen.getByPlaceholderText(PLACEHOLDER)).toBeInTheDocument())
    return screen.getByPlaceholderText(PLACEHOLDER) as HTMLTextAreaElement
  }

  it('does NOT post on Enter while a CJK IME is composing', async () => {
    const ta = await composer()
    // User types a Chinese candidate; the final Enter commits the candidate.
    fireEvent.change(ta, { target: { value: '你好' } })
    fireEvent.compositionStart(ta)
    fireEvent.keyDown(ta, { key: 'Enter' })
    expect(vi.mocked(api).channelPost).not.toHaveBeenCalled()
  })

  it('DOES post on a plain Enter (no composition) — positive control', async () => {
    const ta = await composer()
    fireEvent.change(ta, { target: { value: 'hello' } })
    fireEvent.keyDown(ta, { key: 'Enter' })
    await waitFor(() =>
      expect(vi.mocked(api).channelPost).toHaveBeenCalledWith('ch1', 'hello', undefined, undefined),
    )
  })

  it('does NOT post on Shift+Enter (newline)', async () => {
    const ta = await composer()
    fireEvent.change(ta, { target: { value: 'line one' } })
    fireEvent.keyDown(ta, { key: 'Enter', shiftKey: true })
    expect(vi.mocked(api).channelPost).not.toHaveBeenCalled()
  })

  // @mention-picker branch: pressing Enter with the mention menu open normally
  // selects the highlighted agent (pick), but must be suppressed while a CJK IME
  // is composing so the Enter only commits the candidate.
  async function openMentionMenu() {
    const ta = await composer()
    fireEvent.change(ta, { target: { value: '@' } })
    await waitFor(() =>
      expect(screen.getByRole('listbox', { name: 'Mention suggestions' })).toBeInTheDocument(),
    )
    return ta
  }

  it('does NOT pick a mention on Enter while a CJK IME is composing', async () => {
    const ta = await openMentionMenu()
    fireEvent.compositionStart(ta)
    fireEvent.keyDown(ta, { key: 'Enter' })
    // pick() would rewrite the value to "@Researcher "; while composing it must not fire.
    expect(ta.value).toBe('@')
    expect(vi.mocked(api).channelPost).not.toHaveBeenCalled()
  })

  it('DOES pick a mention on a plain Enter (no composition) — positive control', async () => {
    const ta = await openMentionMenu()
    fireEvent.keyDown(ta, { key: 'Enter' })
    // Enter selects the highlighted agent (does not send the message).
    await waitFor(() => expect(ta.value).toContain('Researcher'))
    expect(vi.mocked(api).channelPost).not.toHaveBeenCalled()
  })
})
