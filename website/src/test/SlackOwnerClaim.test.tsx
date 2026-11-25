/**
 * Settings ▸ Slack ▸ "Claim in Slack" (`pages/settings/SlackOwnerClaim.tsx`).
 * The routes are direct-local only; the block must not even render for a
 * read-only remote viewer.
 */
import { describe, it, expect, vi, afterEach } from 'vitest'
import { screen, fireEvent, waitFor } from '@testing-library/react'

import { renderWithProviders } from './helpers'
import type { SlackConfigData } from '../api/client'
import { slackOwnerClaimApi, type SlackOwnerClaimState } from '../pages/settings/slackOwnerClaimApi'
import { ApiError } from '../api/apiError'
import { SlackOwnerClaim } from '../pages/settings/SlackOwnerClaim'

const onOwner = vi.fn()

afterEach(() => {
  vi.restoreAllMocks()
  onOwner.mockClear()
})

function config(over: Partial<SlackConfigData> = {}): SlackConfigData {
  return {
    read_only: false,
    bot_token_set: true,
    app_token_set: true,
    owner_id: '',
    ...over,
  } as SlackConfigData
}

function state(over: Partial<SlackOwnerClaimState> = {}): SlackOwnerClaimState {
  return { open: false, expires_at: 0, team_name: '', bot_name: '', candidates: [], multiple: false, ignored: 0, confirmed: null, ...over }
}

const candidate = (id: string, text: string) => ({
  user_id: id, display_name: `Name ${id}`, handle: `h${id}`, avatar: '', team_name: 'Acme', text, received_at: 1_700_000_000,
})

