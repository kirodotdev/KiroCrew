import { describe, it, expect, vi, afterEach } from 'vitest'
import { screen, waitFor } from '@testing-library/react'
import { renderWithProviders } from '../test/helpers'
import { api } from '../api/client'
import { DataHomeOwnerStatusNotice } from './DataHomeOwnerNotice'

vi.mock('../api/client', async importOriginal => {
  const mod = await importOriginal<typeof import('../api/client')>()
  return { ...mod, api: { ...mod.api, status: vi.fn() } }
})

const HELP = 'https://github.com/kirodotdev/KiroCrew/blob/main/src/kiro_crew/docs/troubleshooting.md#kiro-crew-files-belong-to-another-account'

function access(owner: string) {
  return {
    data_home_access: {
      ok: false, home: '/srv/crew-data', foreign: ['/srv/crew-data/config.json'], owner, help_url: HELP,
    },
  }
}

describe('DataHomeOwnerStatusNotice', () => {
  afterEach(() => { vi.mocked(api.status).mockReset() })

  it('names the folder and the owner, the likely cause, and links the docs instead of a command', async () => {
    vi.mocked(api.status).mockResolvedValue(access('root'))
    renderWithProviders(<DataHomeOwnerStatusNotice />)
    const notice = await screen.findByTestId('settings-data-home-owner')
    expect(notice).toHaveTextContent("Some Kiro Crew files in /srv/crew-data belong to root, so Kiro Crew can't read them.")
    expect(notice).toHaveTextContent('Kiro Crew was probably started as root once, for example with sudo. Stop running it as root')
    const link = screen.getByTestId('data-home-owner-help-link')
    expect(link).toHaveAttribute('href', HELP)
    expect(link).toHaveAttribute('rel', 'noopener noreferrer')
    expect(notice.querySelector('code')).toBeNull()
  })

  it('says the files cannot be used when this account owns them but cannot read them', async () => {
    vi.mocked(api.status).mockResolvedValue(access(''))
    renderWithProviders(<DataHomeOwnerStatusNotice />)
    expect(await screen.findByTestId('settings-data-home-owner'))
      .toHaveTextContent("Some Kiro Crew files in /srv/crew-data can't be used by this account, so Kiro Crew can't read them.")
  })

  it.each([
    ['this account owns them', { data_home_access: { ok: true, home: '/h', foreign: [], owner: '', help_url: HELP } }],
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
