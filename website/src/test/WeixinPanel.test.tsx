import { describe, it, expect, vi, beforeEach } from 'vitest'
import { screen, fireEvent, waitFor } from '@testing-library/react'
import { renderWithProviders } from './helpers'
import { WeixinPanel } from '../pages/settings/WeixinPanel'
import { api, type WeixinConfigData } from '../api/client'

/**
 * WeChat channel panel — DM-policy picker.
 *
 * Covers the native-<select> → SimpleSelect migration for the "Who can message
 * the bot" control. The panel's Playwright drive
 * (`scripts/test-weixin-panel.mjs`) exercises the same step, but only against a
 * built `dist/`; this keeps the contract under the unit suite too.
 *
 * The picker is a Radix Select, so a `change` event on the trigger does nothing —
 * open it, then click the option.
 */

const CONFIG: WeixinConfigData = {
  connected: true,
  connect_error: '',
  configured: true,
  read_only: false,
  credential_set: true,
  enabled: true,
  account_id: 'a5ace6fd482e@im.bot',
  dm_policy: 'open',
  allowed_user_ids: [],
}

beforeEach(() => {
  vi.restoreAllMocks()
  vi.spyOn(api, 'getWeixinConfig').mockResolvedValue({ ...CONFIG })
})

describe('WeixinPanel — DM policy picker', () => {
  it('labels the trigger from the field caption and shows the saved policy', async () => {
    renderWithProviders(<WeixinPanel />)
    const trigger = await screen.findByRole('combobox', { name: 'Who can message the bot' })
    // The trigger renders before the config query settles (falling back to the
    // 'allowlist' default), so the saved value is an async assertion.
    await waitFor(() => expect(trigger).toHaveTextContent('Anyone who messages the bot'))
  })

  it('selecting a policy PUTs the new value', async () => {
    const save = vi
      .spyOn(api, 'saveWeixinConfig')
      .mockResolvedValue({ ok: true, restart_required: false })
    renderWithProviders(<WeixinPanel />)

    fireEvent.click(await screen.findByRole('combobox', { name: 'Who can message the bot' }))
    fireEvent.click(await screen.findByRole('option', { name: 'Only allowed user IDs' }))

    await waitFor(() => expect(save).toHaveBeenCalledWith({ dm_policy: 'allowlist' }))
  })

  it('disables the picker when the config is read-only', async () => {
    vi.spyOn(api, 'getWeixinConfig').mockResolvedValue({ ...CONFIG, read_only: true })
    renderWithProviders(<WeixinPanel />)
    const trigger = await screen.findByRole('combobox', { name: 'Who can message the bot' })
    await waitFor(() => expect(trigger).toBeDisabled())
  })

  it('lists every policy in the popup', async () => {
    renderWithProviders(<WeixinPanel />)
    fireEvent.click(await screen.findByRole('combobox', { name: 'Who can message the bot' }))
    expect((await screen.findAllByRole('option')).map(o => o.textContent)).toEqual([
      'Anyone who messages the bot',
      'Only allowed user IDs',
      'Nobody (ignore all messages)',
    ])
  })
})