describe('SlackOwnerClaim', () => {
  it('renders nothing and reads nothing for a read-only viewer', () => {
    const get = vi.spyOn(slackOwnerClaimApi, 'get')
    const { container } = renderWithProviders(<SlackOwnerClaim onOwnerChange={onOwner} config={config({ read_only: true })} />)
    expect(container.querySelector('[data-testid="slack-owner-claim"]')).toBeNull()
    expect(screen.queryByTestId('slack-owner-claim-start')).toBeNull()
    expect(screen.queryByTestId('slack-owner-confirm')).toBeNull()
    expect(get).not.toHaveBeenCalled()
  })

  it('opens the window from the Claim button', async () => {
    vi.spyOn(slackOwnerClaimApi, 'get').mockResolvedValue(state())
    const start = vi.spyOn(slackOwnerClaimApi, 'start').mockResolvedValue(state({ open: true, expires_at: 1_700_000_600 }))
    renderWithProviders(<SlackOwnerClaim onOwnerChange={onOwner} config={config()} />)
    fireEvent.click(await screen.findByTestId('slack-owner-claim-start'))
    await waitFor(() => expect(start).toHaveBeenCalledOnce())
  })

  it('names the cause and the next step for an org-wide install, not the route code', async () => {
    vi.spyOn(slackOwnerClaimApi, 'get').mockResolvedValue(state())
    vi.spyOn(slackOwnerClaimApi, 'start').mockRejectedValue(
      new ApiError(400, 'org_install_refused', JSON.stringify({ code: 'org_install_refused' })),
    )
    renderWithProviders(<SlackOwnerClaim onOwnerChange={onOwner} config={config()} />)
    fireEvent.click(await screen.findByTestId('slack-owner-claim-start'))
    const alert = await screen.findByRole('alert')
    expect(alert.textContent).toContain('member ID')
    expect(alert.textContent).not.toContain('org_install_refused')
  })

  it('names the bot to DM while the window is open', async () => {
    vi.spyOn(slackOwnerClaimApi, 'get').mockResolvedValue(state({ open: true, expires_at: 1_700_000_600, bot_name: 'kirocrew-me' }))
    renderWithProviders(<SlackOwnerClaim onOwnerChange={onOwner} config={config()} />)
    expect(await screen.findByText(/@kirocrew-me/)).toBeTruthy()
  })

  it('shows the window another tab opened when Start answers window_open', async () => {
    const get = vi.spyOn(slackOwnerClaimApi, 'get')
      .mockResolvedValueOnce(state())
      .mockResolvedValue(state({ open: true, expires_at: 1_700_000_600, bot_name: 'kirocrew-me' }))
    vi.spyOn(slackOwnerClaimApi, 'start').mockRejectedValue(
      new ApiError(409, 'window_open', JSON.stringify({ code: 'window_open' })),
    )
    renderWithProviders(<SlackOwnerClaim onOwnerChange={onOwner} config={config()} />)
    fireEvent.click(await screen.findByTestId('slack-owner-claim-start'))
    expect(await screen.findByTestId('slack-owner-claim-cancel')).toBeTruthy()
    expect(screen.queryByRole('alert')).toBeNull()
    expect(get.mock.calls.length).toBeGreaterThan(1)
  })

  it('disables Claim until both tokens are saved', async () => {
    vi.spyOn(slackOwnerClaimApi, 'get').mockResolvedValue(state())
    renderWithProviders(<SlackOwnerClaim onOwnerChange={onOwner} config={config({ app_token_set: false })} />)
    expect((await screen.findByTestId('slack-owner-claim-start')).hasAttribute('disabled')).toBe(true)
  })

  it('lists candidates with their text, warns on more than one, and confirms the one clicked', async () => {
    vi.spyOn(slackOwnerClaimApi, 'get').mockResolvedValue(state({
      open: true,
      expires_at: 1_700_000_600,
      candidates: [candidate('U02', 'second'), candidate('U01', 'it is me')],
      multiple: true,
    }))
    const confirm = vi.spyOn(slackOwnerClaimApi, 'confirm').mockResolvedValue({ owner_id: 'U01', connected: true, connect_error: '' })
    renderWithProviders(<SlackOwnerClaim onOwnerChange={onOwner} config={config()} />)
    const rows = await screen.findAllByTestId('slack-owner-candidate')
    expect(rows).toHaveLength(2)
    expect(rows[1].textContent).toContain('it is me')
    expect(screen.getByTestId('slack-owner-claim-multiple')).toBeTruthy()
    fireEvent.click(screen.getAllByTestId('slack-owner-confirm')[1])
    await waitFor(() => expect(confirm).toHaveBeenCalledWith('U01'))
    // The panel's owner draft follows, so a later Save does not undo the claim.
    await waitFor(() => expect(onOwner).toHaveBeenCalledWith('U01'))
  })

  it('says the gateway needs a restart when Confirm saved the owner but Slack did not reconnect', async () => {
    vi.spyOn(slackOwnerClaimApi, 'get').mockResolvedValue(state({
      open: true, expires_at: 1_700_000_600, candidates: [candidate('U01', 'it is me')],
    }))
    vi.spyOn(slackOwnerClaimApi, 'confirm').mockResolvedValue({
      owner_id: 'U01', connected: false, connect_error: 'workspace_switch_needs_restart',
    })
    renderWithProviders(<SlackOwnerClaim onOwnerChange={onOwner} config={config()} />)
    fireEvent.click(await screen.findByTestId('slack-owner-confirm'))
    const alert = await screen.findByRole('alert')
    expect(alert.textContent).toContain('kirocrew restart')
    expect(alert.textContent).toContain('workspace_switch_needs_restart')
    expect(onOwner).toHaveBeenCalledWith('U01')
  })

  it('shows a failed poll instead of stale candidates reading as current', async () => {
    vi.spyOn(slackOwnerClaimApi, 'get').mockRejectedValue(new Error('offline'))
    renderWithProviders(<SlackOwnerClaim onOwnerChange={onOwner} config={config()} />)
    expect(await screen.findByRole('alert')).toBeTruthy()
  })

  it('shows the owner once one is set, with no Claim offered', async () => {
    vi.spyOn(slackOwnerClaimApi, 'get').mockResolvedValue(state({ confirmed: { user_id: 'U01', handle: 'me', team_name: 'Acme' } }))
    renderWithProviders(<SlackOwnerClaim onOwnerChange={onOwner} config={config({ owner_id: 'U01' })} />)
    expect(await screen.findByText(/@me/)).toBeTruthy()
    expect(screen.queryByTestId('slack-owner-claim-start')).toBeNull()
  })
})
