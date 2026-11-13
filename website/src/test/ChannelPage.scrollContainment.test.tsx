/**
 * A new channel message scrolls the message list, never its ancestors:
 * `scrollIntoView` on an end-of-list sentinel scrolls every scrollable box
 * around it, which dragged the page past the composer.
 */
import { describe, it, expect, vi, beforeEach, beforeAll } from 'vitest'
import { screen, waitFor, act } from '@testing-library/react'
import ChannelPage from '../pages/ChannelPage'
import { renderWithProviders } from './helpers'
import { api } from '../api/client'

vi.mock('../api/client')

beforeAll(() => {
  Element.prototype.scrollIntoView = vi.fn()
})

type Raw = Record<string, unknown>

const message = (i: number): Raw => ({
  id: `m${i}`, from_id: 'a1', from_role: 'Researcher', content: `Message ${i}`,
  msg_type: 'broadcast', timestamp: 1_700_000_000 + i, reply_count: 0,
})

const CHANNEL: Raw = {
  id: 'ch1', topic: 'Gamma rollout',
  members: { a1: { id: 'a1', role: 'Researcher', agent_name: 'kirocrew', state: 'listening', listen_mode: 'mention', approval_policy: 'writes' } },
  messages: [message(1), message(2)],
}

beforeEach(() => {
  vi.clearAllMocks()
  vi.mocked(api).channelsList = vi.fn().mockResolvedValue({ channels: [CHANNEL] })
  vi.mocked(api).channelGet = vi.fn().mockResolvedValue(CHANNEL)
  vi.mocked(api).channelPresets = vi.fn().mockResolvedValue({})
  vi.mocked(api).kirocrewAgents = vi.fn().mockResolvedValue({ agents: [], default_agent: 'kirocrew' })
  vi.mocked(api).agentCatalog = vi.fn().mockResolvedValue({ agents: [], default_agent: 'kirocrew' })
})

describe('ChannelPage — following new messages', () => {
  it('scrolls the message list itself, never its ancestors', async () => {
    renderWithProviders(<ChannelPage />)
    const list = (await screen.findByText('Message 2')).closest('.overflow-y-auto') as HTMLElement
    expect(list).not.toBeNull()
    const scrollTo = vi.spyOn(list, 'scrollTo')
    Object.defineProperty(list, 'scrollHeight', { value: 4321, configurable: true })

    act(() => {
      window.dispatchEvent(new CustomEvent('kirocrew-channel', {
        detail: { type: 'channel_message', data: { channel_id: 'ch1', message: message(3) } },
      }))
    })

    await screen.findByText('Message 3')
    await waitFor(() => expect(scrollTo).toHaveBeenCalledWith({ top: 4321, behavior: 'smooth' }))
    expect(Element.prototype.scrollIntoView).not.toHaveBeenCalled()
  })
})
