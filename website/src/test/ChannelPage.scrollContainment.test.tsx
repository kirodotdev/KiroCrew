/**
 * The Channels page keeps its scrolling inside the message list.
 *
 * Following a new message with `scrollIntoView` on an end sentinel scrolls
 * EVERY scrollable ancestor, and a second `overflow-y-auto` wrapper with bottom
 * padding gives the page one: a reply then scrolls the page past the composer
 * and the user has to scroll back up to find the input and the conversation.
 * Only the message list may scroll; the wrapper and channel layout clip.
 *
 * Mock conventions follow `ChannelPageCoverage.test.tsx`: the api client is the
 * single seam, auto-mocked.
 */
import { describe, it, expect, vi, beforeEach, beforeAll } from 'vitest'
import { screen, waitFor, act } from '@testing-library/react'
import ChannelPage from '../pages/ChannelPage'
import { renderWithProviders } from './helpers'
import { api } from '../api/client'

vi.mock('../api/client')

const scrollIntoView = vi.fn()
beforeAll(() => {
  // jsdom implements neither scrollIntoView nor Element.scrollTo.
  Element.prototype.scrollIntoView = scrollIntoView
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

/** The scroller that owns the messages: the nearest `overflow-y-auto` ancestor of a bubble. */
async function messageList(): Promise<HTMLElement> {
  const bubble = await screen.findByText('Message 2')
  const list = bubble.closest('.overflow-y-auto')
  expect(list).not.toBeNull()
  return list as HTMLElement
}

describe('ChannelPage — scrolling stays inside the message list', () => {
  it('follows a new message by scrolling the list, not the page', async () => {
    renderWithProviders(<ChannelPage />)
    const list = await messageList()
    const scrollTo = vi.fn()
    Object.defineProperty(list, 'scrollTo', { value: scrollTo, configurable: true })
    Object.defineProperty(list, 'scrollHeight', { value: 4321, configurable: true })

    act(() => {
      window.dispatchEvent(new CustomEvent('kirocrew-channel', {
        detail: { type: 'channel_message', data: { channel_id: 'ch1', message: message(3) } },
      }))
    })

    await screen.findByText('Message 3')
    await waitFor(() => expect(scrollTo).toHaveBeenCalledWith({ top: 4321, behavior: 'smooth' }))
    // A sentinel's scrollIntoView would also have scrolled every scrollable ancestor.
    expect(scrollIntoView).not.toHaveBeenCalled()
  })

  it('gives the page no second scroll container around the channel', async () => {
    renderWithProviders(<ChannelPage />)
    const list = await messageList()
    const scrollers: Element[] = []
    for (let el = list.parentElement; el; el = el.parentElement) {
      if (el.classList.contains('overflow-y-auto') || el.classList.contains('overflow-auto')) scrollers.push(el)
    }
    expect(scrollers).toEqual([])
    // The route wrapper clips and the channel layout is a flex child that
    // shrinks, so the list is the only element with room to scroll.
    const wrapper = screen.getByText('Gamma rollout', { selector: 'h2' }).closest('.flex-col.flex-1.min-h-0.overflow-hidden')
    expect(wrapper).not.toBeNull()
    expect(wrapper?.querySelector('.h-full')).toBeNull()
  })
})
