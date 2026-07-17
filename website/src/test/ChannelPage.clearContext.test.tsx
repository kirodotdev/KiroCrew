import { describe, it, expect, vi, beforeEach, beforeAll } from 'vitest'
import { screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
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

describe('ChannelPage — Clear Context', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    vi.mocked(api).channelsList = vi.fn().mockResolvedValue({ channels: [mockChannel] })
    vi.mocked(api).channelGet = vi.fn().mockResolvedValue(mockChannel)
    vi.mocked(api).channelPresets = vi.fn().mockResolvedValue({ presets: [] })
    vi.mocked(api).channelClearContext = vi.fn().mockResolvedValue({ ok: true, cleared: ['Researcher'] })
  })

  it('renders Clear Context button in channel header', async () => {
    renderWithProviders(<ChannelPage />)
    await waitFor(() => expect(screen.getByTitle('Clear all context')).toBeInTheDocument())
  })

  it('calls channelClearContext with scope=all on confirm', async () => {
    vi.spyOn(window, 'confirm').mockReturnValue(true)
    renderWithProviders(<ChannelPage />)
    await waitFor(() => expect(screen.getByTitle('Clear all context')).toBeInTheDocument())
    await userEvent.click(screen.getByTitle('Clear all context'))
    await waitFor(() => expect(vi.mocked(api).channelClearContext).toHaveBeenCalledWith('ch1', 'all'))
  })

  it('does not call API when confirm is cancelled', async () => {
    vi.spyOn(window, 'confirm').mockReturnValue(false)
    renderWithProviders(<ChannelPage />)
    await waitFor(() => expect(screen.getByTitle('Clear all context')).toBeInTheDocument())
    await userEvent.click(screen.getByTitle('Clear all context'))
    expect(vi.mocked(api).channelClearContext).not.toHaveBeenCalled()
  })

  it('re-fetches channel data after successful clear', async () => {
    vi.spyOn(window, 'confirm').mockReturnValue(true)
    renderWithProviders(<ChannelPage />)
    await waitFor(() => screen.getByTitle('Clear all context'))
    vi.mocked(api).channelGet.mockClear()  // ignore the initial-render fetch
    await userEvent.click(screen.getByTitle('Clear all context'))
    await waitFor(() => expect(vi.mocked(api).channelGet).toHaveBeenCalledWith('ch1'))
  })

  it('shows alert on API failure', async () => {
    vi.spyOn(window, 'confirm').mockReturnValue(true)
    vi.spyOn(window, 'alert').mockImplementation(() => {})
    vi.mocked(api).channelClearContext = vi.fn().mockRejectedValue(new Error('server error'))
    renderWithProviders(<ChannelPage />)
    await waitFor(() => screen.getByTitle('Clear all context'))
    await userEvent.click(screen.getByTitle('Clear all context'))
    await waitFor(() => expect(window.alert).toHaveBeenCalledWith(expect.stringContaining('server error')))
  })

  it('clears a single agent via the agents panel with scope=agent', async () => {
    vi.spyOn(window, 'confirm').mockReturnValue(true)
    renderWithProviders(<ChannelPage />)
    await waitFor(() => screen.getByRole('button', { name: '1 agent' }))
    await userEvent.click(screen.getByRole('button', { name: '1 agent' }))  // open agents sidebar
    await waitFor(() => screen.getByTitle('Clear context'))
    await userEvent.click(screen.getByTitle('Clear context'))
    await waitFor(() => expect(vi.mocked(api).channelClearContext).toHaveBeenCalledWith('ch1', 'agent', 'a1'))
  })
})
