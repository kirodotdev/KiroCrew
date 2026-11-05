import { beforeEach, describe, expect, it, vi } from 'vitest'
import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { i18nT } from '../../i18n/t'

const { build, save } = vi.hoisted(() => ({ build: vi.fn(), save: vi.fn() }))

vi.mock('./dashboardDownloadFile', async (importOriginal) => ({
  ...(await importOriginal<typeof import('./dashboardDownloadFile')>()),
  buildDashboardDownload: build,
}))
vi.mock('../../utils/download', () => ({ saveBlob: save }))

import CrewDashboardDownload from './CrewDashboardDownload'
import { isCurrentDashboardDocument } from './CrewDynamicDashboard'
import { DashboardDownloadError } from './dashboardDownloadFile'

const INPUT = { html: '<p>3</p>', themeVars: {}, mode: 'light' as const, pageScripts: true }

function renderButton(input: () => typeof INPUT = () => INPUT) {
  render(<CrewDashboardDownload name="Build watcher" input={input} ready />)
  return screen.getByTestId('crew-dashboard-download')
}

describe('CrewDashboardDownload', () => {
  beforeEach(() => {
    build.mockReset()
    save.mockReset()
  })

  it('disables Download until the visible document is current', async () => {
    const input = vi.fn(() => INPUT)
    const user = userEvent.setup()
    render(<CrewDashboardDownload name="Build watcher" input={input} ready={false} />)
    const button = screen.getByTestId('crew-dashboard-download')
    expect(button).toBeDisabled()
    expect(button).toHaveAccessibleName(i18nT('pages.membersPage.dashboard_download'))
    await user.click(button)
    expect(screen.queryByRole('dialog')).toBeNull()
    expect(input).not.toHaveBeenCalled()
    expect(save).not.toHaveBeenCalled()
  })

  it('saves nothing if readiness is withdrawn while the confirm is open', async () => {
    const input = vi.fn(() => INPUT)
    const user = userEvent.setup()
    const { rerender } = render(<CrewDashboardDownload name="Build watcher" input={input} ready />)
    await user.click(screen.getByTestId('crew-dashboard-download'))
    const confirm = await screen.findByRole('button', { name: i18nT('pages.membersPage.dashboard_download_confirm') })
    rerender(<CrewDashboardDownload name="Build watcher" input={input} ready={false} />)
    expect(screen.getByTestId('crew-dashboard-download')).toBeDisabled()
    await user.click(confirm)
    expect(input).not.toHaveBeenCalled()
    expect(build).not.toHaveBeenCalled()
    expect(save).not.toHaveBeenCalled()
    expect(screen.queryByTestId('crew-dashboard-download-error')).toBeNull()
  })

  it('rechecks the newest getter after confirmation and respects its refusal', async () => {
    const user = userEvent.setup()
    const { rerender } = render(<CrewDashboardDownload name="Build watcher" input={() => INPUT} ready />)
    await user.click(screen.getByTestId('crew-dashboard-download'))
    const input = vi.fn(() => null)
    rerender(<CrewDashboardDownload name="Build watcher" input={input} ready />)
    await user.click(await screen.findByRole('button', { name: i18nT('pages.membersPage.dashboard_download_confirm') }))
    expect(input).toHaveBeenCalledTimes(1)
    expect(build).not.toHaveBeenCalled()
    expect(save).not.toHaveBeenCalled()
  })

  it('asks before saving, and saves nothing on Cancel', async () => {
    const user = userEvent.setup()
    await user.click(renderButton())
    expect(await screen.findByText(i18nT('pages.membersPage.dashboard_download_confirm_title'))).toBeInTheDocument()
    expect(screen.getByText(i18nT('pages.membersPage.dashboard_download_confirm_body', { name: 'Build watcher' }))).toBeInTheDocument()
    expect(screen.getByText(/Build watcher/)).toBeInTheDocument()
    await user.click(screen.getByRole('button', { name: i18nT('components.confirmDialog.cancel') }))
    expect(build).not.toHaveBeenCalled()
    expect(save).not.toHaveBeenCalled()
  })

  it('saves the page the tab shows once confirmed', async () => {
    const blob = new Blob(['<!doctype html>'], { type: 'text/html' })
    build.mockResolvedValue(blob)
    save.mockResolvedValue(undefined)
    const user = userEvent.setup()
    await user.click(renderButton())
    await user.click(await screen.findByRole('button', { name: i18nT('pages.membersPage.dashboard_download_confirm') }))
    await vi.waitFor(() => expect(save).toHaveBeenCalledTimes(1))
    expect(build).toHaveBeenCalledWith(INPUT)
    expect(save.mock.calls[0][0]).toBe(blob)
    expect(save.mock.calls[0][1]).toMatch(/^Build watcher \d{4}-\d{2}-\d{2}\.html$/)
    expect(screen.queryByTestId('crew-dashboard-download-error')).toBeNull()
  })

  it('reads what to save when the save is confirmed, not when the button was drawn', async () => {
    build.mockResolvedValue(new Blob(['x']))
    save.mockResolvedValue(undefined)
    let html = '<p>3</p>'
    const user = userEvent.setup()
    await user.click(renderButton(() => ({ ...INPUT, html })))
    // A push lands while the confirm is open.
    html = '<p>4</p>'
    await user.click(await screen.findByRole('button', { name: i18nT('pages.membersPage.dashboard_download_confirm') }))
    await vi.waitFor(() => expect(build).toHaveBeenCalledTimes(1))
    expect(build).toHaveBeenCalledWith({ ...INPUT, html: '<p>4</p>' })
  })

  it('lets the failure notice be dismissed', async () => {
    build.mockRejectedValue(new DashboardDownloadError('too_large'))
    const user = userEvent.setup()
    await user.click(renderButton())
    await user.click(await screen.findByRole('button', { name: i18nT('pages.membersPage.dashboard_download_confirm') }))
    expect(await screen.findByTestId('crew-dashboard-download-error')).toBeInTheDocument()
    await user.click(screen.getByRole('button', { name: i18nT('components.errorNotice.dismiss') }))
    expect(screen.queryByTestId('crew-dashboard-download-error')).toBeNull()
  })

  it('says so when the page is too big', async () => {
    build.mockRejectedValue(new DashboardDownloadError('too_large'))
    const user = userEvent.setup()
    await user.click(renderButton())
    await user.click(await screen.findByRole('button', { name: i18nT('pages.membersPage.dashboard_download_confirm') }))
    expect(await screen.findByText(i18nT('pages.membersPage.dashboard_download_too_large'))).toBeInTheDocument()
    expect(save).not.toHaveBeenCalled()
  })

  it('says so when the save fails', async () => {
    build.mockResolvedValue(new Blob(['x']))
    save.mockRejectedValue(new Error('hand-off failed'))
    const user = userEvent.setup()
    await user.click(renderButton())
    await user.click(await screen.findByRole('button', { name: i18nT('pages.membersPage.dashboard_download_confirm') }))
    expect(await screen.findByText(i18nT('pages.membersPage.dashboard_download_failed'))).toBeInTheDocument()
  })
})
describe('isCurrentDashboardDocument', () => {
  const current = {
    srcdoc: '<p>current</p>',
    mint: { url: '/current', pending: false, failed: false, stalled: false },
    url: '/current',
    stageUrl: null,
    source: { url: '/current', srcdoc: '<p>current</p>' },
    drawn: { url: '/current', loaded: true },
  }

  it('accepts the loaded current document, including unchanged documents receiving pushes or refills', () => {
    expect(isCurrentDashboardDocument(current)).toBe(true)
  })

  it.each([
    ['no document', { srcdoc: null }],
    ['first mint missing', { mint: { ...current.mint, url: null }, url: null }],
    ['mint pending with a retained URL', { mint: { ...current.mint, pending: true } }],
    ['mint failed with a retained URL', { mint: { ...current.mint, failed: true } }],
    ['mint stalled after pending cleared', { mint: { ...current.mint, stalled: true } }],
    ['static read or scripted fallback awaiting a re-mint', { srcdoc: '<p>new values</p>' }],
    ['theme changed before pending was set', { srcdoc: '<p style="color:red">current</p>' }],
    ['replacement off screen', { stageUrl: '/next' }],
    ['visible URL differs from the mint', { mint: { ...current.mint, url: '/next' } }],
    ['visible source not recorded yet', { source: { url: '/old', srcdoc: current.srcdoc } }],
    ['first document not loaded', { drawn: { url: current.url, loaded: false } }],
    ['load belongs to an earlier document', { drawn: { url: '/old', loaded: true } }],
  ])('refuses %s', (_name, change) => {
    expect(isCurrentDashboardDocument({ ...current, ...change })).toBe(false)
  })
})
