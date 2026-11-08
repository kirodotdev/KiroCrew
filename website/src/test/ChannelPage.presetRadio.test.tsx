/**
 * The New Channel dialog's team preset picker says which preset is picked.
 *
 * The picker was a `role="radiogroup"` of plain buttons: the picked preset was
 * shown only by an accent border, so a screen reader heard N buttons and never
 * which one was chosen. It now uses the shared `RadioChipGroup`: each preset is
 * a radio with `aria-checked`, the group is one Tab stop, and arrow keys move
 * between presets.
 *
 * Mock conventions follow `ChannelPage.failedSave.test.tsx`: the api client is
 * the single seam, auto-mocked.
 */
import { describe, it, expect, vi, beforeEach, beforeAll } from 'vitest'
import { screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import ChannelPage from '../pages/ChannelPage'
import { renderWithProviders } from './helpers'
import { api } from '../api/client'

vi.mock('../api/client')

beforeAll(() => {
  // jsdom doesn't implement scrollIntoView
  Element.prototype.scrollIntoView = vi.fn()
})

beforeEach(() => {
  vi.clearAllMocks()
  vi.mocked(api).channelsList = vi.fn().mockResolvedValue({ channels: [] })
  // No `presets` key: the page keeps its built-in Incident Response / Custom pair.
  vi.mocked(api).channelPresets = vi.fn().mockResolvedValue({})
})

async function openDialog() {
  renderWithProviders(<ChannelPage />)
  await waitFor(() => expect(screen.queryByText('Loading channels...')).not.toBeInTheDocument())
  await userEvent.click(screen.getByRole('button', { name: '+ New' }))
  return screen.findByRole('dialog', { name: 'New channel' })
}

describe('New channel team preset picker', () => {
  it('marks the picked preset as checked and moves with the arrow keys', async () => {
    const dialog = await openDialog()
    const group = within(dialog).getByRole('radiogroup', { name: 'Team Preset' })
    const incident = within(group).getByRole('radio', { name: /Incident Response/ })
    const custom = within(group).getByRole('radio', { name: /Custom \(empty\)/ })

    // The first preset is the default pick, and the only Tab stop.
    expect(incident).toHaveAttribute('aria-checked', 'true')
    expect(custom).toHaveAttribute('aria-checked', 'false')
    expect(incident).toHaveAttribute('tabindex', '0')
    expect(custom).toHaveAttribute('tabindex', '-1')

    await userEvent.click(custom)
    expect(custom).toHaveAttribute('aria-checked', 'true')
    expect(incident).toHaveAttribute('aria-checked', 'false')

    // Arrow keys move focus between presets without picking one.
    await userEvent.keyboard('{ArrowDown}')
    expect(document.activeElement).toBe(incident)
    expect(custom).toHaveAttribute('aria-checked', 'true')
  })
})
