/**
 * Report a Problem — the diagnostics bundle's three deliveries.
 *
 * The reveal delivery had no test at all, which is how it survived a sweep of
 * every other reveal surface still naming Finder unconditionally: the bundle is
 * written on the GATEWAY and `/api/reveal` shells out there, so a Windows or
 * Linux operator was told to look in an application their host does not have.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { screen, waitFor, act } from '@testing-library/react'
import userEvent from '@testing-library/user-event'

import ReportProblemModal from '../components/ReportProblemModal'
import { renderWithProviders } from './helpers'
import { api } from '../api/client'

const BUNDLE = {
  zip_path: '/home/builder/.kiro/crew/diagnostics/report-2026-08-13.zip',
  download_url: '/api/diagnostics/report-2026-08-13.zip',
  github_issue_url: 'https://github.com/kirodotdev/KiroCrew/issues/new?title=x',
  total_redactions: 3,
  included: ['gateway.log', 'kiro-cli.log'],
}

/** Render, collect a bundle, and land on the success state. */
async function collect(platform?: string) {
  vi.spyOn(api, 'collectDiagnostics').mockResolvedValue(BUNDLE as never)
  const view = renderWithProviders(<ReportProblemModal open onClose={vi.fn()} />)
  if (platform) {
    act(() => { view.queryClient.setQueryData(['kiro-prerequisite'], { platform }) })
  }
  await userEvent.click(screen.getByRole('button', { name: 'Create report' }))
  await waitFor(() => expect(screen.getByText('Saved to')).toBeInTheDocument())
  return view
}

describe('ReportProblemModal deliveries', () => {
  beforeEach(() => { vi.restoreAllMocks() })

  it('hands the zip to the desktop through the reveal endpoint', async () => {
    const reveal = vi.spyOn(api, 'revealPath').mockResolvedValue(undefined as never)
    await collect('darwin')
    await userEvent.click(screen.getByRole('button', { name: 'Open in Finder' }))
    expect(reveal).toHaveBeenCalledWith(BUNDLE.zip_path)
  })

  it.each([
    ['darwin', 'Open in Finder'],
    ['win32', 'Open in File Explorer'],
    // The sentinel a non-owner dashboard user (and a probe that could not run)
    // receives; it must never be read as a platform we can name.
    ['gateway', 'Show in file manager'],
    ['linux', 'Show in file manager'],
  ])('names the reveal delivery for a %s gateway host', async (platform, label) => {
    await collect(platform)
    expect(screen.getByRole('button', { name: label })).toBeInTheDocument()
    expect(screen.queryByText('Show in Finder')).not.toBeInTheDocument()
  })
})
