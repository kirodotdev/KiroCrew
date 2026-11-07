import { describe, it, expect, vi, afterEach } from 'vitest'
import { fireEvent, screen, waitFor } from '@testing-library/react'
import { renderWithProviders } from '../test/helpers'
import { api } from '../api/client'
import { DataHomeOwnerStatusNotice, DataHomeRepair } from './DataHomeOwnerNotice'

vi.mock('../api/client', async importOriginal => {
  const mod = await importOriginal<typeof import('../api/client')>()
  return { ...mod, api: { ...mod.api, status: vi.fn() } }
})

const FIX = "sudo chown -R ann '/srv/crew-data' && chmod -R u+rwX '/srv/crew-data'"

describe('DataHomeOwnerStatusNotice', () => {
  afterEach(() => {
    vi.mocked(api.status).mockReset()
    delete window.dataHomeFixAPI
  })

  it('shows the sentence and the command to copy when another account owns the files', async () => {
    vi.mocked(api.status).mockResolvedValue({
      data_home_access: { ok: false, home: '/srv/crew-data', foreign: ['/srv/crew-data/config.json'], fix_command: FIX },
    })
    renderWithProviders(<DataHomeOwnerStatusNotice />)
    const notice = await screen.findByTestId('settings-data-home-owner')
    expect(notice).toHaveTextContent("Some Kiro Crew files belong to another user, so Kiro Crew can't read them.")
    expect(notice).toHaveTextContent("Until this is fixed, Kiro Crew can't create a new Bot.")
    expect(notice.querySelector('code')?.textContent).toBe(FIX)
  })

  it('offers no button without the desktop bridge, so the command is the way', async () => {
    vi.mocked(api.status).mockResolvedValue({
      data_home_access: { ok: false, home: '/h', foreign: ['/h'], fix_command: FIX },
    })
    renderWithProviders(<DataHomeOwnerStatusNotice />)
    const notice = await screen.findByTestId('settings-data-home-owner')
    expect(screen.queryByTestId('data-home-fix-button')).toBeNull()
    expect(notice).toHaveTextContent('Run this command in a terminal on this computer, then try again.')
  })

  it('asks the shell about the gateway home and offers no button where it says no', async () => {
    window.dataHomeFixAPI = { available: vi.fn(async () => false), fix: vi.fn() }
    vi.mocked(api.status).mockResolvedValue({
      data_home_access: { ok: false, home: '/h', foreign: ['/h'], fix_command: FIX },
    })
    renderWithProviders(<DataHomeOwnerStatusNotice />)
    await screen.findByTestId('settings-data-home-owner')
    await waitFor(() => expect(window.dataHomeFixAPI!.available).toHaveBeenCalledWith('/h'))
    expect(screen.queryByTestId('data-home-fix-button')).toBeNull()
  })

  it('leads with the button and offers the command as the other way', async () => {
    window.dataHomeFixAPI = { available: vi.fn(async () => true), fix: vi.fn() }
    vi.mocked(api.status).mockResolvedValue({
      data_home_access: { ok: false, home: '/h', foreign: ['/h'], fix_command: FIX },
    })
    renderWithProviders(<DataHomeOwnerStatusNotice />)
    await screen.findByTestId('data-home-fix-button')
    expect(screen.getByTestId('settings-data-home-owner'))
      .toHaveTextContent('Or run this command in a terminal on this computer instead, then try again.')
  })

  it('a fixed repair keeps the notice and asks for a restart before trying again', async () => {
    const fix = vi.fn(async () => ({ ok: true }))
    window.dataHomeFixAPI = { available: vi.fn(async () => true), fix }
    vi.mocked(api.status).mockResolvedValue({
      data_home_access: { ok: false, home: '/h', foreign: ['/h'], fix_command: FIX },
    })
    renderWithProviders(<DataHomeOwnerStatusNotice />)
    fireEvent.click(await screen.findByTestId('data-home-fix-button'))
    expect(fix).toHaveBeenCalledWith('/h')
    expect(await screen.findByRole('status')).toHaveTextContent('Fixed. Restart Kiro Crew, then try again.')
    // The running gateway still holds the unreadable config, so nothing re-reads it now.
    expect(screen.getByTestId('settings-data-home-owner')).toBeInTheDocument()
    expect(api.status).toHaveBeenCalledTimes(1)
  })

  it.each([
    ['cancelled', null],
    ['failed', "Couldn't fix the files. Run the command below in a terminal instead."],
  ])('a %s repair keeps the notice and the command', async (reason, message) => {
    window.dataHomeFixAPI = { available: vi.fn(async () => true), fix: vi.fn(async () => ({ ok: false, reason: reason as 'cancelled' | 'failed' })) }
    vi.mocked(api.status).mockResolvedValue({
      data_home_access: { ok: false, home: '/h', foreign: ['/h'], fix_command: FIX },
    })
    renderWithProviders(<DataHomeOwnerStatusNotice />)
    fireEvent.click(await screen.findByTestId('data-home-fix-button'))
    await waitFor(() => expect(screen.getByTestId('data-home-fix-button')).not.toBeDisabled())
    const notice = screen.getByTestId('settings-data-home-owner')
    if (message) {
      expect(await screen.findByTestId('data-home-fix-failed')).toHaveTextContent(message)
      // One instruction after a failure: the command, with no "Or".
      expect(notice).toHaveTextContent('Run this command in a terminal on this computer, then try again.')
      expect(notice).not.toHaveTextContent('Or run this command')
    } else {
      expect(screen.queryByTestId('data-home-fix-failed')).toBeNull()
      expect(notice).toHaveTextContent('Or run this command in a terminal on this computer instead, then try again.')
    }
    expect(screen.getByTestId('settings-data-home-owner').querySelector('code')?.textContent).toBe(FIX)
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

  it('the repair block says the check failed when its status read fails', async () => {
    vi.mocked(api.status).mockRejectedValue(new Error('network down'))
    renderWithProviders(<DataHomeRepair fixCommand={FIX} />)
    expect(await screen.findByTestId('data-home-repair-check-failed'))
      .toHaveTextContent("Couldn't check who owns the Kiro Crew files.")
    expect(screen.getByTestId('data-home-repair').querySelector('code')?.textContent).toBe(FIX)
  })

  it('says the check failed when the status read fails', async () => {
    vi.mocked(api.status).mockRejectedValue(new Error('network down'))
    renderWithProviders(<DataHomeOwnerStatusNotice />)
    expect(await screen.findByTestId('settings-data-home-owner-check-failed'))
      .toHaveTextContent("Couldn't check who owns the Kiro Crew files.")
  })
})
