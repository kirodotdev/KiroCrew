/**
 * System > Services must answer "is my bot up?" for EVERY channel, not just Slack.
 *
 * The gateway publishes `status.channels` — one `{ connected, error }` per channel
 * type, from the same flags each channel's own settings badge reads. Before this
 * page consumed it, a Telegram or Discord bot that failed to start left Services
 * showing a healthy page and one Slack row, so the operator had no surface that
 * said the bot never came up. These cases pin the render to the payload: delete the
 * per-channel rows and every one of them goes red.
 */
import { describe, it, expect, vi } from 'vitest'
import { screen, waitFor } from '@testing-library/react'

import { createTestStore, renderWithProviders } from './helpers'
import type { RootState, StatusData } from '../types'

vi.mock('../api/client', () => ({
  api: {
    system: () => Promise.resolve({
      hostname: 'test', os: 'linux', arch: 'x86_64', cpu_count: 8,
      load_1m: 0, load_5m: 0, load_15m: 0,
      ip: '127.0.0.1', net_rx_mb: 0, net_tx_mb: 0, net_rx_kbs: 0, net_tx_kbs: 0,
      python: '3.12', pid: 42, cwd: '/tmp',
      proc_mem_mb: 200, proc_cpu_pct: 1, child_processes: 0, thread_count: 10,
      mcp_total: 0,
    }),
    // The MCP gateway card self-hides when disabled, which keeps this file
    // scoped to the channel rows.
    mcpGatewayStatus: () => Promise.resolve({ enabled: false, running: false, ping_ok: false }),
    mcpGatewayMetrics: () => Promise.resolve({ running: false, backends: [] }),
  },
}))

import ServicesTab from '../pages/system/ServicesTab'

function renderWithStatus(status: Partial<StatusData>) {
  const base = createTestStore().getState() as RootState
  const store = createTestStore({
    dashboard: {
      ...base.dashboard,
      status: { uptime: '1m', sessions: 0, messages: 0, cron_jobs: 0, subagents: 0, lessons: 0, ...status },
    },
  } as Partial<RootState>)
  return renderWithProviders(<ServicesTab />, { store })
}

/** The whole row a label belongs to, so a status can be pinned to ITS channel
 *  rather than to whichever cell happens to say "Connected" first. */
function rowText(label: string): string {
  return screen.getByText(label).parentElement?.textContent ?? ''
}

describe('ServicesTab channel rows', () => {
  it('renders one row per channel the payload reports as connected', async () => {
    renderWithStatus({
      slack_connected: false,
      channels: {
        slack: { connected: false, error: '' },
        telegram: { connected: true, error: '' },
        discord: { connected: false, error: '' },
        webex: { connected: false, error: '' },
        wecom: { connected: false, error: '' },
        teams: { connected: true, error: '' },
        weixin: { connected: false, error: '' },
        imessage: { connected: false, error: '' },
      },
    })

    await waitFor(() => expect(screen.getByText('Telegram')).toBeTruthy())
    expect(rowText('Telegram')).toContain('Connected')
    expect(rowText('Microsoft Teams')).toContain('Connected')
    // Slack is the row this page has always carried, so it stays visible even
    // when it is the one channel that is down.
    expect(rowText('Slack')).toContain('Not connected')
  })

  it('surfaces a channel connect error rather than a bare "Not connected"', async () => {
    // The reason IS the feature: "Not connected" alone sends the operator to look
    // at their network when the gateway already knows the token was rejected.
    renderWithStatus({
      slack_connected: false,
      channels: {
        slack: { connected: true, error: '' },
        telegram: { connected: false, error: 'Unauthorized: bot token revoked' },
      },
    })

    await waitFor(() => expect(screen.getByText('Telegram')).toBeTruthy())
    const row = rowText('Telegram')
    expect(row).toContain('Not connected')
    expect(row).toContain('Unauthorized: bot token revoked')
    // The error is labelled, not just coloured — colour alone is not an
    // accessible name.
    expect(screen.getByLabelText('Connection error')).toBeTruthy()
  })

  it('hides a channel that is silent and reasonless, because that is what unconfigured looks like', async () => {
    // `{ connected: false, error: '' }` is indistinguishable from "never set up",
    // so rendering it would put seven dead rows on a Slack-only install — worse
    // than the silence this feature exists to fix.
    renderWithStatus({
      slack_connected: true,
      channels: {
        slack: { connected: true, error: '' },
        telegram: { connected: false, error: '' },
        discord: { connected: false, error: '' },
        webex: { connected: false, error: '' },
        wecom: { connected: false, error: '' },
        teams: { connected: false, error: '' },
        weixin: { connected: false, error: '' },
        imessage: { connected: false, error: '' },
      },
    })

    await waitFor(() => expect(screen.getByText('Slack')).toBeTruthy())
    expect(rowText('Slack')).toContain('Connected')
    for (const name of ['Telegram', 'Discord', 'Webex', 'WeCom', 'Microsoft Teams', 'WeChat', 'iMessage']) {
      expect(screen.queryByText(name), `${name} row should be hidden`).toBeNull()
    }
  })

  it('falls back to slack_connected when an older gateway sends no channels map', async () => {
    // Back-compat: an absent map is "no answer", never eight outages.
    renderWithStatus({ slack_connected: true })

    await waitFor(() => expect(screen.getByText('Slack')).toBeTruthy())
    expect(rowText('Slack')).toContain('Connected')
    expect(screen.queryByText('Telegram')).toBeNull()
    expect(screen.queryByLabelText('Connection error')).toBeNull()
  })

  it('falls back to slack_connected when the map omits slack', async () => {
    renderWithStatus({
      slack_connected: true,
      channels: { telegram: { connected: true, error: '' } },
    })

    await waitFor(() => expect(screen.getByText('Slack')).toBeTruthy())
    expect(rowText('Slack')).toContain('Connected')
    expect(rowText('Telegram')).toContain('Connected')
  })

  it('renders a row for every channel the gateway can report', async () => {
    // The page walks its own label map, not the payload's keys, so a channel the
    // backend grows without a label here is INVISIBLE on this page even while
    // connected or failing. Asserted per channel rather than as a count, since a
    // count passes just as happily when one name is swapped for another.
    //
    // This list is the RENDER contract and is hand-kept, which is a list that fails
    // open: WhatsApp was missed when the map ended at imessage, and Feishu was
    // missed again with this test already in place. The gate that catches the
    // omission is therefore cross-language and lives on the Python side, where the
    // roster is: `test_channel_readiness.py::TestTheServicesPageCoversEveryChannel`
    // parses this map out of the TSX and requires it to cover
    // `builtin_channel_descriptors()`. What THIS test still owns is that a labelled
    // channel actually renders a row.
    const CHANNELS = [
      ['slack', 'Slack'],
      ['telegram', 'Telegram'],
      ['discord', 'Discord'],
      ['webex', 'Webex'],
      ['teams', 'Microsoft Teams'],
      ['wecom', 'WeCom'],
      ['weixin', 'WeChat'],
      ['imessage', 'iMessage'],
      ['whatsapp', 'WhatsApp'],
      ['feishu', 'Feishu'],
    ] as const
    renderWithStatus({
      channels: Object.fromEntries(
        CHANNELS.map(([key]) => [key, { connected: true, error: '' }]),
      ),
    })

    await waitFor(() => expect(screen.getByText('Slack')).toBeTruthy())
    for (const [, label] of CHANNELS) {
      expect(rowText(label)).toContain('Connected')
    }
  })
})
