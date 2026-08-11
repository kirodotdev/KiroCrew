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


describe('WeixinPanel — session folder', () => {
  it('enabling the toggle saves the default name immediately', async () => {
    // This panel has no Save button, so a toggle that only reveals the field
    // loses the setting for every user who turns it on, sees the name already
    // filled in, and walks away.
    const save = vi.spyOn(api, 'saveWeixinConfig').mockResolvedValue({ ...CONFIG })
    renderWithProviders(<WeixinPanel />)

    const toggle = await screen.findByTestId('weixin-session-folder-toggle')
    fireEvent.click(toggle)

    expect(await screen.findByTestId('weixin-session-folder-name')).toBeTruthy()
    await waitFor(() =>
      expect(save).toHaveBeenCalledWith({ session_folder: 'WeChat' }),
    )
  })

  it('leaving right after toggling on still persists the setting', async () => {
    // The exact abandonment path: no blur, no Enter, no further interaction.
    const save = vi.spyOn(api, 'saveWeixinConfig').mockResolvedValue({ ...CONFIG })
    renderWithProviders(<WeixinPanel />)

    fireEvent.click(await screen.findByTestId('weixin-session-folder-toggle'))

    await waitFor(() => expect(save).toHaveBeenCalledTimes(1))
    expect(save.mock.calls[0][0]).toEqual({ session_folder: 'WeChat' })
  })

  it('saves the committed name when the field blurs', async () => {
    const save = vi.spyOn(api, 'saveWeixinConfig').mockResolvedValue({ ...CONFIG })
    renderWithProviders(<WeixinPanel />)

    fireEvent.click(await screen.findByTestId('weixin-session-folder-toggle'))
    const input = await screen.findByTestId('weixin-session-folder-name')
    fireEvent.change(input, { target: { value: 'Team chat' } })
    fireEvent.blur(input)

    await waitFor(() =>
      expect(save).toHaveBeenLastCalledWith({ session_folder: 'Team chat' }),
    )
  })

  it('turning the toggle off saves the cleared value immediately', async () => {
    // Clearing cannot create a folder, so there is nothing to defer.
    vi.spyOn(api, 'getWeixinConfig').mockResolvedValue({
      ...CONFIG,
      session_folder: 'WeChat',
    })
    const save = vi.spyOn(api, 'saveWeixinConfig').mockResolvedValue({ ...CONFIG })
    renderWithProviders(<WeixinPanel />)

    const toggle = await screen.findByTestId('weixin-session-folder-toggle')
    await waitFor(() => expect((toggle as HTMLInputElement).checked).toBe(true))
    fireEvent.click(toggle)

    await waitFor(() => expect(save).toHaveBeenCalledWith({ session_folder: '' }))
  })

  it('shows the server error when a folder name is rejected', async () => {
    vi.spyOn(api, 'saveWeixinConfig').mockRejectedValue(
      new Error('Folder name cannot contain / or \\'),
    )
    renderWithProviders(<WeixinPanel />)

    fireEvent.click(await screen.findByTestId('weixin-session-folder-toggle'))
    const input = await screen.findByTestId('weixin-session-folder-name')
    fireEvent.change(input, { target: { value: 'a/b' } })
    fireEvent.blur(input)

    const err = await screen.findByTestId('weixin-session-folder-error')
    expect(err.textContent).toContain('cannot contain')
  })

  it('gives the name field a visible label, not just an aria-label', async () => {
    // The other five channel panels render a visible caption; this one carried
    // the meaning only in a placeholder that vanishes on first keystroke.
    renderWithProviders(<WeixinPanel />)
    fireEvent.click(await screen.findByTestId('weixin-session-folder-toggle'))
    expect(await screen.findByLabelText('Folder name')).toBeTruthy()
  })
})
