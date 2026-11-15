/**
 * Settings > Display > View: Channels' switch, which replaced turning the hidden
 * built-in app `channels` on and off. Like the app's Library card, it is there
 * only while Channels is on.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { screen, fireEvent, waitFor } from '@testing-library/react'
import { renderWithProviders } from '../../test/helpers'
import { APP_NAV_HIDDEN_KEY, readAppNavHidden } from '../../lib/appNavHidden'

// The gateway's config: a write lands in it, so the read after a save sees it.
const { patchConfigMock, kirocrewConfigMock, gateway } = vi.hoisted(() => {
  const gateway: { config: { dashboard?: Record<string, unknown> } } = { config: {} }
  return {
    gateway,
    patchConfigMock: vi.fn((_path: string, value: unknown) => {
      gateway.config = { dashboard: { ...gateway.config.dashboard, channels_enabled: value } }
      return Promise.resolve({})
    }),
    kirocrewConfigMock: vi.fn((): Promise<unknown> => Promise.resolve(gateway.config)),
  }
})
vi.mock('../../api/client', () => ({
  api: { kirocrewConfig: kirocrewConfigMock, patchConfig: patchConfigMock },
}))

import { ChannelsSetting } from './ChannelsSetting'

const toggle = () => screen.findByRole('switch', { name: 'Show Channels' })

describe('ChannelsSetting', () => {
  beforeEach(() => {
    localStorage.clear()
    patchConfigMock.mockClear()
    kirocrewConfigMock.mockClear()
  })

  it('a user who had Channels on sees it on, and can turn it off and back on', async () => {
    gateway.config = { dashboard: { channels_enabled: true } }
    renderWithProviders(<ChannelsSetting />)
    const row = await toggle()
    expect(row).toHaveAttribute('aria-checked', 'true')
    fireEvent.click(row)
    await waitFor(() => expect(patchConfigMock).toHaveBeenCalledWith('dashboard.channels_enabled', false))
    // Still drawn once off, so the change can be undone here.
    const again = await toggle()
    await waitFor(() => expect(again).toHaveAttribute('aria-checked', 'false'))
    fireEvent.click(again)
    await waitFor(() => expect(patchConfigMock).toHaveBeenCalledWith('dashboard.channels_enabled', true))
  })

  it('draws nothing while Channels is off, as Library listed nothing for it', async () => {
    gateway.config = { dashboard: { channels_enabled: false } }
    const { container } = renderWithProviders(<ChannelsSetting />)
    await waitFor(() => expect(kirocrewConfigMock).toHaveBeenCalled())
    expect(container.querySelector('[role="switch"]')).toBeNull()
  })

  it('turning it back on pins back a row unpinned from the sidebar', async () => {
    localStorage.setItem(APP_NAV_HIDDEN_KEY, JSON.stringify(['channels', 'mochi']))
    gateway.config = { dashboard: { channels_enabled: true } }
    renderWithProviders(<ChannelsSetting />)
    fireEvent.click(await toggle())
    await waitFor(() => expect(patchConfigMock).toHaveBeenCalledWith('dashboard.channels_enabled', false))
    fireEvent.click(await toggle())
    await waitFor(() => expect(patchConfigMock).toHaveBeenCalledWith('dashboard.channels_enabled', true))
    expect([...readAppNavHidden()]).toEqual(['mochi'])
  })

  it('draws nothing for a gateway whose config has no such key', async () => {
    gateway.config = { dashboard: {} }
    const { container } = renderWithProviders(<ChannelsSetting />)
    await waitFor(() => expect(kirocrewConfigMock).toHaveBeenCalled())
    expect(container.querySelector('[role="switch"]')).toBeNull()
  })
})
