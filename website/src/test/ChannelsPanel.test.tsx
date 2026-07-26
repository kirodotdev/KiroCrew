/**
 * ChannelsPanel — responsive list-detail over the five chat integrations.
 *
 * Wide content area (>= 760px): persistent channel list + detail pane side by
 * side, first channel selected by default. Narrow: list only; picking a
 * channel swaps to a full-width detail view with a back button. Selection is
 * URL-backed via ?channel=<key>.
 *
 * Width is driven by useContainerWidth (ResizeObserver) — mocked here since
 * happy-dom reports zero layout sizes.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, fireEvent, act } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { useEffect } from 'react'

let slackMountCount = 0

vi.mock('../pages/settings/SlackPanel', () => ({
  SlackPanel: () => {
    // Mount counter proves instance SURVIVAL across layout transitions —
    // testid presence alone can't distinguish a remount (which would discard
    // unsaved form drafts) from a preserved instance.
    useEffect(() => { slackMountCount += 1 }, [])
    return <div data-testid="slack-panel" />
  },
}))
vi.mock('../pages/settings/DiscordPanel', () => ({ DiscordPanel: () => <div data-testid="discord-panel" /> }))
vi.mock('../pages/settings/TelegramPanel', () => ({ TelegramPanel: () => <div data-testid="telegram-panel" /> }))
vi.mock('../pages/settings/WebexPanel', () => ({ WebexPanel: () => <div data-testid="webex-panel" /> }))
vi.mock('../pages/settings/WeComPanel', () => ({ WeComPanel: () => <div data-testid="wecom-panel" /> }))

vi.mock('../api/client', () => ({
  api: {
    getSlackConfig: vi.fn().mockResolvedValue({ connected: true, configured: true }),
    getDiscordConfig: vi.fn().mockResolvedValue({ connected: false, configured: true }),
    getTelegramConfig: vi.fn().mockResolvedValue({ connected: false, configured: false }),
    getWebexConfig: vi.fn().mockResolvedValue({ connected: false, configured: false }),
    getWeComConfig: vi.fn().mockRejectedValue(new Error('boom')),
  },
}))

// Width control: each test sets mockWidth before render.
let mockWidth: number | null = null
vi.mock('../hooks/useContainerWidth', () => ({
  useContainerWidth: () => [{ current: null }, mockWidth],
}))

import { ChannelsPanel } from '../pages/settings/ChannelsPanel'

function makeUi(route = '/settings?tab=channels') {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  const ui = () => (
    <QueryClientProvider client={qc}>
      <MemoryRouter initialEntries={[route]}>
        <ChannelsPanel />
      </MemoryRouter>
    </QueryClientProvider>
  )
  return ui
}

function renderAt(route = '/settings?tab=channels') {
  const ui = makeUi(route)
  const view = render(ui())
  return { ...view, ui }
}

beforeEach(() => { mockWidth = null; slackMountCount = 0 })

describe('ChannelsPanel — wide (two-pane)', () => {
  it('shows the list and the first channel detail by default', () => {
    mockWidth = 1000
    renderAt()
    expect(screen.getByRole('listbox', { name: 'Chat channels' })).toBeInTheDocument()
    expect(screen.getByTestId('slack-panel')).toBeInTheDocument()
  })

  it('honors ?channel= selection', () => {
    mockWidth = 1000
    renderAt('/settings?tab=channels&channel=webex')
    expect(screen.getByTestId('webex-panel')).toBeInTheDocument()
    expect(screen.queryByTestId('slack-panel')).not.toBeInTheDocument()
  })

  it('switches the detail pane when a row is clicked and keeps the list visible', () => {
    mockWidth = 1000
    renderAt()
    fireEvent.click(screen.getByRole('option', { name: /Telegram/ }))
    expect(screen.getByTestId('telegram-panel')).toBeInTheDocument()
    expect(screen.getByRole('listbox', { name: 'Chat channels' })).toBeInTheDocument()
    expect(screen.queryByTestId('slack-panel')).not.toBeInTheDocument()
  })

  it('treats unmeasured width (null) as wide to avoid a narrow flash', () => {
    mockWidth = null
    renderAt()
    expect(screen.getByTestId('slack-panel')).toBeInTheDocument()
  })

  it('marks the selected row aria-selected', () => {
    mockWidth = 1000
    renderAt('/settings?tab=channels&channel=discord')
    expect(screen.getByRole('option', { name: /Discord/ })).toHaveAttribute('aria-selected', 'true')
    expect(screen.getByRole('option', { name: /Slack/ })).toHaveAttribute('aria-selected', 'false')
  })

  it('shows per-channel connection status from the config endpoints', async () => {
    mockWidth = 1000
    renderAt()
    expect(await screen.findByText('Connected')).toBeInTheDocument()       // slack
    expect(await screen.findByText('Not connected')).toBeInTheDocument()   // discord
    expect((await screen.findAllByText('Needs setup')).length).toBe(2)     // telegram, webex
    expect(await screen.findByText('Status unavailable')).toBeInTheDocument() // wecom (fetch error)
  })
})

describe('ChannelsPanel — narrow (list <-> detail)', () => {
  it('shows only the list when nothing is selected', () => {
    mockWidth = 500
    renderAt()
    expect(screen.getByRole('listbox', { name: 'Chat channels' })).toBeInTheDocument()
    expect(screen.queryByTestId('slack-panel')).not.toBeInTheDocument()
  })

  it('drills into a full-width detail with a back button on row click', () => {
    mockWidth = 500
    renderAt()
    fireEvent.click(screen.getByRole('option', { name: /Discord/ }))
    expect(screen.getByTestId('discord-panel')).toBeInTheDocument()
    expect(screen.queryByRole('listbox', { name: 'Chat channels' })).not.toBeInTheDocument()
    expect(screen.getByRole('button', { name: /Channels/ })).toBeInTheDocument()
  })

  it('back button returns to the list and clears the selection', () => {
    mockWidth = 500
    renderAt('/settings?tab=channels&channel=discord')
    fireEvent.click(screen.getByRole('button', { name: /Channels/ }))
    expect(screen.getByRole('listbox', { name: 'Chat channels' })).toBeInTheDocument()
    expect(screen.queryByTestId('discord-panel')).not.toBeInTheDocument()
  })

  it('ignores an invalid ?channel= value and shows the list', () => {
    mockWidth = 500
    renderAt('/settings?tab=channels&channel=nonsense')
    expect(screen.getByRole('listbox', { name: 'Chat channels' })).toBeInTheDocument()
  })
})

describe('ChannelsPanel — width transitions preserve the mounted panel', () => {
  it('keeps the SAME panel instance mounted when the container narrows (draft preservation)', () => {
    // Wide mount with NO explicit ?channel: Slack renders implicitly and the
    // canonicalization effect writes channel=slack into the URL.
    mockWidth = 1000
    const { rerender, ui } = renderAt('/settings?tab=channels')
    expect(screen.getByTestId('slack-panel')).toBeInTheDocument()
    expect(slackMountCount).toBe(1)

    // Shrink below the two-pane breakpoint. The URL param persisted AND the
    // panel wrapper sits at a stable tree position in both layouts, so the
    // SAME instance stays mounted (mount effect must NOT run again) — unsaved
    // form state survives. The layout just gains a back button.
    mockWidth = 500
    rerender(ui())
    expect(screen.getByTestId('slack-panel')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: /Channels/ })).toBeInTheDocument()
    expect(slackMountCount).toBe(1)

    // And growing back to two-pane keeps it alive too.
    mockWidth = 1000
    rerender(ui())
    expect(screen.getByTestId('slack-panel')).toBeInTheDocument()
    expect(slackMountCount).toBe(1)
  })

  it('does NOT canonicalize before the first real measurement (fresh narrow visit shows the list)', () => {
    // Pre-measurement paint (width=null) optimistically renders wide, but the
    // URL write must wait for a real measurement — otherwise a narrow visit
    // would land on Slack instead of the channel list.
    mockWidth = null
    const { rerender, ui } = renderAt('/settings?tab=channels')
    // First real measurement says narrow: the list must show, meaning no
    // channel param was stamped during the null-width paint.
    mockWidth = 500
    rerender(ui())
    expect(screen.getByRole('listbox', { name: 'Chat channels' })).toBeInTheDocument()
    expect(screen.queryByTestId('slack-panel')).not.toBeInTheDocument()
  })
})

describe('ChannelsPanel — status polling', () => {
  it('re-fetches channel configs on the 30s refetch interval', async () => {
    vi.useFakeTimers()
    try {
      mockWidth = 1000
      renderAt()
      const { api } = await import('../api/client')
      const initial = (api.getDiscordConfig as ReturnType<typeof vi.fn>).mock.calls.length
      await act(async () => { await vi.advanceTimersByTimeAsync(31_000) })
      const after = (api.getDiscordConfig as ReturnType<typeof vi.fn>).mock.calls.length
      expect(after).toBeGreaterThan(initial)
    } finally {
      vi.useRealTimers()
    }
  })
})
