import { describe, it, expect, vi, afterEach } from 'vitest'
import { screen, waitFor } from '@testing-library/react'
import { renderWithProviders } from '../test/helpers'
import { api } from '../api/client'
import { DataHomeOwnerStatusNotice } from './DataHomeOwnerNotice'

vi.mock('../api/client', async importOriginal => {
  const mod = await importOriginal<typeof import('../api/client')>()
  return { ...mod, api: { ...mod.api, status: vi.fn() } }
})

const FIX = "sudo chown -R ann '/srv/crew-data' && chmod -R u+rwX '/srv/crew-data'"

describe('DataHomeOwnerStatusNotice', () => {
  afterEach(() => { vi.mocked(api.status).mockReset() })

  it('shows the sentence and the command to copy when another account owns the files', async () => {
    vi.mocked(api.status).mockResolvedValue({
      data_home_access: { ok: false, home: '/srv/crew-data', foreign: ['/srv/crew-data/config.json'], fix_command: FIX },
    })
    renderWithProviders(<DataHomeOwnerStatusNotice />)
    const notice = await screen.findByTestId('settings-data-home-owner')
    expect(notice).toHaveTextContent("Some Kiro Crew files belong to another user, so Kiro Crew can't read them.")
    expect(notice.querySelector('code')?.textContent).toBe(FIX)
  })

  it.each([
    ['this account owns them', { data_home_access: { ok: true, home: '/h', foreign: [], fix_command: '' } }],
    ['an older gateway sends nothing', {}],
  ])('renders nothing when %s', async (_label, payload) => {
    vi.mocked(api.status).mockResolvedValue(payload)
    const { container } = renderWithProviders(<DataHomeOwnerStatusNotice />)
    await waitFor(() => expect(api.status).toHaveBeenCalled())
    expect(container).toBeEmptyDOMElement()
  })

  it('says the check failed when the status read fails', async () => {
    vi.mocked(api.status).mockRejectedValue(new Error('network down'))
    renderWithProviders(<DataHomeOwnerStatusNotice />)
    expect(await screen.findByTestId('settings-data-home-owner-check-failed'))
      .toHaveTextContent("Couldn't check who owns the Kiro Crew files.")
  })
})
