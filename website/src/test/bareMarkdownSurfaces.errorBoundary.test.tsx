import { describe, it, expect, vi, beforeEach, beforeAll } from 'vitest'
import { screen, waitFor } from '@testing-library/react'
import ChannelPage from '../pages/ChannelPage'
import NotificationDetailPanel from '../components/notifications/NotificationDetailPanel'
import { renderWithProviders } from './helpers'
import { api } from '../api/client'
import type { Notification } from '../types'

// Issue #10620, residual 3. The chat transcript wraps every message in
// `MessageErrorBoundary`, so a markdown render crash (a nesting spelling the
// depth bound does not model yet, an unknown HTML element) degrades ONE
// message. `ChannelPage` and `NotificationDetailPanel` rendered
// `MarkdownRenderer` bare, so the same crash there escalated to the app-level
// `ErrorBoundary` and replaced the whole view. These tests stand in for the
// next unmodeled spelling with a renderer that throws on a marker string, and
// assert the crash stays inside the one item: the boundary's fallback appears
// and the surrounding surface (sibling messages, the panel header) still
// renders.

const CRASH = '__crash_marker__'

vi.mock('../components/MarkdownRenderer', () => ({
  default: ({ content }: { content: string }) => {
    if (content.includes(CRASH)) throw new Error('simulated markdown render crash')
    return <span>{content}</span>
  },
  Lightbox: () => null,
}))

vi.mock('../api/client')

beforeAll(() => {
  Element.prototype.scrollIntoView = vi.fn()
})

beforeEach(() => {
  vi.clearAllMocks()
  // React logs the caught error and the boundary logs it again; both are
  // expected here and would only bury a real failure.
  vi.spyOn(console, 'error').mockImplementation(() => {})
})

describe('ChannelPage — message bodies are wrapped in MessageErrorBoundary', () => {
  const channel = {
    id: 'ch1',
    topic: 'Test Channel',
    members: {
      a1: { id: 'a1', role: 'Researcher', agent_name: 'kirocrew', state: 'listening', listen_mode: 'mention', approval_policy: 'writes', session_key: 'k1' },
    },
    messages: [
      { id: 'm1', from_id: 'a1', from_role: 'Researcher', content: 'healthy message before', msg_type: 'progress', timestamp: 1 },
      { id: 'm2', from_id: 'a1', from_role: 'Researcher', content: `poison ${CRASH}`, msg_type: 'progress', timestamp: 2 },
      { id: 'm3', from_id: 'a1', from_role: 'Researcher', content: 'healthy message after', msg_type: 'progress', timestamp: 3 },
    ],
  }

  beforeEach(() => {
    vi.mocked(api).channelsList = vi.fn().mockResolvedValue({ channels: [channel] })
    vi.mocked(api).channelGet = vi.fn().mockResolvedValue(channel)
    vi.mocked(api).channelPresets = vi.fn().mockResolvedValue({ presets: [] })
  })

  it('a crashing message body degrades to the per-message fallback; siblings still render', async () => {
    renderWithProviders(<ChannelPage />)
    await waitFor(() => expect(screen.getByText('healthy message before')).toBeInTheDocument())
    expect(screen.getByText('healthy message after')).toBeInTheDocument()
    expect(screen.getByText('Message failed to render')).toBeInTheDocument()
    // The channel header survived: the crash did not reach the app boundary.
    expect(screen.getAllByText('Test Channel').length).toBeGreaterThan(0)
  })
})

describe('NotificationDetailPanel — the body is wrapped in MessageErrorBoundary', () => {
  const n: Notification = {
    kind: 'agent',
    title: 'Poisoned notification',
    body: `poison ${CRASH}`,
    ts: '1700000000.000',
  }

  it('a crashing body degrades to the per-item fallback; the panel header still renders', () => {
    renderWithProviders(<NotificationDetailPanel n={n} onClose={() => {}} />)
    expect(screen.getByText('Message failed to render')).toBeInTheDocument()
    expect(screen.getByText('Poisoned notification')).toBeInTheDocument()
  })
})
