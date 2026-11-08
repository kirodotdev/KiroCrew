// Settings > Notifications > Sources: each channel row's "Deliver to" route
// (notification bridge phase B3). The row offers the chat apps the gateway
// reports connected, keeps an app already in the route even after it
// disconnects, shows the priority floor once an app is picked, and saves
// through the existing channel settings PUT.
import { describe, it, expect, beforeEach, vi } from 'vitest'
import { fireEvent, screen, waitFor, within, act } from '@testing-library/react'
import { NotificationsPanel } from '../pages/settings/NotificationsPanel'
import { __resetForTests } from '../hooks/useNotificationSound'
import { sseStatus } from '../store/dashboardSlice'
import { createTestStore, renderWithProviders } from './helpers'
import type { NotificationChannel, StatusData } from '../types'

const mockChannels = vi.fn()
const mockUpdate = vi.fn().mockResolvedValue({})

vi.mock('../api/client', () => ({
  api: {
    notificationChannels: (...args: unknown[]) => mockChannels(...args),
    updateNotificationChannelSettings: (...args: unknown[]) => mockUpdate(...args),
  },
}))

const channel = (id: string, settings: NotificationChannel['settings'] = {}, protectedRow = false): NotificationChannel => ({
  channel: id, source: 'system', registered: true, default_priority: 'default', protected: protectedRow, settings,
})

const STATUS = {
  slack_connected: true,
  channels: {
    slack: { connected: true, error: '' },
    telegram: { connected: true, error: '' },
    discord: { connected: false, error: '' },
    // Connected but not a bridge transport: never offered.
    teams: { connected: true, error: '' },
  },
} as unknown as StatusData

function renderPanel(status: StatusData | null = STATUS) {
  const store = createTestStore()
  if (status) store.dispatch(sseStatus(status))
  return { store, ...renderWithProviders(<NotificationsPanel />, { store, route: '/settings?tab=notifications&sub=sources' }) }
}

// The transports the bridge has a sink for. Webex has none, so a connected Webex
// is never offered.
const DELIVERABLE = ['slack', 'discord', 'telegram']

const routeGroup = (name: string) => screen.getByRole('group', { name: `Deliver ${name} notifications to` })

beforeEach(() => {
  localStorage.clear()
  __resetForTests()
  mockChannels.mockReset()
  mockUpdate.mockClear()
})

