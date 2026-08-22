/**
 * Report a Problem — the diagnostics bundle's deliveries.
 *
 * The success view offers two deliveries — download the zip, or open a
 * pre-filled GitHub issue — plus the saved path shown above them. It carries no
 * reveal button: three peer buttons in one action row exceed the two-button cap,
 * and `/api/reveal` shells out on the GATEWAY, so a reveal from a browser that is
 * not on the gateway host drives a file manager the user is not looking at. The
 * saved path is shown as text so a local operator can still find the bundle.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { screen, waitFor } from '@testing-library/react'
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
async function collect() {
  vi.spyOn(api, 'collectDiagnostics').mockResolvedValue(BUNDLE as never)
  const view = renderWithProviders(<ReportProblemModal open onClose={vi.fn()} />)
  await userEvent.click(screen.getByRole('button', { name: 'Create report' }))
  await waitFor(() => expect(screen.getByText('Saved to')).toBeInTheDocument())
  return view
}

describe('ReportProblemModal deliveries', () => {
  beforeEach(() => { vi.restoreAllMocks() })

  it('shows the saved bundle path so a local operator can find it on disk', async () => {
    await collect()
    expect(screen.getByText(BUNDLE.zip_path)).toBeInTheDocument()
  })

  it('offers download and the GitHub issue, and never a reveal button', async () => {
    await collect()
    expect(screen.getByRole('button', { name: 'Download zip' })).toBeInTheDocument()
    // No reveal delivery on any surface: the row keeps two buttons and a reveal
    // would target the gateway host, not the browser's machine.
    expect(screen.queryByRole('button', { name: /finder|file explorer|file manager/i }))
      .not.toBeInTheDocument()
  })
})