describe('NotificationsPanel bridge route', () => {
  it('offers only the connected bridge transports on each row', async () => {
    mockChannels.mockResolvedValue({ channels: [channel('system.cron')], bridge_transports: DELIVERABLE })
    renderPanel()
    await screen.findByText('cron')
    const group = routeGroup('cron')
    expect(within(group).getByRole('checkbox', { name: 'Slack' })).not.toBeChecked()
    expect(within(group).getByRole('checkbox', { name: 'Telegram' })).not.toBeChecked()
    expect(within(group).queryByRole('checkbox', { name: /Discord/ })).toBeNull()
    expect(within(group).queryByRole('checkbox', { name: /Teams/ })).toBeNull()
    // No floor picker until a transport is picked.
    expect(screen.queryByRole('combobox', { name: 'Lowest priority to deliver for cron' })).toBeNull()
  })

  it('saves a picked transport through the channel settings PUT', async () => {
    mockChannels.mockResolvedValue({ channels: [channel('system.cron')], bridge_transports: DELIVERABLE })
    renderPanel()
    await screen.findByText('cron')
    fireEvent.click(within(routeGroup('cron')).getByRole('checkbox', { name: 'Telegram' }))
    await waitFor(() => expect(mockUpdate).toHaveBeenCalledWith('system.cron', { deliver_to: ['telegram'] }))
  })

  it('shows the floor once a transport is picked, and saves a new floor', async () => {
    mockChannels.mockResolvedValue({ channels: [channel('system.cron', { deliver_to: ['slack'] })], bridge_transports: DELIVERABLE })
    renderPanel()
    await screen.findByText('cron')
    expect(within(routeGroup('cron')).getByRole('checkbox', { name: 'Slack' })).toBeChecked()
    const floor = screen.getByRole('combobox', { name: 'Lowest priority to deliver for cron' })
    // A route saved without a floor runs at the backend default, critical.
    expect(floor).toHaveTextContent('Critical only')
    fireEvent.click(floor)
    fireEvent.click(await screen.findByRole('option', { name: 'Every priority' }))
    await waitFor(() => expect(mockUpdate).toHaveBeenCalledWith('system.cron', { deliver_min_priority: 'all' }))
  })

  it('clears the route, floor included, when the last transport is unpicked', async () => {
    mockChannels.mockResolvedValue({ channels: [channel('system.cron', { deliver_to: ['slack'], deliver_min_priority: 'default' })], bridge_transports: DELIVERABLE })
    // After the save the server holds no route, floor included, which is what
    // the refetch then reads back.
    mockUpdate.mockImplementationOnce(async () => {
      mockChannels.mockResolvedValue({ channels: [channel('system.cron')], bridge_transports: DELIVERABLE })
      return {}
    })
    renderPanel()
    await screen.findByText('cron')
    expect(screen.getByRole('combobox', { name: 'Lowest priority to deliver for cron' })).toBeInTheDocument()
    fireEvent.click(within(routeGroup('cron')).getByRole('checkbox', { name: 'Slack' }))
    await waitFor(() => expect(mockUpdate).toHaveBeenCalledWith('system.cron', { deliver_to: [] }))
    await waitFor(() => expect(screen.queryByRole('combobox', { name: 'Lowest priority to deliver for cron' })).toBeNull())
    expect(within(routeGroup('cron')).getByRole('checkbox', { name: 'Slack' })).not.toBeChecked()
  })

  it('rolls a refused pick back at once, before any refetch lands', async () => {
    mockChannels.mockResolvedValue({ channels: [channel('system.cron', { deliver_to: ['slack'] })], bridge_transports: DELIVERABLE })
    mockUpdate.mockImplementationOnce(async () => {
      // The refetch after the failure never answers, so only the rollback
      // itself can restore the row.
      mockChannels.mockImplementation(() => new Promise(() => {}))
      throw new Error('refused')
    })
    renderPanel()
    await screen.findByText('cron')
    fireEvent.click(within(routeGroup('cron')).getByRole('checkbox', { name: 'Telegram' }))
    await waitFor(() => expect(mockUpdate).toHaveBeenCalledWith('system.cron', { deliver_to: ['slack', 'telegram'] }))
    // onSettled awaits the refetch, so the error banner waits on it too; the
    // rollback in onError is what must already have restored the row.
    await waitFor(() => expect(within(routeGroup('cron')).getByRole('checkbox', { name: 'Telegram' })).not.toBeChecked())
    expect(within(routeGroup('cron')).getByRole('checkbox', { name: 'Slack' })).toBeChecked()
  })

  it('says a refused save failed', async () => {
    mockChannels.mockResolvedValue({ channels: [channel('system.cron')], bridge_transports: DELIVERABLE })
    mockUpdate.mockRejectedValueOnce(new Error('refused'))
    renderPanel()
    await screen.findByText('cron')
    fireEvent.click(within(routeGroup('cron')).getByRole('checkbox', { name: 'Slack' }))
    expect(await screen.findByText(/Failed to save the channel setting/)).toBeInTheDocument()
    expect(within(routeGroup('cron')).getByRole('checkbox', { name: 'Slack' })).not.toBeChecked()
  })

  it('does not offer a connected app the bridge has no sink for', async () => {
    mockChannels.mockResolvedValue({ channels: [channel('system.cron')], bridge_transports: DELIVERABLE })
    renderPanel({ channels: { webex: { connected: true, error: '' }, slack: { connected: true, error: '' } } } as unknown as StatusData)
    await screen.findByText('cron')
    expect(within(routeGroup('cron')).queryByRole('checkbox', { name: /Webex/ })).toBeNull()
    expect(within(routeGroup('cron')).getByRole('checkbox', { name: 'Slack' })).toBeInTheDocument()
  })

  it('keeps a stored pick the bridge has no sink for, marked unavailable', async () => {
    mockChannels.mockResolvedValue({ channels: [channel('system.cron', { deliver_to: ['webex'] })], bridge_transports: DELIVERABLE })
    renderPanel({ channels: { webex: { connected: true, error: '' } } } as unknown as StatusData)
    await screen.findByText('cron')
    expect(within(routeGroup('cron')).getByRole('checkbox', { name: 'Webex (unavailable)' })).toBeChecked()
  })

  it('offers nothing when the gateway names no deliverable transports', async () => {
    mockChannels.mockResolvedValue({ channels: [channel('system.cron')] })
    renderPanel()
    await screen.findByText('cron')
    expect(screen.queryByRole('group', { name: 'Deliver cron notifications to' })).toBeNull()
  })

  it('holds a second pick while the first route save is in flight', async () => {
    // A route is written whole, so two overlapping saves can land out of order
    // and the earlier list would undo the later one on the server.
    mockChannels.mockResolvedValue({ channels: [channel('system.cron')], bridge_transports: DELIVERABLE })
    let finish!: () => void
    mockUpdate.mockImplementationOnce(() => new Promise(resolve => { finish = () => resolve({}) }))
    renderPanel()
    await screen.findByText('cron')
    fireEvent.click(within(routeGroup('cron')).getByRole('checkbox', { name: 'Slack' }))
    await waitFor(() => expect(routeGroup('cron')).toHaveAttribute('aria-busy', 'true'))
    const telegram = within(routeGroup('cron')).getByRole('checkbox', { name: 'Telegram' })
    expect(telegram).toHaveAttribute('aria-disabled', 'true')
    fireEvent.click(telegram)
    expect(mockUpdate).toHaveBeenCalledTimes(1)
    // Once the save and its refetch settle, the next pick goes out with the
    // whole route the server now holds.
    mockChannels.mockResolvedValue({ channels: [channel('system.cron', { deliver_to: ['slack'] })], bridge_transports: DELIVERABLE })
    await act(async () => { finish() })
    await waitFor(() => expect(routeGroup('cron')).not.toHaveAttribute('aria-busy'))
    fireEvent.click(within(routeGroup('cron')).getByRole('checkbox', { name: 'Telegram' }))
    await waitFor(() => expect(mockUpdate).toHaveBeenLastCalledWith('system.cron', { deliver_to: ['slack', 'telegram'] }))
  })

  it('reads a hand-edited, non-list route as no route instead of crashing', async () => {
    // The backend loads stored settings leniently and treats this as unrouted.
    mockChannels.mockResolvedValue({
      channels: [channel('system.cron', { deliver_to: 'slack' as unknown as string[] })],
      bridge_transports: 'slack' as unknown as string[],
    })
    renderPanel()
    await screen.findByText('cron')
    expect(screen.queryByRole('group', { name: 'Deliver cron notifications to' })).toBeNull()
  })

  it('keeps a picked transport that disconnected, marked unavailable', async () => {
    mockChannels.mockResolvedValue({ channels: [channel('system.cron', { deliver_to: ['discord'] })], bridge_transports: DELIVERABLE })
    renderPanel()
    await screen.findByText('cron')
    const kept = within(routeGroup('cron')).getByRole('checkbox', { name: 'Discord (unavailable)' })
    expect(kept).toBeChecked()
  })

  it('offers the route on the protected approval channel too', async () => {
    mockChannels.mockResolvedValue({ channels: [channel('system.approval', {}, true)], bridge_transports: DELIVERABLE })
    renderPanel()
    await screen.findByText('approval')
    expect(within(routeGroup('approval')).getByRole('checkbox', { name: 'Slack' })).toBeInTheDocument()
  })

  it('draws no route when no bridge transport is connected and none is picked', async () => {
    mockChannels.mockResolvedValue({ channels: [channel('system.cron')], bridge_transports: DELIVERABLE })
    renderPanel(null)
    await screen.findByText('cron')
    expect(screen.queryByRole('group', { name: 'Deliver cron notifications to' })).toBeNull()
  })

  it('follows a transport that connects while the page is open', async () => {
    mockChannels.mockResolvedValue({ channels: [channel('system.cron')], bridge_transports: DELIVERABLE })
    const { store } = renderPanel(null)
    await screen.findByText('cron')
    expect(screen.queryByRole('group', { name: 'Deliver cron notifications to' })).toBeNull()
    act(() => { store.dispatch(sseStatus({ channels: { telegram: { connected: true, error: '' } } } as unknown as StatusData)) })
    expect(within(routeGroup('cron')).getByRole('checkbox', { name: 'Telegram' })).toBeInTheDocument()
  })
})
